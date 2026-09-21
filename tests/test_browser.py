"""
Tests that need a real (local, headless) browser: how the recorder turns the
element the model pointed at into replay locators, and exact-name matching
in the replay adapter. Uses a small inline page, no network.
"""

import re

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from cua.browser.adapter import PlaywrightAdapter
from cua.discovery.recorder import ActionRecorder
from cua.schema.artifact import ElementTarget, Locator, LocatorStrategy

PAGE = """
<h1>Accounts Overview</h1>
<table id="accountTable">
  <thead><tr><th>Account</th><th>Balance</th></tr></thead>
  <tbody>
    <tr><td><a href="#a">13344</a></td><td>$10.00</td></tr>
    <tr><td><a href="#b">12345</a></td><td>$20.00</td></tr>
  </tbody>
</table>
<form><p><b>Username</b></p><div><input type="text" name="username"></div>
<div><input type="submit" value="Log In"></div></form>
<table><tr><td>Balance:</td><td id="balance">$99.00</td></tr></table>
"""


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        await pg.set_content(PAGE)
        yield pg
        await browser.close()


def _ref(snapshot: str, pattern: str) -> str:
    return re.search(pattern + r".*?\[ref=(\w+)\]", snapshot).group(1)


def _recorder(**inputs) -> ActionRecorder:
    return ActionRecorder(
        name="t", display_name="T", description="t",
        entry_point="https://example.com/index.htm", inputs=inputs,
    )


@pytest.mark.asyncio
async def test_value_identified_element_has_no_positional_locators(page):
    """The link for the requested account is located by its value, never by position."""
    recorder = _recorder(account_id="12345")
    snapshot = await page.aria_snapshot(mode="ai")
    target = await recorder.describe_element(page, _ref(snapshot, r'link "12345"'), snapshot)

    assert target.locators, "expected at least one locator"
    assert all("12345" in loc.value for loc in target.locators)

    recorder.record_click(target, "Open account")
    cap = recorder.build()
    assert all(loc.value == "{{inputs.account_id}}" for loc in cap.steps[0].target.locators)
    assert "account_id" in cap.contract.inputs


@pytest.mark.asyncio
async def test_structural_element_gets_attribute_and_fallback_locators(page):
    """An unlabelled input (legacy markup) still gets usable locators without the model writing selectors."""
    recorder = _recorder(account_id="12345")
    snapshot = await page.aria_snapshot(mode="ai")
    target = await recorder.describe_element(page, _ref(snapshot, r"textbox"), snapshot)

    values = [loc.value for loc in target.locators]
    assert 'input[name="username"]' in values
    assert target.locators[-1].description == "by position in the page structure"


@pytest.mark.asyncio
async def test_submit_button_located_by_label(page):
    recorder = _recorder()
    snapshot = await page.aria_snapshot(mode="ai")
    target = await recorder.describe_element(page, _ref(snapshot, r'button "Log In"'), snapshot)

    assert target.locators[0].strategy == LocatorStrategy.ROLE
    assert target.locators[0].value == "Log In"
    assert 'input[value="Log In"]' in [loc.value for loc in target.locators]


@pytest.mark.asyncio
async def test_role_name_match_is_exact(page):
    """Account '1234' must not match the link for '12345'."""
    adapter = PlaywrightAdapter(page)
    target = ElementTarget(
        locators=[Locator(strategy=LocatorStrategy.ROLE, value="1234", role="link")],
        description="link 1234",
    )
    with pytest.raises(TimeoutError):
        await adapter.find_element(target)


@pytest.mark.asyncio
async def test_extracted_value_not_located_by_its_current_content(page):
    """A balance cell is found by where it is, not by the amount it shows today."""
    recorder = _recorder()
    snapshot = await page.aria_snapshot(mode="ai")
    target = await recorder.describe_element(page, _ref(snapshot, r'cell "\$99.00"'), snapshot, reading=True)

    assert target.locators
    assert all("$99.00" not in loc.value for loc in target.locators)
    assert '[id="balance"]' in [loc.value for loc in target.locators]


@pytest.mark.asyncio
async def test_hidden_table_is_zero_rows_not_failure(page):
    """An app hiding its empty results table ("No transactions found.") is a successful empty answer."""
    await page.set_content(
        '<table id="transactionTable" style="display:none">'
        "<thead><tr><th>Date</th><th>Amount</th></tr></thead><tbody></tbody></table>"
        "<p>No transactions found.</p>"
    )
    adapter = PlaywrightAdapter(page)
    target = ElementTarget(
        locators=[Locator(strategy=LocatorStrategy.CSS, value='[id="transactionTable"]')],
        description="transactions table",
    )
    headers, rows, _ = await adapter.extract_table(target)
    assert headers == ["Date", "Amount"]
    assert rows == []


@pytest.mark.asyncio
async def test_missing_table_is_still_a_failure(page):
    adapter = PlaywrightAdapter(page)
    target = ElementTarget(
        locators=[Locator(strategy=LocatorStrategy.CSS, value='[id="noSuchTable"]')],
        description="missing table",
    )
    with pytest.raises(TimeoutError):
        await adapter.extract_table(target)


# --------------------------------------------------------------------------- #
# "If missing" business outcome                                                #
# --------------------------------------------------------------------------- #

from cua.browser.safety import SafetyGuard
from cua.replay.engine import ReplayEngine
from cua.schema.artifact import (
    ActionType, Checkpoint, CheckpointType, ErrorClass, ErrorCondition, ErrorHandler, ParamType, SafetyPolicy, Step,
)
from cua.schema.results import ReplayStatus


def _open_account_step() -> Step:
    link = Locator(strategy=LocatorStrategy.ROLE, value="{{inputs.account_id}}", role="link")
    heading = Locator(strategy=LocatorStrategy.ROLE, value="Accounts Overview", role="heading")
    return Step(
        id="click_05",
        action=ActionType.CLICK,
        description="Open the account",
        target=ElementTarget(locators=[link], description='link "{{inputs.account_id}}"'),
        error_handlers=[ErrorHandler(
            condition=ErrorCondition(type=CheckpointType.ELEMENT_NOT_VISIBLE, value="", locator=link),
            also_requires=[ErrorCondition(type=CheckpointType.ELEMENT_VISIBLE, value="", locator=heading)],
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_key="account_not_found",
            outcome_message="The account isn't in this user's account list",
            max_retries=0,
        )],
    )


async def _run_step(page, account_id):
    return await ReplayEngine()._execute_step(
        step=_open_account_step(), inputs={"account_id": account_id},
        adapter=PlaywrightAdapter(page),
        guard=SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])),
        session=None, global_handlers=[],
    )


@pytest.mark.asyncio
async def test_missing_account_on_right_page_is_business_outcome(page):
    result = await _run_step(page, "9999")
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_key == "account_not_found"


@pytest.mark.asyncio
async def test_missing_account_on_wrong_page_is_failure(page):
    """If the overview page itself didn't load, a missing link is NOT 'account not found'."""
    await page.set_content("<h1>Service Unavailable</h1>")
    result = await _run_step(page, "9999")
    assert result.status == ReplayStatus.FAILURE


@pytest.mark.asyncio
async def test_existing_account_still_succeeds(page):
    result = await _run_step(page, "12345")
    assert result.status == ReplayStatus.SUCCESS


@pytest.mark.asyncio
async def test_recorder_parameterizes_if_missing_handler(page):
    recorder = _recorder(account_id="12345")
    snapshot = await page.aria_snapshot(mode="ai")
    target = await recorder.describe_element(page, _ref(snapshot, r'link "12345"'), snapshot)
    page_target = await recorder.describe_element(page, _ref(snapshot, r'heading "Accounts Overview"'), snapshot)
    recorder.record_click(target, "Open account", if_missing=("account_not_found", "Account 12345 is not listed", page_target))

    step = recorder.build().steps[0]
    handler = step.error_handlers[0]
    assert handler.condition.locator.value == "{{inputs.account_id}}"
    assert handler.also_requires[0].locator.value == "Accounts Overview"
    assert step.target.description == 'link "{{inputs.account_id}}"'
    assert handler.outcome_message == "Account {{inputs.account_id}} is not listed"


@pytest.mark.asyncio
async def test_reextracting_an_output_keeps_only_the_latest_step(page):
    recorder = _recorder()
    snapshot = await page.aria_snapshot(mode="ai")
    table = await recorder.describe_element(page, _ref(snapshot, r"table"), snapshot, reading=True)
    recorder.record_extract(table, "accounts", "first read", table_columns=["Account", "Balance"])
    recorder.record_extract(table, "accounts", "second read", table_columns=["Account", "Balance"])

    cap = recorder.build()
    extracts = [s for s in cap.steps if s.extract_output_key == "accounts"]
    assert len(extracts) == 1
    assert cap.contract.outputs["accounts"].extract_from_step == extracts[0].id


# --------------------------------------------------------------------------- #
# Clicks that reload data from the server                                     #
# --------------------------------------------------------------------------- #

import asyncio as _asyncio

from cua.discovery.tools import execute_tool

AJAX_PAGE = """
<table id="txns"><thead><tr><th>Date</th></tr></thead><tbody><tr><td>09-17-2026</td></tr></tbody></table>
<button id="go" onclick="fetch('/api/txns').then(r => r.json()).then(rows => {
  document.querySelector('#txns tbody').innerHTML = rows.map(d => '<tr><td>' + d + '</td></tr>').join('');
})">Go</button>
<button id="noop">Nothing</button>
"""


@pytest_asyncio.fixture
async def ajax_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()

        async def serve(route):
            if route.request.url.endswith("/api/txns"):
                await _asyncio.sleep(0.5)  # a slow server, like the real thing
                await route.fulfill(json=["02-03-2026"])
            else:
                await route.fulfill(content_type="text/html", body=AJAX_PAGE)

        await pg.route("https://app.test/**", serve)
        await pg.goto("https://app.test/page")
        # Like a real replay, the page has fully settled before the click. Without
        # this, a "wait for network idle" would still be waiting on the initial load
        # and hide the bug where it returns instantly for later requests.
        await pg.wait_for_load_state("networkidle")
        yield pg
        await browser.close()


def _target(css: str) -> ElementTarget:
    return ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value=css)], description=css)


@pytest.mark.asyncio
async def test_click_waits_for_server_before_next_read(ajax_page):
    adapter = PlaywrightAdapter(ajax_page)
    await adapter.click(_target("#go"), wait_for_network=True)
    _, rows, _ = await adapter.extract_table(_target("#txns"))
    assert rows == [{"Date": "02-03-2026"}], "read the table before the server's answer arrived"


@pytest.mark.asyncio
async def test_expected_request_that_never_happens_only_warns(ajax_page, caplog):
    """Too noisy to be fatal; the step's checkpoint still catches a wrong page."""
    adapter = PlaywrightAdapter(ajax_page)
    await adapter.click(_target("#noop"), wait_for_network=True)
    assert "no request was seen" in caplog.text


@pytest.mark.asyncio
async def test_discovery_click_reports_whether_it_contacted_the_server(ajax_page):
    snapshot = await ajax_page.aria_snapshot(mode="ai")
    go = await execute_tool("click", {"ref": _ref(snapshot, r'button "Go"'), "description": "go"}, ajax_page)
    snapshot = await ajax_page.aria_snapshot(mode="ai")
    noop = await execute_tool("click", {"ref": _ref(snapshot, r'button "Nothing"'), "description": "x"}, ajax_page)
    assert go["triggered_request"] is True
    assert noop["triggered_request"] is False


@pytest.mark.asyncio
async def test_network_quiet_waits_for_requests_after_page_settled(ajax_page):
    """The case Playwright's networkidle misses: a request that starts long after the page loaded."""
    from cua.browser.network import network_tracker

    tracker = network_tracker(ajax_page)
    await ajax_page.evaluate("() => { window.done = false; fetch('/api/txns').then(() => { window.done = true; }); }")
    assert await tracker.wait_until_quiet()
    assert await ajax_page.evaluate("() => window.done") is True


@pytest.mark.asyncio
async def test_failed_checkpoint_says_what_was_expected_and_seen(page):
    """E.g. a wrong password: the click works, but the next page never shows up."""
    await page.set_content('<h1>Error!</h1><p>The username and password could not be verified.</p><button>Log In</button>')
    heading = Locator(strategy=LocatorStrategy.ROLE, value="Accounts Overview", role="heading")
    step = Step(
        id="click_04", action=ActionType.CLICK, description="Log in",
        target=ElementTarget(locators=[Locator(strategy=LocatorStrategy.ROLE, value="Log In", role="button")], description="Log In"),
        checkpoint=Checkpoint(type=CheckpointType.ELEMENT_VISIBLE, value="", locator=heading, description='the "Accounts Overview" heading'),
        timeout_ms=1000,
    )
    result = await ReplayEngine()._execute_step(
        step=step, inputs={}, adapter=PlaywrightAdapter(page),
        guard=SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), session=None, global_handlers=[],
    )
    assert result.status == ReplayStatus.FAILURE
    assert result.step_id == "click_04"
    assert 'expected the "Accounts Overview" heading' in result.error_message
    assert '"Error!"' in result.error_message


# --------------------------------------------------------------------------- #
# Repeated cards (not an HTML table) as rows                                   #
# --------------------------------------------------------------------------- #

from cua.browser.adapter import find_card_selector, read_cards


def _card(name: str, price: str) -> str:
    return (
        f'<div class="inventory_item" data-test="inventory-item">'
        f'<div data-test="inventory-item-name">{name}</div>'
        f'<div data-test="inventory-item-price">{price}</div>'
        f'<button>Add to cart</button></div>'
    )


def _cart(*cards: str) -> str:
    return ('<div class="cart_list" data-test="cart-list"><div class="cart_quantity_label">QTY</div>'
            '<div class="cart_desc_label">Description</div>' + "".join(cards) + "</div>")


@pytest.mark.asyncio
async def test_find_cards_in_container_or_from_one_card(page):
    await page.set_content('<div data-test="inventory-list">' + _card("Backpack", "$29.99") + _card("Bike Light", "$9.99") + "</div>")
    assert await find_card_selector(page.locator('[data-test="inventory-list"]')) == '[data-test="inventory-item"]'
    # Model pointed at a single card instead of the list: still found.
    assert await find_card_selector(page.locator('[data-test="inventory-item"]').first) == '[data-test="inventory-item"]'


@pytest.mark.asyncio
async def test_read_cards_as_rows_keyed_by_data_test(page):
    await page.set_content('<div data-test="inventory-list">' + _card("Backpack", "$29.99") + _card("Bike Light", "$9.99") + "</div>")
    keys, rows = await read_cards(page.locator('[data-test="inventory-list"]'), '[data-test="inventory-item"]')
    assert keys == ["inventory-item-name", "inventory-item-price"]
    assert rows == [
        {"inventory-item-name": "Backpack", "inventory-item-price": "$29.99"},
        {"inventory-item-name": "Bike Light", "inventory-item-price": "$9.99"},
    ]


@pytest.mark.asyncio
async def test_recorded_card_selector_reads_one_or_zero_items(page):
    """Discovery sees two items; replay can see one or none, and still reads correctly."""
    await page.set_content(_cart(_card("Backpack", "$29.99"), _card("Bike Light", "$9.99")))
    selector = await find_card_selector(page.locator('[data-test="cart-list"]'))
    assert selector == '[data-test="inventory-item"]'

    adapter = PlaywrightAdapter(page)
    target = ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value='[data-test="cart-list"]')], description="cart")

    await page.set_content(_cart(_card("Backpack", "$29.99")))
    _, rows, _ = await adapter.extract_cards(target, selector)
    assert rows == [{"inventory-item-name": "Backpack", "inventory-item-price": "$29.99"}]

    await page.set_content(_cart())
    _, rows, _ = await adapter.extract_cards(target, selector)
    assert rows == []


# --------------------------------------------------------------------------- #
# List inputs: "the Add to cart button in the card showing {{item}}"           #
# --------------------------------------------------------------------------- #

CATALOG = (
    '<div data-test="inventory-list">'
    + "".join(
        f'<div class="inventory_item" data-test="inventory-item"><div data-test="inventory-item-name">{n}</div>'
        f'<div data-test="inventory-item-price">{p}</div><button>Add to cart</button></div>'
        for n, p in [("Sauce Labs Backpack", "$29.99"), ("Sauce Labs Bike Light", "$9.99"), ("Sauce Labs Onesie", "$7.99")]
    )
    + "</div>"
)


async def _record_two_adds(page):
    items = ["Sauce Labs Backpack", "Sauce Labs Bike Light"]
    recorder = _recorder(items=items)
    for item in items:
        snapshot = await page.aria_snapshot(mode="ai")
        # The "Add to cart" button that comes right after this product's name in the snapshot.
        after_name = snapshot[snapshot.index(item):]
        ref = re.search(r'button "Add to cart".*?\[ref=(\w+)\]', after_name).group(1)
        target = await recorder.describe_element(page, ref, snapshot)
        recorder.record_click(target, f"Add {item} to cart")
    return recorder


@pytest.mark.asyncio
async def test_repeated_label_is_located_by_its_card(page):
    await page.set_content(CATALOG)
    recorder = _recorder(items=["Sauce Labs Backpack", "Sauce Labs Bike Light"])
    snapshot = await page.aria_snapshot(mode="ai")
    ref = re.search(r'button "Add to cart".*?\[ref=(\w+)\]', snapshot[snapshot.index("Sauce Labs Bike Light"):]).group(1)
    target = await recorder.describe_element(page, ref, snapshot)

    assert all(loc.inside_card == '[data-test="inventory-item"]' for loc in target.locators)
    assert all(loc.card_text == "Sauce Labs Bike Light" for loc in target.locators)


@pytest.mark.asyncio
async def test_two_adds_fold_into_one_repeated_step(page):
    await page.set_content(CATALOG)
    cap = (await _record_two_adds(page)).build()

    assert len(cap.steps) == 1
    step = cap.steps[0]
    assert step.for_each == "items"
    assert step.target.locators[0].card_text == "{{inputs.items}}"
    items = cap.contract.inputs["items"]
    assert items.type == ParamType.LIST
    assert items.allowed_values == ["Sauce Labs Backpack", "Sauce Labs Bike Light", "Sauce Labs Onesie"]


@pytest.mark.asyncio
async def test_scoped_locator_clicks_the_right_card(page):
    await page.set_content(CATALOG + '<script>document.querySelectorAll("button").forEach(b => '
                           'b.onclick = () => b.textContent = "Remove")</script>')
    adapter = PlaywrightAdapter(page)
    target = ElementTarget(locators=[Locator(
        strategy=LocatorStrategy.ROLE, value="Add to cart", role="button",
        inside_card='[data-test="inventory-item"]', card_text="Sauce Labs Onesie",
    )], description="add onesie")
    await adapter.click(target)
    buttons = await page.locator("button").all_inner_texts()
    assert buttons == ["Add to cart", "Add to cart", "Remove"]


@pytest.mark.asyncio
async def test_screenshot_blanks_fields_holding_sensitive_values():
    from cua.browser.session import BrowserSession
    from cua.config import BrowserConfig

    async with BrowserSession(BrowserConfig(headless=True, cdp_port=9333)) as session:
        page = await session.new_page()
        await page.set_content('<input id="first" value="Jane"><input id="product" value="Backpack">')
        plain = await session.screenshot()
        masked = await session.screenshot(hide_values=["Jane"])

        assert plain != masked
        assert await page.locator("#first").get_attribute("data-cua-mask") == ""
        assert await page.locator("#product").get_attribute("data-cua-mask") is None


@pytest.mark.asyncio
async def test_test_attribute_used_when_present(page):
    await page.set_content('<ul data-test="inventory-list"><li>a</li><li>b</li></ul>')
    recorder = _recorder()
    snapshot = await page.aria_snapshot(mode="ai")
    ref = _ref(snapshot, r"- list")
    target = await recorder.describe_element(page, ref, snapshot, reading=True)
    assert '[data-test="inventory-list"]' in [loc.value for loc in target.locators]


@pytest.mark.asyncio
async def test_asking_for_a_table_on_cards_reads_the_cards(page):
    await page.set_content('<ul data-test="cart-list">' + _card("Backpack", "$29.99") + _card("Bike Light", "$9.99") + "</ul>")
    snapshot = await page.aria_snapshot(mode="ai")
    result = await execute_tool(
        "extract", {"ref": _ref(snapshot, r"- list"), "output_name": "items", "as_table": True, "description": "x"}, page,
    )
    assert result["card_selector"] == '[data-test="inventory-item"]'
    assert result["row_count"] == 2


@pytest.mark.asyncio
async def test_list_read_from_one_card_targets_its_container(page):
    await page.set_content('<div data-test="cart-list"><ul>' + _card("Backpack", "$29.99") + _card("Bike Light", "$9.99") + "</ul></div>")
    recorder = _recorder()
    # Point at the first card itself (not the list), as the model did.
    first_card = page.locator('[data-test="inventory-item"]').first
    await first_card.evaluate("el => el.setAttribute('role', 'listitem')")
    snapshot = await page.aria_snapshot(mode="ai")
    card_ref = _ref(snapshot, r"- listitem")

    result = await execute_tool("extract", {"ref": card_ref, "output_name": "items", "as_list": True, "description": "x"}, page)
    assert result["pointed_at_card"] is True

    container = await recorder.describe_element(page, card_ref, snapshot, reading=True, parent=True)
    adapter = PlaywrightAdapter(page)
    _, rows, _ = await adapter.extract_cards(container, result["card_selector"])
    assert [r["inventory-item-name"] for r in rows] == ["Backpack", "Bike Light"]


# --------------------------------------------------------------------------- #
# What the human did while they had control                                    #
# --------------------------------------------------------------------------- #

HANDOFF_PAGE = """
<h1>Checkout</h1>
<input type="text" name="firstName" placeholder="First Name">
<input type="password" name="password" placeholder="Password">
<select name="sort"><option>Name (A to Z)</option><option>Price (low to high)</option></select>
<button id="finish"><span>Finish</span></button>
"""


@pytest_asyncio.fixture
async def handoff_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        await pg.set_content(HANDOFF_PAGE)
        yield pg
        await browser.close()


@pytest.mark.asyncio
async def test_human_actions_are_captured_only_while_the_human_has_control(handoff_page):
    """Clicks and entries during the handoff are recorded; the automation's own are not."""
    from cua.escalation.actions import IN_CONTROL_HUMAN, HumanActionRecorder, summarize

    control = None
    recorder = HumanActionRecorder(
        handoff_page, control=lambda: control, hide_values=["s3cret-zip"],
    )
    await recorder.attach()

    # Automation acts first: nothing of this belongs in the handoff record.
    await handoff_page.click("#finish")
    await recorder.flush()
    assert recorder.actions == []

    control = IN_CONTROL_HUMAN
    await handoff_page.fill("input[name=firstName]", "Ada")
    await handoff_page.fill("input[name=password]", "secret_sauce")
    await handoff_page.select_option("select[name=sort]", label="Price (low to high)")
    await handoff_page.click("#finish")
    await recorder.flush()

    lines = summarize(recorder.actions)
    assert 'filled textbox "First Name" with "Ada"' in lines
    assert 'selected "Price (low to high)" in combobox "sort"' in lines
    assert 'filled password "Password"' in lines, "a password field reports only that it was filled"
    assert any(line.startswith("clicked button") and "Finish" in line for line in lines)
    assert not any("secret_sauce" in line for line in lines), "a password field never reports its value"


@pytest.mark.asyncio
async def test_captured_values_are_masked(handoff_page):
    """A value the caller marked sensitive is masked even in an ordinary text field."""
    from cua.escalation.actions import REDACTED, HumanActionRecorder, summarize

    from cua.escalation.actions import IN_CONTROL_HUMAN

    recorder = HumanActionRecorder(
        handoff_page, control=lambda: IN_CONTROL_HUMAN, hide_values=["90210"],
    )
    await recorder.attach()

    await handoff_page.fill("input[name=firstName]", "90210")
    await recorder.flush()

    assert REDACTED in summarize(recorder.actions)[0]
    assert "90210" not in summarize(recorder.actions)[0]


@pytest.mark.asyncio
async def test_actions_taken_before_the_operator_formally_takes_control_are_still_recorded(handoff_page):
    """
    Operators do the thing first and press the console button second. The
    run is parked either way, so it was still a person -- and losing it
    would leave the evidence saying a takeover happened with nothing in it.
    """
    from cua.escalation.actions import IN_CONTROL_PAUSED, HumanActionRecorder, summarize

    recorder = HumanActionRecorder(handoff_page, control=lambda: IN_CONTROL_PAUSED)
    await recorder.attach()

    await handoff_page.click("#finish")
    await recorder.flush()

    assert recorder.actions[0]["while"] == IN_CONTROL_PAUSED
    line = summarize(recorder.actions)[0]
    assert "clicked button" in line and "Finish" in line
    assert "before taking control" in line, "the order of events stays visible"


AMBIGUOUS_PAGE = """
<h1>Accounts</h1>
<div id="panel">first</div>
<div id="panel" data-test="the-real-one"><a href="#x">Continue</a></div>
"""


@pytest_asyncio.fixture
async def ambiguous_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        await pg.set_content(AMBIGUOUS_PAGE)
        yield pg
        await browser.close()


@pytest.mark.asyncio
async def test_recorded_locators_are_verified_against_the_page(ambiguous_page):
    """
    A duplicated id is present but useless. Writing it into the artifact
    means a replay that fails on a page where the element is in plain sight,
    so every candidate is tried at record time and kept only if it finds
    this element and nothing else. (SauceDemo really does ship two elements
    with id="inventory_container".)
    """
    recorder = _recorder()
    snapshot = await ambiguous_page.aria_snapshot(mode="ai")
    target = await recorder.describe_element(
        ambiguous_page, _ref(snapshot, r'link "Continue"'), snapshot, parent=True,
    )

    assert target.locators, "something has to identify it"
    assert not any('[id="panel"]' == l.value for l in target.locators), "ambiguous id dropped"
    assert any(l.value == '[data-test="the-real-one"]' for l in target.locators)

    for locator in target.locators:
        assert await ambiguous_page.locator(locator.value).count() == 1, locator.value
