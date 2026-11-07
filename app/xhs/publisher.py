"""发布流程:图文 + 视频(selector 与等待逻辑复刻 social-auto-upload 实测实现)。"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from patchright.async_api import Page

from .. import config
from ..models import Account, Note, log
from .browser import launch_account_context, playwright_session, creator_base
from .humanize import js_click_by_text, rand_sleep, scroll_idle, type_like_human

# 视频预览区出现这些关键词之一即视为上传完成
UPLOAD_DONE_KEYWORDS = ["上传成功", "分辨率", "重新上传", "编辑封面", "已上传", "已选择", "100%"]
TITLE_MAX = 20
TAGS_MAX = 10


def _abs_media(path: str) -> str:
    """media_paths 存的是相对 data/uploads 的路径,转绝对路径。"""
    if os.path.isabs(path):
        return path
    return str(config.UPLOADS_DIR / path)


async def _ensure_publish_page_ready(page: Page, is_video: bool, base: str) -> None:
    """等发布页真正渲染出来(上传控件/标题框出现)。

    rednote 版创作平台加载慢且偶发白屏:脚本 MIME 错误导致 DOM 为空。
    未渲染时 reload 重试,最多 3 轮,每轮最长 40 秒。
    """
    ready_selectors = [
        'input[type="file"]',
        'div[class^="upload-content"]',
        'input[placeholder*="填写标题"]',
    ]
    for attempt in range(3):
        waited = 0.0
        while waited < 40:
            for sel in ready_selectors:
                try:
                    if await page.locator(sel).first.count():
                        return
                except Exception:
                    pass
            await asyncio.sleep(1)
            waited += 1
        log("warn", "publish", f"发布页未渲染(第{attempt+1}次),reload 重试")
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(3)
    raise RuntimeError("发布页 40 秒内未渲染出上传控件(可能网络异常或风控),请重试")


async def _wait_image_upload(page: Page, timeout: float = 120.0) -> None:
    """图文:等标题输入框出现即上传完成;超时抛错,避免永久卡死全局队列(F-05)。"""
    title = page.locator('input[placeholder*="填写标题"]').first
    waited = 0.0
    while waited < timeout:
        try:
            await title.wait_for(state="visible", timeout=3000)
            return
        except Exception:
            await asyncio.sleep(1)
            waited += 4
    raise TimeoutError("图片上传超时(120 秒未进入编辑状态),请检查图片格式与网络")


async def _wait_video_upload(page: Page, timeout: float = config.VIDEO_UPLOAD_TIMEOUT) -> None:
    """视频:轮询预览区文本,出现关键词之一即完成(social-auto-upload 方案)。"""
    waited = 0.0
    while waited < timeout:
        try:
            upload_input = page.locator("input.upload-input").first
            if await upload_input.count():
                preview = upload_input.locator(
                    'xpath=following-sibling::div[contains(@class, "preview-new")]'
                )
                if await preview.count():
                    text = await preview.first.inner_text()
                    if any(k in text for k in UPLOAD_DONE_KEYWORDS):
                        return
            # 兜底:标题框出现说明已进入编辑态
            title = page.locator('input[placeholder*="填写标题"]').first
            if await title.count() and await title.is_visible():
                return
        except Exception:
            pass
        await asyncio.sleep(config.UPLOAD_POLL_INTERVAL)
        waited += config.UPLOAD_POLL_INTERVAL
    raise TimeoutError("视频上传超时")


async def _fill_title(page: Page, title: str) -> None:
    title = (title or "").strip()[:TITLE_MAX]
    box = page.locator('input[placeholder*="填写标题"]').first
    await box.click()
    await rand_sleep(0.2, 0.5)
    await type_like_human(page, title)


async def _fill_content(page: Page, content: str) -> None:
    desc = page.locator('p[data-placeholder*="输入正文描述"]').first
    await desc.click()
    await rand_sleep(0.2, 0.5)
    await page.keyboard.press("ControlOrMeta+A")
    await page.keyboard.press("Delete")
    if content:
        await type_like_human(page, content)


async def _fill_tags(page: Page, tags: list[str]) -> None:
    """逐字符输入 #tag 触发联想下拉,点第一个候选;失败的删掉继续。

    前置条件:_fill_content 已点击正文编辑区获得焦点。
    """
    tags = [t.strip() for t in (tags or []) if t.strip()][:TAGS_MAX]
    if not tags:
        return
    for tag in tags:
        try:
            await page.keyboard.type("#" + tag, delay=30)
            container = page.locator("#creator-editor-topic-container").first
            await container.wait_for(state="visible", timeout=6000)
            item = container.locator(".item").first
            await item.wait_for(state="visible", timeout=4000)
            await item.click()
            await rand_sleep(0.3, 0.8)
        except Exception:
            # 没出候选:回删已键入的 "#tag",不让它残留在正文
            for _ in range(len("#" + tag)):
                await page.keyboard.press("Backspace")
            await rand_sleep(0.1, 0.3)
            log("warn", "publish", f"话题「{tag}」未匹配到候选,已跳过")


async def _set_platform_schedule(page: Page, when: datetime) -> None:
    """打开平台原生「定时发布」并填时间(格式 %Y-%m-%d %H:%M)。"""
    card = page.locator(".custom-switch-card").filter(has_text="定时发布").first
    await card.locator(".d-switch").click()
    await rand_sleep(0.8, 1.2)
    time_input = page.locator(".d-datepicker-input-filter input.d-text").first
    await time_input.click()
    await time_input.fill(when.strftime("%Y-%m-%d %H:%M"))
    await page.keyboard.press("Escape")
    await rand_sleep(0.5, 1.0)


def _parse_success_url(url: str, base: str = "xiaohongshu.com") -> tuple[str | None, str, str | None]:
    """从发布成功页 URL 里抠 note_id、跳转成功页 URL 与实际公开/查看链接。"""
    qs = parse_qs(urlparse(url).query)
    note_id = None
    for key in ("note_id", "xhsShareLink", "noteId"):
        if key in qs and qs[key]:
            val = qs[key][0]
            m = re.search(r"[0-9a-f]{24}", val)
            note_id = m.group(0) if m else val
            break
    if note_id is None:
        m = re.search(r"(?:note_id=|/explore/)([0-9a-f]{24})", url)
        if m:
            note_id = m.group(1)

    domain = "rednote.com" if "rednote" in base else "xiaohongshu.com"
    detail_url = f"https://www.{domain}/explore/{note_id}" if note_id else None
    return note_id, url, detail_url


async def publish_note(
    note: Note,
    account: Account,
    headless: bool = True,
    on_stage: Any | None = None,
) -> dict:
    """执行一次发布。成功返回 {xhs_note_id, xhs_publish_url, xhs_url},失败抛异常。

    不直接写库——由调度队列在自己的会话里更新 note/job 状态。
    """
    is_video = note.note_type == "video"
    media = [_abs_media(p) for p in (note.media_paths or [])]
    for p in media:
        if not os.path.exists(p):
            raise FileNotFoundError(f"媒体文件不存在: {p}")
    if not is_video and not media:
        raise ValueError("图文笔记至少需要一张图片")

    async with playwright_session() as pw:
        context = await launch_account_context(pw, account, headless=headless)
        try:
            page = await context.new_page()
            base = await creator_base(context)
            if on_stage:
                on_stage("open_page")
            url = base + "/publish/publish?from=homepage&target=" + ("video" if is_video else "image")
            await page.goto(url, wait_until="domcontentloaded")
            if page.url.startswith(base + "/login"):
                raise PermissionError("登录态已失效,请重新扫码登录")
            await _ensure_publish_page_ready(page, is_video, base)

            await scroll_idle(page)

            # 1) 上传媒体
            if on_stage:
                on_stage("upload_media")
            if is_video:
                upload_input = page.locator(
                    "div[class^='upload-content'] input[class='upload-input']"
                ).first
                await upload_input.set_input_files(media[0])
                await _wait_video_upload(page)
            else:
                upload_input = page.locator('input[type="file"][accept*="image"]').first
                if not await upload_input.count():
                    upload_input = page.locator(
                        "div[class^='upload-content'] input[class='upload-input']"
                    ).first
                await upload_input.set_input_files(media)
                await _wait_image_upload(page)
            await rand_sleep(0.8, 1.6)

            # 2) 标题 / 正文 / 话题
            if on_stage:
                on_stage("fill_content")
            await _fill_title(page, note.title)
            await _fill_content(page, note.content or "")
            await _fill_tags(page, note.tags or [])
            await rand_sleep(0.5, 1.0)

            # 3) 平台原生定时(距今足够远时由调度层决定)
            if note.schedule_mode == "platform" and note.scheduled_at:
                await _set_platform_schedule(page, note.scheduled_at)

            # 4) 点发布并等待成功页;失败自动重试点击
            btn_text = "定时发布" if note.schedule_mode == "platform" else "发布"
            if on_stage:
                on_stage("submitting")
            success_url: str | None = None
            for _attempt in range(20):
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count():
                        await btn.click()
                    else:
                        await js_click_by_text(page, btn_text)
                except Exception:
                    pass
                if on_stage:
                    on_stage("redirecting")
                try:
                    await page.wait_for_url(config.PUBLISH_SUCCESS_PATTERN, timeout=3000)
                    success_url = page.url
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            if not success_url:
                raise RuntimeError("点击发布后未跳转到成功页(可能被风控或字段校验失败)")

            if on_stage:
                on_stage("completed")
            note_id, publish_url, detail_url = _parse_success_url(success_url, base=base)
            log("info", "publish", f"发布成功: {note.title} (note_id={note_id}, detail={detail_url})")
            return {
                "xhs_note_id": note_id,
                "xhs_publish_url": publish_url,
                "xhs_url": detail_url,
            }
        finally:
            await context.close()


async def verify_note_on_platform(account: Account, title: str) -> dict:
    """提交中断后的自动核实:打开平台「笔记管理」列表,按标题查找。

    返回 {"found": bool|None, "published_at": str|None}
    found=None 表示核实本身失败(网络/渲染),不改变笔记状态。
    """
    from . import profile_lock

    if not profile_lock.acquire(account.id, timeout=10):
        return {"found": None, "reason": "账号浏览器被占用"}
    context = None
    try:
        async with playwright_session() as pw:
            context = await launch_account_context(pw, account, headless=True)
            page = await context.new_page()
            base = await creator_base(context)
            await page.goto(base + "/publish/publish", wait_until="domcontentloaded",
                            timeout=40000)
            # 渲染等待(rednote 白屏率高):侧边栏出现「笔记管理」才算好
            rendered = False
            for _round in range(3):
                waited = 0.0
                while waited < 30:
                    if await page.get_by_text("笔记管理", exact=True).count():
                        rendered = True
                        break
                    await asyncio.sleep(2)
                    waited += 2
                if rendered:
                    break
                log("warn", "verify", f"平台页面未渲染(第{_round+1}次),reload")
                await page.reload(wait_until="domcontentloaded")
            if not rendered:
                return {"found": None, "reason": "平台页面未渲染"}

            await page.get_by_text("笔记管理", exact=True).first.click()
            await page.wait_for_timeout(4000)
            # 列表按标题查找(向上找含发布时间的行容器)
            hit = await page.evaluate(
                """(title) => {
                    const leaves = [...document.querySelectorAll('*')].filter(
                        e => e.children.length === 0 && (e.textContent || '').trim() === title
                    );
                    if (!leaves.length) return {found: false};
                    const el = leaves[leaves.length - 1];
                    let row = el;
                    for (let i = 0; i < 6 && row; i++) {
                        const t = row.innerText || '';
                        const m = t.match(/(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2})/);
                        if (t.includes(title) && m) {
                            return {found: true, published_at: m[1]};
                        }
                        row = row.parentElement;
                    }
                    return {found: false};
                }""",
                title,
            )
            return hit
    except Exception as e:  # noqa: BLE001
        log("warn", "verify", f"平台核实异常: {str(e)[:80]}")
        return {"found": None, "reason": str(e)[:80]}
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        profile_lock.release(account.id)
