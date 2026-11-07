"""拟人化操作:随机停顿、逐字输入、JS 点击兜底。

话题/正文必须逐字符键入才能触发小红书前端的联想下拉
(参考 xhs-toolkit v1.3.0 与 social-auto-upload 的实测结论)。
"""
from __future__ import annotations

import asyncio
import random

from patchright.async_api import Locator, Page


async def rand_sleep(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def type_like_human(page: Page, text: str, click_first: Locator | None = None) -> None:
    """模拟真人键入:每字符 30~120ms,标点/换行后偶尔停顿。"""
    if click_first is not None:
        await click_first.click()
        await rand_sleep(0.3, 0.8)
    for ch in text:
        await page.keyboard.type(ch, delay=random.uniform(30, 120))
        if ch in "。!?,;、\n":
            await asyncio.sleep(random.uniform(0.1, 0.5))


async def scroll_idle(page: Page) -> None:
    """发布前页面里随机滚动两下,更像真人浏览。"""
    for _ in range(random.randint(1, 3)):
        await page.mouse.wheel(0, random.randint(60, 240))
        await rand_sleep(0.3, 0.9)
    await page.mouse.wheel(0, -random.randint(60, 300))


async def js_click_by_text(page: Page, text: str) -> bool:
    """JS 找到文字完全匹配的最内层元素,原生 click 并向上冒泡。

    小红书大量可点项的 span 设了 pointer-events:none,
    Playwright 常规 click 会一直超时,必须走原生 click 触发 Vue 事件
    (social-auto-upload 的 _js_click_by_text 方案)。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )
