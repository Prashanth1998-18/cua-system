"""
Error handler matching.

After a step fails its checkpoint, the engine checks error handlers
against the current page state. First matching handler determines
the classification and response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..browser.adapter import SurfaceAdapter
from ..schema.artifact import (
    CheckpointType, ElementTarget, ErrorClass, ErrorCondition, ErrorHandler, RecoveryAction,
)
from .locator import fill_templates, resolve_condition, resolve_target_templates

logger = logging.getLogger(__name__)


@dataclass
class HandlerMatch:
    """Result of matching an error handler."""
    handler: ErrorHandler
    classification: ErrorClass
    outcome_key: str | None = None
    outcome_message: str | None = None
    recovery: RecoveryAction | None = None


async def _holds(
    condition: ErrorCondition,
    adapter: SurfaceAdapter,
    inputs: dict[str, str],
    alert_before: str = "",
) -> bool:
    c = resolve_condition(condition, inputs)
    if c.type == CheckpointType.ALERT_VISIBLE:
        # A refusal is the page saying something *new*. A notice that was
        # already on screen before the step is part of the furniture, and
        # reading it as "your action was refused" would turn a banner into a
        # business outcome.
        announced = await adapter.read_alert_text()
        return bool(announced) and announced != alert_before
    return await adapter.check_condition(c.type, c.value, c.locator)


async def match_error_handlers(
    handlers: list[ErrorHandler],
    adapter: SurfaceAdapter,
    inputs: dict[str, str] | None = None,
    alert_before: str = "",
) -> HandlerMatch | None:
    """
    Check each error handler's condition against the current page state.

    Returns the first matching handler, or None if no handler matches.
    Handlers are checked in order, so specific handlers should come
    before generic ones.
    """
    inputs = inputs or {}
    for handler in handlers:
        condition = handler.condition
        matched = await _holds(condition, adapter, inputs, alert_before)
        for required in handler.also_requires:
            if not matched:
                break
            matched = await _holds(required, adapter, inputs, alert_before)

        if matched:
            logger.info(
                "Error handler matched: %s (classification=%s, condition=%s='%s')",
                handler.outcome_key or handler.classification.value,
                handler.classification.value,
                condition.type.value,
                condition.value,
            )
            message = handler.outcome_message
            if message:
                message = fill_templates(message, inputs)
            return HandlerMatch(
                handler=handler,
                classification=handler.classification,
                outcome_key=handler.outcome_key,
                outcome_message=message,
                recovery=handler.recovery,
            )

    return None


async def execute_recovery(
    recovery: RecoveryAction,
    adapter: SurfaceAdapter,
    target: ElementTarget | None = None,
    inputs: dict[str, str] | None = None,
) -> None:
    """Execute a recovery action."""
    match recovery:
        case RecoveryAction.DISMISS_DIALOG:
            await adapter.dismiss_dialog()
        case RecoveryAction.DISMISS_ELEMENT:
            if target is None:
                logger.warning("dismiss_element recovery has nothing to dismiss")
                return
            await adapter.click(resolve_target_templates(target, inputs or {}))
        case RecoveryAction.REFRESH_PAGE:
            url = await adapter.get_current_url()
            await adapter.navigate(url)
        case RecoveryAction.NAVIGATE_BACK:
            url = await adapter.get_current_url()
            # Best effort: go back. If no history, this is a no-op.
            await adapter.key_press("Alt+ArrowLeft")
        case RecoveryAction.WAIT_AND_RETRY:
            import asyncio
            await asyncio.sleep(2)
        case RecoveryAction.RETRY_STEP:
            pass  # Caller handles retry
        case _:
            logger.warning("Unknown recovery action: %s", recovery)
