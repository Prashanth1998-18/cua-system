"""
Action recorder: captures agent actions into a Capability artifact.

As the discovery agent executes tool calls, the recorder builds up
the steps list, locators, and contract for the capability.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Page

from ..schema.artifact import (
    TEMPLATE_PATTERN,
    ActionType,
    Capability,
    CapabilityMeta,
    Checkpoint,
    CheckpointType,
    Contract,
    ElementTarget,
    ErrorClass,
    ErrorCondition,
    ErrorHandler,
    HumanAssist,
    InputParam,
    Locator,
    LocatorStrategy,
    OutputParam,
    negate,
    ParamType,
    RecoveryAction,
    RiskLevel,
    SafetyPolicy,
    Step,
    SurfaceInfo,
    SurfaceType,
)
from ..browser.adapter import snapshot_headings
from .tools import element_by_ref

logger = logging.getLogger(__name__)


_ELEMENT_INFO_JS = """
(el) => {
    const uniqueId = (node) =>
        node.id && document.querySelectorAll(`[id="${CSS.escape(node.id)}"]`).length === 1;

    const cssPath = (node) => {
        const parts = [];
        while (node && node.nodeType === 1 && node !== document.documentElement) {
            // An id is only a shortcut if it identifies one element. Pages
            // with a duplicated id are common enough in older apps, and a
            // path anchored on one is a path to nowhere.
            if (uniqueId(node)) { parts.unshift(`[id="${node.id}"]`); break; }
            let sel = node.tagName.toLowerCase();
            const parent = node.parentElement;
            if (parent) {
                const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
                if (same.length > 1) sel += `:nth-of-type(${same.indexOf(node) + 1})`;
            }
            parts.unshift(sel);
            node = parent;
        }
        return parts.join(" > ");
    };
    return {
        tag: el.tagName.toLowerCase(),
        id: uniqueId(el) ? el.id : "",
        dataTest: el.getAttribute("data-test") || el.getAttribute("data-testid") || "",
        dataTestAttr: el.hasAttribute("data-test") ? "data-test" : "data-testid",
        name: el.getAttribute("name") || "",
        type: el.getAttribute("type") || "",
        value: el.getAttribute("value") || "",
        text: (el.innerText || el.textContent || "").trim().slice(0, 200),
        cssPath: cssPath(el),
    };
}
"""


_CARD_SCOPE_JS = r"""
(el, values) => {
    // The nearest repeated card around `el` that shows one of `values`
    // (e.g. the product tile around an "Add to cart" button), plus that
    // same field's text in every sibling card (the choices on the page).
    const text = (n) => (n.innerText || n.textContent || "").trim();
    const selectorFor = (n) => {
        const dt = n.getAttribute("data-test");
        if (dt) return `[data-test="${dt}"]`;
        const cls = typeof n.className === "string" ? n.className.trim().split(/\s+/)[0] : "";
        return cls ? n.tagName.toLowerCase() + "." + cls : null;
    };
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const leaf = Array.from(a.querySelectorAll("*")).find((n) => n.children.length === 0 && values.includes(text(n)));
        if (!leaf) continue;
        const cardSelector = selectorFor(a);
        if (!cardSelector) continue;
        const cards = document.querySelectorAll(cardSelector);
        if (cards.length < 2) return null;
        const leafSelector = selectorFor(leaf);
        const options = leafSelector
            ? Array.from(cards).map((c) => c.querySelector(leafSelector)).filter(Boolean).map(text)
            : [];
        return { cardSelector, text: text(leaf), options };
    }
    return null;
}
"""


def _role_and_name(snapshot: str, ref: str) -> tuple[str | None, str]:
    """Role and accessible name of the element with `ref`, read from its snapshot line."""
    marker = f"[ref={ref}]"
    for line in snapshot.splitlines():
        if marker in line:
            m = re.match(r'\s*-\s+([\w-]+)(?:\s+"((?:[^"\\]|\\.)*)")?', line)
            if m:
                return m.group(1), (m.group(2) or "").replace('\\"', '"')
    return None, ""


def _quote(value: str) -> str:
    return value.replace('"', '\\"')


# Replay waits this many times longer than discovery observed, and at least
# the minimum: a page that took 3s today may take 8s tomorrow.
_TIMEOUT_HEADROOM = 3
# Floor and slack for the whole-run budget written into the artifact.
_MIN_RUN_SECONDS = 120
_RUN_HEADROOM_S = 30
_MIN_TIMEOUT_MS = 10_000


class ActionRecorder:
    """
    Records discovery agent actions into a Capability artifact.

    Usage:
        recorder = ActionRecorder(name="account_details", ..., inputs={...})
        target = await recorder.describe_element(page, ref, snapshot)   # before acting
        ... perform the action ...
        recorder.record_click(target, description)
        capability = recorder.build()
    """

    def __init__(
        self,
        name: str,
        display_name: str,
        description: str,
        entry_point: str,
        allowed_domains: list[str] | None = None,
        discovery_run_id: str | None = None,
        inputs: dict[str, str] | None = None,
        secrets: set[str] | None = None,
    ):
        self._discovery_inputs = inputs or {}
        self._secrets = secrets or set()
        self._name = name
        self._display_name = display_name
        self._description = description
        self._entry_point = entry_point
        self._allowed_domains = allowed_domains or []
        self._discovery_run_id = discovery_run_id or str(uuid4())

        self._steps: list[Step] = []
        self._human_assists: list[HumanAssist] = []
        # Conditions that can block any step, not just one: things the
        # agent had to clear out of the way (see record_blocker).
        self._global_handlers: list[ErrorHandler] = []
        # step id -> (outcome_key, message) awaiting the step's checkpoint
        self._pending_rejections: dict[str, tuple[str, str]] = {}
        self._inputs: dict[str, InputParam] = {}
        self._outputs: dict[str, OutputParam] = {}
        self._pii_fields: list[str] = []
        self._step_counter = 0
        self._select_options: dict[str, list[str]] = {}  # step id -> dropdown options seen
        self._card_options: dict[str, list[str]] = {}    # value picked in a card -> that field across all cards
        self._input_formats: dict[str, str] = {}         # input name -> rule the task stated (ADR-016)

    def _input_values(self) -> list[str]:
        """Every discovery-time input value, with list inputs flattened."""
        out: list[str] = []
        for v in self._discovery_inputs.values():
            out.extend(v if isinstance(v, list) else [v])
        return [v for v in out if v]

    def _next_step_id(self, prefix: str) -> str:
        self._step_counter += 1
        return f"{prefix}_{self._step_counter:02d}"

    async def describe_element(
        self, page: Page, ref: str, snapshot: str, reading: bool = False, parent: bool = False,
    ) -> ElementTarget:
        """
        Build replay locators for the element the model pointed at, from the
        element itself. Must be called before acting on it: a click can
        navigate away and the element is gone.

        Elements identified by one of the task's input values (e.g. the link
        labelled with the requested account number) only keep locators that
        contain that value, so it can be parameterized. A value-agnostic
        backup like "first link in the table" would otherwise make replay
        silently act on the wrong record when the value isn't there.

        `reading=True` is the mirror case, for elements whose content is being
        extracted: their current content (e.g. "$304022.93") is exactly what
        changes between runs, so it can't be used to find them.

        `parent=True` describes the element's container instead (e.g. the list
        around the one item card the model pointed at).
        """
        role, name = _role_and_name(snapshot, ref)
        element = element_by_ref(page, ref)
        if parent:
            element = element.locator("xpath=..")
            role, name = None, ""
        info = await element.evaluate(_ELEMENT_INFO_JS)
        tag = info["tag"]
        values = self._input_values()

        # A label every card repeats ("Add to cart"): the right one is the
        # one inside the card showing an input value. Locate it only that way.
        if (not reading and role and name and values
                and await page.get_by_role(role, name=name, exact=True).count() > 1):
            scope = await element.evaluate(_CARD_SCOPE_JS, values)
            if scope:
                shown = scope["text"]
                self._card_options[shown] = scope["options"]
                return ElementTarget(
                    locators=[
                        Locator(strategy=LocatorStrategy.ROLE, value=name, role=role,
                                inside_card=scope["cardSelector"], card_text=shown,
                                description=f'{role} "{name}" in the card showing "{shown}"'),
                        Locator(strategy=LocatorStrategy.CSS, value=tag,
                                inside_card=scope["cardSelector"], card_text=shown,
                                description=f'the {tag} in the card showing "{shown}"'),
                    ],
                    description=f'{role} "{name}" for "{shown}"',
                )

        identifying = [v for v in values if v in (name, info["text"])]
        if reading:
            identifying = []
            name = ""

        candidates: list[Locator] = []
        if role and name:
            candidates.append(Locator(
                strategy=LocatorStrategy.ROLE, value=name, role=role, description=f'{role} "{name}"',
            ))
        if identifying and info["text"]:
            candidates.append(Locator(
                strategy=LocatorStrategy.TEXT, value=info["text"], description=f'text "{info["text"]}"',
            ))
        if info["dataTest"]:
            candidates.append(Locator(
                strategy=LocatorStrategy.CSS,
                value=f'[{info["dataTestAttr"]}="{_quote(info["dataTest"])}"]',
                description="by test attribute",
            ))
        if info["id"]:
            candidates.append(Locator(
                strategy=LocatorStrategy.CSS, value=f'[id="{_quote(info["id"])}"]', description="by id",
            ))
        if info["name"]:
            candidates.append(Locator(
                strategy=LocatorStrategy.CSS,
                value=f'{tag}[name="{_quote(info["name"])}"]',
                description="by name attribute",
            ))
        if tag == "input" and info["type"] in ("submit", "button") and info["value"]:
            candidates.append(Locator(
                strategy=LocatorStrategy.CSS,
                value=f'input[value="{_quote(info["value"])}"]',
                description="by button label",
            ))
        candidates.append(Locator(
            strategy=LocatorStrategy.CSS, value=info["cssPath"], description="by position in the page structure",
        ))

        if identifying:
            candidates = [c for c in candidates if any(v in c.value for v in identifying)]

        locators: list[Locator] = []
        for c in candidates:
            if not any(c.strategy == l.strategy and c.value == l.value for l in locators):
                locators.append(c)

        locators = await self._verified(page, element, locators)
        description = f'{role} "{name}"' if role and name else (f"{role or tag}")
        return ElementTarget(locators=locators, description=description)

    async def _verified(
        self, page: Page, element, locators: list[Locator],
    ) -> list[Locator]:
        """
        Keep the locators that actually find this element, right now.

        An attribute being present doesn't make a locator work: SauceDemo has
        two elements with `id="inventory_container"`, so locating by that id
        is ambiguous and resolves to nothing usable. Writing it into the
        artifact anyway means a replay that fails on a page where the element
        is sitting in plain sight. Each candidate is tried here, against this
        page, and kept only if it finds exactly one element and that element
        is the one the model pointed at.
        """
        from ..browser.adapter import PlaywrightAdapter

        resolver = PlaywrightAdapter(page)
        try:
            handle = await element.element_handle()
        except Exception:
            return locators

        kept: list[Locator] = []
        for locator in locators:
            try:
                candidate = resolver._resolve_locator(locator)
                if await candidate.count() != 1:
                    logger.debug("Dropping ambiguous locator %s", locator.value)
                    continue
                found = await candidate.element_handle()
                if await page.evaluate("([a, b]) => a === b", [found, handle]):
                    kept.append(locator)
                else:
                    logger.debug("Dropping locator %s: finds a different element", locator.value)
            except Exception as e:
                logger.debug("Dropping locator %s: %s", locator.value, e)

        if not kept:
            # Better a recording we can review than none at all, but say so.
            logger.warning(
                "No locator for '%s' could be verified against the page; keeping them unverified",
                getattr(element, "_selector", "element"),
            )
            return locators
        return kept

    def add_checkpoint(
        self, step_id: str, before: str, after: str, elapsed_ms: int,
        before_url: str = "", after_url: str = "",
    ) -> None:
        """
        Give a recorded step a checkpoint from what its action changed:
        - the first page heading that appeared (e.g. "Accounts Overview" after
          logging in). Headings say which page you're on, not the data on it,
          so they hold across accounts and runs;
        - otherwise, if the URL path changed, the new path (e.g. "cart.html").
          Apps whose page titles aren't real headings (SauceDemo) still get a
          checkpoint this way.
        Steps that don't change the page (typing, filtering in place) get none.
        """
        step = next((s for s in self._steps if s.id == step_id), None)
        if step is None:
            return
        new = [h for h in snapshot_headings(after) if h not in snapshot_headings(before)]
        before_page = urlparse(before_url).path.rstrip("/").split("/")[-1]
        after_page = urlparse(after_url).path.rstrip("/").split("/")[-1]
        if new:
            step.checkpoint = Checkpoint(
                type=CheckpointType.ELEMENT_VISIBLE,
                value="",
                locator=Locator(strategy=LocatorStrategy.ROLE, value=new[0], role="heading",
                                description=f'heading "{new[0]}"'),
                description=f'the "{new[0]}" heading',
            )
        elif after_page and after_page != before_page:
            step.checkpoint = Checkpoint(
                type=CheckpointType.URL_CONTAINS,
                value=after_page,
                description=f'the "{after_page}" page',
            )
        else:
            return
        step.timeout_ms = max(_MIN_TIMEOUT_MS, elapsed_ms * _TIMEOUT_HEADROOM)
        self._add_rejection_handler(step)

    def _add_rejection_handler(self, step: Step) -> None:
        """
        Turn "the task says what a refusal here means" into a handler.

        The condition is the application announcing something, and the
        checkpoint that proves this step worked *not* holding. Both, because
        either alone lies: an alert can be left over from something else,
        and a page that simply failed to load is a hard failure, not a
        business outcome. What the application actually said is read from
        the page when it happens, not guessed now.
        """
        pending = self._pending_rejections.pop(step.id, None)
        if not pending or not step.checkpoint:
            return
        opposite = negate(step.checkpoint.type)
        if opposite is None:
            return
        outcome_key, message = pending
        step.error_handlers.append(ErrorHandler(
            condition=ErrorCondition(type=CheckpointType.ALERT_VISIBLE, value=""),
            also_requires=[ErrorCondition(
                type=opposite, value=step.checkpoint.value, locator=step.checkpoint.locator,
            )],
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_key=outcome_key,
            outcome_message=message or outcome_key,
            report_page_message=True,
            max_retries=0,
        ))

    @property
    def last_step_id(self) -> str | None:
        return self._steps[-1].id if self._steps else None

    def _append(self, step: Step) -> None:
        self._steps.append(step)

    def record_blocker(self, target: ElementTarget) -> None:
        """
        Something was in the way and the agent cleared it.

        This is recorded as a *global* recoverable condition, not as a step
        and not against one step. Two reasons. A dismissal isn't part of the
        flow — the thing it dismissed may not be there next time, and a
        recording that always clicks "Dismiss" fails on the run where nothing
        needed dismissing. And a modal doesn't politely block only the action
        you were about to take: it blocks whatever comes next, which on a
        slower day is a different step than it was today.
        """
        if any(
            handler.recovery_target
            and handler.recovery_target.description == target.description
            for handler in self._global_handlers
        ):
            return  # the same blocker, seen again
        self._global_handlers.append(ErrorHandler(
            condition=ErrorCondition(
                type=CheckpointType.ELEMENT_VISIBLE, value="",
                locator=target.locators[0].model_copy(),
            ),
            classification=ErrorClass.RECOVERABLE,
            recovery=RecoveryAction.DISMISS_ELEMENT,
            recovery_target=target,
            max_retries=2,
        ))

    def note_human_assist(
        self,
        reason: str,
        what_the_human_did: list[str],
        operator_notes: str = "",
    ) -> None:
        """
        Remember that an operator took control at this point in the run.

        Their actions are deliberately not turned into steps: we recorded
        what they did, not how to find the controls they used, and guessing
        the latter is how a capability ends up clicking the wrong thing on
        replay. The artifact carries the gap instead.
        """
        self._human_assists.append(HumanAssist(
            after_step_id=self.last_step_id,
            reason=reason,
            what_the_human_did=what_the_human_did,
            operator_notes=operator_notes,
        ))

    def record_navigate(self, url: str, description: str = "") -> None:
        self._append(Step(
            id=self._next_step_id("navigate"),
            action=ActionType.NAVIGATE,
            description=description or f"Navigate to {url}",
            url=url,
            timeout_ms=10000,
        ))

    def record_click(
        self,
        target: ElementTarget,
        description: str,
        risk_level: RiskLevel = RiskLevel.SAFE,
        if_missing: tuple[str, str, ElementTarget] | None = None,
        waits_for_network: bool = False,
        if_rejected: tuple[str, str] | None = None,
    ) -> None:
        """
        `if_missing` = (outcome_key, message, page_target): if at replay time the
        target can't be found while page_target *is* showing, that's the named
        business outcome (e.g. account_not_found), not a failure.

        `if_rejected` = (outcome_key, message): if at replay time this step
        doesn't achieve what it did here *and* the application is announcing
        something, that's the named business outcome (e.g. login_rejected)
        rather than a failure. The condition can only be built once we know
        what "achieved" looks like, so it is finished in add_checkpoint.

        Anything cleared out of the way just before this (see record_blocker)
        is attached here as a recoverable condition.
        """
        handlers = []
        if if_missing:
            outcome_key, message, page_target = if_missing
            handlers.append(ErrorHandler(
                condition=ErrorCondition(
                    type=CheckpointType.ELEMENT_NOT_VISIBLE, value="", locator=target.locators[0].model_copy(),
                ),
                also_requires=[ErrorCondition(
                    type=CheckpointType.ELEMENT_VISIBLE, value="", locator=page_target.locators[0].model_copy(),
                )],
                classification=ErrorClass.BUSINESS_OUTCOME,
                outcome_key=outcome_key,
                outcome_message=message or outcome_key,
                max_retries=0,
            ))
        step_id = self._next_step_id("click")
        if if_rejected:
            self._pending_rejections[step_id] = if_rejected
        self._append(Step(
            id=step_id,
            action=ActionType.CLICK,
            description=description or f"Click {target.description}",
            target=target,
            risk_level=risk_level,
            error_handlers=handlers,
            waits_for_network=waits_for_network,
        ))

    def _register_input(self, param_name: str, description: str, sensitive: bool = False) -> str:
        self._inputs[param_name] = InputParam(
            type=ParamType.STRING, required=True, sensitive=sensitive, description=description,
        )
        if sensitive and param_name not in self._pii_fields:
            self._pii_fields.append(param_name)
        return f"{{{{inputs.{param_name}}}}}"

    def record_type(
        self,
        target: ElementTarget,
        text: str,
        description: str = "",
        is_input_param: bool = False,
        param_name: str | None = None,
        is_sensitive: bool = False,
    ) -> None:
        value = text
        if is_input_param and param_name:
            value = self._register_input(param_name, description or f"Value for {target.description}", is_sensitive)
        self._append(Step(
            id=self._next_step_id("type"),
            action=ActionType.TYPE,
            description=description or f"Type into {target.description}",
            target=target,
            value=value,
        ))

    def record_select_option(
        self,
        target: ElementTarget,
        value: str,
        description: str = "",
        is_input_param: bool = False,
        param_name: str | None = None,
        options: list[str] | None = None,
    ) -> None:
        resolved = value
        if is_input_param and param_name:
            resolved = self._register_input(param_name, description or f"Value for {target.description}")
        step_id = self._next_step_id("select")
        if options:
            self._select_options[step_id] = options
        self._append(Step(
            id=step_id,
            action=ActionType.SELECT_OPTION,
            description=description or f"Select '{value}' in {target.description}",
            target=target,
            value=resolved,
        ))

    def record_extract(
        self,
        target: ElementTarget,
        output_name: str,
        description: str,
        table_columns: list[str] | None = None,
        card_selector: str | None = None,
    ) -> None:
        """Record a data extraction. `table_columns` set means a TABLE output (from repeated cards if `card_selector`)."""
        # If the agent re-read the same output, keep only the latest read.
        self._steps = [s for s in self._steps if s.extract_output_key != output_name]
        step_id = self._next_step_id("extract")
        self._outputs[output_name] = OutputParam(
            type=ParamType.TABLE if table_columns is not None else ParamType.STRING,
            extract_from_step=step_id,
            description=description,
            columns=table_columns,
            card_selector=card_selector,
        )
        self._append(Step(
            id=step_id,
            action=ActionType.EXTRACT,
            description=description,
            target=target,
            extract_output_key=output_name,
        ))

    def set_input_format(self, param_name: str, rule: str) -> None:
        """A rule the task stated for an input (e.g. zip is an existing US zip)."""
        self._input_formats[param_name] = rule

    def record_key_press(self, key: str) -> None:
        self._append(Step(
            id=self._next_step_id("key_press"),
            action=ActionType.KEY_PRESS,
            description=f"Press {key}",
            value=key,
        ))

    def _parameterize_inputs(self) -> None:
        """
        Replace literal discovery-time input values in the recorded steps with
        {{inputs.name}} placeholders, and register those inputs in the contract.

        Only type/select steps let the model flag a value as an input. Anything
        else that used an input (e.g. clicking the link labelled with the
        account number) would otherwise be baked in, and replay would always
        act on the discovery-time value.
        """
        # Every element of a list input becomes the list's own placeholder;
        # inside a repeated step it means "the current element" (ADR-014).
        pairs = [
            (name, element)
            for name, v in self._discovery_inputs.items()
            for element in (v if isinstance(v, list) else [v])
            if element
        ]
        # Longest values first, so a value that contains another isn't split.
        for name, value in sorted(pairs, key=lambda kv: -len(kv[1])):
            placeholder = f"{{{{inputs.{name}}}}}"
            used = False
            for step in self._steps:
                step.description = step.description.replace(value, placeholder)
                if step.value and value in step.value:
                    step.value = step.value.replace(value, placeholder)
                    used = True
                if step.url and value in step.url:
                    step.url = step.url.replace(value, placeholder)
                    used = True
                locators = list(step.target.locators) if step.target else []
                if step.checkpoint and step.checkpoint.locator:
                    locators.append(step.checkpoint.locator)
                for handler in step.error_handlers:
                    for condition in [handler.condition, *handler.also_requires]:
                        if condition.locator:
                            locators.append(condition.locator)
                            condition.locator.description = (condition.locator.description or "").replace(value, placeholder)
                    if handler.outcome_message and value in handler.outcome_message:
                        handler.outcome_message = handler.outcome_message.replace(value, placeholder)
                for loc in locators:
                    if value in loc.value:
                        loc.value = loc.value.replace(value, placeholder)
                        used = True
                    if loc.card_text and value in loc.card_text:
                        loc.card_text = loc.card_text.replace(value, placeholder)
                        used = True
                if step.target:
                    step.target.description = step.target.description.replace(value, placeholder)
                    for loc in step.target.locators:
                        loc.description = (loc.description or "").replace(value, placeholder)
            if used and name not in self._inputs:
                is_list = isinstance(self._discovery_inputs.get(name), list)
                self._inputs[name] = InputParam(
                    type=ParamType.LIST if is_list else ParamType.STRING,
                    required=True,
                    description=f"Parameterized from the value supplied as '{name}' during discovery",
                )

    def _fold_repeats(self) -> None:
        """
        Discovery ran with a list input (e.g. two products), so the same
        action was recorded once per element. After parameterization those
        steps are identical; fold each such run into one step that repeats
        for every element at replay (ADR-014). Done mechanically, rather than
        asking the model to reason about loops.
        """
        def signature(step: Step) -> str:
            # waits_for_network is left out: whether a click happened to catch a
            # request is noisy, and a folded step keeps it if any run had it.
            return step.model_dump_json(include={
                "action", "target", "value", "url", "risk_level", "error_handlers",
            })

        for name, v in self._discovery_inputs.items():
            if not isinstance(v, list) or len(v) < 2:
                continue
            placeholder = f"{{{{inputs.{name}}}}}"
            i = 0
            while i < len(self._steps):
                first = self._steps[i]
                sig = signature(first)
                if placeholder in sig:
                    j = i + 1
                    while j < len(self._steps) and signature(self._steps[j]) == sig:
                        j += 1
                    if j - i == len(v):
                        first.for_each = name
                        first.waits_for_network = any(s.waits_for_network for s in self._steps[i:j])
                        del self._steps[i + 1:j]
                    elif j - i != 1:
                        logger.warning("Step '%s' repeats %d times for %d values of '%s'; not folded",
                                       first.id, j - i, len(v), name)
                i += 1

        # An output named after one specific list value (e.g. "backpack_price")
        # only works for the items used during discovery.
        for step in self._steps:
            if step.extract_output_key and not step.for_each:
                text = f"{step.extract_output_key} {step.description}".lower()
                for name, v in self._discovery_inputs.items():
                    if isinstance(v, list) and any(x.lower() in text for x in v):
                        logger.warning(
                            "Output '%s' seems specific to one value of '%s' used in discovery; "
                            "it won't generalise to other values", step.extract_output_key, name,
                        )

        # The choices seen across the cards become the list's allowed values.
        for name, v in self._discovery_inputs.items():
            if isinstance(v, list) and name in self._inputs:
                options = next((self._card_options[x] for x in v if x in self._card_options), None)
                if options:
                    self._inputs[name].allowed_values = options

    def _register_placeholder_inputs(self) -> None:
        """
        Every {{inputs.x}} used by a step becomes a declared input, and secret
        ones are marked sensitive (redacted in replay logs). Secrets arrive in
        steps already as placeholders, since their values are never typed by
        the model directly.
        """
        for step in self._steps:
            texts = [step.value or "", step.url or ""]
            if step.target:
                texts += [loc.value for loc in step.target.locators]
            for text in texts:
                for name in TEMPLATE_PATTERN.findall(text):
                    if name not in self._inputs:
                        self._inputs[name] = InputParam(
                            type=ParamType.STRING, required=True,
                            description=f"Input '{name}' used during discovery",
                        )
                    if name in self._secrets:
                        self._inputs[name].sensitive = True
                        if name not in self._pii_fields:
                            self._pii_fields.append(name)

    def build(self, final_url: str | None = None) -> Capability:
        """
        Build the final Capability artifact from recorded actions.

        Args:
            final_url: The page URL when the agent called 'done'. Used to
                derive the success condition; falls back to the entry point's
                last path segment if not given (a much weaker signal, since
                most flows end up on a different page than they started on).
        """
        self._parameterize_inputs()
        self._fold_repeats()
        self._register_placeholder_inputs()
        for name, rule in self._input_formats.items():
            if name in self._inputs:
                self._inputs[name].format = rule
            else:
                logger.warning("Rule '%s' was recorded for input '%s', which no step uses", rule, name)

        # A dropdown fed entirely by one input constrains that input to the
        # dropdown's options.
        for step in self._steps:
            options = self._select_options.get(step.id)
            match = TEMPLATE_PATTERN.fullmatch(step.value or "")
            if options and match and match.group(1) in self._inputs:
                self._inputs[match.group(1)].allowed_values = options

        allowed_domains = self._allowed_domains
        if not allowed_domains:
            hostname = urlparse(self._entry_point).hostname
            if hostname:
                allowed_domains = [hostname]

        # Success = the last page-level checkpoint reached (e.g. the "Account
        # Details" heading). Only if the flow never changed page, fall back to
        # the final URL's path.
        checkpoints = [s.checkpoint for s in self._steps if s.checkpoint]
        if checkpoints:
            success_condition = checkpoints[-1].model_copy(deep=True)
        else:
            success_url = final_url or self._entry_point
            success_path = urlparse(success_url).path
            success_condition = Checkpoint(
                type=CheckpointType.URL_CONTAINS,
                value=success_path.split("/")[-1] if success_path else success_url,
                description="the final page of the flow",
            )

        return Capability(
            meta=CapabilityMeta(
                name=self._name,
                display_name=self._display_name,
                description=self._description,
                source_discovery_run_id=self._discovery_run_id,
                surface=SurfaceInfo(
                    type=SurfaceType.WEB,
                    entry_point=self._entry_point,
                    app_identifier=self._name,
                ),
                human_assisted=list(self._human_assists),
            ),
            contract=Contract(
                inputs=self._inputs,
                outputs=self._outputs,
                success_condition=success_condition,
            ),
            steps=self._steps,
            global_error_handlers=self._global_handlers,
            policy=SafetyPolicy(
                allowed_domains=allowed_domains,
                pii_fields=self._pii_fields,
                # How long this flow should take, from how long it took when
                # it was recorded. A default number would be a guess about an
                # application nobody had run yet; this is a measurement.
                timeout_seconds=max(
                    _MIN_RUN_SECONDS,
                    int(sum(step.timeout_ms for step in self._steps) / 1000) + _RUN_HEADROOM_S,
                ),
            ),
        )
