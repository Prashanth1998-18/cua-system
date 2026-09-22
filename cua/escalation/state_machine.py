"""
Escalation state machine (ADR-006).

Three states:
  AUTOMATION_ACTIVE  ←→  PAUSED  ←→  HUMAN_ACTIVE

Transitions:
  - AUTOMATION_ACTIVE → PAUSED: automation hits a failure it can't handle
  - PAUSED → HUMAN_ACTIVE: operator accepts the intervention
  - HUMAN_ACTIVE → AUTOMATION_ACTIVE: operator clicks Resume
  - PAUSED → AUTOMATION_ACTIVE: operator clicks Resume (skip human control)
  - Any → ABORTED: operator aborts the run

The state machine is synchronous and thread-safe via asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class EscalationState(str, Enum):
    AUTOMATION_ACTIVE = "automation_active"
    PAUSED = "paused"
    HUMAN_ACTIVE = "human_active"
    ABORTED = "aborted"


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""

    def __init__(self, from_state: EscalationState, to_state: EscalationState):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition: {from_state.value} → {to_state.value}"
        )


# Valid transitions: from_state → set of allowed to_states
_VALID_TRANSITIONS: dict[EscalationState, set[EscalationState]] = {
    EscalationState.AUTOMATION_ACTIVE: {
        EscalationState.PAUSED,
        EscalationState.ABORTED,
    },
    EscalationState.PAUSED: {
        EscalationState.HUMAN_ACTIVE,
        EscalationState.AUTOMATION_ACTIVE,
        EscalationState.ABORTED,
    },
    EscalationState.HUMAN_ACTIVE: {
        EscalationState.AUTOMATION_ACTIVE,
        EscalationState.ABORTED,
    },
    EscalationState.ABORTED: set(),  # terminal state
}


class EscalationStateMachine:
    """
    Manages the escalation lifecycle for a single replay run.

    Usage:
        sm = EscalationStateMachine()
        assert sm.state == EscalationState.AUTOMATION_ACTIVE

        # Automation hits a problem → pause
        await sm.pause(reason="Element not found after retries")

        # Operator takes over
        await sm.activate_human()

        # Operator fixes the issue and resumes
        await sm.resume()
    """

    def __init__(
        self,
        on_transition: Callable[[EscalationState, EscalationState, dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self._state = EscalationState.AUTOMATION_ACTIVE
        self._lock = asyncio.Lock()
        self._on_transition = on_transition
        self._pause_event = asyncio.Event()
        self._history: list[dict[str, Any]] = []

    @property
    def state(self) -> EscalationState:
        return self._state

    @property
    def is_paused(self) -> bool:
        return self._state in (EscalationState.PAUSED, EscalationState.HUMAN_ACTIVE)

    @property
    def is_aborted(self) -> bool:
        return self._state == EscalationState.ABORTED

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    async def _transition(
        self,
        to_state: EscalationState,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Execute a state transition with validation."""
        async with self._lock:
            from_state = self._state
            allowed = _VALID_TRANSITIONS.get(from_state, set())
            if to_state not in allowed:
                raise InvalidTransition(from_state, to_state)

            self._state = to_state
            record = {
                "from": from_state.value,
                "to": to_state.value,
                **(metadata or {}),
            }
            self._history.append(record)

            logger.info(
                "Escalation: %s → %s %s",
                from_state.value,
                to_state.value,
                metadata or "",
            )

        # Notify listener outside the lock
        if self._on_transition:
            await self._on_transition(from_state, to_state, metadata or {})

        # Signal waiters when transitioning out of paused states
        if to_state in (EscalationState.AUTOMATION_ACTIVE, EscalationState.ABORTED):
            self._pause_event.set()

    async def pause(self, reason: str = "") -> None:
        """Pause automation and wait for operator intervention."""
        self._pause_event.clear()
        await self._transition(
            EscalationState.PAUSED,
            {"reason": reason},
        )

    async def activate_human(self) -> None:
        """Operator accepts and takes active control."""
        await self._transition(EscalationState.HUMAN_ACTIVE)

    async def resume(self) -> None:
        """Operator releases control back to automation."""
        await self._transition(EscalationState.AUTOMATION_ACTIVE)

    async def abort(self, reason: str = "") -> None:
        """Abort the entire run."""
        await self._transition(
            EscalationState.ABORTED,
            {"reason": reason},
        )

    async def wait_for_resolution(self, timeout: float | None = None) -> EscalationState:
        """
        Block until the operator resumes or aborts.

        Called by the replay engine when escalation is triggered.
        Returns the new state (AUTOMATION_ACTIVE or ABORTED).
        """
        try:
            await asyncio.wait_for(self._pause_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Escalation timed out after %.1fs, aborting", timeout)
            await self.abort(reason="Escalation timeout")

        return self._state
