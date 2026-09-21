"""
Replay engine: deterministic artifact execution without LLM.

Reads a Capability artifact and executes its steps mechanically.
Zero LLM calls. Same inputs produce same behavior.

The engine:
1. Validates inputs against the contract
2. Executes each step in order
3. Uses the locator cascade to find elements
4. Checks checkpoints after each step
5. Matches error handlers against page state on failure
6. Returns a structured ReplayResult
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..browser.adapter import PlaywrightAdapter, SurfaceAdapter, snapshot_headings
from ..browser.safety import SafetyGuard, SafetyViolation
from ..browser.session import BrowserSession
from ..config import Config
from ..schema.artifact import (
    ActionType,
    Capability,
    ErrorClass,
    OutputParam,
    ParamType,
    RecoveryAction,
    RiskLevel,
    Step,
)
from ..schema.results import ReplayResult, ReplayStatus, StepResult
from .handlers import execute_recovery, match_error_handlers
from ..escalation.channel import EscalationChannel
from ..schema.formats import FORMATS
from ..escalation.intervention import HandBack, InterventionType, OperatorAction
from .locator import fill_templates, resolve_condition, resolve_target_templates

logger = logging.getLogger(__name__)

# Slack on top of the recorded step timeouts, for page loads and the gaps
# between steps that no single step is charged for.
_CLOCK_HEADROOM_S = 30


class ReplayEngine:
    """
    Executes a Capability artifact deterministically.

    Usage:
        engine = ReplayEngine(config)
        result = await engine.run(capability, inputs)
    """

    def __init__(self, config: Config | None = None, escalation: EscalationChannel | None = None):
        self.config = config or Config()
        # None = no human available: failures stop the run, and steps that
        # change data are blocked rather than run unattended.
        self.escalation = escalation
        self._interventions: list[dict[str, Any]] = []
        self._hide_values: list[str] = []  # sensitive input values, blanked in screenshots

    async def run(
        self,
        capability: Capability,
        inputs: dict[str, str],
    ) -> ReplayResult:
        """
        Execute a capability with the given inputs.

        Returns a ReplayResult with status, outputs, and step trace.
        """
        start_time = time.monotonic()
        self._interventions = []
        for assist in capability.meta.human_assisted:
            # Recorded with a person's help: the flow has a gap where their
            # actions were, and this is the last chance to say so out loud.
            logger.warning(
                "This capability was recorded with human help after %s (%s). "
                "What the human did was not recorded, so replay may fail there.",
                assist.after_step_id, assist.reason,
            )
        self._hide_values = [
            v
            for name, param in capability.contract.inputs.items()
            if param.sensitive and name in inputs
            for v in (inputs[name] if isinstance(inputs[name], list) else [inputs[name]])
        ]
        if self.escalation is not None:
            self.escalation.hide_values = self._hide_values

        # Validate required inputs
        missing = [
            name
            for name, param in capability.contract.inputs.items()
            if param.required and name not in inputs
        ]
        if missing:
            return ReplayResult(
                capability_id=capability.meta.id,
                capability_name=capability.meta.name,
                status=ReplayStatus.FAILURE,
                failed_at_step="input_validation",
                failure_reason=f"Missing required inputs: {', '.join(missing)}",
                duration_ms=0,
            )

        invalid = self._invalid_inputs(capability, inputs)
        if invalid:
            return ReplayResult(
                capability_id=capability.meta.id,
                capability_name=capability.meta.name,
                status=ReplayStatus.FAILURE,
                failed_at_step="input_validation",
                failure_reason="Invalid input: " + "; ".join(invalid),
                duration_ms=0,
            )

        guard = SafetyGuard(capability.policy)
        guard.reset_step_count()

        safe_inputs = guard.redact_for_log(inputs)
        logger.info(
            "Starting replay: %s with inputs %s",
            capability.meta.display_name,
            safe_inputs,
        )

        async with BrowserSession(self.config.browser) as session:
            page = await session.new_page()
            adapter = PlaywrightAdapter(page)
            if self.escalation is not None:
                self.escalation.live_view = lambda: session.screenshot(hide_values=self._hide_values)

            result = await self._execute_steps(
                capability=capability,
                inputs=inputs,
                adapter=adapter,
                guard=guard,
                session=session,
            )

        result.duration_ms = int((time.monotonic() - start_time) * 1000)
        result.interventions = self._interventions
        return result

    async def _execute_steps(
        self,
        capability: Capability,
        inputs: dict[str, str],
        adapter: SurfaceAdapter,
        guard: SafetyGuard,
        session: BrowserSession,
    ) -> ReplayResult:
        """Execute all steps and collect results."""
        outputs: dict[str, Any] = {}
        step_results: list[StepResult] = []

        # The run's time budget. The policy's value is the floor; a list input
        # that turns one recorded step into ten is extra work the recording
        # never timed, so each repeat is allowed the time that step was given.
        plan = list(self._expand(capability.steps, inputs))
        guard.allow_seconds(sum(step.timeout_ms for step, _, _ in plan) / 1000 + _CLOCK_HEADROOM_S)

        for step, step_inputs, label in plan:
            step_start = time.monotonic()

            # Wait before action if specified
            if step.wait_before_ms > 0:
                await asyncio.sleep(step.wait_before_ms / 1000)

            execute = lambda: self._execute_step(
                step=step,
                inputs=step_inputs,
                adapter=adapter,
                guard=guard,
                session=session,
                global_handlers=capability.global_error_handlers,
                output_param=capability.contract.outputs.get(step.extract_output_key or ""),
            )

            step_result: StepResult | None = None
            if guard.needs_confirmation(step):
                # A step that changes data (opens an account, sends a payment)
                # never runs unattended: it needs an operator's approval.
                if self.escalation is None:
                    return ReplayResult(
                        capability_id=capability.meta.id,
                        capability_name=capability.meta.name,
                        status=ReplayStatus.FAILURE,
                        failed_at_step=label,
                        failure_reason=(
                            f"Blocked: '{step.description}' changes data and needs a human's "
                            f"approval, and this run has no way to ask for one."
                        ),
                        step_results=step_results,
                    )
                outcome = await self._escalate(
                    InterventionType.CONFIRMATION_REQUIRED,
                    f"Approval needed before: {step.description}",
                    step, capability, step_inputs, adapter, session, guard,
                )
                if outcome.action in (None, OperatorAction.ABORT):
                    return self._stopped_by_escalation(outcome, step, capability, step_results)
                if outcome.took_over:
                    step_result = await self._after_takeover(step, step_inputs, adapter, outcome.hand_back)

            if step_result is None:
                step_result = await execute()

            # Something went wrong that nothing explains, even after a retry:
            # hand it to a human (at most twice per step).
            escalations = 0
            while (
                step_result.status == ReplayStatus.FAILURE
                and self.escalation is not None
                and escalations < 2
            ):
                escalations += 1
                screenshot_path = await self._capture_failure_screenshot(session, capability.meta.name, step.id)
                outcome = await self._escalate(
                    InterventionType.HARD_FAILURE,
                    f"Step '{step.description}' failed",
                    step, capability, step_inputs, adapter, session, guard,
                    error_message=step_result.error_message,
                    screenshot_path=screenshot_path,
                )
                if outcome.action in (None, OperatorAction.ABORT):
                    return self._stopped_by_escalation(outcome, step, capability, step_results, screenshot_path)
                if outcome.took_over:
                    step_result = (
                        await self._after_takeover(step, step_inputs, adapter, outcome.hand_back)
                        or await execute()
                    )
                else:
                    # Resuming means "try again", but a committing step is
                    # checked first: an operator can have done it in the
                    # window without formally taking control.
                    step_result = await self._already_done(step, step_inputs, adapter) or await execute()

            step_result.step_id = label
            step_result.duration_ms = int((time.monotonic() - step_start) * 1000)
            step_results.append(step_result)

            # Collect extracted values
            if (
                step_result.status == ReplayStatus.SUCCESS
                and step_result.extracted_value is not None
                and step.extract_output_key
            ):
                outputs[step.extract_output_key] = step_result.extracted_value

            # Stop on non-success
            if step_result.status == ReplayStatus.BUSINESS_OUTCOME:
                screenshot_path = await self._capture_failure_screenshot(
                    session, capability.meta.name, step.id
                )
                return ReplayResult(
                    capability_id=capability.meta.id,
                    capability_name=capability.meta.name,
                    status=ReplayStatus.BUSINESS_OUTCOME,
                    outcome_key=step_result.outcome_key,
                    outcome_message=step_result.error_message,
                    step_results=step_results,
                    screenshot_path=screenshot_path,
                )

            if step_result.status == ReplayStatus.FAILURE:
                screenshot_path = await self._capture_failure_screenshot(
                    session, capability.meta.name, step.id
                )
                return ReplayResult(
                    capability_id=capability.meta.id,
                    capability_name=capability.meta.name,
                    status=ReplayStatus.FAILURE,
                    failed_at_step=label,
                    failure_reason=step_result.error_message,
                    step_results=step_results,
                    screenshot_path=screenshot_path,
                )

        # All steps succeeded. Check the overall success condition.
        success = resolve_condition(capability.contract.success_condition, inputs)
        success_met = await adapter.wait_for_condition(
            success.type, success.value, success.locator, timeout_ms=5000,
        )

        if not success_met:
            screenshot_path = await self._capture_failure_screenshot(
                session, capability.meta.name, "success_condition"
            )
            return ReplayResult(
                capability_id=capability.meta.id,
                capability_name=capability.meta.name,
                status=ReplayStatus.FAILURE,
                failed_at_step="success_condition",
                failure_reason=(
                    f"All steps completed, but expected {success.description or 'the final page'}; "
                    f"instead saw {await self._observed(adapter)}"
                ),
                step_results=step_results,
                screenshot_path=screenshot_path,
            )

        return ReplayResult(
            capability_id=capability.meta.id,
            capability_name=capability.meta.name,
            status=ReplayStatus.SUCCESS,
            outputs=outputs,
            step_results=step_results,
        )

    async def _execute_step(
        self,
        step: Step,
        inputs: dict[str, str],
        adapter: SurfaceAdapter,
        guard: SafetyGuard,
        session: BrowserSession,
        global_handlers: list,
        output_param: OutputParam | None = None,
    ) -> StepResult:
        """Execute a single step with error handling and retry logic."""

        # Safety checks
        try:
            guard.check_action(step)
        except SafetyViolation as e:
            return StepResult(
                step_id=step.id,
                status=ReplayStatus.FAILURE,
                error_class=ErrorClass.HARD_FAILURE,
                error_message=str(e),
            )

        # What the page was already announcing, so a handler can tell a new
        # refusal from a notice that was there all along.
        self._alert_before = await adapter.read_alert_text()

        try:
            return await self._do_action(step, inputs, adapter, guard, output_param)
        except TimeoutError as e:
            # All locators exhausted or element not found
            logger.warning("Step '%s' timed out: %s", step.id, e)
            return await self._handle_step_failure(
                step, str(e), inputs, adapter, guard, global_handlers, output_param
            )
        except SafetyViolation as e:
            return StepResult(
                step_id=step.id,
                status=ReplayStatus.FAILURE,
                error_class=ErrorClass.HARD_FAILURE,
                error_message=str(e),
            )
        except Exception as e:
            logger.error("Step '%s' raised unexpected error: %s", step.id, e)
            return await self._handle_step_failure(
                step, str(e), inputs, adapter, guard, global_handlers, output_param
            )

    async def _do_action(
        self,
        step: Step,
        inputs: dict[str, str],
        adapter: SurfaceAdapter,
        guard: SafetyGuard,
        output_param: OutputParam | None = None,
    ) -> StepResult:
        """Execute the actual action for a step."""
        locator_used = None
        extracted_value = None

        match step.action:
            case ActionType.NAVIGATE:
                url = step.resolve_url(inputs) or ""
                guard.check_url_for_navigation(url)
                await adapter.navigate(url)

            case ActionType.CLICK:
                if step.target is None:
                    raise ValueError(f"Step '{step.id}' has action=click but no target")
                resolved_target = resolve_target_templates(step.target, inputs)
                locator_used = await adapter.click(resolved_target, wait_for_network=step.waits_for_network)

            case ActionType.TYPE:
                if step.target is None:
                    raise ValueError(f"Step '{step.id}' has action=type but no target")
                resolved_target = resolve_target_templates(step.target, inputs)
                value = step.resolve_value(inputs) or ""
                locator_used = await adapter.type_text(resolved_target, value)

            case ActionType.SELECT_OPTION:
                if step.target is None:
                    raise ValueError(f"Step '{step.id}' has action=select_option but no target")
                resolved_target = resolve_target_templates(step.target, inputs)
                value = step.resolve_value(inputs) or ""
                locator_used = await adapter.select_option(resolved_target, value)

            case ActionType.EXTRACT:
                if step.target is None:
                    raise ValueError(f"Step '{step.id}' has action=extract but no target")
                resolved_target = resolve_target_templates(step.target, inputs)
                if output_param and output_param.type == ParamType.TABLE:
                    if output_param.card_selector:
                        headers, extracted_value, locator_used = await adapter.extract_cards(
                            resolved_target, output_param.card_selector,
                        )
                    else:
                        headers, extracted_value, locator_used = await adapter.extract_table(resolved_target)
                    # An empty card list has no keys to compare (a table still has its
                    # header row), and it's a valid empty answer.
                    comparable = bool(extracted_value) or not output_param.card_selector
                    if output_param.columns and comparable and headers != output_param.columns:
                        raise ValueError(
                            f"Table columns changed for step '{step.id}': "
                            f"expected {output_param.columns}, found {headers}"
                        )
                else:
                    extracted_value, locator_used = await adapter.extract_text(resolved_target)

            case ActionType.WAIT:
                if step.checkpoint:
                    await adapter.wait_for_condition(
                        step.checkpoint.type,
                        step.checkpoint.value,
                        step.checkpoint.locator,
                        timeout_ms=step.timeout_ms,
                    )

            case ActionType.ASSERT:
                if step.checkpoint:
                    met = await adapter.check_condition(
                        step.checkpoint.type,
                        step.checkpoint.value,
                        step.checkpoint.locator,
                    )
                    if not met:
                        raise AssertionError(
                            f"Assertion failed: {step.checkpoint.description}"
                        )

            case ActionType.KEY_PRESS:
                key = step.resolve_value(inputs) or ""
                await adapter.key_press(key)

            case ActionType.SCROLL:
                direction = step.resolve_value(inputs) or "down"
                await adapter.scroll(direction)

            case _:
                raise ValueError(f"Unknown action type: {step.action}")

        # Check post-action checkpoint
        if step.checkpoint and step.action not in (ActionType.WAIT, ActionType.ASSERT):
            checkpoint = resolve_condition(step.checkpoint, inputs)
            checkpoint_met = await adapter.wait_for_condition(
                checkpoint.type, checkpoint.value, checkpoint.locator, timeout_ms=step.timeout_ms,
            )
            if not checkpoint_met:
                raise TimeoutError(
                    f"After '{step.description}', expected {fill_templates(step.checkpoint.description or 'the next page', inputs)} "
                    f"within {step.timeout_ms // 1000}s; instead saw {await self._observed(adapter)}"
                )

        return StepResult(
            step_id=step.id,
            status=ReplayStatus.SUCCESS,
            locator_used=locator_used,
            extracted_value=extracted_value,
        )

    async def _handle_step_failure(
        self,
        step: Step,
        error_message: str,
        inputs: dict[str, str],
        adapter: SurfaceAdapter,
        guard: SafetyGuard,
        global_handlers: list,
        output_param: OutputParam | None = None,
    ) -> StepResult:
        """
        Handle a step failure by checking error handlers.

        Checks step-level handlers first, then global handlers. A RECOVERABLE
        match actually retries the step's action after running the recovery
        (e.g. dismiss a dialog, refresh) — a recovery that never re-attempts
        the step is indistinguishable from a hard failure to the caller.
        """
        async def find_match():
            # Step-level handlers first, then global ones.
            before = getattr(self, "_alert_before", "")
            return (
                await match_error_handlers(step.error_handlers, adapter, inputs, before)
                or await match_error_handlers(global_handlers, adapter, inputs, before)
            )

        match = await find_match()

        if match is None:
            # Nothing explains the failure. Try once more before calling it an
            # error (a slow or flaky load), unless the step changes data: a
            # blind retry could open an account or send a payment twice.
            if step.risk_level != RiskLevel.SAFE:
                return StepResult(
                    step_id=step.id,
                    status=ReplayStatus.FAILURE,
                    error_class=ErrorClass.HARD_FAILURE,
                    error_message=f"{error_message} (not retried: this step changes data)",
                )
            logger.info("Step '%s' failed with no known cause; retrying once", step.id)
            try:
                result = await self._do_action(step, inputs, adapter, guard, output_param)
                logger.info("Step '%s' succeeded on retry", step.id)
                return result
            except Exception as e:
                error_message = f"{e} (failed again after one retry)"
            match = await find_match()
            if match is None:
                return StepResult(
                    step_id=step.id,
                    status=ReplayStatus.FAILURE,
                    error_class=ErrorClass.HARD_FAILURE,
                    error_message=error_message,
                )

        match match.classification:
            case ErrorClass.BUSINESS_OUTCOME:
                message = match.outcome_message or match.outcome_key
                if match.handler.report_page_message:
                    # The recorded message says which outcome this is; the
                    # application says why, in its own words, right now.
                    said = await adapter.read_alert_text()
                    if said:
                        message = f"{message}: {said}"
                return StepResult(
                    step_id=step.id,
                    status=ReplayStatus.BUSINESS_OUTCOME,
                    error_class=ErrorClass.BUSINESS_OUTCOME,
                    error_message=message,
                    outcome_key=match.outcome_key,
                )

            case ErrorClass.RECOVERABLE:
                last_error = error_message
                max_retries = match.handler.max_retries
                for attempt in range(max_retries):
                    logger.info(
                        "Recovery attempt %d/%d for step '%s': %s",
                        attempt + 1,
                        max_retries,
                        step.id,
                        match.recovery.value if match.recovery else "none",
                    )
                    if match.recovery:
                        await execute_recovery(
                            match.recovery, adapter, match.handler.recovery_target, inputs,
                        )
                    try:
                        result = await self._do_action(step, inputs, adapter, guard, output_param)
                        logger.info(
                            "Step '%s' succeeded after recovery (attempt %d/%d)",
                            step.id, attempt + 1, max_retries,
                        )
                        return result
                    except Exception as e:
                        last_error = str(e)
                        continue

                return StepResult(
                    step_id=step.id,
                    status=ReplayStatus.FAILURE,
                    error_class=ErrorClass.RECOVERABLE,
                    error_message=f"Recoverable error, exhausted {max_retries} retries: {last_error}",
                )

            case ErrorClass.HARD_FAILURE:
                return StepResult(
                    step_id=step.id,
                    status=ReplayStatus.FAILURE,
                    error_class=ErrorClass.HARD_FAILURE,
                    error_message=error_message,
                )

    @staticmethod
    def _expand(steps: list[Step], inputs: dict[str, Any]):
        """Yield (step, its inputs, label): a for_each step once per list element."""
        for step in steps:
            if step.for_each:
                for i, element in enumerate(inputs.get(step.for_each, []), 1):
                    yield step, {**inputs, step.for_each: element}, f"{step.id}[{i}]"
            else:
                yield step, inputs, step.id

    @staticmethod
    def _invalid_inputs(capability: Capability, inputs: dict[str, Any]) -> list[str]:
        """Problems with the caller's inputs, found before any browser work."""
        problems: list[str] = []
        for name, param in capability.contract.inputs.items():
            if name not in inputs:
                continue
            value = inputs[name]
            shown = "(hidden)" if param.sensitive else repr(value)
            allowed = param.allowed_values
            if param.type == ParamType.LIST:
                if not isinstance(value, list) or not value:
                    problems.append(f"'{name}' must be a non-empty list")
                    continue
                repeats = sorted({v for v in value if value.count(v) > 1})
                if repeats:
                    problems.append(f"'{name}' lists the same value more than once: {', '.join(repeats)}")
                unknown = [v for v in value if allowed and v not in allowed]
                if unknown:
                    problems.append(f"'{name}' has unknown values {unknown}; each must be one of: {', '.join(allowed)}")
            elif isinstance(value, list):
                problems.append(f"'{name}' must be a single value, not a list")
            elif allowed and value not in allowed:
                problems.append(f"'{name}' must be one of: {', '.join(allowed)} (got {shown})")
            elif param.format:
                check, expected = FORMATS[param.format]
                if not check(value):
                    problems.append(f"'{name}' must be {expected} (got {shown})")
        return problems

    @staticmethod
    async def _page_state(adapter: SurfaceAdapter) -> dict[str, Any]:
        try:
            return {
                "title": await adapter.get_page_title(),
                "url": await adapter.get_current_url(),
                "headings": snapshot_headings(await adapter.accessibility_snapshot()),
            }
        except Exception as e:
            return {"error": f"could not read the page: {e}"}

    @classmethod
    async def _observed(cls, adapter: SurfaceAdapter) -> str:
        """A short description of the page as it is right now, for failure messages."""
        state = await cls._page_state(adapter)
        if "error" in state:
            return f"({state['error']})"
        shown = ", ".join(f'"{h}"' for h in state["headings"][:4]) or "none"
        return f'page "{state["title"]}" ({state["url"]}) with headings {shown}'

    async def _escalate(
        self, intervention_type, reason, step, capability, inputs, adapter, session, guard,
        error_message=None, screenshot_path=None,
    ):
        guard.start_waiting_for_human()
        outcome = await self.escalation.request(
            intervention_type=intervention_type,
            reason=reason,
            capability_name=capability.meta.name,
            capability_display_name=capability.meta.display_name,
            step_id=step.id,
            step_description=step.description,
            describe_page=lambda: self._page_state(adapter),
            error_message=error_message,
            screenshot_path=screenshot_path,
            redacted_inputs=guard.redact_for_log(inputs),
            page=getattr(adapter, "page", None),
        )
        guard.stop_waiting_for_human()
        self._interventions.append(outcome.record)
        return outcome

    async def _already_done(
        self, step: Step, inputs: dict[str, str], adapter: SurfaceAdapter,
    ) -> StepResult | None:
        """A committing step whose checkpoint already holds must not be run again."""
        if step.risk_level == RiskLevel.SAFE or not step.checkpoint:
            return None
        checkpoint = resolve_condition(step.checkpoint, inputs)
        if await adapter.check_condition(checkpoint.type, checkpoint.value, checkpoint.locator):
            return StepResult(
                step_id=step.id,
                status=ReplayStatus.SUCCESS,
                error_message="already done when the run resumed (this step's checkpoint holds)",
            )
        return None

    async def _after_takeover(
        self,
        step: Step,
        inputs: dict[str, str],
        adapter: SurfaceAdapter,
        claim: HandBack | None = None,
    ) -> StepResult | None:
        """
        Decide what to do with the step an operator interrupted.

        Two independent signals: what the operator says they did while they
        had control, and whether the step's own recorded checkpoint now
        holds. The page is the one that can't be mistaken, so it wins; where
        the two disagree the run stops rather than guesses, because the steps
        that reach here are the ones that change data.

        Returns a StepResult to record for the step, or None to run it.
        """
        checkpoint_holds: bool | None = None
        if step.checkpoint:
            checkpoint = resolve_condition(step.checkpoint, inputs)
            checkpoint_holds = await adapter.check_condition(
                checkpoint.type, checkpoint.value, checkpoint.locator
            )

        def done(message: str) -> StepResult:
            return StepResult(step_id=step.id, status=ReplayStatus.SUCCESS, error_message=message)

        def stop(message: str) -> StepResult:
            return StepResult(
                step_id=step.id,
                status=ReplayStatus.FAILURE,
                error_class=ErrorClass.HARD_FAILURE,
                error_message=message,
            )

        if checkpoint_holds:
            # The step's own success condition is met, whatever was claimed.
            # Re-running now would repeat whatever the operator already did.
            return done("completed by the operator (verified: this step's checkpoint holds)")

        if claim == HandBack.COMPLETED:
            if checkpoint_holds is False:
                return stop(
                    "The operator reported completing this step, but its checkpoint "
                    f"({step.checkpoint.type.value}: {step.checkpoint.value!r}) does not hold. "
                    "Stopping instead of guessing which is right."
                )
            return done("completed by the operator (this step has no checkpoint to verify it against)")

        if claim == HandBack.RETRY:
            if step.risk_level != RiskLevel.SAFE and checkpoint_holds is None:
                return stop(
                    "The operator asked to re-run a step that changes data, but it has no "
                    "checkpoint to confirm it hasn't already happened. Not re-running it."
                )
            return None  # checkpoint says it didn't happen, or the step is harmless

        # No claim: the operator took control and the run resumed without one.
        if step.risk_level != RiskLevel.SAFE:
            return stop(
                "The operator took over, but this step's result isn't visible; not re-running it "
                "automatically because it changes data"
            )
        return None

    def _stopped_by_escalation(self, outcome, step, capability, step_results, screenshot_path=None) -> ReplayResult:
        if outcome.action is None:
            reason = f"Stopped: no operator responded within {int(self.escalation.timeout_seconds)}s"
        else:
            notes = outcome.record.get("operator_notes")
            reason = "Stopped: aborted by the operator" + (f" ({notes})" if notes else "")
        return ReplayResult(
            capability_id=capability.meta.id,
            capability_name=capability.meta.name,
            status=ReplayStatus.ESCALATED,
            failed_at_step=step.id,
            failure_reason=reason,
            step_results=step_results,
            screenshot_path=screenshot_path,
        )

    async def _capture_failure_screenshot(
        self,
        session: BrowserSession,
        capability_name: str,
        step_id: str,
    ) -> str | None:
        """Capture a screenshot on failure for debugging."""
        try:
            screenshot_dir = Path(self.config.browser.screenshot_dir)
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            # Timestamped, so one run's evidence doesn't overwrite another's.
            safe_step = step_id.replace("[", "_").replace("]", "")
            path = str(screenshot_dir / f"{capability_name}_{safe_step}_{time.strftime('%Y%m%d-%H%M%S')}_failure.png")
            await session.screenshot(path, hide_values=self._hide_values)
            return path
        except Exception as e:
            logger.warning("Failed to capture screenshot: %s", e)
            return None
