"""
The replay engine's side of human escalation.

`EscalationChannel.request()` pauses the run, publishes an intervention
request to the operator (via EscalationManager, which the operator console
reads), and blocks until the operator resumes, aborts, or the timeout
passes. If the operator took over the live browser in between, it records
what changed while they were in control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from playwright.async_api import Page

from .actions import IN_CONTROL_HUMAN, IN_CONTROL_PAUSED, HumanActionRecorder, summarize
from .intervention import EscalationManager, HandBack, InterventionType, OperatorAction
from .state_machine import EscalationState, EscalationStateMachine

logger = logging.getLogger(__name__)


@dataclass
class EscalationOutcome:
    action: OperatorAction | None  # None = nobody answered before the timeout
    took_over: bool
    hand_back: HandBack | None = None  # what the operator says they did, if they took over
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)


class EscalationChannel:
    def __init__(
        self,
        state_machine: EscalationStateMachine | None = None,
        manager: EscalationManager | None = None,
        cdp_url: str | None = None,
        timeout_seconds: float = 300,
        hide_values: list[str] | None = None,
    ):
        self.state_machine = state_machine or EscalationStateMachine()
        self.manager = manager or EscalationManager()
        self.cdp_url = cdp_url
        self.timeout_seconds = timeout_seconds
        # Sensitive values to mask out of anything we record about the handoff.
        self.hide_values = hide_values or []
        # The live session an operator would be taking control of, while an
        # intervention is open. This is the handle a richer operator console
        # would drive; here it is what makes "the same session" checkable.
        self.current_page: Page | None = None
        # Returns a masked screenshot of that session, taken now. The console
        # polls it so the operator can see what the automation is looking at
        # without attaching anything to the browser.
        self.live_view: Callable[[], Awaitable[bytes]] | None = None
        # Called just before the first intervention is published, so a run
        # that never needs anyone never stands anything up.
        self.on_first_request: Callable[[], Awaitable[None]] | None = None
        self._announced = False

    async def request(
        self,
        intervention_type: InterventionType,
        reason: str,
        capability_name: str,
        capability_display_name: str,
        step_id: str,
        step_description: str,
        describe_page: Callable[[], Awaitable[dict[str, str]]],
        error_message: str | None = None,
        screenshot_path: str | None = None,
        redacted_inputs: dict[str, Any] | None = None,
        page: Page | None = None,
    ) -> EscalationOutcome:
        before = await describe_page()

        # Pages the human visits while in control, for the record.
        visited: list[str] = []

        def who_is_in_control() -> str | None:
            """None while the automation is driving: then an action isn't a person's."""
            match self.state_machine.state:
                case EscalationState.HUMAN_ACTIVE:
                    return IN_CONTROL_HUMAN
                case EscalationState.PAUSED:
                    return IN_CONTROL_PAUSED
                case _:
                    return None

        def on_navigate(frame):
            if page is not None and frame == page.main_frame and who_is_in_control():
                visited.append(frame.url)

        # And what they actually did on those pages.
        actions: HumanActionRecorder | None = None
        self.current_page = page
        if page is not None:
            page.on("framenavigated", on_navigate)
            actions = HumanActionRecorder(
                page, control=who_is_in_control, hide_values=self.hide_values,
            )
            await actions.attach()

        if self.on_first_request is not None and not self._announced:
            self._announced = True
            await self.on_first_request()

        history_start = len(self.state_machine.history)
        await self.state_machine.pause(reason=reason)

        # Published only once the run is actually paused. The other order
        # lets an operator answer a request while automation is still
        # running, which the state machine then refuses -- confusing for
        # them, and a race for anything driving the console.
        request = self.manager.create_request(
            intervention_type=intervention_type,
            reason=reason,
            capability_name=capability_name,
            capability_display_name=capability_display_name,
            failed_step_id=step_id,
            failed_step_description=step_description,
            error_message=error_message,
            current_url=before.get("url", ""),
            page_title=before.get("title", ""),
            cdp_url=self.cdp_url,
            screenshot_path=screenshot_path,
            redacted_inputs=redacted_inputs,
        )
        logger.warning(
            "Escalated to operator (%s) at step '%s': %s. Waiting up to %ds.",
            intervention_type.value, step_id, reason, self.timeout_seconds,
        )
        final_state = await self.state_machine.wait_for_resolution(timeout=self.timeout_seconds)

        if page is not None:
            page.remove_listener("framenavigated", on_navigate)

        took_over = any(h["to"] == EscalationState.HUMAN_ACTIVE.value for h in self.state_machine.history[history_start:])
        acted_while_paused = any(a.get("while") == IN_CONTROL_PAUSED for a in (actions.actions if actions else []))
        response = self.manager.last_response
        answered = response is not None and response.request_id == request.id
        timed_out = final_state == EscalationState.ABORTED and not answered

        if final_state == EscalationState.ABORTED:
            action = None if timed_out else OperatorAction.ABORT
        else:
            # Working in the window and then pressing Approve is a takeover in
            # everything but name, and the run has to treat it as one or it
            # will redo what they already did.
            action = OperatorAction.TAKE_OVER if (took_over or acted_while_paused) else OperatorAction.RESUME

        if actions is not None:
            await actions.flush()
        human_actions = actions.actions if actions else []
        after = await describe_page()
        self.manager.clear()
        # Abort is terminal for the state machine; a timed-out or aborted run ends here.

        record = {
            "request_id": request.id,
            "type": intervention_type.value,
            "step_id": step_id,
            "reason": reason,
            "error_message": error_message or "",
            "operator_action": action.value if action else "no_response",
            "operator_took_over": took_over or acted_while_paused,
            "operator_formally_took_control": took_over,
            "operator_notes": response.operator_notes if answered else "",
            "page_before": before,
            "page_after": after,
            "pages_visited_by_operator": visited,
            "human_actions": human_actions,
            "what_the_human_did": summarize(human_actions),
        }
        hand_back = response.hand_back if (answered and took_over) else None
        took_over = took_over or acted_while_paused
        if hand_back:
            record["operator_says"] = hand_back.value
        logger.info("Intervention finished: %s", {k: record[k] for k in ("step_id", "operator_action", "operator_took_over")})
        return EscalationOutcome(
            action=action,
            took_over=took_over,
            hand_back=hand_back,
            human_actions=human_actions,
            record=record,
        )
