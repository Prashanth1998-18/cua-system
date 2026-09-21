"""
Locator cascade resolver.

Tries each locator strategy in priority order until one resolves.
This is a thin wrapper since the heavy lifting is in PlaywrightAdapter.find_element(),
but this module handles template resolution in locator values.
"""

from __future__ import annotations

from ..schema.artifact import ElementTarget, fill_templates




def resolve_condition(condition, inputs: dict[str, str]):
    """A Checkpoint or ErrorCondition with {{inputs.x}} filled in its value and locator."""
    locator = condition.locator
    if locator is not None:
        locator = locator.model_copy(update={"value": fill_templates(locator.value, inputs)})
    return condition.model_copy(update={"value": fill_templates(condition.value, inputs), "locator": locator})


def resolve_target_templates(target: ElementTarget, inputs: dict) -> ElementTarget:
    """
    Replace {{inputs.param_name}} templates in all locator values (and card
    scopes and descriptions) within an ElementTarget.

    Returns a new ElementTarget with resolved values (does not mutate the original).
    """
    return ElementTarget(
        locators=[
            loc.model_copy(update={
                "value": fill_templates(loc.value, inputs),
                "description": fill_templates(loc.description or "", inputs),
                "card_text": fill_templates(loc.card_text, inputs) if loc.card_text else None,
            })
            for loc in target.locators
        ],
        description=fill_templates(target.description, inputs),
        iframe_path=target.iframe_path,
    )
