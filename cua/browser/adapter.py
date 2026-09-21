"""
Surface adapter interface.

Abstracts browser automation behind a common interface so desktop
apps (via UI Automation / Accessibility API) can be supported later
with a new adapter, not a rewrite.
"""

from __future__ import annotations

import abc
import logging
import re
from typing import Any

from playwright.async_api import Page, Locator as PlaywrightLocator, TimeoutError as PlaywrightTimeoutError

from .network import SERVER_REQUEST_TYPES, network_tracker, same_site
from ..schema.artifact import (
    ActionType,
    CheckpointType,
    ElementTarget,
    Locator,
    LocatorStrategy,
    Step,
)

logger = logging.getLogger(__name__)


_READ_TABLE_JS = """
(table) => {
    const clean = (el) => (el.innerText || el.textContent || "").trim();
    let headerRow = table.querySelector("thead tr");
    const bodyRows = Array.from(table.querySelectorAll("tbody tr"));
    if (!headerRow && bodyRows.length && bodyRows[0].querySelector("th")) {
        headerRow = bodyRows.shift();
    }
    const headers = headerRow ? Array.from(headerRow.children).map(clean) : [];
    const rows = bodyRows
        .map((tr) => Array.from(tr.children).map(clean))
        .filter((cells) => cells.some((c) => c !== ""));
    return { headers, rows };
}
"""


def snapshot_headings(snapshot: str) -> list[str]:
    """Page headings, in order, from an accessibility snapshot (aria_snapshot text)."""
    return [h.replace('\\"', '"') for h in re.findall(r'heading "((?:[^"\\]|\\.)*)"', snapshot)]


_FIND_CARDS_JS = r"""
(el) => {
    // Which child elements repeat? Cards share a tag and a data-test (or class).
    const sig = (n) => n.tagName + "|" + (n.getAttribute("data-test") || (typeof n.className === "string" ? n.className : ""));
    const repeated = (node) => {
        const counts = {};
        for (const k of node.children) counts[sig(k)] = (counts[sig(k)] || 0) + 1;
        let best = null, most = 1;
        for (const [s, c] of Object.entries(counts)) if (c > most) { best = s; most = c; }
        return best ? Array.from(node.children).find((k) => sig(k) === best) : null;
    };
    let card = repeated(el);
    let node = el;
    while (!card && node.children.length === 1) { node = node.children[0]; card = repeated(node); }
    if (!card && el.parentElement) card = repeated(el.parentElement);  // pointed at a single card
    if (!card) return null;
    const dt = card.getAttribute("data-test");
    if (dt) return `[data-test="${dt}"]`;
    const cls = typeof card.className === "string" ? card.className.trim().split(/\s+/)[0] : "";
    return card.tagName.toLowerCase() + (cls ? "." + cls : "");
}
"""

_READ_CARDS_JS = r"""
(el, cardSelector) => {
    const scope = el.querySelectorAll(cardSelector).length ? el : (el.parentElement || el);
    const skip = new Set(["BUTTON", "INPUT", "SELECT", "TEXTAREA", "SCRIPT", "STYLE", "IMG"]);
    const text = (n) => (n.innerText || n.textContent || "").trim();
    const keyOf = (n, i) => n.getAttribute("data-test")
        || (typeof n.className === "string" && n.className.trim() ? n.className.trim().split(/\s+/)[0] : "")
        || n.tagName.toLowerCase() + i;
    const rows = Array.from(scope.querySelectorAll(cardSelector)).map((card) => {
        const row = {};
        Array.from(card.querySelectorAll("*"))
            .filter((n) => !skip.has(n.tagName) && n.children.length === 0 && text(n) !== "")
            .forEach((n, i) => { let k = keyOf(n, i); while (k in row) k += "_"; row[k] = text(n); });
        return row;
    });
    return rows;
}
"""


async def find_card_selector(locator: PlaywrightLocator) -> str | None:
    """Discovery: the selector for the repeated cards in or around this element, if any."""
    return await locator.evaluate(_FIND_CARDS_JS)


async def read_cards(locator: PlaywrightLocator, card_selector: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read repeated cards (not an HTML table) into (keys, rows)."""
    rows = await locator.evaluate(_READ_CARDS_JS, card_selector)
    keys = list(rows[0].keys()) if rows else []
    return keys, rows


async def read_table(locator: PlaywrightLocator) -> tuple[list[str], list[dict[str, str]]]:
    """Read an HTML table into (headers, rows), each row keyed by header name."""
    data = await locator.evaluate(_READ_TABLE_JS)
    headers: list[str] = data["headers"]
    rows = []
    for cells in data["rows"]:
        keys = headers if len(headers) == len(cells) else [f"col_{i}" for i in range(len(cells))]
        rows.append(dict(zip(keys, cells)))
    return headers, rows


class SurfaceAdapter(abc.ABC):
    """
    Abstract interface for interacting with an application surface.

    Implementations translate generic actions (click, type, extract)
    into surface-specific operations (Playwright calls, UI Automation, etc.).
    """

    @abc.abstractmethod
    async def navigate(self, url: str) -> None:
        """Navigate to a URL."""

    @abc.abstractmethod
    async def find_element(self, target: ElementTarget) -> Any:
        """Find an element using the locator cascade. Returns surface-specific handle."""

    @abc.abstractmethod
    async def click(self, target: ElementTarget, wait_for_network: bool = False) -> Locator:
        """
        Click an element. Returns which locator resolved. With wait_for_network,
        waits for the server request the click triggers to happen and finish.
        """

    @abc.abstractmethod
    async def type_text(self, target: ElementTarget, text: str) -> Locator:
        """Type text into an element. Returns which locator resolved."""

    @abc.abstractmethod
    async def select_option(self, target: ElementTarget, value: str) -> Locator:
        """Select an option from a dropdown. Returns which locator resolved."""

    @abc.abstractmethod
    async def extract_text(self, target: ElementTarget) -> tuple[str, Locator]:
        """Extract text content from an element. Returns (text, locator_used)."""

    @abc.abstractmethod
    async def extract_table(self, target: ElementTarget) -> tuple[list[str], list[dict[str, str]], Locator]:
        """Read a table element. Returns (headers, rows, locator_used)."""

    @abc.abstractmethod
    async def extract_cards(
        self, target: ElementTarget, card_selector: str,
    ) -> tuple[list[str], list[dict[str, str]], Locator]:
        """Read repeated cards inside an element. Returns (keys, rows, locator_used)."""

    @abc.abstractmethod
    async def check_condition(self, condition_type: CheckpointType, value: str, locator: Locator | None = None) -> bool:
        """Check if a condition is met on the current page."""

    @abc.abstractmethod
    async def get_current_url(self) -> str:
        """Get the current page URL."""

    @abc.abstractmethod
    async def get_page_title(self) -> str:
        """Get the current page title."""

    @abc.abstractmethod
    async def screenshot(self, path: str | None = None) -> bytes:
        """Capture a screenshot."""

    @abc.abstractmethod
    async def accessibility_snapshot(self) -> str:
        """The accessibility tree as text, one element per line."""

    @abc.abstractmethod
    async def key_press(self, key: str) -> None:
        """Press a keyboard key."""

    @abc.abstractmethod
    async def scroll(self, direction: str = "down", amount: int = 3) -> None:
        """Scroll the page."""

    @abc.abstractmethod
    async def wait_for_condition(self, condition_type: CheckpointType, value: str, locator: Locator | None = None, timeout_ms: int = 5000) -> bool:
        """Wait for a condition to be met, with timeout."""

    @abc.abstractmethod
    async def dismiss_dialog(self) -> bool:
        """Dismiss any open dialog/alert. Returns True if a dialog was dismissed."""

    async def read_alert_text(self) -> str:
        """
        What the surface is currently announcing (an ARIA alert or live
        region on the web). Surfaces that can't tell us return "", and the
        caller falls back to the message recorded at discovery time.
        """
        return ""


class PlaywrightAdapter(SurfaceAdapter):
    """
    Playwright-based implementation of the surface adapter.

    Translates generic actions into Playwright calls.
    Uses the locator cascade to find elements robustly.
    """

    def __init__(self, page: Page):
        self._page = page
        self._network = network_tracker(page)

    @property
    def page(self) -> Page:
        return self._page

    async def navigate(self, url: str) -> None:
        # Wait for the full load, not just the HTML: on ParaBank's login page,
        # typing right after DOMContentLoaded intermittently landed in a form
        # that was then re-rendered empty, submitting a blank login.
        await self._page.goto(url, wait_until="load")
        await self._network.wait_until_quiet()

    def _resolve_locator(self, locator: Locator) -> PlaywrightLocator:
        """Convert a schema Locator into a Playwright Locator."""
        base = self._page
        if locator.inside_card and locator.card_text:
            # Only inside the card that shows this exact text (e.g. the product
            # tile for "Sauce Labs Backpack"), for labels every card repeats.
            base = self._page.locator(locator.inside_card).filter(
                has=self._page.get_by_text(locator.card_text, exact=True)
            )
        match locator.strategy:
            # exact=True: by default Playwright matches names/text by substring,
            # so the link for account "1234" would also match "12345".
            case LocatorStrategy.ROLE:
                if locator.role:
                    return base.get_by_role(locator.role, name=locator.value, exact=True)
                return base.get_by_role(locator.value)
            case LocatorStrategy.TEXT:
                return base.get_by_text(locator.value, exact=True)
            case LocatorStrategy.LABEL:
                return base.get_by_label(locator.value)
            case LocatorStrategy.PLACEHOLDER:
                return base.get_by_placeholder(locator.value)
            case LocatorStrategy.CSS:
                return base.locator(locator.value)
            case LocatorStrategy.XPATH:
                return base.locator(f"xpath={locator.value}")
            case LocatorStrategy.TEST_ID:
                return base.get_by_test_id(locator.value)
            case _:
                raise ValueError(f"Unknown locator strategy: {locator.strategy}")

    async def find_element(self, target: ElementTarget) -> tuple[PlaywrightLocator, Locator]:
        """
        Try each locator in the cascade until one resolves.

        Returns (playwright_locator, schema_locator_that_worked).
        Raises TimeoutError if none resolve.
        """
        # Handle iframe navigation if needed
        page_or_frame = self._page
        if target.iframe_path:
            for iframe_selector in target.iframe_path:
                frame_element = page_or_frame.locator(iframe_selector)
                frame = await frame_element.content_frame()
                if frame is None:
                    raise RuntimeError(f"Could not find iframe: {iframe_selector}")
                page_or_frame = frame

        errors: list[str] = []
        for locator in target.locators:
            try:
                pw_locator = self._resolve_locator(locator)
                # Check if the element exists and is visible (short timeout)
                await pw_locator.wait_for(state="visible", timeout=2000)
                logger.debug(
                    "Locator resolved: %s=%s", locator.strategy.value, locator.value
                )
                return pw_locator, locator
            except Exception as e:
                errors.append(f"{locator.strategy.value}={locator.value}: {e}")
                continue

        raise TimeoutError(
            f"All locators exhausted for '{target.description}':\n"
            + "\n".join(f"  - {err}" for err in errors)
        )

    async def click(self, target: ElementTarget, wait_for_network: bool = False) -> Locator:
        pw_locator, schema_locator = await self.find_element(target)
        if not wait_for_network:
            await pw_locator.click()
            return schema_locator

        page_url = self._page.url
        try:
            async with self._page.expect_request(
                lambda r: r.resource_type in SERVER_REQUEST_TYPES and same_site(r.url, page_url), timeout=5000,
            ) as request_info:
                await pw_locator.click()
            request = await request_info.value
            if request.resource_type == "document":
                await self._page.wait_for_load_state("load")
        except PlaywrightTimeoutError:
            # Not fatal: whether a click triggers a request can't be verified
            # strictly without false alarms (e.g. a cached response), and the
            # step's checkpoint still catches a genuinely wrong page.
            logger.warning(
                "Clicking '%s' was expected to contact the server (it did during discovery), "
                "but no request was seen; continuing once the page is quiet", target.description,
            )
        # Wait until the server has answered and anything that answer set off
        # (e.g. a page's follow-up data requests) has finished too.
        await self._network.wait_until_quiet()
        return schema_locator

    async def type_text(self, target: ElementTarget, text: str) -> Locator:
        pw_locator, schema_locator = await self.find_element(target)
        await pw_locator.fill(text)
        return schema_locator

    async def select_option(self, target: ElementTarget, value: str) -> Locator:
        pw_locator, schema_locator = await self.find_element(target)
        await pw_locator.select_option(value)
        # Some apps process the native 'change' event asynchronously; the
        # next action (often a click) can fire before that settles and
        # silently no-op. Give it a beat. See discovery/tools.py for the
        # same fix on the discovery-time path.
        await self._page.wait_for_timeout(300)
        return schema_locator

    async def extract_text(self, target: ElementTarget) -> tuple[str, Locator]:
        # Content is often filled in by a request that finishes after the
        # element itself is visible; let any in-flight request land first.
        await self._network.wait_until_quiet(timeout_ms=5000)
        pw_locator, schema_locator = await self.find_element(target)
        text = await pw_locator.text_content() or ""
        return text.strip(), schema_locator

    async def extract_table(self, target: ElementTarget) -> tuple[list[str], list[dict[str, str]], Locator]:
        # Before looking: a table hidden only because its data is still
        # loading must not be mistaken for a legitimately empty one below.
        await self._network.wait_until_quiet(timeout_ms=5000)
        try:
            pw_locator, schema_locator = await self.find_element(target)
        except TimeoutError:
            # Apps commonly hide an empty results table and show a "no results"
            # message instead. A table that's on the page but hidden is a
            # legitimate zero-row answer; one that's missing entirely is not.
            for schema_locator in target.locators:
                pw_locator = self._resolve_locator(schema_locator).first
                if await pw_locator.count():
                    logger.info("Table '%s' is present but hidden; treating as zero rows", target.description)
                    headers, _ = await read_table(pw_locator)
                    return headers, [], schema_locator
            raise
        headers, rows = await read_table(pw_locator)
        return headers, rows, schema_locator

    async def extract_cards(
        self, target: ElementTarget, card_selector: str,
    ) -> tuple[list[str], list[dict[str, str]], Locator]:
        await self._network.wait_until_quiet(timeout_ms=5000)
        # The container must be there; zero cards inside it is a legitimate
        # empty answer (an empty cart), like a hidden table.
        pw_locator, schema_locator = await self.find_element(target)
        keys, rows = await read_cards(pw_locator, card_selector)
        return keys, rows, schema_locator

    async def check_condition(
        self,
        condition_type: CheckpointType,
        value: str,
        locator: Locator | None = None,
    ) -> bool:
        try:
            match condition_type:
                case CheckpointType.URL_CONTAINS:
                    return value in self._page.url
                case CheckpointType.URL_EQUALS:
                    return self._page.url == value
                case CheckpointType.PAGE_TITLE_CONTAINS:
                    title = await self._page.title()
                    return value.lower() in title.lower()
                case CheckpointType.ELEMENT_VISIBLE:
                    if locator:
                        pw_loc = self._resolve_locator(locator)
                    else:
                        pw_loc = self._page.get_by_text(value)
                    return await pw_loc.is_visible()
                case CheckpointType.ELEMENT_CONTAINS_TEXT:
                    if locator:
                        pw_loc = self._resolve_locator(locator)
                        text = await pw_loc.text_content() or ""
                        return value.lower() in text.lower()
                    return False
                case CheckpointType.ELEMENT_NOT_VISIBLE:
                    if locator:
                        pw_loc = self._resolve_locator(locator)
                    else:
                        pw_loc = self._page.get_by_text(value)
                    return not await pw_loc.is_visible()
                case CheckpointType.URL_NOT_CONTAINS:
                    return value not in self._page.url
                case CheckpointType.ALERT_VISIBLE:
                    return bool(await self.read_alert_text())
                case _:
                    logger.warning("Unknown checkpoint type: %s", condition_type)
                    return False
        except Exception as e:
            logger.debug("Condition check failed: %s", e)
            return False

    async def wait_for_condition(
        self,
        condition_type: CheckpointType,
        value: str,
        locator: Locator | None = None,
        timeout_ms: int = 5000,
    ) -> bool:
        """Poll for a condition with timeout."""
        import asyncio

        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            if await self.check_condition(condition_type, value, locator):
                return True
            await asyncio.sleep(0.25)
        return False

    async def read_alert_text(self) -> str:
        """
        What the page is announcing, in its own words.

        ARIA alerts and live regions are how a page says "that didn't work,
        and here's why" -- the same thing a human operator would read off
        the screen. Nothing here knows which application it is looking at;
        a surface that announces nothing simply returns "".
        """
        for candidate in (
            self._page.get_by_role("alert"),
            self._page.locator("[aria-live]:not([aria-live='off'])"),
        ):
            try:
                for i in range(min(await candidate.count(), 3)):
                    one = candidate.nth(i)
                    if await one.is_visible():
                        text = (await one.inner_text() or "").strip()
                        if text:
                            return " ".join(text.split())
            except Exception as e:
                logger.debug("Could not read an alert: %s", e)
        return ""

    async def get_current_url(self) -> str:
        return self._page.url

    async def get_page_title(self) -> str:
        return await self._page.title()

    async def screenshot(self, path: str | None = None) -> bytes:
        from pathlib import Path as P

        data = await self._page.screenshot(full_page=False)
        if path:
            p = P(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return data

    async def accessibility_snapshot(self) -> str:
        return await self._page.aria_snapshot(mode="ai")

    async def key_press(self, key: str) -> None:
        await self._page.keyboard.press(key)

    async def scroll(self, direction: str = "down", amount: int = 3) -> None:
        delta = amount * 100
        if direction == "up":
            delta = -delta
        await self._page.mouse.wheel(0, delta)

    async def dismiss_dialog(self) -> bool:
        # Playwright handles dialogs via event listeners.
        # This is a best-effort attempt to dismiss any pending dialog.
        try:
            self._page.on("dialog", lambda d: d.dismiss())
            return True
        except Exception:
            return False
