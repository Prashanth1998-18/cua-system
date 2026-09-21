"""
Safety policy enforcement.

Checked on every action in both discovery and replay.
Three layers: domain lockdown, action risk classification, PII redaction.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from urllib.parse import urlparse

from ..schema.artifact import ActionType, RiskLevel, SafetyPolicy, Step

logger = logging.getLogger(__name__)


class SafetyViolation(Exception):
    """Raised when an action violates the safety policy."""

    def __init__(self, message: str, violation_type: str):
        super().__init__(message)
        self.violation_type = violation_type


class SafetyGuard:
    """
    Enforces the SafetyPolicy on every action.

    Usage:
        guard = SafetyGuard(policy)
        guard.check_navigation(url)      # raises SafetyViolation if blocked
        guard.check_action(step)         # raises SafetyViolation if blocked
        guard.needs_confirmation(step)   # returns True if step needs human OK
        safe_value = guard.redact(value, field_name)  # masks PII fields
    """

    def __init__(self, policy: SafetyPolicy):
        self.policy = policy
        self._step_count = 0
        self._started: float | None = None
        self._budget_seconds: float = float(policy.timeout_seconds)
        # Time the run spent waiting for a person. A human thinking for four
        # minutes is not the automation running away with itself, so it is
        # taken off the clock rather than counted against it.
        self._waiting_on_human = 0.0
        self._wait_started: float | None = None

    def check_navigation(self, url: str) -> None:
        """Verify a URL is allowed before navigating."""
        parsed = urlparse(url)
        domain = parsed.hostname or ""

        # Check allowed domains
        if not any(
            domain == d or domain.endswith(f".{d}")
            for d in self.policy.allowed_domains
        ):
            raise SafetyViolation(
                f"Navigation to {domain} blocked: not in allowed domains {self.policy.allowed_domains}",
                violation_type="domain_blocked",
            )

        # Check blocked URL patterns
        full_path = parsed.path or "/"
        for pattern in self.policy.blocked_url_patterns:
            if fnmatch.fnmatch(full_path, pattern):
                raise SafetyViolation(
                    f"Navigation to {full_path} blocked: matches blocked pattern {pattern}",
                    violation_type="url_pattern_blocked",
                )

    def check_action(self, step: Step) -> None:
        """Verify an action is allowed before executing."""
        # Check action type
        if step.action not in self.policy.allowed_actions:
            raise SafetyViolation(
                f"Action {step.action.value} not in allowed actions",
                violation_type="action_type_blocked",
            )

        # Check risk level
        if step.risk_level == RiskLevel.BLOCKED:
            raise SafetyViolation(
                f"Step '{step.id}' is blocked (risk_level=blocked). Requires human escalation.",
                violation_type="risk_blocked",
            )

        # Check step count
        self._step_count += 1
        if self._step_count > self.policy.max_steps:
            raise SafetyViolation(
                f"Maximum step count ({self.policy.max_steps}) exceeded",
                violation_type="max_steps_exceeded",
            )

        # Check the clock
        spent = self.elapsed_seconds()
        if spent > self._budget_seconds:
            raise SafetyViolation(
                f"Run exceeded its time budget of {self._budget_seconds:.0f}s "
                f"(spent {spent:.0f}s on automation, excluding any wait for a person)",
                violation_type="timeout_exceeded",
            )

    def needs_confirmation(self, step: Step) -> bool:
        """Check if a step requires human confirmation before executing."""
        return step.risk_level == RiskLevel.NEEDS_CONFIRMATION

    def redact(self, value: str, field_name: str) -> str:
        """Redact a value if it belongs to a PII field."""
        if field_name in self.policy.pii_fields:
            return "***REDACTED***"
        return value

    def redact_for_log(self, inputs: dict[str, str]) -> dict[str, str]:
        """Return a copy of inputs with PII fields redacted for logging."""
        return {
            k: self.redact(v, k)
            for k, v in inputs.items()
        }

    def check_url_for_navigation(self, url: str) -> None:
        """Check a URL just before Playwright navigates to it."""
        self.check_navigation(url)

    def reset_step_count(self) -> None:
        """Reset the step counter and start the clock (once per run)."""
        self._step_count = 0
        self._started = time.monotonic()
        self._waiting_on_human = 0.0
        self._wait_started = None

    def allow_seconds(self, seconds: float) -> None:
        """
        Set the run's time budget.

        The policy's value is the floor; the caller can raise it for work the
        recording didn't cover, such as a list input that expands one recorded
        step into ten.
        """
        self._budget_seconds = max(float(self.policy.timeout_seconds), seconds)

    def elapsed_seconds(self) -> float:
        """Time spent on automation, with any wait for a person taken out."""
        if self._started is None:
            return 0.0
        waiting = self._waiting_on_human
        if self._wait_started is not None:
            waiting += time.monotonic() - self._wait_started
        return (time.monotonic() - self._started) - waiting

    def start_waiting_for_human(self) -> None:
        """Stop the clock: the run is paused for an operator."""
        if self._wait_started is None:
            self._wait_started = time.monotonic()

    def stop_waiting_for_human(self) -> None:
        """Restart the clock once control is back with the automation."""
        if self._wait_started is not None:
            self._waiting_on_human += time.monotonic() - self._wait_started
            self._wait_started = None
