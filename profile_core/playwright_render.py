"""本地 Playwright HTML 截图；浏览器缺失时自动安装 Chromium。"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

_INSTALL_TIMEOUT = 600.0


def is_missing_chromium_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return (
        "executable doesn't exist" in message
        and (
            "playwright install" in message
            or "download new browsers" in message
        )
    )


async def install_chromium() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(), timeout=_INSTALL_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("Playwright Chromium 安装超时") from exc
    if process.returncode != 0:
        tail = (output or b"").decode(errors="replace")[-1200:]
        raise RuntimeError(
            f"Playwright Chromium 安装失败（{process.returncode}）：{tail}"
        )


class PlaywrightCardRenderer:
    def __init__(self, logger: Any, data_dir: str) -> None:
        self._logger = logger
        self._output_dir = Path(data_dir) / "card_cache"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._pw: Any = None
        self._browser: Any = None

    async def _start_browser(self, *, auto_install: bool = True) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._pw is None:
            self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
            return self._browser
        except Exception as exc:
            # Playwright 自带 Chromium 未下载时，优先复用系统 Chrome/Edge。
            for executable in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ):
                if not Path(executable).exists():
                    continue
                try:
                    self._browser = await self._pw.chromium.launch(
                        headless=True, executable_path=executable
                    )
                    self._logger.info(
                        "Playwright 使用系统浏览器：%s", executable
                    )
                    return self._browser
                except Exception:
                    continue
            if not auto_install or not is_missing_chromium_error(exc):
                raise
            self._logger.info(
                "Playwright Chromium 未安装，开始自动下载（首次可能较慢）"
            )
            await install_chromium()
            self._browser = await self._pw.chromium.launch(headless=True)
            self._logger.info("Playwright Chromium 安装完成")
            return self._browser

    async def render(self, html: str, *, width: int = 760) -> str:
        async with self._lock:
            browser = await self._start_browser()
            page = await browser.new_page(
                viewport={"width": width, "height": 900},
                device_scale_factor=1,
            )
            try:
                await page.set_content(html, wait_until="load", timeout=15000)
                await page.emulate_media(media="screen")
                target = self._output_dir / (
                    f"portrait-card-{uuid.uuid4().hex}.png"
                )
                card = page.locator(".card")
                if await card.count():
                    await card.first.screenshot(path=str(target))
                else:
                    await page.screenshot(path=str(target), full_page=True)
                self._prune()
                return str(target)
            finally:
                await page.close()

    def _prune(self) -> None:
        files = sorted(
            self._output_dir.glob("portrait-card-*.png"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for old in files[50:]:
            try:
                old.unlink()
            except OSError:
                pass

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
