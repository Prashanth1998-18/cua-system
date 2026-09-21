"""
Capturing what the human did while they had control.

The assignment asks us to "record what the human did" and "capture the
human's actions" across a handoff (section 3.6). Page state before and
after only says where they ended up, so this records the clicks and field
entries themselves, described the way the rest of the system describes
elements: by role and accessible name, never by selector.

The listeners are installed on the page once and report every event to
Python, which keeps the ones that happen while the run is stopped. That
way there is no script to toggle on and off at exactly the right moment,
and a missed toggle can't lose an action.

Anything that happens while the run is paused is a person, because the
automation is parked: operators routinely do the thing first and press
the console button afterwards, and an action isn't worth less as evidence
for having happened in that order. Each one records which state was in
effect, so "they acted before formally taking control" stays visible
instead of being flattened away -- or lost.

Values are redacted the same way logs are: password fields never report a
value at all, and anything matching a known sensitive input is masked.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from playwright.async_api import Page

logger = logging.getLogger(__name__)

REDACTED = "«redacted»"

# Installed into every document of the page. Capture phase, so an action is
# reported even if the app stops the event from bubbling.
_CAPTURE_JS = """
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;

  const roleOf = (el) => {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'password') return 'password';
      return 'textbox';
    }
    return tag || 'element';
  };

  const attr = (el, names) => {
    for (const name of names) {
      const value = el.getAttribute && el.getAttribute(name);
      if (value) return String(value).trim().slice(0, 80);
    }
    return '';
  };

  const nameOf = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    const labelled = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelled) {
      const by = document.getElementById(labelled);
      if (by && by.innerText) return by.innerText.trim().slice(0, 80);
    }
    const aria = attr(el, ['aria-label']);
    if (aria) return aria;
    // A form control's own text is its options or its contents, not its
    // name: ask its label and its attributes instead.
    if (['input', 'select', 'textarea'].includes(tag)) {
      if (el.labels && el.labels.length && el.labels[0].innerText) {
        return el.labels[0].innerText.trim().slice(0, 80);
      }
      return attr(el, ['placeholder', 'name', 'id', 'data-test', 'value']);
    }
    const text = (el.innerText || '').trim();
    if (text) return text.slice(0, 80);
    return attr(el, ['name', 'id', 'data-test', 'value']);
  };

  // The clickable ancestor, so a click on the label inside a button reports
  // the button rather than a bare span.
  const actionable = (el) => {
    let node = el;
    for (let depth = 0; node && depth < 4; depth++) {
      const tag = (node.tagName || '').toLowerCase();
      if (['a', 'button', 'input', 'select', 'textarea'].includes(tag)) return node;
      if (node.getAttribute && node.getAttribute('role')) return node;
      node = node.parentElement;
    }
    return el;
  };

  const report = (payload) => {
    try {
      if (window.__cuaHumanAction) window.__cuaHumanAction(payload);
    } catch (e) { /* the binding goes away on navigation; nothing to do */ }
  };

  document.addEventListener('click', (ev) => {
    const el = actionable(ev.target);
    if (!el) return;
    report({
      type: 'click',
      role: roleOf(el),
      name: nameOf(el),
      url: location.href,
    });
  }, true);

  // Both, because a browser only fires 'change' on blur: an operator who
  // types into the last field and hands control straight back would
  // otherwise leave no trace of it. Python keeps the final value per field.
  const onEdit = (ev) => {
    const el = ev.target;
    if (!el) return;
    const role = roleOf(el);
    if (role === 'password') {
      report({ type: 'fill', role: role, name: nameOf(el), url: location.href });
      return;
    }
    let value = '';
    if ((el.tagName || '').toLowerCase() === 'select') {
      const option = el.options && el.options[el.selectedIndex];
      value = option ? (option.label || option.text || '') : '';
    } else if (role === 'checkbox' || role === 'radio') {
      value = el.checked ? 'checked' : 'unchecked';
    } else {
      value = el.value == null ? '' : String(el.value);
    }
    report({
      type: role === 'combobox' ? 'select' : 'fill',
      role: role,
      name: nameOf(el),
      value: String(value).slice(0, 120),
      url: location.href,
    });
  };

  document.addEventListener('input', onEdit, true);
  document.addEventListener('change', onEdit, true);
})()
"""


# Who was in control when an action happened.
IN_CONTROL_HUMAN = "human"      # they had formally taken over
IN_CONTROL_PAUSED = "paused"    # the run was stopped, waiting on them


class HumanActionRecorder:
    """
    Records the operator's clicks and field entries on a live page.

    `control` says who holds the session right now: one of the labels
    above, or None while the automation is driving -- in which case the
    action is the automation's own and is not recorded.
    """

    def __init__(
        self,
        page: Page,
        control: Callable[[], str | None],
        hide_values: Iterable[str] = (),
    ):
        self._page = page
        self._control = control
        self._hide = [v for v in hide_values if v]
        self._actions: list[dict[str, Any]] = []

    @property
    def actions(self) -> list[dict[str, Any]]:
        return list(self._actions)

    def since(self, index: int) -> list[dict[str, Any]]:
        return self._actions[index:]

    @property
    def count(self) -> int:
        return len(self._actions)

    async def attach(self) -> None:
        """
        Install the listeners. Safe to call repeatedly for the same page:
        the binding is registered once, and the script guards itself.
        """
        if not getattr(self._page, "_cua_action_binding", False):
            try:
                await self._page.expose_function("__cuaHumanAction", self._record)
                setattr(self._page, "_cua_action_binding", True)
            except Exception as e:
                # An already-registered binding belongs to an earlier recorder
                # on the same page; nothing useful to do but carry on.
                logger.debug("Could not expose the action binding: %s", e)
                return
            await self._page.add_init_script(_CAPTURE_JS)

        try:
            await self._page.evaluate(_CAPTURE_JS)  # the document already open
        except Exception as e:
            logger.debug("Could not install action capture on the current page: %s", e)

    async def flush(self) -> None:
        """
        Let reports already on their way from the page arrive.

        The page calls into Python asynchronously, and those calls do not
        queue behind an ordinary round trip to the page, so an action taken
        a moment before the operator hands control back can still be in
        flight. A short wait costs nothing at handoff time.
        """
        try:
            await self._page.evaluate("0")
        except Exception:
            pass
        await asyncio.sleep(0.05)

    def _record(self, payload: dict[str, Any]) -> None:
        control = self._control()
        if control is None:
            return  # the automation's own action
        action = {
            "at": datetime.now(timezone.utc).isoformat(),
            "type": payload.get("type", "click"),
            "role": payload.get("role", ""),
            "name": self._mask(payload.get("name", "")),
            "url": payload.get("url", ""),
            "while": control,
        }
        if "value" in payload:
            action["value"] = self._mask(payload.get("value", ""))

        # Typing reports as it goes; keep one entry per field, holding what
        # they ended up with rather than every keystroke on the way there.
        last = self._actions[-1] if self._actions else None
        if (
            action["type"] in ("fill", "select")
            and last is not None
            and (last["type"], last["role"], last["name"], last["url"])
            == (action["type"], action["role"], action["name"], action["url"])
        ):
            self._actions[-1] = action
            logger.debug("Operator action (updated): %s", _describe(action))
            return

        self._actions.append(action)
        logger.info("Operator action: %s", _describe(action))

    def _mask(self, text: str) -> str:
        for secret in self._hide:
            if secret and secret in text:
                text = text.replace(secret, REDACTED)
        return text


def _describe(action: dict[str, Any]) -> str:
    """One readable line, for logs and for the intervention record."""
    where = f'{action.get("role", "element")} "{action.get("name", "")}"'.strip()
    if action["type"] == "click":
        line = f"clicked {where}"
    elif action["type"] == "select":
        line = f'selected "{action.get("value", "")}" in {where}'
    elif "value" not in action:  # a password: that it was filled, never with what
        line = f"filled {where}"
    else:
        line = f'filled {where} with "{action.get("value", "")}"'
    if action.get("while") == IN_CONTROL_PAUSED:
        line += " (while the run was paused, before taking control)"
    return line


def summarize(actions: list[dict[str, Any]]) -> list[str]:
    """Human-readable lines for the replay result and the operator console."""
    return [_describe(a) for a in actions]
