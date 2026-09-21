"""
Discovery's side of escalation, against a real browser and a real operator
console, with the LLM replaced by a scripted one.

What matters here is that the recording run treats an irreversible action
the same way production replay does: it never takes it on the model's word,
and what a human does instead of it is recorded as a gap rather than
invented as steps.
"""

import asyncio
import json
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2 as httpx
import pytest

from cua.config import Config
from cua.discovery.agent import DiscoveryAgent
from cua.escalation.channel import EscalationChannel
from cua.escalation.operator_server import create_operator_app

PAGE = b"""<!doctype html>
<h1>Review your order</h1>
<div>Total: $9.99</div>
<button id="finish">Finish</button>
<h1 id="thanks" hidden>Thank you for your order!</h1>
<script>
  document.getElementById('finish').onclick = () => {
    document.getElementById('thanks').hidden = false;
    document.getElementById('finish').remove();
  };
</script>
"""


@pytest.fixture(scope="module")
def site():
    """A one-page site on localhost. No network, but a real origin."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


# --------------------------------------------------------------------------- #
# A scripted stand-in for the model                                            #
# --------------------------------------------------------------------------- #

@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


@dataclass
class _Message:
    tool_calls: list[_ToolCall]
    content: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in self.tool_calls
            ],
        }


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]


class ScriptedAgent(DiscoveryAgent):
    """A DiscoveryAgent whose next action comes from a list instead of an LLM."""

    def __init__(self, config: Config, script: list[tuple[str, dict]]):
        super().__init__(config)
        self.script = list(script)
        self.tool_results: list[dict] = []
        self._calls = 0

    async def _create_completion(self, **kwargs):
        # Whatever the agent last saw, so a scripted step can point at an
        # element by its name the way the model points at it by ref.
        snapshot = kwargs["messages"][-1].get("content") or ""
        for message in reversed(kwargs["messages"]):
            if message.get("role") == "user" and "[ref=" in (message.get("content") or ""):
                snapshot = message["content"]
                break
        self.tool_results = [
            json.loads(m["content"]) for m in kwargs["messages"] if m.get("role") == "tool"
        ]

        name, args = self.script[min(self._calls, len(self.script) - 1)]
        self._calls += 1
        args = dict(args)
        if "ref_for" in args:
            match = re.search(re.escape(args.pop("ref_for")) + r'.*?\[ref=(\w+)\]', snapshot)
            assert match, f"scripted target not in the snapshot: {snapshot[:400]}"
            args["ref"] = match.group(1)
        return _Response([_Choice(_Message([_ToolCall(f"call_{self._calls}", _Function(name, json.dumps(args)))]))])


def _config() -> Config:
    config = Config()
    config.browser.headless = True
    config.llm.api_key = "not-used-here"  # the scripted agent never calls out
    return config


FINISH = ("click", {"ref_for": 'button "Finish"', "description": "Place the order", "changes_data": True})
DONE = ("done", {"success": True, "summary": "order placed"})


async def _operator(channel: EscalationChannel, actions: list[tuple[str, str]], page_actions=None):
    """Answer intervention requests through the console's real HTTP API."""
    app = create_operator_app(channel.state_machine, channel.manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
        for action, notes in actions:
            while not (await client.get("/api/status")).json()["pending_intervention"]:
                await asyncio.sleep(0.02)
            if action.startswith("takeover"):
                assert (await client.post("/api/intervene", json={"action": "takeover"})).status_code == 200
                if page_actions:
                    await page_actions()
                hand_back = action.split(":")[1] if ":" in action else "completed"
                r = await client.post(
                    "/api/intervene", json={"action": "resume", "hand_back": hand_back, "notes": notes},
                )
            else:
                r = await client.post("/api/intervene", json={"action": action, "notes": notes})
            assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_irreversible_action_is_refused_when_no_operator_is_connected(site):
    """Discovery drives a live application: it must not commit anything unsupervised."""
    agent = ScriptedAgent(_config(), [FINISH, ("done", {"success": False, "summary": "needs approval"})])
    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=4,
    )

    assert result.success is False
    blocked = [r for r in agent.tool_results if "Blocked" in str(r.get("message", ""))]
    assert blocked, "the agent should have been told why it was refused"
    assert not any(s.action == "click" for s in result.capability.steps)


@pytest.mark.asyncio
async def test_operator_approval_lets_discovery_record_the_step(site):
    agent = ScriptedAgent(_config(), [FINISH, DONE])
    channel = EscalationChannel(timeout_seconds=10)
    operator = asyncio.create_task(_operator(channel, [("resume", "approved")]))

    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=4, escalation=channel,
    )
    await operator

    assert result.success is True
    assert [s.description for s in result.capability.steps if s.action == "click"] == ["Place the order"]
    assert result.interventions[0]["type"] == "confirmation"
    assert result.capability.meta.human_assisted == [], "automation did it, so there is no gap"


@pytest.mark.asyncio
async def test_what_the_operator_does_instead_becomes_a_recorded_gap(site):
    """A human clicking Finish themselves is evidence, not a step we can replay."""
    agent = ScriptedAgent(_config(), [FINISH, DONE])
    channel = EscalationChannel(timeout_seconds=10)
    clicked = asyncio.Event()

    async def do_it_by_hand():
        # The operator works in the same live page the agent was driving.
        page = channel.current_page
        await page.click("#finish")
        clicked.set()

    operator = asyncio.create_task(_operator(channel, [("takeover", "did it myself")], do_it_by_hand))

    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=4, escalation=channel,
    )
    await operator

    assert clicked.is_set()
    assert not any(s.action == "click" for s in result.capability.steps), "we can't record what we didn't do"
    assists = result.capability.meta.human_assisted
    assert len(assists) == 1
    assert 'clicked button "Finish"' in assists[0].what_the_human_did
    assert assists[0].operator_notes == "did it myself"


@pytest.mark.asyncio
async def test_an_agent_that_gives_up_asks_a_human_before_stopping(site):
    """'Stuck during discovery' is an escalation, not a silent failure."""
    agent = ScriptedAgent(_config(), [("done", {"success": False, "summary": "I can't find the button"})])
    channel = EscalationChannel(timeout_seconds=10)
    operator = asyncio.create_task(_operator(channel, [("abort", "the site is down")]))

    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=4, escalation=channel,
    )
    await operator

    assert result.success is False
    assert result.interventions[0]["type"] == "hard_failure"
    assert "I can't find the button" in result.interventions[0]["error_message"]
    assert "called the run off" in result.summary


@pytest.mark.asyncio
async def test_acting_first_and_pressing_approve_after_still_counts_as_taking_over(site):
    """
    What operators actually do: click the button in the live window, then
    go to the console. The run is parked the whole time, so that was a
    person -- and if it isn't treated as a takeover, automation performs
    the irreversible step a second time.
    """
    agent = ScriptedAgent(_config(), [FINISH, DONE])
    channel = EscalationChannel(timeout_seconds=10)

    async def operator():
        app = create_operator_app(channel.state_machine, channel.manager)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console") as client:
            while not (await client.get("/api/status")).json()["pending_intervention"]:
                await asyncio.sleep(0.02)
            await channel.current_page.click("#finish")      # done by hand, while paused
            await channel.current_page.wait_for_timeout(200)
            r = await client.post(                            # only now, the console
                "/api/intervene", json={"action": "resume", "notes": "placed it myself"},
            )
            assert r.status_code == 200, r.text

    task = asyncio.create_task(operator())
    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=4, escalation=channel,
    )
    await task

    record = result.interventions[0]
    assert record["operator_took_over"] is True
    assert record["operator_formally_took_control"] is False, "they never pressed Take over"
    assert any("before taking control" in line for line in record["what_the_human_did"])
    assert not any(s.action == "click" for s in result.capability.steps), "not done twice, not recorded"


@pytest.mark.asyncio
async def test_discovery_stops_on_its_time_budget(site):
    """
    Section 3.1 names three stopping conditions: max steps, timeout, dead
    end. A run can be well inside its step budget and still be going
    nowhere slowly, so the clock has to stop it too.
    """
    # A script that never finishes: it keeps scrolling, getting nowhere.
    agent = ScriptedAgent(_config(), [("scroll", {"direction": "down"})])
    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=100, max_seconds=2,
    )

    assert result.success is False
    assert "without completing the goal" in result.summary
    assert "2s" in result.summary
    assert agent._calls < 100, "it stopped on the clock, not on the step budget"


@pytest.mark.asyncio
async def test_a_person_can_grant_a_stalled_run_more_time(site):
    """Running out of time is a kind of stuck: someone looks, and decides."""
    agent = ScriptedAgent(_config(), [("scroll", {"direction": "down"})])
    channel = EscalationChannel(timeout_seconds=10)
    operator = asyncio.create_task(_operator(channel, [("abort", "it is not getting anywhere")]))

    result = await agent.discover(
        goal="Place the order", entry_point=site, capability_name="place_order",
        allowed_domains=["127.0.0.1"], max_steps=100, max_seconds=2, escalation=channel,
    )
    await operator

    assert result.success is False
    assert result.interventions[0]["type"] == "max_retries"
    assert "has been working for 2s" in result.interventions[0]["reason"]
    assert "called the run off" in result.summary
