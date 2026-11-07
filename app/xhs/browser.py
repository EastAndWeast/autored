"""浏览器管理:每账号独立 persistent context(隔离核心)。

- 用 patchright(反检测版 Playwright)替代官方 playwright,API 完全一致
- 每账号一个 user_data_dir:cookie/localStorage/缓存/指纹完全隔离
- UA/视窗在账号创建时随机生成并固定保存,避免指纹漂移
- 不额外叠加 stealth 脚本:patchright 官方不建议,叠加以外脚本反而增加特征
- Playwright 实例按"每次操作独立启停":避免跨线程共享事件循环绑定的
  future("future belongs to a different loop"),启动开销约百毫秒可忽略
"""
from __future__ import annotations

import random
import time
from contextlib import asynccontextmanager

from patchright.async_api import BrowserContext, Playwright, async_playwright

from .. import config
from ..models import Account


@asynccontextmanager
async def playwright_session():
    """一次自动化工序(登录/发布/采集)独占一个 Playwright 实例。"""
    pw: Playwright = await async_playwright().start()
    try:
        yield pw
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


def generate_fingerprint() -> tuple[str, int, int]:
    """创建账号时调用一次,结果永久保存到数据库。"""
    return random.choice(config.UA_POOL), *random.choice(config.VIEWPORT_POOL)


async def launch_account_context(
    pw: Playwright, account: Account, headless: bool = True
) -> BrowserContext:
    """打开账号专属浏览器环境(persistent)。"""
    proxy = None
    args = ["--disable-blink-features=AutomationControlled"]
    if account.proxy:
        proxy = {"server": account.proxy}
    else:
        # 未配置账号专属代理时强制直连,避免继承系统残留代理(如 127.0.0.1:7890)导致
        # ERR_PROXY_CONNECTION_FAILED 或误走海外节点触发小红书风控/超时
        args.append("--no-proxy-server")
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=account.profile_dir,
        headless=headless,
        user_agent=account.ua,
        viewport={"width": account.viewport_w, "height": account.viewport_h},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        proxy=proxy,
        args=args,
    )
    return context


async def persist_session_cookies(context: BrowserContext) -> None:
    """让平台的会话 Cookie 随 profile 保存，避免 Chromium 退出后删除。

    仅给无 expires 的 Cookie 设置本地保留期，不更改值或已有过期时间。
    此期限不会延长服务端登录有效期；每次仍需访问后台验证。
    凭据由 Chromium 原生加密写入 Cookie 数据库，不额外导出明文。
    """
    cookies = []
    for cookie in await context.cookies():
        domain = cookie["domain"].lstrip(".")
        if not any(domain == host or domain.endswith("." + host)
                   for host in ("xiaohongshu.com", "rednote.com")):
            continue
        if cookie.get("expires", -1) > 0:
            continue
        saved = {k: v for k, v in cookie.items() if k in (
            "name", "value", "domain", "path", "httpOnly", "secure", "sameSite", "partitionKey"
        )}
        saved["expires"] = time.time() + 7 * 24 * 60 * 60
        cookies.append(saved)
    if cookies:
        await context.add_cookies(cookies)


async def creator_base(context: BrowserContext) -> str:
    """根据账号已有登录凭据选择国内或国际创作者平台。"""
    cookies = await context.cookies()
    for domain in ("rednote.com", "xiaohongshu.com"):
        if any(c.get("name") in ("web_session", "customer-sso-sid") and c.get("value")
               and c.get("domain", "").lstrip(".") in (domain, "creator." + domain)
               for c in cookies):
            return "https://creator." + domain
    return config.CREATOR_BASE
