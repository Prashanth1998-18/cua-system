# REPORT

Built against [SauceDemo](https://www.saucedemo.com) — browse catalog, total with tax, place order — and a mock back-office app for the conditions a polished demo site never produces. Every artifact in `evidence/` came out of a real LLM-driven run. Per-decision rationale: [`decisions.md`](decisions.md), 24 ADRs.

---

## 1. Architecture

Two halves sharing one file format, in one process:

```
workflows.md (plain English)        Capability artifact (JSON)
        │                            typed contract · locator cascade
        ▼                            checkpoints · error handlers
  cua discover ──── records ────────────────┬────────► cua replay
  LLM ↔ Playwright                          │          zero LLM calls
  observe → decide → act                    │
        └────────► operator console ◄───────┘
               pause · take the live session · hand back
```

**Discovery** is a hand-written observe/decide/act loop on the `openai` SDK against an OpenAI-compatible endpoint — no agent framework (ADR-008/009). Each turn: an accessibility snapshot where every element has a ref (`button "Add to cart" [ref=e44]`), one tool call, executed, recorded. The loop itself is a dozen lines; what surrounds it is escalation, stuck detection and budgets. **Replay** walks the steps mechanically — validate inputs, resolve `{{inputs.x}}`, try locators in cascade order, verify the checkpoint, classify what goes wrong — with no model in the path.

**The model points at refs and never writes a selector.** The load-bearing decision (ADR-002/003). An LLM asked for a CSS selector produces something plausible and unverified; a ref is something it can actually see, and the recorder derives locators from the element that ref resolves to. The model contributes *intent*, the recorder *identification*.

**Goals are procedures, not wishes** (ADR-011). `workflows.md` briefs the agent the way you'd brief a new starter, with no selectors and no page structure. A vague goal makes the model invent a business process; a procedure with selectors in it makes the document the automation and the model a formality.

---

## 2. Artifact schema

A `Capability` is what an agent calls, so it's a contract first and a script second: `meta` (identity, provenance, where a human took over while recording), `contract` (typed inputs with `sensitive`/`allowed_values`/`format`, typed outputs, a success condition), `steps`, `policy` (domains, action types, PII fields, step and time limits), and `requires_operator` — computed from the steps so it can't drift, serialized so a caller knows *before* invoking whether this one stops and asks. Three choices worth defending:

**The cascade is ranked by what survives change** (ADR-003): role + accessible name → visible text → `data-test` → `id` → `name` → button value → structural CSS path. Role and name come from what a *user* sees, so they survive restyling and most version drift; the structural path is last because it breaks first. Replay reports which rung resolved, so drift is visible before it's a failure. **Every one of them is tried against the page before it's written** (ADR-024) and kept only if it finds exactly one element and that element is the one the model pointed at — SauceDemo ships two `id="inventory_container"`, and an artifact full of plausible-looking selectors that resolve to nothing is worse than a short list that works. An artifact's locators are observations, not claims.

**A value-identified element keeps only locators containing that value.** If the model pointed at something because it showed `{{inputs.account_id}}`, a positional fallback like "first link in the table" would silently act on the *wrong record* when the value isn't there. Better to fail. The mirror case: an element being *extracted* is never located by its current content, since that content is what changes between runs.

**Repeated controls are located by the card they sit in** — "the Add to cart button inside the card showing `{{inputs.items}}`", since six such buttons share a name. The recorder derives this from the label repeating across siblings, which also hands it the catalog's values as `allowed_values`. Repeats fold into one `for_each` step, so the artifact says "add each requested item" rather than hard-coding two.

---

## 3. Determinism & error handling

Same artifact, same inputs, same decisions — never a fresh interpretation of the page. The interesting work isn't repeating clicks, it's being right about *why* something didn't work.

| Class | Meaning | What replay does |
|---|---|---|
| Business outcome | A legitimate "no" the caller needs | Stop, return `outcome_key` + message, **exit 2** |
| Recoverable | Something in the way that can be cleared | Clear it, run the step again, bounded |
| Hard failure | Nothing explains this | Screenshot, ask a person, **exit 1** |

All three are demonstrated in `/evidence/`, which took a second application (ADR-023): SauceDemo has no *recoverable* condition anywhere, and a taxonomy branch that has never run is a claim rather than a design. `scripts/mock_legacy_app.py` supplies one — table markup, no test ids, a modal that really covers the form, and a `--break` mode that removes the control a recording depends on.

**A refusal by the application is an answer** (ADR-020). `locked_out_user` can't log in, and the site says so. The model never sees that during a successful recording, so it can't point at the error element — and putting the site's wording into the procedure would be writing a spec with selectors in it. So the outcome is recorded **against the step not working**: the procedure says *"the site rejects the login — report it with the reason the site shows"*, and the recorder derives the condition from the step's own checkpoint. It fires when the page is **announcing something new** (ARIA `role="alert"`) **and** that checkpoint **does not hold** — both, because either alone lies: a page that failed to load is a hard failure, and an alert already on screen is furniture. The message the caller gets is read from the page as it happens: *"Epic sadface: Sorry, this user has been locked out."*

**What gets cleared out of the way isn't a step.** A modal the agent dismissed while recording becomes a *global* recoverable handler: dismiss and retry, but only when it's actually there. As a step it would fail on the run where nothing needed dismissing; attached to one step it would miss the run where it blocks a different one. Live, the blocked step times out, the handler matches, the modal goes, the step runs again (`logs/11_*`); with the control removed instead, every locator misses and it stops with a screenshot and the locator trace (`logs/12_*`).

**Bad inputs never reach the browser**: an unknown item, a duplicate, or a zip that isn't a real US zip code is refused in milliseconds against `allowed_values` recorded from the live catalog and a bundled 41,488-entry list, without echoing sensitive values.

**Waiting and retrying are derived, not guessed.** Checkpoints come from what the action changed — a heading that appeared, else a URL path change — with step timeouts of `max(10s, 3× what discovery observed)` and a whole-run budget measured the same way; time waiting for a person doesn't count against it. Playwright's `networkidle` only covers a page's first load, so a custom same-site tracker answers "has this page stopped talking to the server". An unexplained failure is retried once, because pages are flaky. A step that changes data never is: a blind retry on the Finish button is a second order.

---

## 4. Heterogeneity & multi-tenant

**Surfaces.** `SurfaceAdapter` is the only thing that knows what a browser is: find, click, type, extract, check a condition, read what the page is announcing. Discovery and replay speak in roles, names and conditions. A desktop UI-Automation adapter would implement the same verbs against control patterns and the artifact wouldn't change shape, because a `Locator` is "role + name" — what accessibility APIs give you on both. The seam is real; only Playwright is behind it. Frames are half-done in a revealing way: replay can descend an `iframe_path`, discovery never records one. Cascades exist because the pretty option often isn't there: against ParaBank (the original target, ADR-001/012 — table layouts, no test ids) and the mock app, the system works by falling down the cascade and scoping to a container.

**Multi-tenant.** Many institutions run the same vendor product, branded and versioned differently. The artifact separates what varies across tenants (entry point, domains, branding-dependent strings) from what doesn't (the shape of the flow, the roles of the controls, the checkpoints), so reuse is a *specialization* rather than a re-recording: a sparse overlay keyed by step id, applied at load time. Drift detection falls out of what replay already reports — which rung of the cascade resolved. A tenant sliding down to the structural path is one whose next release will break: a signal to re-record *before* a failure. None of the overlay machinery is built (§7).

---

## 5. Escalation & handoff

**Who the operator is.** These are internal back-office tools: the person who asked for the work is waiting for the answer, so "is a human available?" is always yes — no availability flag, no queue, no routing (ADR-022). What varies is whether they need to *see* anything, and the artifact already knows, because discovery recorded which steps commit something. A capability that commits opens a browser and asks; one that doesn't runs headless and answers.

**Detecting "stuck"** isn't one condition, so it isn't one check. In replay: a hard failure, retries exhausted (max 2 per step), or any step marked `needs_confirmation` — an approval, not a fault. In discovery: before any `changes_data` click; when the agent gives up; after three failures in a row; when the step or time budget runs out, where "keep going" grants more, twice at most. With no way to ask, an irreversible click is **refused outright** — discovery drives a live application and gets one attempt at it.

The request carries the capability and goal, the step, live URL and title, a masked screenshot, redacted inputs, and why it stopped — published *after* the state machine pauses, since the other order lets an operator answer a run that's still going.

**Control transfer is real.** The automation parks on an event inside `channel.request()`; the operator works in **that same window**, same session, same cookies. Taking over deliberately doesn't wake the run, and the request stays open while they work. Hand-back is an explicit signal, and it states intent — "I did the step" or "I cleared it, run it again"; a bare resume after a takeover is rejected. Replay checks that claim against the step's own checkpoint:

| Checkpoint | Operator says | Replay does |
|---|---|---|
| holds | anything | records "completed by the operator (verified)", **skips the step** |
| doesn't hold | "I did it" | **stops** — two signals disagree, and this step changes data |
| doesn't hold | "retry" | runs the step; the page proves it didn't happen |
| none at all | "retry" on a committing step | refuses — nothing could confirm it hasn't already happened |

The checkpoint pays for itself twice: success condition during normal replay, reconciliation probe after a handoff. It's what stops a second order being placed.

**What the human did is recorded, not summarized.** Page listeners report clicks and entries while the run is stopped, described as `clicked button "Finish"`, with passwords reporting only *that* they were filled. The tagging by state came from watching someone use it: operators click the button first and the console second, so recording only during `HUMAN_ACTIVE` produced evidence saying "a takeover happened" with nothing in it — acting before pressing Approve now counts as a takeover, since calling it a plain resume means redoing what they did. In **discovery** a takeover is a recorded **gap** (`meta.human_assisted`), never steps: we recorded what they did, not how to find the controls they used. The console meanwhile polls a masked screenshot — a view, not a control; acting happens in the browser window.

**Where we'd ship this: a desktop application with the browser inside it.** The local setup isn't a stand-in for a cloud service; it's the deployment we envision. These applications are reachable only from inside the institution's network, and the session is the operator's own — SSO, MFA, sometimes hardware-bound. Running on their machine means acting inside a session the bank already trusts, with no inbound path into their estate and no regulated content leaving the building; it's also the only way to reach the desktop surfaces of §4. Most of all it makes the hardest requirement trivial rather than clever: "let the human operate the same live session" is a distributed-systems problem when the browser is in a pod and the human is elsewhere, and no problem when the session is on their desk. The mechanism built here *is* the production one.

**What the product looks like.** A chat interface: someone types "place an order for these two items", the agent invokes the capability, and usually they just get an answer. When a decision is needed a browser pane opens beside the conversation and they approve — or do it themselves and hand back. The console here is that pane with worse clothes: same intervention, same live view, same state machine, on a port instead of docked beside a chat.

**Honest limits, as built.** Control transfer is *cooperative* — the automation is genuinely parked, but nothing physically stops a human clicking mid-automation. Capture is per page, so a tab the operator opens themselves isn't recorded. No answer in 300s aborts the run with evidence rather than hanging.

---

## 6. Safety

**Allowlist.** Every navigation is checked against the artifact's allowed domains before Playwright is asked to go there; action types are allowlisted; step count and wall-clock time are enforced per run, human time excluded.

**Irreversible means irreversible.** `changes_data` is set only for what can't be undone — an order, a payment, opening an account; adding to a cart is reversible and stays unattended. Such a step is blocked with no way to ask, approved-then-executed with one, never blind-retried, and never re-run after a takeover unless the page proves it didn't happen.

**Sensitive data has one narrow path.** `--secret` values never reach the model — it sees `{{inputs.password}}` and types the placeholder, filled in at execution — and are redacted in logs, results and intervention requests, masked out of screenshots, and recorded in the artifact only as *names* marked sensitive. The customer's name and zip are treated exactly like the password, which is why they're supplied per call: the system never holds them. `grep -rE "Ada|Lovelace|94105|secret_sauce" evidence/` returns nothing.

**The model is not the guardrail.** It doesn't decide whether an action is permitted, whether an input is valid, or whether approval was given. It proposes; the guard, the validator and the state machine dispose.

---

## 7. Cuts

In rough order of what I'd do next.

1. **Tenant overlays.** §4 designs the specialization model; nothing implements it. Next: a sparse per-tenant patch keyed by step id, plus a drift report from which cascade rung resolved.
2. **A second surface, and frames.** Only Playwright sits behind `SurfaceAdapter`; a desktop adapter would be the honest test of it. Closer to hand: teach the recorder to record `iframe_path`, which replay already follows.
3. **Recorded input validation goes stale.** Item names are checked against the catalog *as it was at discovery time*, so a genuinely new product is refused until re-recording. A freshness check against the live page would fix it.
4. **The operator console is minimal by intent**: polling, one run at a time, no auth, no queue, no audit trail beyond the run's evidence. The desktop packaging and chat surface of §5 are a design, not a build.
5. **No confidence scoring or draft→approved gating.** It fits the existing result data, but it's breadth rather than depth.
6. **Success conditions are derived, not proposed.** The recorder infers them from what changed — Place order's is the "Thank you for your order!" heading — but one proposed by the model and verified by the recorder would be stronger.

**What I'd want a reviewer to press on:** the cooperative control-transfer model (§5); whether deriving a rejection handler from a negated checkpoint is principled or merely clever (§3); and whether `alert_visible` is surface-level or SauceDemo knowledge in a trench coat. I think it's fine — ARIA alerts are how any web surface announces a refusal, the adapter falls back to the recorded message when a surface announces nothing, and the mock app exercises the same path — but it's the sharpest edge in the design.
