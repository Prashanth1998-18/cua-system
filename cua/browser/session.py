"""
Browser session lifecycle management.

Handles launching Playwright, creating browser contexts, and cleanup.
Each replay starts a fresh session (ADR-013).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from ..config import BrowserConfig
from .network import network_tracker

logger = logging.getLogger(__name__)


class BrowserSession:
    """
    Manages a single Playwright browser instance and its contexts.

    Usage:
        async with BrowserSession(config) as session:
            page = await session.new_page()
            await page.goto("https://example.com")
    """

    def __init__(self, config: BrowserConfig | None = None):
        self.config = config or BrowserConfig()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        """Launch the browser."""
        self._playwright = await async_playwright().start()

        launch_args = [
            f"--remote-debugging-port={self.config.cdp_port}",
            "--disable-blink-features=AutomationControlled",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=launch_args,
        )

        self._context = await self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
        )
        self._context.set_default_timeout(self.config.default_timeout_ms)

        logger.info(
            "Browser session started (headless=%s, cdp_port=%d)",
            self.config.headless,
            self.config.cdp_port,
        )

    async def stop(self) -> None:
        """Close browser and clean up Playwright."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        logger.info("Browser session stopped")

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    @property
    def page(self) -> Page:
        """The current active page."""
        if self._page is None:
            raise RuntimeError("No page open. Call new_page() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """The current browser context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def new_page(self) -> Page:
        """Create a new page in the current context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        self._page = await self._context.new_page()
        network_tracker(self._page)  # attach before any request so none is missed
        return self._page

    async def screenshot(self, path: str | None = None, hide_values: list[str] | None = None) -> bytes:
        """
        Capture a screenshot of the current page. Form fields currently holding
        any of `hide_values` (sensitive inputs: passwords, customer names, zip)
        are blanked out, so they don't end up in evidence files or the
        operator console.
        """
        page = self.page
        mask = []
        if hide_values:
            await page.evaluate(
                """(values) => document.querySelectorAll("input, textarea").forEach((el) => {
                    if (values.includes(el.value)) el.setAttribute("data-cua-mask", "");
                })""",
                hide_values,
            )
            mask = [page.locator("[data-cua-mask]")]
        screenshot_bytes = await page.screenshot(full_page=False, mask=mask)

        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(screenshot_bytes)
            logger.debug("Screenshot saved to %s", path)

        return screenshot_bytes

    async def accessibility_snapshot(self) -> str:
        """
        The page's accessibility tree as text, with a ref on every element
        (e.g. `link "13344" [ref=e31]`) that `aria-ref=<ref>` locators resolve.
        Primary observation method (ADR-002).
        """
        return await self.page.aria_snapshot(mode="ai")

    @property
    def cdp_url(self) -> str:
        """The CDP WebSocket URL for external debugging connections."""
        return f"http://127.0.0.1:{self.config.cdp_port}"
