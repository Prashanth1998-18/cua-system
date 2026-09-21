"""
Page observer: captures the current state of the page for the LLM.

Primary: Playwright's accessibility snapshot in "ai" mode (ADR-002), in which
every element carries a ref the agent's tools can point at. Secondary:
screenshots, for observability.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..browser.network import network_tracker
from ..browser.session import BrowserSession

logger = logging.getLogger(__name__)

# Keeps a pathological page from blowing up the prompt; ParaBank pages are a few KB.
_MAX_SNAPSHOT_CHARS = 40_000


class PageObserver:
    """Observes the current page and formats it for the LLM."""

    def __init__(
        self, session: BrowserSession, screenshot_dir: str = "evidence/screenshots",
        hide_values: list[str] | None = None,
    ):
        self._session = session
        self._hide_values = hide_values or []  # secret values, blanked in screenshots
        self._screenshot_dir = screenshot_dir
        self._step_counter = 0

    async def observe(self) -> dict[str, Any]:
        """Capture url, title, the ref-annotated snapshot, and a screenshot."""
        page = self._session.page
        self._step_counter += 1

        # Many actions (e.g. ParaBank's "Open New Account") submit via AJAX
        # rather than a page navigation, so the DOM update lags the action
        # by a beat. Observing immediately after acting can show the agent a
        # stale pre-update page, making it wrongly conclude nothing happened.
        await network_tracker(page).wait_until_quiet()

        snapshot = await self._session.accessibility_snapshot()

        screenshot_path = None
        try:
            screenshot_dir = Path(self._screenshot_dir)
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = str(screenshot_dir / f"discovery_step_{self._step_counter:03d}.png")
            await self._session.screenshot(screenshot_path, hide_values=self._hide_values)
        except Exception as e:
            logger.warning("Failed to capture screenshot: %s", e)

        return {
            "url": page.url,
            "title": await page.title(),
            "snapshot": snapshot,
            "screenshot_path": screenshot_path,
        }

    def format_for_llm(self, observation: dict[str, Any]) -> str:
        snapshot = observation["snapshot"]
        if len(snapshot) > _MAX_SNAPSHOT_CHARS:
            snapshot = snapshot[:_MAX_SNAPSHOT_CHARS] + "\n... (snapshot truncated)"
        return (
            f"Current URL: {observation['url']}\n"
            f"Page Title: {observation['title']}\n"
            f"Page snapshot (use the [ref=...] values to target elements):\n{snapshot}"
        )
