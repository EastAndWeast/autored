"""扫码登录与登录态校验。

流程参考 social-auto-upload 已验证实现:
打开创作平台登录页 → 提取二维码(含切到「扫一扫」面板)→ 网页端展示
→ 确认后台加载 → 保留会话 Cookie → 关闭重开验证登录态。
"""
from __future__ import annotations

import asyncio
import base64
from typing import TypedDict
from urllib.parse import urlsplit

from patchright.async_api import Page

from .. import config
from ..models import Account, SessionLocal, log
from .browser import launch_account_context, playwright_session, persist_session_cookies, creator_base
from . import profile_lock


class LoginState(TypedDict, total=False):
    status: str      # waiting_qr | scanning | success | failed | timeout
    qr: str | None   # 二维码 data URL
    message: str


# account_id -> LoginState(登录进行中的状态,前端轮询读取)
_sessions: dict[int, LoginState] = {}
_tasks: dict[int, asyncio.Task] = {}


def get_login_state(account_id: int) -> LoginState | None:
    return _sessions.get(account_id)


async def _extract_qr(page: Page) -> str | None:
    """切到扫码面板并提取二维码 data URL。

    二维码硬校验(2026-11 实测登录页结构):
    - 面板默认停在「短信登录」,需点 img.css-wemwzq 切换
    - 真二维码 160x160、data:image/ 开头(如 css-1lhmg90)
    - 切换按钮图标 64x64、装饰图标 14x14 —— 一律按尺寸过滤,绝不误抓
    """
    login_box = page.locator("div[class*='login-box']").first
    try:
        await login_box.wait_for(state="visible", timeout=20000)
    except Exception:
        return None

    # 已在扫码面板则不重复切换;不在则点切换按钮并验证
    scan_text = login_box.locator("div:has-text('APP扫一扫登录')").first
    if not await scan_text.is_visible():
        for attempt in range(3):
            switch_img = login_box.locator("img.css-wemwzq").first
            try:
                await switch_img.wait_for(state="visible", timeout=6000)
                await switch_img.click()
            except Exception:
                pass
            await asyncio.sleep(1.5)
            if await login_box.locator("div:has-text('APP扫一扫登录')").first.is_visible():
                break

    # 挑最大的 data:image 图片 = 二维码
    try:
        qr = await page.evaluate(
            """() => {
                const box = document.querySelector("div[class*='login-box']");
                const scope = box || document;
                let best = null, bestArea = 0;
                for (const img of scope.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    const src = img.getAttribute('src') || '';
                    const area = r.width * r.height;
                    if (src.startsWith('data:image/') && area > bestArea) {
                        best = src; bestArea = area;
                    }
                }
                return best && bestArea >= 120 * 120 ? best : null;
            }"""
        )
        if qr:
            return qr
        # src 是远程 URL 的大图(少见):定位到 >=120px 的 img 截图兜底
        imgs = login_box.locator("img")
        n = await imgs.count()
        for i in range(n):
            img = imgs.nth(i)
            box_ = await img.bounding_box()
            if box_ and box_["width"] >= 120 and box_["height"] >= 120:
                src_attr = await img.get_attribute("src")
                if src_attr and not src_attr.startswith("data:image/"):
                    s = await img.screenshot()
                    return "data:image/png;base64," + base64.b64encode(s).decode()
    except Exception:
        pass
    return None


async def _peek_qr(page: Page) -> str | None:
    """轻量读取当前二维码(页面自动换码后同步到界面);若过期自动点击刷新。"""
    try:
        # 若页面出现二维码已过期/点击刷新提示,自动点击刷新
        await page.evaluate(
            """() => {
                const box = document.querySelector("div[class*='login-box']") || document;
                const refreshBtn = box.querySelector("[class*='mask'], [class*='refresh'], .qrcode-mask");
                if (refreshBtn && (refreshBtn.innerText.includes('刷新') || refreshBtn.innerText.includes('失效') || refreshBtn.innerText.includes('过期'))) {
                    refreshBtn.click();
                }
            }"""
        )
    except Exception:
        pass

    try:
        return await page.evaluate(
            """() => {
                const box = document.querySelector("div[class*='login-box']");
                const scope = box || document;
                let best = null, bestArea = 0;
                for (const img of scope.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    const src = img.getAttribute('src') || '';
                    if (src.startsWith('data:image/') && r.width >= 120 && r.height >= 120
                        && r.width * r.height > bestArea) {
                        best = src; bestArea = r.width * r.height;
                    }
                }
                return best;
            }"""
        )
    except Exception:
        return None


async def _login_completed(page: Page) -> bool:
    """后台 URL、登录凭据和已加载 UI 必须同时满足，不能在 SSO 跳转中关窗。"""
    url = urlsplit(page.url or "")
    if url.hostname not in ("creator.xiaohongshu.com", "creator.rednote.com"):
        return False
    if "/login" in url.path:
        return False
    login_box = page.locator("div[class*='login-box']").first
    if await login_box.count() and await login_box.is_visible():
        return False
    # 创作者登录使用独立 SSO，普通站 web_session 或页面上的“发布笔记”不能证明成功。
    cookies = await page.context.cookies([f"https://{url.hostname}/"])
    if not any(c.get("name") in ("customer-sso-sid", "web_session") and c.get("value") for c in cookies):
        return False
    return await page.get_by_text("发布笔记", exact=False).first.is_visible()


async def check_cookie(account: Account) -> bool:
    """访问发布页判断登录态:跳回 /login 或出现登录框即失效。"""
    async with playwright_session() as pw:
        return await _check_cookie_inner(pw, account)


async def _check_cookie_inner(pw, account: Account) -> bool:
    context = await launch_account_context(pw, account, headless=True)
    try:
        page = await context.new_page()
        await page.goto(
            (await creator_base(context)) + "/publish/publish?from=homepage&target=video",
            wait_until="domcontentloaded",
        )
        # 后台加载完成即返回；慢网络最多等 20 秒，不再固定等待 12 秒。
        login_rounds = 0
        for _ in range(40):
            if await _login_completed(page):
                return True
            login_rounds = login_rounds + 1 if "/login" in urlsplit(page.url).path else 0
            if login_rounds >= 6:
                return False
            await page.wait_for_timeout(500)
        return False
    except Exception as e:
        log("warn", "login", f"账号[{account.name}]登录态检测异常: {type(e).__name__}")
        return False
    finally:
        await context.close()


async def _do_login(account_id: int) -> None:
    with SessionLocal() as s:
        account = s.get(Account, account_id)
        if account is None:
            return
        account.status = "logging_in"
        s.commit()

    state = _sessions.setdefault(account_id, LoginState(status="waiting_qr", qr=None, message=""))
    state.update(status="waiting_qr", qr=None, message="正在打开登录页…")

    if not profile_lock.acquire(account_id):
        state.update(status="failed", message="该账号正在执行发布/采集任务,请稍后再试")
        _tasks.pop(account_id, None)
        with SessionLocal() as s:
            acc = s.get(Account, account_id)
            if acc:
                acc.status = "pending"
                s.commit()
        return

    try:
        async with playwright_session() as pw:
            await _do_login_flow(pw, account_id, state)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state.update(status="failed", qr=None, message=f"无法启动登录浏览器: {e}")
        log("error", "login", f"账号[{account.name}]登录启动失败: {e}")
    finally:
        profile_lock.release(account_id)
        _tasks.pop(account_id, None)
        with SessionLocal() as s:
            acc = s.get(Account, account_id)
            if acc and acc.status == "logging_in":
                acc.status = "pending"
                s.commit()


async def _do_login_flow(pw, account_id: int, state: LoginState) -> None:
    with SessionLocal() as s:
        account = s.get(Account, account_id)
    if account is None:
        return
    ok = False
    context = await launch_account_context(pw, account, headless=False)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        qr = await _extract_qr(page)
        state.update(
            qr=qr, status="scanning",
            message="请用小红书 APP 扫码，并在手机确认；如需验证，请在打开的浏览器中完成"
            if qr else "未提取到二维码，请在打开的浏览器中完成扫码或短信登录",
        )
        log("info", "login", f"账号[{account.name}]登录页已打开，二维码{'已就绪' if qr else '未提取'}")

        waited = 0.0
        while waited < config.LOGIN_TIMEOUT:
            await asyncio.sleep(3)
            waited += 3
            # 小红书登录码 1~2 分钟就会过期,页面会自动换码:
            # 每轮轻量读取最新 QR 同步到界面,保证用户扫到的始终是有效码
            try:
                fresh_qr = await _peek_qr(page)
                if fresh_qr and fresh_qr != state.get("qr"):
                    state.update(qr=fresh_qr, message="二维码已更新,请扫最新码")
            except Exception:
                pass
            # Cookie 可能是过期的历史凭证；必须等后台页面真正加载完成。
            if await _login_completed(page):
                await asyncio.sleep(2)
                if await _login_completed(page):
                    await persist_session_cookies(context)
                    ok = True
                    break
            if not ok:
                # 调试可见:界面实时显示页面停在哪
                state.update(message=f"等待登录确认…({int(waited)}s)，请在手机确认；如需验证码或验证操作，请在打开的浏览器中完成")

        if not ok and waited >= config.LOGIN_TIMEOUT:
            state.update(status="timeout", message="登录未完成：请确认手机授权，并完成浏览器中的设备验证或短信验证")
            log("warn", "login", f"账号[{account.name}]登录超时，停留路径: {urlsplit(page.url).hostname}{urlsplit(page.url).path}")
    except Exception as e:  # noqa: BLE001
        state.update(status="failed", message=f"登录异常: {e}")
        log("error", "login", f"账号[{account.name}]登录异常: {e}")
    finally:
        await context.close()

    if ok:
        state.update(status="verifying", qr=None, message="扫码已完成，正在重新打开浏览器验证登录态…")
        ok = await _check_cookie_inner(pw, account)
        if not ok:
            state.update(status="failed", message="扫码后登录态恢复校验未通过，请重新扫码；若仍失败请查看运行日志")
            log("warn", "login", f"账号[{account.name}]扫码后重新打开浏览器校验未通过")

    with SessionLocal() as s:
        account = s.get(Account, account_id)
        if account:
            account.status = "online" if ok else "pending"
            if ok:
                from datetime import datetime as _dt
                account.last_check_at = _dt.now()
            s.commit()
    if ok:
        state.update(status="success", message="登录成功")
        log("info", "login", f"账号[{account.name}]登录成功")
    _tasks.pop(account_id, None)


def start_login(account_id: int) -> bool:
    """启动登录后台任务;同一账号不重复启动。"""
    if account_id in _tasks and not _tasks[account_id].done():
        return False
    _sessions.pop(account_id, None)
    _tasks[account_id] = asyncio.ensure_future(_do_login(account_id))
    return True


def cancel_login(account_id: int) -> None:
    """删除账号前调用:终止登录任务并释放浏览器,避免其重建 profile。"""
    task = _tasks.pop(account_id, None)
    if task and not task.done():
        task.cancel()
    _sessions.pop(account_id, None)
