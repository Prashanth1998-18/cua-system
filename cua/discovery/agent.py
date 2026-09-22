"""
Discovery agent: LLM-driven observe/decide/act loop.

The loop itself is small (ADR-009): call the model with tools, execute
the tool call it returns, feed the result back, repeat until done. Most
of what follows is not the loop -- it is asking a human before anything
irreversible, noticing when the agent is stuck, and the step and time
budgets that stop a run going nowhere.

The agent:
1. Observes the page (accessibility tree)
2. Sends observation + goal to the LLM
3. LLM returns a tool call (click, type, extract, etc.)
4. Agent executes the tool call against the browser
5. Recorder captures the action for the capability artifact
6. Repeat until LLM calls 'done' or max steps reached
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI, BadRequestError

from ..browser.safety import SafetyGuard, SafetyViolation
from ..browser.session import BrowserSession
from ..config import Config
from ..escalation.channel import EscalationChannel, EscalationOutcome
from ..escalation.intervention import InterventionType, OperatorAction
from ..schema.artifact import TEMPLATE_PATTERN, Capability, ElementTarget, RiskLevel, SafetyPolicy
from .observer import PageObserver
from .recorder import ActionRecorder
from .tools import TOOL_SCHEMAS, element_by_ref, execute_tool

logger = logging.getLogger(__name__)

# How many more steps an operator's "keep going" is worth, and how many
# times they can grant them before the run is called off for good.
STEP_EXTENSION = 10
MAX_STEP_EXTENSIONS = 2

# Actions failing back to back, with nothing to explain it, is the agent
# being stuck rather than the page being awkward.
STUCK_AFTER_ERRORS = 3

# A run can be within its step budget and still be going nowhere slowly, so
# the clock is a stopping condition of its own. Time spent waiting for a
# person doesn't count against it.
DEFAULT_MAX_SECONDS = 900
TIME_EXTENSION_S = 300


@dataclass
class DiscoveryResult:
    """
    What a discovery run produced.

    `capability` is always built, because a half-recorded flow is worth
    reading even when it isn't worth keeping; `success` says whether the
    agent actually reached the goal, and only the caller decides what to do
    with a run that didn't.
    """
    capability: Capability
    success: bool
    summary: str
    interventions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def human_assisted(self) -> bool:
        return bool(self.capability.meta.human_assisted)


def _stop_reason(outcome: EscalationOutcome, timeout_seconds: float) -> str:
    """Why the run ended, phrased for whoever reads the log."""
    if outcome.action is None:
        return f"no operator responded within {int(timeout_seconds)}s"
    notes = outcome.record.get("operator_notes")
    return "the operator called the run off" + (f" ({notes})" if notes else "")

SYSTEM_PROMPT = """You are a browser automation agent. Your goal is to complete a task on a web application by observing the page and taking actions.

You will receive:
1. The task, usually as a step-by-step procedure. Follow it as written.
2. The current page: URL, title, and a snapshot of the page's accessibility tree in which every element has a ref, e.g. `link "13344" [ref=e31]`.

You must:
- Take ONE action at a time by calling a tool.
- Target elements by their ref from the LATEST snapshot. Refs change between snapshots.
- When the task identifies something by a value (e.g. "the row for account 13344"), target the element that shows that value (the link "13344"), never "the first row" or similar.
- If the task says what it means when something isn't there (e.g. "if the account isn't listed, report account not found"), set if_missing_outcome, if_missing_message and page_ref on the click that depends on it.
- If something that only appears sometimes is in your way (a banner, a cookie notice, a session prompt), click whatever dismisses it and set clears_a_blocker=true on that click. Don't treat it as part of the task: the next run may not have it.
- If the task says what it means when an action is refused by the application itself (e.g. "if the login is rejected, report it with the reason the site shows"), set if_rejected_outcome and if_rejected_message on that click. You don't need to have seen the refusal: it is recorded against this step not working, and the application's own message is read at the time.
- Use one extract per value the task asks you to return, pointing at the element that holds just that value. Capture tables of records with as_table=true.
- When the task asks for values from each of a list of items, read the whole list as ONE output (as_list=true), never one output per specific item: the capability must work for any items.
- Only extract values the task asks you to return. You already see the whole page in every snapshot, so don't extract just to look around.
- When you type or select a value that came from the task's inputs, set is_input_param=true with the input's name.
- Secret inputs (like passwords) are given to you only as placeholders such as {{inputs.password}}. Type the placeholder exactly; it is filled in for you.
- If the task states a rule an input must follow (e.g. "the zip code must be an existing 5-digit US zip code"), record it with set_input_rule.
- Call 'done' when the task is complete, or with success=false if it can't be completed, saying why.

Important:
- If a page hasn't loaded yet, use the 'wait' tool.
- Set changes_data=true only on a click that commits something that can't simply be undone (placing an order, a payment or transfer, opening an account). Adding to a cart, cancelling, logging in, navigating and sorting are reversible: false.
- Never refuse an action the task asks for because it needs approval, and never ask for approval in text. Call the click with changes_data=true: the system pauses, asks a human operator, and tells you what they decided. Waiting for approval is not your job.
"""


def with_secrets(args: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    """
    Fill secret placeholders in the text to be typed, just before execution.
    The model's own arguments (which get logged and sent back to it) keep
    the placeholder.
    """
    if not secrets or "text" not in args:
        return args
    text = TEMPLATE_PATTERN.sub(lambda m: secrets.get(m.group(1), m.group(0)), args["text"])
    return {**args, "text": text}


class DiscoveryAgent:
    """
    Runs the observe/decide/act loop for discovering a new capability.

    Usage:
        agent = DiscoveryAgent(config)
        capability = await agent.discover(
            goal="Log in and check the balance of account 12345",
            entry_point="https://parabank.parasoft.com/parabank/index.htm",
            capability_name="check_account_balance",
        )
    """

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._client = AsyncOpenAI(
            base_url=self.config.llm.base_url,
            api_key=self.config.llm.api_key,
        )
        # Different OpenAI-compatible endpoints disagree on request shape
        # (older ones want 'max_tokens', newer reasoning models want
        # 'max_completion_tokens' and reject tool calls unless
        # reasoning_effort='none'). These are discovered lazily from the
        # first error(s) returned and then reused for the rest of the run.
        self._token_param_name = "max_tokens"
        self._extra_params: dict[str, Any] = {}
        self._unsupported_params: set[str] = set()

    async def _create_completion(self, **kwargs: Any):
        """Call chat.completions.create, adapting to endpoint quirks as they're discovered."""
        for _ in range(4):
            call_kwargs = {
                **kwargs,
                self._token_param_name: self.config.llm.max_tokens,
                **self._extra_params,
            }
            for param in self._unsupported_params:
                call_kwargs.pop(param, None)
            try:
                return await self._client.chat.completions.create(**call_kwargs)
            except BadRequestError as e:
                body = e.body if isinstance(e.body, dict) else {}
                bad_param = body.get("param") or body.get("error", {}).get("param")
                message = body.get("message") or body.get("error", {}).get("message") or ""

                if bad_param in ("max_tokens", "max_completion_tokens"):
                    self._token_param_name = (
                        "max_completion_tokens" if bad_param == "max_tokens" else "max_tokens"
                    )
                    logger.info("Retrying with '%s' instead of '%s'", self._token_param_name, bad_param)
                    continue

                if bad_param and bad_param in call_kwargs and bad_param not in (
                    "model", "messages", "tools", "tool_choice", "temperature",
                ):
                    logger.info("Endpoint rejects '%s'; dropping it from future requests", bad_param)
                    self._unsupported_params.add(bad_param)
                    continue

                if bad_param == "reasoning_effort" or "reasoning_effort" in message:
                    logger.info("Retrying with reasoning_effort='none' for tool-call support")
                    self._extra_params["reasoning_effort"] = "none"
                    continue

                raise
        raise RuntimeError("Could not adapt chat.completions request after 4 attempts")

    async def discover(
        self,
        goal: str,
        entry_point: str,
        capability_name: str,
        display_name: str | None = None,
        description: str | None = None,
        allowed_domains: list[str] | None = None,
        max_steps: int = 30,
        inputs: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        escalation: EscalationChannel | None = None,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> DiscoveryResult:
        """
        Run discovery: navigate to entry_point, achieve goal, return artifact.

        Args:
            goal: Natural language description of what to accomplish
            entry_point: URL to start from
            capability_name: Machine-friendly name for the capability
            display_name: Human-friendly name (defaults to capability_name)
            description: What this capability does
            allowed_domains: Domains the browser can navigate to
            max_steps: Maximum tool calls before giving up
            inputs: Concrete values for parameterized inputs during discovery
                    (e.g., {"username": "john", "account_id": "13344"})
            secrets: Sensitive inputs (e.g., {"password": "demo"}). The model only
                    ever sees a placeholder; the value is filled in when typing, so
                    it never reaches the LLM provider or the logs.
            escalation: Where to send an intervention request when the run needs a
                    person: before anything irreversible, and when the agent is
                    stuck. Without one, an irreversible action is refused outright
                    rather than taken unsupervised.
        """
        secrets = secrets or {}
        display_name = display_name or capability_name.replace("_", " ").title()
        description = description or goal

        recorder = ActionRecorder(
            name=capability_name,
            display_name=display_name,
            description=description,
            entry_point=entry_point,
            allowed_domains=allowed_domains,
            inputs=inputs,
            secrets=set(secrets),
        )

        guard = SafetyGuard(SafetyPolicy(
            allowed_domains=allowed_domains or [self._extract_domain(entry_point)],
            max_steps=max_steps,
        ))
        if escalation is not None:
            escalation.hide_values = list(secrets.values())

        interventions: list[dict[str, Any]] = []
        success = False
        summary = ""
        stopped: str | None = None

        async with BrowserSession(self.config.browser) as session:
            page = await session.new_page()
            observer = PageObserver(session, self.config.browser.screenshot_dir, hide_values=list(secrets.values()))
            if escalation is not None:
                escalation.live_view = lambda: session.screenshot(hide_values=list(secrets.values()))

            async def describe_page() -> dict[str, str]:
                try:
                    return {"url": page.url, "title": await page.title()}
                except Exception:
                    return {"url": "", "title": ""}

            async def escalate(kind: InterventionType, reason: str, what: str, error: str | None = None):
                """Hand the live session to an operator and wait for their decision."""
                nonlocal waiting_on_human
                paused_at = time.monotonic()
                outcome = await escalation.request(
                    intervention_type=kind,
                    reason=reason,
                    capability_name=capability_name,
                    capability_display_name=display_name,
                    step_id=recorder.last_step_id or "discovery",
                    step_description=what,
                    describe_page=describe_page,
                    error_message=error,
                    screenshot_path=await self._escalation_screenshot(session, capability_name, secrets),
                    redacted_inputs={
                        **{k: str(v) for k, v in (inputs or {}).items()},
                        **{k: "«redacted»" for k in secrets},
                    },
                    page=page,
                )
                waiting_on_human += time.monotonic() - paused_at
                interventions.append(outcome.record)
                if outcome.took_over:
                    # What they did by hand is a gap in the recording, not steps.
                    recorder.note_human_assist(
                        reason=reason,
                        what_the_human_did=outcome.record.get("what_the_human_did", []),
                        operator_notes=outcome.record.get("operator_notes", ""),
                    )
                return outcome

            def report_takeover(outcome) -> str:
                did = outcome.record.get("what_the_human_did") or ["nothing we could see"]
                return (
                    "A human operator took control of the browser and did the following: "
                    + "; ".join(did)
                    + ". They have handed control back. Their actions were NOT recorded as "
                    "steps of this capability. Look at the page as it is now and continue."
                )

            # Navigate to entry point
            guard.check_url_for_navigation(entry_point)
            await page.goto(entry_point, wait_until="domcontentloaded")
            recorder.record_navigate(entry_point, f"Navigate to {display_name} entry point")

            # Run the agent loop
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
            ]

            # (step id, snapshot before its action, action start time) of the
            # last recorded step, waiting for the next observation to supply
            # what the action changed.
            pending: tuple[str, str, str, float] | None = None

            # Steps the agent may take, and how long it may take them.
            # Running out of either is a kind of stuck: a person can look at
            # where it got to and decide whether to grant more.
            budget = max_steps
            extensions = 0
            used = 0
            consecutive_errors = 0
            deadline = time.monotonic() + max_seconds
            waiting_on_human = 0.0

            def time_left() -> float:
                return deadline + waiting_on_human - time.monotonic()

            while True:
                if time_left() <= 0:
                    if escalation is None or extensions >= MAX_STEP_EXTENSIONS:
                        stopped = f"ran for {max_seconds:.0f}s without completing the goal"
                        logger.warning("Discovery %s", stopped)
                        break
                    before = time.monotonic()
                    outcome = await escalate(
                        InterventionType.MAX_RETRIES_EXCEEDED,
                        f"The agent has been working for {max_seconds:.0f}s without finishing",
                        goal,
                    )
                    waiting_on_human += time.monotonic() - before
                    if outcome.action in (None, OperatorAction.ABORT):
                        stopped = _stop_reason(outcome, escalation.timeout_seconds)
                        break
                    if outcome.took_over:
                        messages.append({"role": "user", "content": report_takeover(outcome)})
                    extensions += 1
                    deadline += TIME_EXTENSION_S
                    logger.info("Operator granted %ds more (budget now %.0fs)", TIME_EXTENSION_S, max_seconds + TIME_EXTENSION_S * extensions)
                    continue

                if used >= budget:
                    if escalation is None or extensions >= MAX_STEP_EXTENSIONS:
                        stopped = f"hit the {budget}-step limit without completing the goal"
                        logger.warning("Discovery %s", stopped)
                        break
                    outcome = await escalate(
                        InterventionType.MAX_RETRIES_EXCEEDED,
                        f"The agent has used all {budget} of its steps without finishing",
                        goal,
                    )
                    if outcome.action in (None, OperatorAction.ABORT):
                        stopped = _stop_reason(outcome, escalation.timeout_seconds)
                        break
                    if outcome.took_over:
                        messages.append({"role": "user", "content": report_takeover(outcome)})
                    extensions += 1
                    budget += STEP_EXTENSION
                    logger.info("Operator granted %d more steps (budget now %d)", STEP_EXTENSION, budget)
                    continue

                used += 1
                step = used

                # 1. Observe
                observation = await observer.observe()
                if pending:
                    step_id, before, before_url, started = pending
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    recorder.add_checkpoint(
                        step_id, before, observation["snapshot"], elapsed_ms,
                        before_url=before_url, after_url=observation["url"],
                    )
                    pending = None
                page_state = observer.format_for_llm(observation)

                user_message = f"Goal: {goal}\n\n{page_state}"
                if inputs:
                    user_message += f"\n\nAvailable input values: {json.dumps(inputs)}"
                if secrets:
                    placeholders = ", ".join(f"{name}: {{{{inputs.{name}}}}}" for name in secrets)
                    user_message += (
                        "\n\nSecret inputs (values are hidden from you). To enter one, type its "
                        f"placeholder exactly as written and it will be filled in: {placeholders}"
                    )

                messages.append({"role": "user", "content": user_message})

                # 2. Decide (LLM picks a tool)
                logger.info("Discovery step %d: asking LLM for next action...", step)
                response = await self._create_completion(
                    model=self.config.llm.model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                    temperature=self.config.llm.temperature,
                )

                choice = response.choices[0]

                # Check if LLM wants to call a tool
                if not choice.message.tool_calls:
                    # LLM responded with text instead of a tool call
                    logger.info("LLM responded with text: %s", choice.message.content)
                    messages.append({
                        "role": "assistant",
                        "content": choice.message.content,
                    })
                    # Prompt it to take an action
                    messages.append({
                        "role": "user",
                        "content": "Please take an action by calling one of the available tools, or call 'done' if the goal is achieved.",
                    })
                    continue

                tool_call = choice.message.tool_calls[0]
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                logger.info(
                    "Discovery step %d: LLM called %s(%s)",
                    step, tool_name, json.dumps(tool_args, indent=None),
                )

                # Add assistant message with tool call
                messages.append(choice.message.model_dump())

                # The system prompt asks for one action at a time, but some
                # models return multiple tool_calls in one turn even with
                # parallel_tool_calls=False. Only the first is executed; the
                # rest must still get a tool response or the next API call
                # is rejected for leaving tool_call_ids unanswered.
                for extra_call in choice.message.tool_calls[1:]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": extra_call.id,
                        "content": json.dumps({
                            "status": "skipped",
                            "message": "Only one tool call is executed per step. Wait for the next observation.",
                        }),
                    })

                # Check for done
                if tool_name == "done":
                    finished = bool(tool_args.get("success", True))
                    summary = tool_args.get("summary", "")
                    if finished:
                        logger.info("Discovery complete: %s", summary)
                        success = True
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"status": "done"}),
                        })
                        break

                    # The agent gave up. That is exactly the "stuck" case the
                    # operator exists for: they can finish it by hand, tell it
                    # to carry on, or call the run off.
                    logger.warning("Discovery agent gave up: %s", summary)
                    if escalation is None:
                        stopped = f"the agent stopped: {summary}"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"status": "stopped"}),
                        })
                        break
                    outcome = await escalate(
                        InterventionType.HARD_FAILURE,
                        f"The agent stopped before finishing: {summary}",
                        goal,
                        error=summary,
                    )
                    if outcome.action in (None, OperatorAction.ABORT):
                        stopped = _stop_reason(outcome, escalation.timeout_seconds)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"status": "stopped"}),
                        })
                        break
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps({
                            "status": "continue",
                            "message": (
                                report_takeover(outcome) if outcome.took_over
                                else "An operator reviewed this and asked you to keep going."
                            ),
                        }),
                    })
                    continue

                # 3. Act (execute the tool against the browser)
                # Safety check for navigation
                if tool_name == "navigate":
                    try:
                        guard.check_url_for_navigation(tool_args["url"])
                    except SafetyViolation as e:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"status": "error", "message": str(e)}),
                        })
                        continue

                # Describe the target element before acting on it: a click
                # can navigate away, and the element is gone afterwards.
                target = None
                result = None
                if tool_args.get("ref"):
                    try:
                        target = await recorder.describe_element(
                            page, tool_args["ref"], observation["snapshot"], reading=tool_name == "extract",
                        )
                    except Exception as e:
                        result = {
                            "status": "error",
                            "message": f"No element with ref '{tool_args['ref']}' in the latest snapshot: {e}",
                        }

                page_target = None
                if result is None and tool_args.get("if_missing_outcome") and tool_args.get("page_ref"):
                    try:
                        page_target = await recorder.describe_element(
                            page, tool_args["page_ref"], observation["snapshot"],
                        )
                    except Exception as e:
                        result = {
                            "status": "error",
                            "message": f"No element with page_ref '{tool_args['page_ref']}' in the latest snapshot: {e}",
                        }

                options = None
                if result is None and tool_name == "select_option":
                    try:
                        options = await element_by_ref(page, tool_args["ref"]).evaluate(
                            "el => Array.from(el.options || []).map(o => (o.label || o.text).trim())"
                        )
                    except Exception as e:
                        logger.debug("Could not read dropdown options: %s", e)

                # An action that commits something irreversible is never taken
                # on the model's say-so, not even while recording: the run is
                # against a live application, and discovery only gets one go.
                if result is None and tool_name == "click" and tool_args.get("changes_data"):
                    what = tool_args.get("description") or "this action"
                    if escalation is None:
                        result = {
                            "status": "error",
                            "message": (
                                f"Blocked: '{what}' changes data and needs a human's approval, and "
                                "this run has no way to ask for one. Do not retry; call done with "
                                "success=false."
                            ),
                        }
                    else:
                        outcome = await escalate(
                            InterventionType.CONFIRMATION_REQUIRED,
                            f"Approval needed before: {what}",
                            what,
                        )
                        if outcome.action in (None, OperatorAction.ABORT):
                            stopped = _stop_reason(outcome, escalation.timeout_seconds)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({"status": "not_approved"}),
                            })
                            break
                        if outcome.took_over:
                            # The operator did it themselves; nothing for us to
                            # execute, and nothing we can honestly record.
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({
                                    "status": "done_by_operator",
                                    "message": report_takeover(outcome),
                                }),
                            })
                            continue
                        logger.info("Operator approved: %s", what)

                started = time.monotonic()
                steps_before = recorder.last_step_id
                if result is None:
                    result = await execute_tool(tool_name, with_secrets(tool_args, secrets), page)

                # A list read from one item card: record the list around it.
                container_target = None
                if result.get("pointed_at_card"):
                    container_target = await recorder.describe_element(
                        page, tool_args["ref"], observation["snapshot"], reading=True, parent=True,
                    )

                # 4. Record the action
                self._record_action(recorder, tool_name, tool_args, result, target, page_target, options, container_target)
                if recorder.last_step_id != steps_before:
                    pending = (recorder.last_step_id, observation["snapshot"], observation["url"], started)

                # Feed result back to LLM
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result),
                })

                # Repeated failures with no progress are the other kind of
                # stuck: the model keeps trying and the page keeps refusing.
                if result.get("status") == "error":
                    consecutive_errors += 1
                    if consecutive_errors >= STUCK_AFTER_ERRORS and escalation is not None:
                        outcome = await escalate(
                            InterventionType.HARD_FAILURE,
                            f"{consecutive_errors} actions in a row failed",
                            tool_args.get("description") or tool_name,
                            error=str(result.get("message", "")),
                        )
                        consecutive_errors = 0
                        if outcome.action in (None, OperatorAction.ABORT):
                            stopped = _stop_reason(outcome, escalation.timeout_seconds)
                            break
                        if outcome.took_over:
                            messages.append({"role": "user", "content": report_takeover(outcome)})
                else:
                    consecutive_errors = 0

        # Build the artifact. Whether it is worth keeping is the caller's
        # call: a run that never reached the goal has recorded a flow that
        # stops in the middle of one.
        capability = recorder.build(final_url=page.url)
        logger.info(
            "Discovery produced capability '%s' with %d steps, %d inputs, %d outputs",
            capability.meta.name,
            len(capability.steps),
            len(capability.contract.inputs),
            len(capability.contract.outputs),
        )
        return DiscoveryResult(
            capability=capability,
            success=success,
            summary=summary if success else (stopped or summary or "discovery did not finish"),
            interventions=interventions,
        )

    async def _escalation_screenshot(
        self, session: BrowserSession, capability_name: str, secrets: dict[str, str],
    ) -> str | None:
        """A masked screenshot of the live page, so the operator sees where it stopped."""
        try:
            directory = Path(self.config.browser.screenshot_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = str(directory / f"{capability_name}_discovery_{time.strftime('%Y%m%d-%H%M%S')}.png")
            await session.screenshot(path, hide_values=list(secrets.values()))
            return path
        except Exception as e:
            logger.warning("Could not capture a screenshot for the operator: %s", e)
            return None

    def _record_action(
        self,
        recorder: ActionRecorder,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        target: ElementTarget | None,
        page_target: ElementTarget | None = None,
        options: list[str] | None = None,
        container_target: ElementTarget | None = None,
    ) -> None:
        """Record a successful tool execution into the artifact."""
        if result.get("status") == "error":
            return  # Don't record failed actions

        match tool_name:
            case "navigate":
                recorder.record_navigate(args["url"])

            case "click":
                if args.get("clears_a_blocker") and target is not None:
                    # Not a step: it belongs to whatever it was blocking.
                    recorder.record_blocker(target)
                    return
                desc = args.get("description", "")
                risk = RiskLevel.NEEDS_CONFIRMATION if args.get("changes_data") else RiskLevel.SAFE
                if_missing = None
                if args.get("if_missing_outcome") and page_target:
                    if_missing = (args["if_missing_outcome"], args.get("if_missing_message", ""), page_target)
                if_rejected = None
                if args.get("if_rejected_outcome"):
                    if_rejected = (args["if_rejected_outcome"], args.get("if_rejected_message", ""))
                recorder.record_click(
                    target, desc, risk, if_missing,
                    waits_for_network=bool(result.get("triggered_request")),
                    if_rejected=if_rejected,
                )

            case "type_text":
                recorder.record_type(
                    target,
                    args["text"],
                    description=args.get("description", ""),
                    is_input_param=args.get("is_input_param", False),
                    param_name=args.get("param_name"),
                    is_sensitive=args.get("is_sensitive", False),
                )

            case "select_option":
                recorder.record_select_option(
                    target,
                    args["value"],
                    description=args.get("description", ""),
                    is_input_param=args.get("is_input_param", False),
                    param_name=args.get("param_name"),
                    options=options,
                )

            case "extract":
                if result.get("pointed_at_card") and container_target is not None:
                    target = container_target
                recorder.record_extract(
                    target,
                    output_name=args["output_name"],
                    description=args.get("description", ""),
                    table_columns=result.get("columns") if (args.get("as_table") or args.get("as_list")) else None,
                    card_selector=result.get("card_selector"),
                )

            case "key_press":
                recorder.record_key_press(args["key"])

            case "set_input_rule":
                recorder.set_input_format(args["param_name"], args["rule"])

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract the domain from a URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.hostname or ""
