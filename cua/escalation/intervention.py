"""
Intervention request/response models.

When the replay engine escalates, it creates an InterventionRequest
containing the current state (screenshot, step, failure reason, CDP URL).
The operator responds with an InterventionResponse (resume or abort).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InterventionType(str, Enum):
    """Why escalation was triggered."""
    HARD_FAILURE = "hard_failure"           # Unrecoverable error
    CONFIRMATION_REQUIRED = "confirmation"  # High-risk action needs human OK
    MAX_RETRIES_EXCEEDED = "max_retries"    # Recovery attempts exhausted
    CUSTOM = "custom"                       # Agent or policy triggered


class OperatorAction(str, Enum):
    """What the operator decided to do."""
    RESUME = "resume"     # Continue automation from where it stopped
    ABORT = "abort"       # Stop the entire run
    TAKE_OVER = "takeover"  # Operator takes manual control via CDP


class HandBack(str, Enum):
    """
    What the operator says they did before handing control back.

    This is their claim, not a verdict: the replay engine checks it against
    the step's recorded checkpoint, and stops if the two disagree. Two
    independent signals are worth more than either alone when the step is
    something like placing an order.
    """
    COMPLETED = "completed"  # "I did this step myself; carry on from the next one"
    RETRY = "retry"          # "I cleared the problem; run the step again"


class InterventionRequest(BaseModel):
    """
    Sent to the operator when escalation is triggered.

    Contains everything the operator needs to understand the situation:
    - What went wrong (type, reason, step)
    - What they're looking at (screenshot, CDP URL for live browser)
    - Context (capability name, inputs used)
    """
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # What happened
    intervention_type: InterventionType
    reason: str
    failed_step_id: str | None = None
    failed_step_description: str | None = None
    error_message: str | None = None

    # Context
    capability_name: str
    capability_display_name: str = ""
    current_url: str = ""
    page_title: str = ""

    # Access to the live browser
    cdp_url: str | None = None
    screenshot_base64: str | None = None
    screenshot_path: str | None = None

    # What inputs were being used (redacted). Values are whatever the input
    # was -- a string, or a list for a list input -- so the operator sees it
    # the way the caller passed it.
    redacted_inputs: dict[str, Any] = Field(default_factory=dict)


class InterventionResponse(BaseModel):
    """
    The operator's response to an intervention request.
    """
    request_id: str
    action: OperatorAction
    hand_back: HandBack | None = None  # only meaningful when handing control back
    operator_notes: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EscalationManager:
    """
    Coordinates between the replay engine and the operator.

    In production this would notify via WebSocket/webhook.
    For the demo, it exposes the intervention via the operator API server.
    """

    def __init__(self):
        self._pending: InterventionRequest | None = None
        self._response: InterventionResponse | None = None

    @property
    def pending_request(self) -> InterventionRequest | None:
        return self._pending

    @property
    def last_response(self) -> InterventionResponse | None:
        return self._response

    def create_request(
        self,
        intervention_type: InterventionType,
        reason: str,
        capability_name: str,
        capability_display_name: str = "",
        failed_step_id: str | None = None,
        failed_step_description: str | None = None,
        error_message: str | None = None,
        current_url: str = "",
        page_title: str = "",
        cdp_url: str | None = None,
        screenshot_path: str | None = None,
        redacted_inputs: dict[str, Any] | None = None,
    ) -> InterventionRequest:
        """Create and store an intervention request."""
        request = InterventionRequest(
            intervention_type=intervention_type,
            reason=reason,
            capability_name=capability_name,
            capability_display_name=capability_display_name,
            failed_step_id=failed_step_id,
            failed_step_description=failed_step_description,
            error_message=error_message,
            current_url=current_url,
            page_title=page_title,
            cdp_url=cdp_url,
            screenshot_path=screenshot_path,
            redacted_inputs=redacted_inputs or {},
        )
        self._pending = request
        logger.info(
            "Intervention request created: %s (type=%s, step=%s)",
            request.id,
            intervention_type.value,
            failed_step_id,
        )
        return request

    def note(self, response: InterventionResponse) -> None:
        """Record an operator response that doesn't close the request (taking over)."""
        self._response = response

    def resolve(self, response: InterventionResponse) -> None:
        """Record the operator's response."""
        self._response = response
        self._pending = None
        logger.info(
            "Intervention resolved: request=%s, action=%s",
            response.request_id,
            response.action.value,
        )

    def clear(self) -> None:
        """Clear pending state."""
        self._pending = None
        self._response = None
