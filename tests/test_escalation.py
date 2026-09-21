"""
Escalation wired into replay, exercised end to end through the real operator
console app (its HTTP API) with a simulated operator, and a fake surface
instead of a browser.
"""

import asyncio

import httpx2 as httpx
import pytest

from cua.browser.safety import SafetyGuard
from cua.escalation.channel import EscalationChannel
from cua.escalation.intervention import InterventionType
from cua.escalation.operator_server import create_operator_app
from cua.replay.engine import ReplayEngine
from cua.schema.artifact import (
    ActionType, Capability, CapabilityMeta, Checkpoint, CheckpointType, Contract, ElementTarget,
    Locator, LocatorStrategy, RiskLevel, SafetyPolicy, Step, SurfaceInfo, SurfaceType,
)
from cua.schema.results import ReplayStatus
from tests.test_core import FakeAdapter


def _capability(step: Step) -> Capability:
    return Capability(
        meta=CapabilityMeta(
            name="t", display_name="T", description="t",
            surface=SurfaceInfo(type=SurfaceType.WEB, entry_point="https://example.com", app_identifier="t"),
        ),
        contract=Contract(success_condition=Checkpoint(type=CheckpointType.URL_CONTAINS, value="example.com")),
        steps=[step],
        policy=SafetyPolicy(allowed_domains=["example.com"]),
    )


def _click(risk=RiskLevel.SAFE, checkpoint_value: str | None = None) -> Step:
    return Step(
        id="click_01", action=ActionType.CLICK, description="Open the account",
        target=ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#b")], description="b"),
        risk_level=risk,
        checkpoint=Checkpoint(type=CheckpointType.ELEMENT_VISIBLE, value=checkpoint_value) if checkpoint_value else None,
    )


async def _run(step: Step, adapter: FakeAdapter, channel: EscalationChannel | None, operator_actions=()):
    """Run one-step replay while a simulated operator answers through the console's HTTP API."""
    engine = ReplayEngine(escalation=channel)
    capability = _capability(step)
    guard = SafetyGuard(capability.policy)

    async def operator():
        app = create_operator_app(channel.state_machine, channel.manager)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
            for action, notes in operator_actions:
                while not (await client.get("/api/status")).json()["pending_intervention"]:
                    await asyncio.sleep(0.01)
                hand_back = None
                if action.startswith("takeover"):
                    # "takeover" alone means the operator says they did the
                    # step; "takeover:retry" means they only cleared the way.
                    hand_back = action.split(":")[1] if ":" in action else "completed"
                    r = await client.post("/api/intervene", json={"action": "takeover", "notes": notes})
                    assert r.status_code == 200
                    adapter.fail_times = 0  # the human fixes whatever was wrong
                    action = "resume"      # ...then hands control back
                elif action == "resume":
                    adapter.fail_times = 0
                body = {"action": action, "notes": notes}
                if hand_back:
                    body["hand_back"] = hand_back
                r = await client.post("/api/intervene", json=body)
                assert r.status_code == 200, r.text

    tasks = [asyncio.create_task(operator())] if operator_actions else []
    result = await engine._execute_steps(capability, {}, adapter, guard, session=None)
    result.interventions = engine._interventions
    for t in tasks:
        await t
    return result


@pytest.mark.asyncio
async def test_failure_escalates_and_resumes_after_operator_fix():
    adapter = FakeAdapter(fail_times=99)
    result = await _run(_click(), adapter, EscalationChannel(timeout_seconds=5), [("resume", "reloaded the page")])
    assert result.status == ReplayStatus.SUCCESS
    assert result.interventions[0]["type"] == "hard_failure"
    assert result.interventions[0]["operator_action"] == "resume"
    assert result.interventions[0]["operator_notes"] == "reloaded the page"


@pytest.mark.asyncio
async def test_operator_abort_stops_the_run():
    adapter = FakeAdapter(fail_times=99)
    result = await _run(_click(), adapter, EscalationChannel(timeout_seconds=5), [("abort", "wrong customer")])
    assert result.status == ReplayStatus.ESCALATED
    assert "aborted by the operator (wrong customer)" in result.failure_reason


@pytest.mark.asyncio
async def test_no_operator_response_times_out():
    adapter = FakeAdapter(fail_times=99)
    result = await _run(_click(), adapter, EscalationChannel(timeout_seconds=0.3))
    assert result.status == ReplayStatus.ESCALATED
    assert "no operator responded" in result.failure_reason
    assert result.interventions[0]["operator_action"] == "no_response"


@pytest.mark.asyncio
async def test_data_changing_step_blocked_without_escalation():
    adapter = FakeAdapter()
    result = await _run(_click(RiskLevel.NEEDS_CONFIRMATION), adapter, None)
    assert result.status == ReplayStatus.FAILURE
    assert "Blocked" in result.failure_reason
    assert adapter.click_attempts == 0, "must not act before a human approves"


@pytest.mark.asyncio
async def test_data_changing_step_runs_once_approved():
    adapter = FakeAdapter()
    result = await _run(_click(RiskLevel.NEEDS_CONFIRMATION), adapter, EscalationChannel(timeout_seconds=5), [("resume", "approved")])
    assert result.status == ReplayStatus.SUCCESS
    assert adapter.click_attempts == 1
    assert result.interventions[0]["type"] == "confirmation"


@pytest.mark.asyncio
async def test_takeover_that_completes_a_data_change_is_not_repeated():
    """The operator did the risky step by hand; its checkpoint now holds, so automation must not redo it."""
    adapter = FakeAdapter(recoverable_condition="Account Opened!")
    step = _click(RiskLevel.NEEDS_CONFIRMATION, checkpoint_value="Account Opened!")
    result = await _run(step, adapter, EscalationChannel(timeout_seconds=5), [("takeover", "did it myself")])
    assert result.status == ReplayStatus.SUCCESS
    assert adapter.click_attempts == 0
    assert result.interventions[0]["operator_took_over"] is True


@pytest.mark.asyncio
async def test_console_takeover_keeps_request_open_until_handed_back():
    channel = EscalationChannel(timeout_seconds=5)
    app = create_operator_app(channel.state_machine, channel.manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
        channel.manager.create_request(intervention_type=InterventionType.HARD_FAILURE, reason="x", capability_name="t")
        await channel.state_machine.pause("x")

        assert (await client.post("/api/intervene", json={"action": "takeover"})).status_code == 200
        status = (await client.get("/api/status")).json()
        assert status["state"] == "human_active"
        assert status["pending_intervention"] is not None, "request must stay open while the human has control"

        bare = await client.post("/api/intervene", json={"action": "resume"})
        assert bare.status_code == 422, "handing back must say what the human did"

        handed_back = await client.post(
            "/api/intervene", json={"action": "resume", "hand_back": "completed"}
        )
        assert handed_back.status_code == 200
        assert (await client.get("/api/status")).json()["state"] == "automation_active"


@pytest.mark.asyncio
async def test_operator_claim_is_checked_against_the_page():
    """They say they placed the order; the page says otherwise. Don't guess: ask again, saying why."""
    adapter = FakeAdapter(recoverable_condition="something else")
    step = _click(RiskLevel.NEEDS_CONFIRMATION, checkpoint_value="Account Opened!")
    result = await _run(
        step, adapter, EscalationChannel(timeout_seconds=5),
        [("takeover", "placed it"), ("abort", "my mistake, it did not go through")],
    )
    assert adapter.click_attempts == 0, "must not repeat a data-changing step on a disputed claim"
    assert "does not hold" in result.interventions[1]["error_message"], "the operator is told what disagreed"
    assert result.status == ReplayStatus.ESCALATED


@pytest.mark.asyncio
async def test_retry_claim_reruns_a_step_the_page_says_did_not_happen():
    """They only cleared the way, and the checkpoint confirms the step never ran."""
    adapter = FakeAdapter(recoverable_condition="something else")
    step = _click(RiskLevel.NEEDS_CONFIRMATION, checkpoint_value="Account Opened!")
    result = await _run(step, adapter, EscalationChannel(timeout_seconds=5), [("takeover:retry", "cleared a dialog")])
    assert adapter.click_attempts == 1
    assert result.interventions[0]["operator_says"] == "retry"


@pytest.mark.asyncio
async def test_retry_claim_refused_when_nothing_can_confirm_the_step_did_not_run():
    adapter = FakeAdapter()
    step = _click(RiskLevel.NEEDS_CONFIRMATION)  # no checkpoint to check
    result = await _run(
        step, adapter, EscalationChannel(timeout_seconds=5),
        [("takeover:retry", "had a look"), ("abort", "leave it")],
    )
    assert adapter.click_attempts == 0
    assert "no checkpoint" in result.interventions[1]["error_message"]
    assert result.status == ReplayStatus.ESCALATED


def test_intervention_request_carries_list_inputs():
    """A capability whose input is a list of items must still be escalatable."""
    from cua.escalation.intervention import EscalationManager

    request = EscalationManager().create_request(
        intervention_type=InterventionType.CONFIRMATION_REQUIRED,
        reason="Approval needed",
        capability_name="place_order",
        redacted_inputs={"items": ["Sauce Labs Backpack", "Sauce Labs Bike Light"], "zip": "***REDACTED***"},
    )
    assert request.redacted_inputs["items"] == ["Sauce Labs Backpack", "Sauce Labs Bike Light"]


@pytest.mark.asyncio
async def test_devtools_link_resolves_to_an_inspectable_page():
    """
    The CDP port's root serves an empty 200, so linking to it gives the
    operator a blank tab. The console has to look the inspector URL up.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    targets = [
        {"type": "background_page", "url": "chrome-extension://x", "devtoolsFrontendUrl": "/ext"},
        {"type": "page", "url": "about:blank", "devtoolsFrontendUrl": "https://devtools/inspector.html?ws=blank"},
        {"type": "page", "url": "https://example.com/checkout",
         "devtoolsFrontendUrl": "https://devtools/inspector.html?ws=real"},
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps(targets).encode() if self.path == "/json/list" else b""
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        channel = EscalationChannel()
        app = create_operator_app(
            channel.state_machine, channel.manager, cdp_url=f"http://127.0.0.1:{server.server_port}",
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
            response = await client.get("/api/devtools")
        assert response.status_code == 307
        assert response.headers["location"] == "https://devtools/inspector.html?ws=real", (
            "a real page beats a blank tab, and a background page is not a page"
        )
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_devtools_link_says_so_when_there_is_no_live_browser():
    channel = EscalationChannel()
    app = create_operator_app(channel.state_machine, channel.manager, cdp_url="http://127.0.0.1:1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
        response = await client.get("/api/devtools")
    assert response.status_code == 502
    assert "Could not reach the browser" in response.json()["detail"]


@pytest.mark.asyncio
async def test_live_view_is_captured_on_demand_not_cached():
    """The console polls this; a stale image would mislead the operator."""
    frames = [b"\x89PNG\r\n\x1a\nFIRST", b"\x89PNG\r\n\x1a\nSECOND"]

    async def live_view() -> bytes:
        return frames.pop(0)

    channel = EscalationChannel()
    app = create_operator_app(channel.state_machine, channel.manager, live_view=live_view)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
        assert (await client.get("/api/status")).json()["live_view"] is True
        first = await client.get("/api/live.png")
        second = await client.get("/api/live.png")

    assert first.content == b"\x89PNG\r\n\x1a\nFIRST"
    assert second.content == b"\x89PNG\r\n\x1a\nSECOND", "each request captures the session again"
    assert first.headers["content-type"] == "image/png"
    assert first.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_live_view_says_so_when_there_is_no_session():
    channel = EscalationChannel()
    app = create_operator_app(channel.state_machine, channel.manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
        assert (await client.get("/api/status")).json()["live_view"] is False
        assert (await client.get("/api/live.png")).status_code == 404
