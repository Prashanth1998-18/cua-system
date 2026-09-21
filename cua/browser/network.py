"""
Network quiet detection that works at any point in a page's life.

Playwright's wait_for_load_state("networkidle") only waits during a page's
initial load; once reached, later calls return immediately even with a
request in flight. This tracks server requests (xhr/fetch/document) as they
start and finish, so "quiet" means "nothing in flight right now".
"""

from __future__ import annotations

import asyncio
import weakref

from playwright.async_api import Page, Request

SERVER_REQUEST_TYPES = ("xhr", "fetch", "document")

_trackers: "weakref.WeakKeyDictionary[Page, NetworkTracker]" = weakref.WeakKeyDictionary()


class NetworkTracker:
    def __init__(self, page: Page):
        self._in_flight: set[Request] = set()
        page.on("request", self._started)
        page.on("requestfinished", self._done)
        page.on("requestfailed", self._done)

    def _started(self, request: Request) -> None:
        if request.resource_type in SERVER_REQUEST_TYPES:
            self._in_flight.add(request)

    def _done(self, request: Request) -> None:
        self._in_flight.discard(request)

    async def wait_until_quiet(self, quiet_ms: int = 300, timeout_ms: int = 10000) -> bool:
        """
        Wait until no server request has been in flight for `quiet_ms`.
        Returns False if that doesn't happen within `timeout_ms` (e.g. a page
        that polls constantly); callers treat that as best-effort.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        quiet_since = None
        while loop.time() < deadline:
            if self._in_flight:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = loop.time()
            elif loop.time() - quiet_since >= quiet_ms / 1000:
                return True
            await asyncio.sleep(0.05)
        return False


def same_site(url: str, page_url: str) -> bool:
    """Is `url` on the page's own site (ignoring subdomains)? Third-party
    analytics and ads don't count as the page talking to its server."""
    from urllib.parse import urlparse

    def site(u: str) -> str:
        return ".".join((urlparse(u).hostname or "").split(".")[-2:])

    return site(url) == site(page_url)


def network_tracker(page: Page) -> NetworkTracker:
    """The page's tracker, created on first use. Attach early so no request is missed."""
    tracker = _trackers.get(page)
    if tracker is None:
        tracker = _trackers[page] = NetworkTracker(page)
    return tracker
