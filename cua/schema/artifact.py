"""
Capability Artifact Schema

The core data model for the system. A Capability is a typed, versioned,
parameterized automation flow that an AI agent can invoke.

Design principles:
- The schema is the contract between discovery (producer) and replay (consumer).
- Every field exists because replay needs it or a human reviewer needs it.
- Locators use a priority cascade for robustness across tenants/versions.
- Error handlers distinguish business outcomes from failures.
- Input values are templated ({{inputs.field_name}}) so the same artifact
  replays with different parameters.
"""

from __future__ import annotations

import enum
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field


# --------------------------------------------------------------------------- #
# Enums                                                                        #
# --------------------------------------------------------------------------- #

class SurfaceType(str, enum.Enum):
    """What kind of application surface this capability targets."""
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class ActionType(str, enum.Enum):
    """The kinds of actions a step can perform."""
    CLICK = "click"
    TYPE = "type"
    SELECT_OPTION = "select_option"
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    WAIT = "wait"
    ASSERT = "assert"
    SCROLL = "scroll"
    KEY_PRESS = "key_press"


class LocatorStrategy(str, enum.Enum):
    """How to find an element. Ordered by typical robustness."""
    ROLE = "role"
    TEXT = "text"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    CSS = "css"
    XPATH = "xpath"
    TEST_ID = "test_id"


class RiskLevel(str, enum.Enum):
    """How risky/irreversible an action is."""
    SAFE = "safe"
    NEEDS_CONFIRMATION = "needs_confirmation"
    BLOCKED = "blocked"


class ErrorClass(str, enum.Enum):
    """The three classes of non-success outcomes."""
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class CheckpointType(str, enum.Enum):
    """What kind of assertion a checkpoint makes."""
    URL_CONTAINS = "url_contains"
    URL_EQUALS = "url_equals"
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_CONTAINS_TEXT = "element_contains_text"
    PAGE_TITLE_CONTAINS = "page_title_contains"
    ELEMENT_NOT_VISIBLE = "element_not_visible"
    URL_NOT_CONTAINS = "url_not_contains"
    # The page is announcing something to the user: an ARIA alert or live
    # region. Surface-level and standard, not app-specific -- it is how a
    # page says "that didn't work, and here's why", and what a human
    # operator reads first.
    ALERT_VISIBLE = "alert_visible"


# Conditions that are each other's opposite, for turning "this step worked"
# into "this step didn't".
_NEGATED: dict[CheckpointType, CheckpointType] = {
    CheckpointType.ELEMENT_VISIBLE: CheckpointType.ELEMENT_NOT_VISIBLE,
    CheckpointType.ELEMENT_NOT_VISIBLE: CheckpointType.ELEMENT_VISIBLE,
    CheckpointType.URL_CONTAINS: CheckpointType.URL_NOT_CONTAINS,
    CheckpointType.URL_NOT_CONTAINS: CheckpointType.URL_CONTAINS,
}


def negate(condition_type: CheckpointType) -> CheckpointType | None:
    """The condition that holds exactly when this one doesn't, if there is one."""
    return _NEGATED.get(condition_type)


class RecoveryAction(str, enum.Enum):
    """What to do when a recoverable error is detected."""
    DISMISS_DIALOG = "dismiss_dialog"
    # Click something on the page to clear the way -- the close button on a
    # banner, "Continue working" on a session-timeout prompt. Which element
    # is in the handler's recovery_target, recorded like any other.
    DISMISS_ELEMENT = "dismiss_element"
    RETRY_STEP = "retry_step"
    WAIT_AND_RETRY = "wait_and_retry"
    REFRESH_PAGE = "refresh_page"
    NAVIGATE_BACK = "navigate_back"


class ParamType(str, enum.Enum):
    """Supported types for input/output parameters."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    TABLE = "table"  # list of rows, each a {column_name: cell_text} dict
    LIST = "list"    # list of strings (inputs), e.g. product names


# --------------------------------------------------------------------------- #
# Building blocks                                                              #
# --------------------------------------------------------------------------- #

class Locator(BaseModel):
    """
    One way to find an element on the page.

    Multiple locators are stored per target in priority order.
    Replay tries them top-to-bottom until one resolves.
    """
    strategy: LocatorStrategy
    value: str
    role: str | None = None
    description: str | None = None
    # Scope: only look inside the repeated card (e.g. a product tile, matched
    # by this CSS selector) that shows `card_text` exactly. For controls whose
    # own label repeats on every card ("Add to cart"): what makes one the
    # right one is the card it sits in.
    inside_card: str | None = None
    card_text: str | None = None


class ElementTarget(BaseModel):
    """
    How to find a specific element/control on the page.

    Stores multiple locator strategies ranked by robustness.
    The replay engine tries them in order, using the first that resolves.
    """
    locators: list[Locator]
    description: str
    iframe_path: list[str] | None = None


class Checkpoint(BaseModel):
    """
    A post-action assertion to verify the step actually worked.

    If the checkpoint fails and no error handler matches, it's a HardFailure.
    """
    type: CheckpointType
    value: str
    locator: Locator | None = None
    description: str | None = None


class ErrorCondition(BaseModel):
    """
    A condition that indicates a specific error state on the page.

    Checked after a step fails its checkpoint or raises an exception.
    """
    type: CheckpointType
    value: str
    locator: Locator | None = None


class ErrorHandler(BaseModel):
    """
    What to do when a specific error condition is detected.

    Step-level handlers catch step-specific errors.
    Global handlers catch cross-cutting issues (session timeouts, app errors).
    """
    condition: ErrorCondition
    # All of these must also hold for the handler to match. Used to confirm
    # we're on the expected page before concluding something is absent, so a
    # page that failed to load isn't misread as e.g. "account not found".
    also_requires: list[ErrorCondition] = []
    classification: ErrorClass
    outcome_key: str | None = None
    outcome_message: str | None = None
    # Append what the page itself says (its alert text) to the message the
    # caller gets. The recorded message says which outcome this is; the app
    # says why, in its own words, at the moment it happens.
    report_page_message: bool = False
    recovery: RecoveryAction | None = None
    # For DISMISS_ELEMENT: what to click to clear the way.
    recovery_target: ElementTarget | None = None
    max_retries: int = 1


# --------------------------------------------------------------------------- #
# Input / Output contract                                                      #
# --------------------------------------------------------------------------- #

class InputParam(BaseModel):
    """A typed input parameter the caller must (or may) provide."""
    type: ParamType = ParamType.STRING
    required: bool = True
    sensitive: bool = False
    default: Any | None = None
    description: str | None = None
    # When the input feeds a dropdown (or picks items from a list on the page):
    # the choices seen at discovery time. For LIST inputs, each element must
    # be one of them. Replay rejects anything else up front, before touching
    # the browser.
    allowed_values: list[str] | None = None
    # A named rule from cua.schema.formats (e.g. "us_zip_code"), for rules the
    # app itself can't reveal. Checked up front, like allowed_values.
    format: str | None = None


class OutputParam(BaseModel):
    """A typed output value extracted during replay."""
    type: ParamType = ParamType.STRING
    extract_from_step: str
    description: str | None = None
    # For TABLE outputs: the column headers seen at discovery time. Replay
    # checks the live table still has these, so a changed table fails loudly
    # instead of returning rows keyed by the wrong names.
    columns: list[str] | None = None
    # For TABLE outputs read from repeated cards (e.g. product tiles) rather
    # than an HTML <table>: the selector for one card, found at discovery.
    # Rows are the cards matching it inside the target; columns are each
    # value's data-test/class name.
    card_selector: str | None = None


class Contract(BaseModel):
    """
    The capability's function signature.

    Defines what goes in (inputs), what comes out (outputs),
    and how to know it succeeded (success_condition).
    """
    inputs: dict[str, InputParam] = {}
    outputs: dict[str, OutputParam] = {}
    success_condition: Checkpoint


# --------------------------------------------------------------------------- #
# Steps                                                                        #
# --------------------------------------------------------------------------- #

TEMPLATE_PATTERN = re.compile(r"\{\{inputs\.(\w+)\}\}")


def fill_templates(text: str, inputs: dict[str, Any]) -> str:
    """Replace {{inputs.x}} with the input's value (a list input is joined with ', ')."""
    def value(m: re.Match) -> str:
        v = inputs.get(m.group(1), m.group(0))
        return ", ".join(v) if isinstance(v, list) else str(v)
    return TEMPLATE_PATTERN.sub(value, text)


class Step(BaseModel):
    """
    One action in the capability flow.

    Granularity: one meaningful user action per step.
    Fill a field, click a button, extract a value.
    """
    id: str
    action: ActionType
    description: str
    target: ElementTarget | None = None
    value: str | None = None
    url: str | None = None
    extract_output_key: str | None = None
    checkpoint: Checkpoint | None = None
    error_handlers: list[ErrorHandler] = []
    risk_level: RiskLevel = RiskLevel.SAFE
    # The action made the page contact the server during discovery (e.g. a
    # filter button that reloads a table via AJAX). Replay waits for that
    # round trip before the next step, so it never reads the pre-update page.
    waits_for_network: bool = False
    # Repeat this step once per element of this LIST input. Inside the step,
    # {{inputs.<name>}} means the current element.
    for_each: str | None = None
    timeout_ms: int = 5000
    wait_before_ms: int = 0

    def resolve_value(self, inputs: dict[str, Any]) -> str | None:
        """Replace {{inputs.param_name}} templates with actual values."""
        return None if self.value is None else fill_templates(self.value, inputs)

    def resolve_url(self, inputs: dict[str, Any]) -> str | None:
        """Replace templates in URL."""
        return None if self.url is None else fill_templates(self.url, inputs)


# --------------------------------------------------------------------------- #
# Safety policy                                                                #
# --------------------------------------------------------------------------- #

class SafetyPolicy(BaseModel):
    """
    What the automation is and isn't allowed to do.

    Enforced on every action in both discovery and replay.
    """
    allowed_domains: list[str]
    blocked_url_patterns: list[str] = []
    allowed_actions: list[ActionType] = list(ActionType)
    pii_fields: list[str] = []
    max_steps: int = 30
    timeout_seconds: int = 120


# --------------------------------------------------------------------------- #
# Top-level capability                                                         #
# --------------------------------------------------------------------------- #

class SurfaceInfo(BaseModel):
    """What application surface this capability targets."""
    type: SurfaceType
    entry_point: str
    app_identifier: str
    tenant_id: str | None = None


class HumanAssist(BaseModel):
    """
    A point in the discovery run where a human took control.

    Whatever the human did by hand was not recorded as steps, so the flow
    has a gap here. Keeping it in the artifact means a reviewer can see that
    the capability isn't purely machine-recorded, and replay can say so
    before it runs.
    """
    after_step_id: str | None = None
    reason: str
    what_the_human_did: list[str] = []
    operator_notes: str = ""


class CapabilityMeta(BaseModel):
    """Identity and provenance."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    display_name: str
    description: str
    version: str = "1.0.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_discovery_run_id: str | None = None
    surface: SurfaceInfo
    # Empty for a flow the agent recorded on its own.
    human_assisted: list[HumanAssist] = []


class Capability(BaseModel):
    """
    The complete capability artifact.

    This is the top-level object that gets serialized to JSON and stored.
    Both human reviewers and calling agents read this to understand
    what the capability does, what it needs, and what it returns.
    """
    schema_version: str = "1.0.0"
    meta: CapabilityMeta
    contract: Contract
    steps: list[Step]
    global_error_handlers: list[ErrorHandler] = []
    policy: SafetyPolicy

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_operator(self) -> bool:
        """
        Whether running this capability involves a person.

        True when any step commits something irreversible, which discovery
        decided and recorded as a risk level. Derived rather than stored, so
        it cannot drift from the steps it describes — and serialized, so a
        calling agent can see it without walking them. It is the answer to
        "will this open a browser and ask me something?", which the caller
        deserves to know before invoking, not sixteen steps in.
        """
        return any(step.risk_level != RiskLevel.SAFE for step in self.steps)
