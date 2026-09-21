"""
Replay result models.

Returned by the replay engine to the caller after execution.
This is the response contract: the calling AI agent reads this
to know what happened and what data came back.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel

from .artifact import ErrorClass, Locator


class ReplayStatus(str, enum.Enum):
    """Overall outcome of a replay run."""
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"


class StepResult(BaseModel):
    """Result of executing one step during replay."""
    step_id: str
    status: ReplayStatus
    locator_used: Locator | None = None
    extracted_value: str | list[dict[str, str]] | None = None
    error_class: ErrorClass | None = None
    error_message: str | None = None
    outcome_key: str | None = None
    screenshot_path: str | None = None
    duration_ms: int = 0


class ReplayResult(BaseModel):
    """
    The structured result returned to the caller after replay.

    On SUCCESS: outputs contains the extracted data.
    On BUSINESS_OUTCOME: outcome_key and outcome_message describe what happened.
    On FAILURE: failed_at_step and failure_reason provide debugging info.
    """
    capability_id: str
    capability_name: str
    status: ReplayStatus
    outputs: dict[str, Any] = {}
    outcome_key: str | None = None
    outcome_message: str | None = None
    failed_at_step: str | None = None
    failure_reason: str | None = None
    step_results: list[StepResult] = []
    screenshot_path: str | None = None
    duration_ms: int = 0
    # Each time a human was brought in: why, what they chose, and what changed.
    interventions: list[dict[str, Any]] = []
