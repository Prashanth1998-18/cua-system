"""
Tool definitions for the discovery agent.

These are the JSON tool schemas sent to the LLM, and the execution
functions that carry them out against the browser.

The model never writes selectors. Each observation is an accessibility
snapshot in which every element carries a ref (e.g. `link "13344" [ref=e31]`);
element tools take that ref, and the recorder derives replay locators from
the actual element (see recorder.describe_element). Model-authored selectors
were how position-based locators like `tr:first-child a` got into artifacts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Locator as PlaywrightLocator, Page

from ..browser.adapter import find_card_selector, read_cards, read_table
from ..browser.network import SERVER_REQUEST_TYPES, same_site

logger = logging.getLogger(__name__)


def element_by_ref(page: Page, ref: str) -> PlaywrightLocator:
    """The element a snapshot ref points at. Refs are only valid for the latest snapshot."""
    return page.locator(f"aria-ref={ref}")


_REF_PARAM = {
    "type": "string",
    "description": "The element's ref from the latest page snapshot, e.g. 'e31'",
}


# --------------------------------------------------------------------------- #
# Tool schemas (sent to the LLM as JSON)                                       #
# --------------------------------------------------------------------------- #

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element, identified by its ref from the latest page snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": _REF_PARAM,
                    "description": {
                        "type": "string",
                        "description": "Brief description of what this click does and why",
                    },
                    "changes_data": {
                        "type": "boolean",
                        "description": (
                            "True only if this click commits something that can't simply be undone: "
                            "placing an order, sending a payment or transfer, opening or closing an account. "
                            "False for reversible actions: adding to or removing from a cart, cancelling, "
                            "logging in, navigating, opening a record, sorting or filtering."
                        ),
                    },
                    "if_missing_outcome": {
                        "type": "string",
                        "description": (
                            "If the task says what it means when this element isn't there (e.g. 'if the "
                            "account isn't listed, report account not found'), a short snake_case name "
                            "for that outcome, e.g. 'account_not_found'. Omit otherwise."
                        ),
                    },
                    "if_missing_message": {
                        "type": "string",
                        "description": "With if_missing_outcome: a one-line explanation for the caller",
                    },
                    "page_ref": {
                        "type": "string",
                        "description": (
                            "With if_missing_outcome: ref of an element that shows you're on the right "
                            "page (e.g. the page heading), so a missing element is only treated as that "
                            "outcome when the page itself loaded correctly"
                        ),
                    },
                    "if_rejected_outcome": {
                        "type": "string",
                        "description": (
                            "If the task says what it means when this action doesn't go through and the "
                            "application says why on screen (e.g. 'if the login is rejected, report it "
                            "with the reason the site shows'), a short snake_case name for that outcome, "
                            "e.g. 'login_rejected'. It is reported to the caller as a legitimate answer, "
                            "with the application's own message. Omit otherwise."
                        ),
                    },
                    "if_rejected_message": {
                        "type": "string",
                        "description": "With if_rejected_outcome: a one-line explanation for the caller",
                    },
                    "clears_a_blocker": {
                        "type": "boolean",
                        "description": (
                            "True if this click is only needed because something was in the way that "
                            "may not be there next time: a banner, a cookie notice, a 'your session "
                            "is about to expire' prompt. It is not recorded as a step of the flow — "
                            "the step it was blocking gets it as a condition to recover from, so "
                            "replay dismisses it and retries only when it actually appears."
                        ),
                    },
                },
                "required": ["ref", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into an input field, identified by its ref from the latest page snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": _REF_PARAM,
                    "text": {"type": "string", "description": "The text to type into the field"},
                    "is_input_param": {
                        "type": "boolean",
                        "description": "True if this value came from the task's inputs rather than being a fixed value",
                    },
                    "param_name": {
                        "type": "string",
                        "description": "If is_input_param is true, the input's name (e.g. 'username')",
                    },
                    "is_sensitive": {
                        "type": "boolean",
                        "description": "True for sensitive data like passwords, which must be redacted from logs",
                    },
                    "description": {"type": "string", "description": "Brief description of what this input is"},
                },
                "required": ["ref", "text", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": (
                "Select an option in a dropdown (combobox), identified by its ref from the latest page "
                "snapshot. Use this instead of clicking the dropdown and pressing arrow keys."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": _REF_PARAM,
                    "value": {
                        "type": "string",
                        "description": "The option to select, by its visible label (e.g. 'SAVINGS')",
                    },
                    "is_input_param": {
                        "type": "boolean",
                        "description": "True if this selection came from the task's inputs rather than being fixed",
                    },
                    "param_name": {
                        "type": "string",
                        "description": "If is_input_param is true, the input's name (e.g. 'month')",
                    },
                    "description": {"type": "string", "description": "Brief description of what this selection does"},
                },
                "required": ["ref", "value", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract",
            "description": (
                "Read a value the task asks you to return, from the element with this ref. Use one "
                "extract per requested value, pointing at the element that holds just that value. "
                "When the task asks for values from each of a list of items (e.g. each item's name and "
                "price), read the whole list as ONE output with as_list=true, never one output per "
                "specific item: the capability must work for any items, not just the ones in this run. "
                "For a table of records (e.g. transactions), point at the table itself and set "
                "as_table=true. For repeated items that aren't a table (e.g. product cards), point at "
                "the element containing them all and set as_list=true. Don't use extract just to look at the page: "
                "every observation already includes the full page snapshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": _REF_PARAM,
                    "output_name": {
                        "type": "string",
                        "description": "Name for this value in the capability's output (e.g. 'balance')",
                    },
                    "as_table": {
                        "type": "boolean",
                        "description": "True if the element is an HTML table to capture as a list of rows",
                    },
                    "as_list": {
                        "type": "boolean",
                        "description": (
                            "True for repeated items that aren't a table (e.g. product cards or cart items): "
                            "point at the element that contains all of them. Each item becomes a row."
                        ),
                    },
                    "description": {"type": "string", "description": "What this value is"},
                },
                "required": ["ref", "output_name", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_press",
            "description": "Press a keyboard key (Enter, Tab, Escape, etc.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The key to press (e.g. 'Enter', 'Tab', 'Escape')"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page to see more content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "Direction to scroll"},
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait for a condition to be met on the page before proceeding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "enum": ["element_visible", "url_contains", "page_title_contains"],
                        "description": "Type of condition to wait for",
                    },
                    "value": {
                        "type": "string",
                        "description": "The value to wait for (text to be visible, URL fragment, etc.)",
                    },
                    "timeout_seconds": {"type": "integer", "description": "Maximum seconds to wait (default 10)"},
                },
                "required": ["condition", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_input_rule",
            "description": (
                "Record a rule the task states an input must follow, which the application itself "
                "won't enforce or reveal (e.g. 'the zip code must be an existing 5-digit US zip code'). "
                "Replay then rejects bad values before starting. Doesn't touch the page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "param_name": {"type": "string", "description": "The input's name, e.g. 'zip'"},
                    "rule": {"type": "string", "enum": ["us_zip_code"], "description": "The rule"},
                },
                "required": ["param_name", "rule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": (
                "Signal that the task is finished. Call this when everything the task asked for has been "
                "read, or when the task can't be completed (success=false, with the reason)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "description": "Whether the task was completed"},
                    "summary": {"type": "string", "description": "Brief summary of what was done or why it stopped"},
                },
                "required": ["success", "summary"],
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# Tool execution (against the browser)                                         #
# --------------------------------------------------------------------------- #

async def execute_tool(tool_name: str, args: dict[str, Any], page: Page) -> dict[str, Any]:
    """Execute a tool call against the browser. Returns a result dict fed back to the LLM."""
    try:
        match tool_name:
            case "navigate":
                await page.goto(args["url"], wait_until="domcontentloaded")
                return {"status": "success", "url": page.url}

            case "click":
                requests: list[str] = []

                def on_request(request):
                    if request.resource_type in SERVER_REQUEST_TYPES and same_site(request.url, page.url):
                        requests.append(request.url)

                page.on("request", on_request)
                try:
                    await element_by_ref(page, args["ref"]).click(timeout=5000)
                    # A click handler's request can start a moment after the click returns.
                    await page.wait_for_timeout(500)
                finally:
                    page.remove_listener("request", on_request)
                return {"status": "success", "clicked": args["ref"], "triggered_request": bool(requests)}

            case "type_text":
                await element_by_ref(page, args["ref"]).fill(args["text"], timeout=5000)
                return {"status": "success", "typed_into": args["ref"]}

            case "select_option":
                element = element_by_ref(page, args["ref"])
                option = args["value"]
                try:
                    await element.select_option(label=option, timeout=5000)
                except Exception:
                    await element.select_option(value=option, timeout=5000)
                # Some apps handle the native 'change' event asynchronously; a click
                # fired right after can land before that settles and silently no-op.
                await page.wait_for_timeout(300)
                return {"status": "success", "selected": option, "on": args["ref"]}

            case "extract":
                element = element_by_ref(page, args["ref"])
                # Asked for a table, but it isn't one (e.g. item cards laid out
                # like a table): read it as repeated cards instead of returning
                # an empty table.
                is_table = await element.evaluate("el => el.tagName === 'TABLE'")
                if args.get("as_list") or (args.get("as_table") and not is_table):
                    card_selector = await find_card_selector(element)
                    if not card_selector:
                        return {"status": "error", "message": "No repeated items found in or around that element"}
                    keys, rows = await read_cards(element, card_selector)
                    return {
                        "status": "success",
                        "output_name": args["output_name"],
                        "card_selector": card_selector,
                        # Pointed at one card rather than the list: the recorder
                        # targets its container, or replay would match every card.
                        "pointed_at_card": await element.evaluate("(el, sel) => el.matches(sel)", card_selector),
                        "columns": keys,
                        "row_count": len(rows),
                        "sample_rows": rows[:5],
                    }
                if args.get("as_table"):
                    headers, rows = await read_table(element)
                    return {
                        "status": "success",
                        "output_name": args["output_name"],
                        "columns": headers,
                        "row_count": len(rows),
                        # Enough for the model to sanity-check it picked the right table.
                        "sample_rows": rows[:5],
                    }
                text = await element.text_content(timeout=5000) or ""
                return {"status": "success", "output_name": args["output_name"], "extracted_value": text.strip()}

            case "key_press":
                await page.keyboard.press(args["key"])
                return {"status": "success", "key": args["key"]}

            case "scroll":
                await page.mouse.wheel(0, 300 if args["direction"] == "down" else -300)
                return {"status": "success", "direction": args["direction"]}

            case "wait":
                condition, value = args["condition"], args["value"]
                timeout = args.get("timeout_seconds", 10) * 1000
                if condition == "element_visible":
                    await page.get_by_text(value).first.wait_for(state="visible", timeout=timeout)
                elif condition == "url_contains":
                    await page.wait_for_url(f"**{value}**", timeout=timeout)
                elif condition == "page_title_contains":
                    deadline = asyncio.get_event_loop().time() + timeout / 1000
                    while asyncio.get_event_loop().time() < deadline:
                        if value.lower() in (await page.title()).lower():
                            break
                        await asyncio.sleep(0.25)
                return {"status": "success", "waited_for": f"{condition}={value}"}

            case "done":
                return {"status": "done", "success": args["success"], "summary": args["summary"]}

            case "set_input_rule":
                return {"status": "success", "recorded": f"{args['param_name']}: {args['rule']}"}

            case _:
                return {"status": "error", "message": f"Unknown tool: {tool_name}"}

    except Exception as e:
        logger.warning("Tool '%s' failed: %s", tool_name, e)
        return {"status": "error", "message": str(e)}
