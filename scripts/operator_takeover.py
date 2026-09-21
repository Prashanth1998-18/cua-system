"""
Stands in for a person at the operator console, so the takeover path can be
demonstrated (and re-demonstrated) without a human at the keyboard.

It does exactly what a person would do, through the same two surfaces they
would use:

  1. the console's HTTP API  -- take control, then hand it back saying what
     they did;
  2. the live browser itself -- reached over the CDP URL the intervention
     request advertises, which is how an operator on another machine would
     attach to the session the automation is using.

Everything it does by hand is captured by the handoff recorder, exactly as a
real operator's clicks would be.

    python scripts/operator_takeover.py --click "Finish"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx2 as httpx
from playwright.async_api import async_playwright


async def wait_for_request(client: httpx.AsyncClient, timeout: float) -> dict:
    """Poll the console the way the operator UI does."""
    for _ in range(int(timeout / 0.5)):
        status = (await client.get("/api/status")).json()
        if status.get("pending_intervention"):
            return status
        await asyncio.sleep(0.5)
    raise SystemExit("No intervention request arrived")


async def click_in_the_live_session(cdp_url: str, button: str) -> None:
    """Attach to the browser the automation is using and click, as a person would."""
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        page = next((pg for pg in context.pages if not pg.is_closed()), None)
        if page is None:
            raise SystemExit("No open page in the live session")
        print(f"  operator is on {page.url}")
        await page.get_by_role("button", name=button, exact=True).click()
        await page.wait_for_timeout(1000)
        print(f"  operator clicked {button!r}; page is now {page.url}")
        await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", default="http://127.0.0.1:8080")
    parser.add_argument("--click", default="Finish", help="Button the operator presses by hand")
    parser.add_argument("--hand-back", default="completed", choices=["completed", "retry"])
    parser.add_argument("--notes", default="Placed the order myself during the handoff")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.console, timeout=10) as client:
        status = await wait_for_request(client, args.timeout)
        request = status["pending_intervention"]
        print(f"Intervention: {request['intervention_type']} - {request['reason']}")
        print(f"  at {request['current_url']}")

        taken = await client.post("/api/intervene", json={"action": "takeover"})
        taken.raise_for_status()
        print(f"  {taken.json()['message']}")

        await click_in_the_live_session(status["cdp_url"] or request["cdp_url"], args.click)

        handed_back = await client.post(
            "/api/intervene",
            json={"action": "resume", "hand_back": args.hand_back, "notes": args.notes},
        )
        handed_back.raise_for_status()
        print(f"  {handed_back.json()['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
