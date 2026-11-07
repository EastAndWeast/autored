"""创作者中心数据采集:每篇笔记的观看/点赞/收藏/评论/分享/涨粉。

策略(MediaCrawler 方法论):登录态页面上下文里直接调创作者中心同源 API
——请求由页面自身的 JS 环境发出,签名/风控参数天然有效,无需逆向。
API 拿不到时降级解析「内容分析」页表格 DOM。

DOM 解析要点(xhs-toolkit 源码实测):
- 页面 URL: /statistics/data-analysis(旧入口 /analytics/note 兜底)
- 表格是 Element-UI 的 el-table(div 结构),也可能退化成原生 table,两者都支持
- 实测列序:0=标题(含"发布于…"需截断) 1=发布时间 2=观看 3=点赞 4=评论
  5=收藏 6=涨粉 7=分享 8=人均时长;表头映射优先,固定索引兜底
- 数值可能是 "1.2万" 格式,需中文数字换算
"""
from __future__ import annotations

import asyncio
import json
import re

from patchright.async_api import Page

from .. import config
from ..models import Account, log

# 中文表头 -> 统一字段
HEADER_MAP = {
    "观看量": "read",
    "观看": "read",
    "阅读量": "read",
    "访问量": "read",
    "曝光量": "impression",
    "曝光": "impression",
    "点赞量": "like",
    "点赞数": "like",
    "点赞": "like",
    "收藏量": "collect",
    "收藏数": "collect",
    "收藏": "collect",
    "评论量": "comment",
    "评论数": "comment",
    "评论": "comment",
    "分享量": "share",
    "分享数": "share",
    "分享": "share",
    "涨粉": "fans_gain",
    "涨粉量": "fans_gain",
    "新增粉丝": "fans_gain",
}

# 表头映射失败时的固定列索引(xhs-toolkit 实测 COLUMN_MAPPING)
FALLBACK_COLUMN_INDEX = {
    0: "title",
    2: "read",
    3: "like",
    4: "comment",
    5: "collect",
    6: "fans_gain",
    7: "share",
}

# 表头行/噪音行关键词
HEADER_ROW_KEYWORDS = ["笔记基础信息", "观看", "点赞", "评论", "收藏", "涨粉", "分享", "操作"]

# 已知的创作者中心数据分析接口(带版本,失败不影响 DOM 兜底)
API_NOTE_LIST_CANDIDATES = [
    "/api/galaxy/creative/data/analysis/note/list?date_type=all&sub_data_type=all&order_by=&page=1&page_size=50",
]


def cn_to_int(text: str) -> int:
    """把 "1.2万" / "3,456" / "56" / "56%" / "观看 1.5万" 转成整数。"""
    if not text:
        return 0
    s = text.strip().replace(",", "").replace("%", "")
    multiplier = 1
    if "万" in s:
        multiplier, s = 10000, s.replace("万", "")
    elif "亿" in s:
        multiplier, s = 100000000, s.replace("亿", "")
    # 剥离可能混入的文案(如「观看 1.5」),只保留数字与小数点
    s = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    if not s:
        return 0
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return 0


async def _wait_for_data_table(page: Page, timeout_s: float | None = None) -> bool:
    """等待数据表格(el-table 或原生 table)出现。"""
    timeout_s = timeout_s if timeout_s is not None else config.STATS_TABLE_WAIT
    selectors = [".el-table__row", ".note-data-table tr", "table tbody tr"]
    waited = 0.0
    while waited < timeout_s:
        for sel in selectors:
            if await page.locator(sel).first.count():
                return True
        await asyncio.sleep(1)
        waited += 1
    return False


async def _try_api(page: Page) -> list[dict] | None:
    """在页面上下文 fetch 同源 API;任何失败返回 None 走 DOM。"""
    for path in API_NOTE_LIST_CANDIDATES:
        try:
            raw = await page.evaluate(
                """async (p) => {
                    const r = await fetch(p, {credentials: 'include'});
                    if (!r.ok) return null;
                    return await r.text();
                }""",
                path,
            )
            if not raw:
                continue
            data = json.loads(raw)
            payload = data.get("data") or {}
            rows = payload.get("notes") or payload.get("list")
            if rows is None:
                continue
            out = []
            for row in rows:
                out.append(
                    {
                        "title": row.get("title") or row.get("display_title") or "",
                        "note_id": str(row.get("note_id") or ""),
                        "impression": int(row.get("view_count") or row.get("impression") or 0),
                        "read": int(row.get("read_count") or row.get("click_count") or 0),
                        "like": int(row.get("like_count") or 0),
                        "collect": int(row.get("collect_count") or 0),
                        "comment": int(row.get("comment_count") or 0),
                        "share": int(row.get("share_count") or 0),
                        "fans_gain": int(row.get("fans_increase_count") or 0),
                        "source": "api",
                    }
                )
            if out:
                return out
        except Exception:
            continue
    return None


def _clean_title(raw: str) -> str:
    """标题单元格含「发布于 …」,截断;再去掉换行噪音。"""
    t = (raw or "").strip().split("发布于")[0].strip()
    return t.split("\n")[0].strip()


async def _read_table_structure(page: Page) -> dict | None:
    """一次性读出表头(文本数组)和数据行(二维文本数组)。

    支持两种结构:
    1. Element-UI el-table:.el-table__header 单元格做表头,.el-table__row 做行
    2. 原生 table:thead th / tbody tr
    """
    return await page.evaluate(
        """() => {
            // ---- Element-UI 结构 ----
            const headerCells = [...document.querySelectorAll(
                '.el-table__header-wrapper th, .el-table__header th'
            )];
            const rows = [...document.querySelectorAll('.el-table__row')];
            if (headerCells.length && rows.length) {
                const headers = headerCells.map(th => (th.innerText || '').trim());
                const data = rows.map(r =>
                    [...r.querySelectorAll('.el-table__cell, td')].map(td => (td.innerText || '').trim())
                );
                return {kind: 'el-table', headers, data};
            }
            // ---- 原生 table ----
            const tables = [...document.querySelectorAll('table')];
            for (const tb of tables) {
                const ths = [...tb.querySelectorAll('thead th')].map(th => (th.innerText || '').trim());
                const trs = [...tb.querySelectorAll('tbody tr')].map(tr =>
                    [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim())
                );
                if (ths.length && trs.length) return {kind: 'table', headers: ths, data: trs};
            }
            return null;
        }"""
    )


def _build_column_map(headers: list[str]) -> dict[int, str] | None:
    """按中文表头建立「列索引 -> 字段」映射;认不出任何指标列则返回 None。"""
    col: dict[int, str] = {}
    for i, h in enumerate(headers):
        h = h.replace("\n", "").strip()
        field = HEADER_MAP.get(h)
        if field:
            col[i] = field
        elif ("笔记" in h or "标题" in h) and "title" not in col.values():
            col[i] = "title"
    return col or None


def _is_header_row(r: list[str], col: dict[int, str] | None) -> bool:
    """表头行判定:指标列上是文字(非数字)即为表头,不看整行文本
    (标题里常含「分享」等词,整行匹配会误杀)。"""
    idxs = [i for i, f in (col or FALLBACK_COLUMN_INDEX).items() if f in ("read", "like", "collect")]
    if idxs:
        return all(i >= len(r) or not any(ch.isdigit() for ch in r[i]) for i in idxs)
    return sum(1 for k in HEADER_ROW_KEYWORDS if k in "".join(r)) >= 4


def _rows_to_items(structure: dict) -> list[dict]:
    """把表格结构转成统一行记录;表头映射优先,固定索引兜底。"""
    headers = structure.get("headers") or []
    raw_rows = structure.get("data") or []
    col = _build_column_map(headers)

    def cell(r: list[str], field: str) -> str:
        if col:
            for i, f in col.items():
                if f == field and i < len(r):
                    return r[i]
            return ""
        # 无表头映射:固定索引兜底
        for i, f in FALLBACK_COLUMN_INDEX.items():
            if f == field and i < len(r):
                return r[i]
        return ""

    items: list[dict] = []
    for r in raw_rows:
        if not r or _is_header_row(r, col):
            continue
        item = {
            "title": _clean_title(cell(r, "title")),
            "note_id": "",
            "impression": cn_to_int(cell(r, "impression")),
            "read": cn_to_int(cell(r, "read")),
            "like": cn_to_int(cell(r, "like")),
            "collect": cn_to_int(cell(r, "collect")),
            "comment": cn_to_int(cell(r, "comment")),
            "share": cn_to_int(cell(r, "share")),
            "fans_gain": cn_to_int(cell(r, "fans_gain")),
            "source": "dom",
        }
        if item["title"] or item["read"] or item["like"]:
            items.append(item)
    return items


async def _parse_table(page: Page) -> list[dict]:
    """解析内容分析页表格,含翻页。"""
    items: list[dict] = []
    while True:
        if not await _wait_for_data_table(page):
            break
        structure = await _read_table_structure(page)
        if not structure:
            break
        page_items = _rows_to_items(structure)
        if not page_items:
            break
        items.extend(page_items)

        # 翻页:Element-UI 分页 .btn-next;其他分页组件找「下一页」
        next_btn = page.locator(
            ".btn-next:not(.disabled), .d-pagination-next:not(.disabled), button:has-text('下一页')"
        ).first
        try:
            if await next_btn.count() and await next_btn.is_enabled():
                await next_btn.click()
                await asyncio.sleep(2)
                continue
        except Exception:
            pass
        break
    return items


async def _collect_from_note_list(page, base: str) -> list[dict]:
    """从「笔记管理」列表卡片抓取每篇数据(rednote 版最稳路径)。

    卡片结构实测:标题 / 发布时间 / 曝光 评论 点赞 收藏 分享
    rednote 页面白屏率高,带渲染重试。
    """
    date_pat = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"
    rows: list[dict] = []
    for attempt in range(3):
        try:
            await page.goto(base + "/publish/publish", wait_until="domcontentloaded",
                            timeout=40000)
        except Exception:
            await asyncio.sleep(5)
            continue
        # 等侧边栏渲染
        nav_ok = False
        for _ in range(15):
            await asyncio.sleep(2)
            try:
                if await page.get_by_text("笔记管理", exact=True).count():
                    nav_ok = True
                    break
            except Exception:
                pass
        if not nav_ok:
            log("warn", "stats", f"笔记管理入口未渲染(第{attempt+1}次)")
            continue
        try:
            await page.get_by_text("笔记管理", exact=True).first.click()
        except Exception:
            continue
        # 等列表行出现(卡片含标题+日期)
        got_row = False
        for _ in range(15):
            await asyncio.sleep(2)
            try:
                body = await page.evaluate("() => document.body.innerText")
                if re.search(date_pat, body) and len(body.strip()) > 60:
                    got_row = True
                    break
            except Exception:
                pass
        if not got_row:
            log("warn", "stats", f"笔记列表未出数据(第{attempt+1}次)")
            continue
        rows = await page.evaluate(
            """(datePat) => {
                const dateRe = new RegExp(datePat);
                const seen = [];
                for (const el of document.querySelectorAll('*')) {
                    const t = (el.innerText || '');
                    if (t.length < 300 && dateRe.test(t)
                        && (t.match(/[0-9]+/g) || []).length >= 6) {
                        seen.push({t: t, len: t.length});
                    }
                }
                seen.sort(function(a, b) { return a.len - b.len; });
                const out = [];
                const used = [];
                for (const item of seen) {
                    let dup = false;
                    for (const t of used) { if (item.t.indexOf(t) !== -1) { dup = true; break; } }
                    if (dup) continue;
                    used.push(item.t);
                    out.push(item.t.split(String.fromCharCode(10)));
                }
                return out;
            }""",
            date_pat,
        )
        if rows:
            break
        log("warn", "stats", f"列表行解析为空(第{attempt+1}次)")

    # 行结构:[标题, 时间, 曝光, 评论, 点赞, 收藏, 分享]
    out = []
    for r in rows or []:
        if not r or len(r) < 7:
            continue
        nums = [_to_int_safe(x) for x in r[2:7]]
        out.append(
            {
                "title": (r[0] or "").strip(),
                "note_id": "",
                "published_at": (r[1] or "").strip(),
                "impression": nums[0],
                "read": 0,
                "like": nums[2],
                "collect": nums[3],
                "comment": nums[1],
                "share": nums[4],
                "fans_gain": 0,
            }
        )
    return out


def _to_int_safe(text: str) -> int:
    try:
        return int("".join(ch for ch in text if ch.isdigit()) or 0)
    except Exception:
        return 0


async def collect_account_stats(account: Account) -> list[dict]:
    """打开账号的创作中心抓取内容分析数据;返回行列表。"""
    from .browser import launch_account_context, playwright_session, creator_base

    async with playwright_session() as pw:
        context = await launch_account_context(pw, account, headless=True)
        try:
            page = await context.new_page()
            base = await creator_base(context)

            if "rednote" in base:
                # rednote 版:直接走笔记管理列表(自带 goto 渲染重试),
                # 不先访问 data-analysis——rednote 网络抖动大,少一次跳转少一次失败
                rows = await _collect_from_note_list(page, base)
                source = "notelist"
                if not rows and page.url.startswith(base + "/login"):
                    raise PermissionError("登录态已失效")
            else:
                # xiaohongshu 版:多入口尝试数据表格 + API
                for path in config.STATS_URL_CANDIDATES:
                    await page.goto(base + path, wait_until="domcontentloaded")
                    await page.wait_for_timeout(config.STATS_PAGE_SETTLE * 1000)
                    if page.url.startswith(base + "/login"):
                        raise PermissionError("登录态已失效")
                    if await _wait_for_data_table(page, timeout_s=8):
                        break
                rows = await _try_api(page)
                source = "api"
                if not rows:
                    rows = await _parse_table(page)
                    source = "dom"
            log("info", "stats", f"账号[{account.name}]采集 {len(rows)} 条({source})")
            return rows
        finally:
            await context.close()
