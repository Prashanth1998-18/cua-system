# Architecture Decision Log

Decisions made during design of the Computer-Use Automation System for interface.ai.

This is a living document: it's updated as design discussions happen, not written after the fact. Later ADRs carry a **Status** — *Decided* (agreed, and built or being built), *Proposed* (agreed direction, not built yet), or *Open* (still being discussed). Open questions are collected at the end.

---

## ADR-001: Target Application — ParaBank

**Status**: Superseded by ADR-012 (switched to SauceDemo after live testing). Kept for the reasoning and because ParaBank shaped several design decisions.

**Decision**: Use ParaBank (parabank.parasoft.com) as the target application.

**Context**: Need a non-trivial multi-step flow (search > detail > action) on a publicly accessible app. No real bank systems.

**Options considered**:
- SauceDemo (saucedemo.com) — test e-commerce site with built-in error users. Reliable, but shallow: no search, limited data extraction, not banking-themed.
- DemoBlaze (demoblaze.com) — electronics store with categories and checkout. Has order IDs to extract, but still not banking.
- ParaBank (parabank.parasoft.com) — banking demo with account lookup, fund transfers, bill pay, loan applications.
- Local mock banking app — full control, can make DOM intentionally hostile. But "automating your own app" is less compelling as evidence.

**Why ParaBank**:
- Banking domain maps directly to interface.ai's problem space (back-office banking apps).
- Multiple distinct workflows to record as capabilities (account lookup, fund transfer, bill pay).
- Real data extraction: account numbers, balances, transaction IDs, confirmation messages.
- The assignment's own examples ("look up member 12345 and read their current savings balance") map almost verbatim to ParaBank's features.
- Registration requires SSN field, which is useful for demonstrating PII redaction in the safety layer.
- Publicly accessible, stable (maintained by Parasoft for years), no credentials needed beyond self-registration.

**Risk**: Site could go down. Mitigated by the fact that it's been stable for years and is actively maintained as a test/demo product.

---

## ADR-002: Page Observation Strategy — Accessibility Tree Primary, Screenshots Secondary

**Decision**: Use the browser's accessibility tree as the primary way the AI "sees" the page. Screenshots are captured alongside for observability and as a fallback.

**Context**: The AI agent needs to observe page state during discovery. The choice here directly affects what locator data we capture for replay.

**Options considered**:
- Pure screenshots + coordinates (pixel-based, like Anthropic's computer use API).
- Raw DOM / HTML extraction.
- Accessibility tree (the structured representation browsers expose for screen readers).
- Hybrid: accessibility tree primary, screenshots secondary.

**Why accessibility tree**:
- Gives us structured element data (role, name, value, state) that maps directly to Playwright's `getByRole()` locators — the most robust locator strategy for replay.
- More stable than raw HTML across UI changes. Legacy apps with ugly DOM still expose a usable a11y tree.
- Cheaper per LLM call than sending screenshots (text vs. image tokens).
- Extends to desktop apps: both Windows (UI Automation) and macOS (Accessibility API) expose similar trees, so the same observation approach works beyond browsers.
- Screenshots alone give you coordinates, which are fragile for replay (window size, resolution, dynamic layout).

**Trade-off**: Some visual-only information is lost (colors, layout, icons without labels). Screenshots are captured as supplementary evidence, and the system can fall back to screenshot-based observation if the a11y tree is insufficient (e.g., canvas-rendered content).

---

## ADR-003: Locator Strategy — Priority Cascade

**Decision**: Each step in the artifact stores multiple locator strategies, tried in priority order during replay.

**Priority order**:
1. Accessibility role + name (`getByRole('button', { name: 'Login' })`)
2. Text content (`getByText('Submit Application')`)
3. CSS selector (`#login-button`, `.account-table tr:nth-child(2)`)
4. XPath (`//input[@name='username']`)

**Why cascade over single locator**:
- No single locator strategy is reliable across all surfaces. Role-based locators are most stable but not every element has a meaningful accessible name. CSS selectors are precise but brittle to DOM changes.
- The cascade degrades gracefully: if the best locator breaks (e.g., an accessible name changes between app versions), the next one picks it up.
- This directly supports multi-tenant reuse: two tenants running the same vendor app with different branding may change button text but keep the same DOM structure, or vice versa.
- During discovery, the recorder captures ALL available locator strategies for each element, so the artifact is pre-populated with fallbacks.

---

## ADR-004: Error Taxonomy — Three Classes

**Decision**: Classify all non-success outcomes into three categories with distinct handling.

**Categories**:
- **BusinessOutcome**: A legitimate alternative result the caller needs to know about. Examples: "account not found", "insufficient funds", "duplicate transaction". Returned to the caller as structured data — this is a valid answer, not an error.
- **Recoverable**: A transient or expected interruption the system can handle automatically. Examples: dismiss an unexpected dialog, retry a slow page load, re-find a stale element. The system retries/recovers and continues.
- **HardFailure**: The system cannot proceed. Examples: all locators for a step exhausted, page in a completely unrecognized state, timeout after max retries. Stop execution, capture diagnostic evidence, escalate or return error.

**Why this matters**: The assignment calls conflating business outcomes with failures "the most common design mistake." A banking system that crashes on "no such account" instead of returning it as data is broken. This taxonomy is the contract between the replay engine and its caller.

---

## ADR-005: Tech Stack

**Decision**: Python 3.11+, Pydantic v2, Playwright (async), an LLM reached through the `openai` SDK, FastAPI for the operator console.

> **Correction.** This ADR originally said "Claude API (Anthropic SDK)". That was the intention when it was written and it was reversed a few decisions later by ADR-008, which chose the `openai` SDK pointed at an OpenAI-compatible endpoint — `cua/discovery/agent.py` imports `AsyncOpenAI` and nothing else. The stale wording survived here longer than it should have; the rest of this ADR is corrected to match what was built.

**Rationale**:
- **Python**: First-class support for Playwright, the model SDKs, and Pydantic. Fastest path to a working system.
- **Pydantic v2**: Typed models with built-in JSON serialization, validation, and schema generation. The artifact schema IS Pydantic models — no separate schema definition layer needed.
- **Playwright (async)**: Best Python browser automation library. Built-in accessibility tree snapshots, screenshot capture, role-based locators, iframe handling, and CDP support (needed for human escalation).
- **An LLM with tool calling**: structured tool calls for the discovery agent loop. The model picks from defined tools (click, type, extract, done), returning structured actions rather than free-text instructions.
- **FastAPI**: a lightweight async web framework for the operator console. In the end it serves a plain HTML page that polls `/api/status`; no WebSockets are used.

**What we're NOT using**: No task queues, no databases, no containerization, no microservices. Single-process CLI. The assignment explicitly says "simpler is fine if justified" and warns against building scaling infrastructure.

---

## ADR-006: Human Escalation — Real State Machine, Minimal Operator Console

**Decision**: Implement a real pause/resume/handoff state machine. The operator console UI is mocked (simple web page) but the control-transfer mechanism is real.

**State machine**: `automation_active` <-> `paused` <-> `human_active`

**How it works**:
- When stuck: the system pauses Playwright (browser stays alive), captures an InterventionRequest (screenshot, current step, failure reason), and sends it to the operator endpoint.
- The browser's CDP remote debugging port is exposed so a human can connect to the exact same browser session.
- The mock operator UI shows the intervention context and provides Resume/Abort controls.
- On resume: the system re-snapshots the page (to see what the human changed), logs the human's intervention, and continues from the next step or re-attempts the failed step.

**Why keep the UI minimal but make the mechanism real**: The assignment explicitly says "mock the operator UI if needed, but make the handoff mechanism and the control-transfer model real." A full real-time co-browsing console is out of scope. The state machine, the pause/resume lifecycle, and the context preservation across handoff are the load-bearing parts.

**What it actually became** (this ADR was written before any of it existed): the console is thin but not mocked. It serves a real intervention with a masked screenshot and redacted inputs, polls a live view of the page (`/api/live.png`), and offers Approve / Take over / Abort — not just Resume/Abort. A takeover hands the operator the same live browser window and requires them to state what they did when handing back (ADR-019), and everything they did is recorded. What remains deliberately minimal: polling rather than push, one run at a time, no auth, no queue.

---

## ADR-007: Architecture Shape — Single-Process CLI

**Decision**: The system is a single-process CLI. It began with three commands — `discover`, `replay`, `operator` — and ended with five: `validate` (check an artifact with no browser and no model) and `capabilities` (the catalog an agent would call) were added later.

**Why not services/microservices**:
- The assignment warns: "We do not reward feature breadth, framework name-dropping, or building scaling infrastructure."
- A single process is simpler to run, debug, and review. The evaluator runs one command and sees results.
- The boundaries that matter (surface adapter, replay engine, safety layer) are module boundaries, not service boundaries. They could become services later without changing the interfaces.
- The only separate server is the FastAPI operator console, started in-process on the first intervention and shut down with the run (ADR-022).

---

## ADR-008: LLM Integration — OpenAI-Compatible Endpoint, Not Claude SDK

**Decision**: Use the `openai` Python SDK pointed at an OpenAI-compatible endpoint, rather than the Anthropic Claude SDK.

**Context**: Need an LLM for the discovery agent loop (observe page, decide action, execute). The choice of SDK affects provider lock-in and swappability.

**Why OpenAI-compatible**:
- Massive provider network: OpenAI, Azure OpenAI, Groq, Together, Fireworks, Ollama (local), and dozens more all expose OpenAI-compatible APIs.
- Swapping providers is a config change (`base_url` + `api_key`), not a code change.
- The Anthropic SDK locks you to Claude models. The `openai` SDK with a configurable base URL keeps options open.
- For a production system at interface.ai's scale (hundreds of tenants), provider flexibility matters: different tenants might need different models, or you might want to fall back across providers.

**Trade-off**: Claude's computer use API has purpose-built support for screenshot-based interaction. We lose that. But since our primary observation channel is the accessibility tree (ADR-002), not screenshots, this trade-off is acceptable. Any vision-capable model (GPT-4o, Claude via OpenRouter, Gemini) can handle the screenshot fallback path.

---

## ADR-009: Agent Framework — Raw SDK, No Framework

**Decision**: Use the `openai` Python SDK directly with a hand-written agent loop. No agent framework (pydantic-ai, LangChain, browser-use, etc.).

**Context**: Need to implement the observe > decide > act loop for discovery. Several Python agent frameworks exist that could reduce boilerplate.

**Options considered**:
- **Raw `openai` SDK**: Write the tool-use loop manually. Define tools as JSON schemas, call the model, execute the returned tool call, feed result back, repeat until goal met or stop condition.
- **`pydantic-ai`** (by the Pydantic team): Agent loop management, structured outputs, OpenAI-compatible. Adds a dependency but reduces boilerplate.
- **`browser-use`**: Open source lib specifically for LLM + browser automation. Does most of what we need out of the box, but then we're explaining someone else's architecture.
- **LangChain / LangGraph**: Heavy dependency, complex abstractions. The assignment warns against framework name-dropping.
- **Vercel AI SDK**: Strong agent framework but TypeScript-only, which would mean switching from Python and losing Pydantic for artifact schemas.

**Why raw SDK**:
- The agent loop is genuinely simple: call model with tools, execute tool, feed result back, repeat. A framework doesn't reduce meaningful complexity here.
- Writing the observe/decide/act loop demonstrates understanding of it; using someone else's agent runner demonstrates dependency management.
- No black-box behavior. Every decision point is visible in the code.
- The assignment explicitly warns against framework name-dropping. A thinner stack shows better judgment.
- One less dependency to install, debug, and explain.

**Trade-off**: Slightly more boilerplate for tool definition and the retry/error loop. Worth it for clarity and ownership of the design.

**How it aged**: the *decision* held — there is still no framework, and the model call is a dozen lines. The *loop* did not stay small. `DiscoveryAgent.discover()` is around 470 lines, and almost none of that is the agent: it is escalation before irreversible actions, stuck detection, step and time budgets, recording checkpoints, and the provider-quirk handling in `_create_completion`. Earlier drafts of this repo described it as "a ~50-line loop" long after that stopped being true, which is exactly the kind of claim a reviewer should check and I should not have left standing.

---

## ADR-010: Language — Python Over TypeScript

**Decision**: Python 3.11+ for the entire project.

**Context**: TypeScript (with Vercel AI SDK + Playwright Node API + Zod) was a viable alternative stack.

**Why Python**:
- **Pydantic** is the strongest argument. The artifact schema is the focal point of evaluation. Pydantic v2 gives typed models, JSON serialization, validation, and JSON Schema generation from a single class definition. Zod can do similar things but Pydantic is more natural for data modeling.
- The AI/ML/LLM ecosystem is Python-first. The `openai` SDK, Playwright async API, FastAPI all have mature Python support.
- Simpler for evaluators to read and run. `pip install` + `python -m cua discover` vs. Node/npm setup.

**Trade-off**: TypeScript has better type safety at compile time. Python compensates with Pydantic runtime validation and type hints for IDE support.

---

## ADR-011: Goal Specification — `workflows.md` as the Source of Discovery Goals

**Decision**: Maintain a `workflows.md` document with one plain-English entry per capability we want discovered (read balance by account ID, open an account, check transactions, transfer funds, ...), each spelling out the entry point, the exact page/element sequence a human would follow, which values should be parameterized vs. hardcoded, and what "success" concretely means. `cua discover` goals are drawn from this document rather than invented ad hoc per run.

**Context**: In live testing, a one-line free-form `--goal` string repeatedly let the model take the path of least resistance instead of the intended one. Concrete examples from this session:
- Asked to "look up the balance of account number 12345," the agent extracted the *entire* accounts overview table and eyeballed the number for 12345 out of the raw text, rather than clicking through to that account's own Account Details page. The resulting artifact recorded no `account_id` input at all — it was functionally identical to "dump the whole table," just with a more specific-sounding goal.
- Asked to "open a new savings account, then transfer funds from account 12456," the agent interpreted "transfer funds" as a *separate* Transfer Funds page action after account creation, when the actual intended workflow was the minimum-deposit funding transfer built into the Open New Account form itself. Both readings are defensible from the sentence alone.
- Getting the account-opening workflow right at all took several goal-wording iterations even before the interpretation issues above, purely to get the agent to attempt the right sequence of pages.

**Why it's impractical to rely on the model's judgment alone**: The model has no ground truth about a given site's actual navigation structure, so a vague goal is genuinely ambiguous to it in ways it doesn't reliably surface as ambiguous back to the caller — it picks a plausible interpretation and reports success. A capability discovered this way looks fine (`cua validate` passes, replay works) while quietly not being the capability that was asked for. This is worse than an outright failure, because it doesn't get caught until someone reads the recorded steps.

**Why a plain-English spec doc over other fixes**:
- **Structured goal schema (JSON goal spec) instead of prose**: more rigid and parseable, but the payoff over well-written prose is small — the goal still becomes a natural-language message to the LLM either way, so the real fix is precision of that language, not its serialization format.
- **More tool-calling guardrails in the agent loop** (e.g. forcing `select_option` for dropdowns, as already done — see `decisions.md` history and `REPORT.md` §4): necessary but insufficient. It fixes *how* an action executes once chosen; it doesn't fix the agent choosing the wrong action (or a too-easy one) in the first place.
- **`workflows.md`**: puts the precision where the actual ambiguity lives — the goal statement — and produces a durable, human-reviewable artifact that documents the target app's known workflows independent of any one discovery run. A wrong or ambiguous spec entry is easy for a human to spot and fix before burning a discovery run on it; a wrong interpretation buried inside a passing artifact is not.

**Mechanism**: today this just means copying the relevant `workflows.md` entry into `--goal` by hand for each `cua discover` run — no code change required, since `--goal` is already a free-text string. A natural follow-up (not yet built) is a `--workflow-doc`/`--workflow` pair of CLI flags that load a named section from the file automatically, and optionally surface the rest of the document to the agent as background site context even when it isn't the active goal.

**Trade-off**: `workflows.md` has to be written and kept in sync by a human who understands the target app, which is more upfront effort than typing a one-line goal. For a system whose whole premise is capturing *known* back-office workflows as reusable capabilities, that's the right place to spend the effort — the alternative is discovering the same ambiguity live, silently, inside an artifact that looks correct until someone checks its steps.

---

## ADR-012: Target Application — Switch from ParaBank to SauceDemo

**Decision**: Use SauceDemo (www.saucedemo.com) as the target application instead of ParaBank. Supersedes ADR-001.

**Context**: ParaBank was chosen for its banking domain fit (ADR-001). Building and testing against it live showed that its public demo is a shared, mutable, unstable environment, which makes it a poor base for evidence that has to be reproducible:
- **Shared data changes underneath you.** Accounts used earlier in testing (e.g. 12345) disappeared, balances and transaction counts shifted between runs, and accounts created by one run showed up in others. Other people use the same demo and it gets reset.
- **It stopped rejecting logins.** At one point any username/password, including a nonexistent user, was logged straight into the Accounts Overview page. The invalid-credentials case (a core "business outcome vs. error" example) could no longer be demonstrated at all.
- **Intermittent server errors.** Pages showed "An internal error has occurred and has been logged," including for valid logins.

The assignment evaluates robustness and error handling, and human escalation, right after the core loop. Those need error states that can be triggered on purpose and reproduced, which ParaBank could no longer provide.

**Options considered**:
- **Stay on ParaBank and inject failures** (e.g. a replay flag that makes a step's element unfindable). Allowed by the brief ("an injected/simulated failure"), but every error demo would be artificial, and the target's instability would still undermine the success-path evidence.
- **Build a local mock bank app.** Full control and could be deliberately hostile (legacy markup), but "automating your own app" is weaker evidence, and it's a detour from the core work.
- **SauceDemo.** Sauce Labs' own practice site.

**Why SauceDemo**:
- **Stable and deterministic.** Fixed product catalogue and no shared database, so the same replay gives the same answer tomorrow.
- **Documented, reproducible error states built in.** These map directly onto the assignment's runtime conditions:
  - `locked_out_user`: login refused with a clear message (a permission denial, i.e. a business outcome).
  - `performance_glitch_user`: deliberately slow login (tests waiting and checkpoint timeouts).
  - `problem_user` / `error_user`: parts of the app misbehave or fail (real hard failures to escalate, not injected ones).
  - Checkout form validation errors ("First Name is required").
- **Matches the brief's own example.** The assignment lists "add a specific item to the cart and reach the checkout review page" as an example goal for a public proxy target.
- **A natural irreversible action.** "Finish" places the order, so the risky-action handling (blocked without a human, approved through the operator console) gets exercised by a real workflow rather than only by unit tests.

**Trade-offs**:
- **ADR-001 rejected SauceDemo as "shallow: no search, limited data extraction."** Partly still true: there's no search box, and the data is simpler than bank accounts and transactions. But there's still real structured data to extract (the product list, cart contents, and the checkout overview's item total, tax and total), and the multi-step login → cart → checkout → confirmation flow is the kind the brief asks for. In hindsight, ADR-001 weighed data richness and domain fit over something the evaluation weights more heavily: being able to trigger error states on purpose and reproduce the results.
- **Loses the banking domain fit.** Accepted: the brief explicitly allows any public proxy target, and none of the system is banking-specific.
- **Loses the live "legacy markup" showcase.** SauceDemo is a clean modern single-page app with test IDs, unlike ParaBank's unlabelled form fields and server-rendered tables. Several design decisions came directly out of ParaBank and stay in the system and the write-up:
  - the model points at elements by snapshot ref and the recorder builds locators itself (ParaBank's login fields had no accessible names);
  - value-identified elements are located only by their value (the "first row" bug);
  - hidden tables count as zero rows ("No transactions found");
  - waiting for the real server response instead of Playwright's first-load-only network idle.
  The system doesn't rely on test IDs, so it's no less capable on legacy surfaces for having been demonstrated on a clean one.
- **Rework.** New procedures in `workflows.md`, new discovery runs and evidence. One code change: SauceDemo's page titles aren't semantic headings, so checkpoints fall back to "the URL path changed" (e.g. `/inventory.html` → `/cart.html`) when no new heading appears. *Built.*

---

## ADR-013: SauceDemo Capabilities

**Status**: Decided and recorded — all three are in `evidence/artifacts/`, plus a fourth against the mock app (ADR-023)

**Decision**: Record three capabilities against SauceDemo, each from a short plain-English procedure in `workflows.md` (ADR-011):
- **Browse catalog**: log in, optionally choose one of the four sort orders (name A–Z / Z–A, price low–high / high–low), and return every product's name, description and price. This also answers "how much do X and Y cost?": the caller reads X and Y's catalog prices from the list. The catalog is public, so returning all six products has no data-minimisation cost.
- **Total with tax**: only for when a user explicitly asks for the cost *including tax*. Log in, add the requested items to the cart, fill in the checkout details (provided by the caller), and return each item's price plus the item total, tax and total from the review page. Then Cancel, so no order is placed.
- **Place order**: the same as Total with tax, but Finish (after operator approval, ADR-015), then return the confirmation.

A plain "how much does X cost?" never goes through checkout. Nobody asks for the estimated tax unless they say so, and catalog prices answer the question without touching the checkout form.

**How the application behaves** (as described by the user, who has been checking the site directly):
- Six products; no filters; four sort options. One of each item per order: there's no quantity. Items can be removed from the catalog page or the cart.
- The catalog shows per-item prices only. The cart shows per-item prices only, no subtotal. The review page shows the item total, tax and total.
- Checkout asks for first name, last name and zip, then shows the review page with Finish and Cancel. Cancel returns to the catalog and keeps the cart as it was.
- An invalid zip silently sends you back to the cart, with no error message.
- The thank-you page after Finish offers "Back Home" and a PDF download.

**Details**:
- Callers pass **exact product names** (e.g. "Sauce Labs Backpack"). Discovery records the six names as the allowed values, so a typo is rejected up front with the list of valid names (the same mechanism as dropdown options).
- Each replay starts with a fresh browser, so the cart is always empty at the start. Cancel keeping the cart doesn't leak between runs.
- **Place order doesn't download the confirmation PDF.** It returns the thank-you confirmation only; the PDF is skipped unless a user specifically asks for it.
- **Login** is parked (see Open questions). For now every capability logs in as `standard_user`, and a failed login is reported as an error.

---

## ADR-014: Variable-Length Item Lists

**Status**: Decided, built, and exercised live with one, two and three items (`evidence/logs/04_replay_total_*`)

**Context**: A request can name any number of products ("price check the backpack, the bike light and the onesie"). A recorded flow is a fixed sequence of steps, so a capability recorded with two items would always add exactly two.

**Decision**: Three additions:
1. **List inputs and a "repeat for each" step.** An input can be a list of strings (`items`). A step can repeat once per list element, with the element available as a placeholder. Discovery runs with a two-item list. The recorder notices consecutive actions that are identical except for which list element they're for, and folds them into one repeat step. That's done mechanically, without asking the model to reason about loops.
2. **Locating a button by its surroundings.** Every product's button is labelled "Add to cart"; what makes it the right one is that it's inside the card showing the product's name. A locator can say "the button named 'Add to cart' inside the element that shows `{{item}}`". The recorder builds this automatically when the model clicks an element whose label isn't unique on the page but whose surrounding card shows one of the input values. As with the "row for account X" fix, no position-based backup: if the product isn't there, the step fails instead of adding a different product.
3. **Reading repeated items that aren't a table.** The review page lists items as cards, not an HTML `<table>`, so the existing table reader doesn't apply. Extraction gains "read each repeated card as a row" (e.g. name and price per item), returned the same way table outputs are.

**As built**:
- **List inputs** are passed as JSON (`--input 'items=["Sauce Labs Backpack","Sauce Labs Bike Light"]'`). Before any browser work, replay rejects a list that's empty, repeats an item (clicking a product's button twice would add then remove it, since there's no quantity), or names an unknown product.
- **Allowed product names** are collected mechanically. When the recorder builds a card-scoped locator, it also reads the same field (the product name) from every card on the page, and those become the list's allowed values.
- **Card reading.** At discovery, the reader works out which elements repeat and records one card's selector in the output. Replay uses that selector directly, so a cart with one item (or none) still reads correctly; with only one card, "the thing that repeats" can't be spotted. Row keys are each value's `data-test` or class name (e.g. `inventory-item-name`).
- **Repeated steps** show up in replay's step trace as e.g. `click_05[1]`, `click_05[2]`.

---

## ADR-015: What Gets Escalated to a Human

**Status**: Decided and built: approvals, failure escalation, caller-provided details as sensitive inputs, and screenshot masking. *The `--escalate` flag referred to below no longer exists — see ADR-022; approvals are declared by the artifact and always available.*

**Decision**: Some steps never run unattended:
- **Irreversible actions need an operator's approval.** Placing the order (Finish) pauses the run and asks the operator to approve. Without an operator (`--escalate` not set), such a step is blocked, not auto-approved. *Built*: the discovery agent marks clicks that change data (`changes_data`), and replay asks for approval before running them.
- **Customer details are provided by the caller and typed automatically.** First name, last name and zip are inputs to the capability, collected from the user beforehand; the checkout form is filled without a human. *Decided* (this replaced an earlier idea of having the operator type the details into the live browser so the system never held them; that made every checkout-based request need a person present).
  - Names and zip are personal data (together they narrow down to a person), so all three are **sensitive inputs**: redacted in replay logs and results, and during discovery the model only ever sees placeholders (the same `--secret` mechanism as the password). *Decided.*
  - If the site silently bounces back to the cart (e.g. a bad zip), the checkpoint catches it ("expected the review page, landed on the cart") instead of carrying on.
- **Screenshots mask customer details.** Right before any screenshot (discovery, replay failure evidence, and so also the operator console), form fields currently holding a sensitive input's value (password, names, zip) are blanked out using Playwright's screenshot masking. *Built.* Failure screenshots are also timestamped, so runs no longer overwrite each other's evidence.
- **Failures nothing explains are escalated** after one automatic retry. *Built.*

**Trade-off**: Place order can't run unattended (it always needs an approval); everything else can. Customer names do pass through the system, so they're handled as sensitive data rather than kept out entirely.

---

## ADR-016: Zip Code Validation

**Status**: Decided and built

**Context**: The zip is a caller-provided input (ADR-015). SauceDemo gives no error for an invalid zip; it silently returns to the cart. The site can't tell discovery what counts as valid.

**Rule** (decided): a valid, existing US 5-digit zip. A zip that fails this is rejected before the browser is opened, like any other invalid input.

**Options**:
- **Format only** ("5 digits"): a one-line check.
- **Existence**: ship a public list of real US zip codes (~40k) and check against it. More accurate, but it's a data file to maintain.

**Proposed**:
- **"Existing" needs the reference list.** Ship a public list of real US zip codes with the project and check against it. Format alone (5 digits) would accept zips that don't exist.
- **Not as "allowed values".** Dropdown inputs list every valid option in their error message; with ~40k zips that would be useless. The zip gets its own check with a short message ("'00000' is not an existing US zip code").
- **Where the rule comes from.** The site can't reveal it, so discovery can't find it by looking. The procedure in `workflows.md` states it in plain English, and the agent gets a way to record rules it's told about (as it already records "if the account isn't listed, that means not found"), so the rule lands in the artifact's input contract without hand-editing.
- The checkpoint still catches a silent bounce back to the cart, as a second line of defence.

**As built**:
- **The list:** GeoNames' US postal codes (41,488 zips, including PO-box zips, which the Census list leaves out), shipped as `cua/data/us_zip_codes.txt`. Licensed CC BY 4.0, so it's credited in the file header and the README.
- **How the rule gets into the artifact:** the discovery agent has a `set_input_rule` tool; the procedure states the rule and the agent records it. The input's `format` is set to `us_zip_code` in the artifact.
- **The check at replay:** it runs before the browser opens. Since the zip is sensitive, the error message never shows the value ("'zip' must be an existing 5-digit US zip code (got (hidden))").

---

## ADR-017: Login Model and SauceDemo's Special Accounts

**Status**: Decided (option A) and built. Only `locked_out_user` was exercised, as a business outcome (ADR-020); the other special accounts were not tested

**Context**: SauceDemo only accepts six usernames (`standard_user`, `locked_out_user`, `problem_user`, `performance_glitch_user`, `error_user`, `visual_user`), all with the same password. So "whose account logs in" decides what a failed login means:
- **A: a configured account the system logs in with**, acting on the requester's behalf. A failed login means the system's own setup is broken, so it's an error that escalates.
- **B: the customer's own credentials, per request.** A failed login is a legitimate answer about that customer ("account locked"), so it's a business outcome.

**Decided: A.**
- It matches the assignment's back-office framing: a staff or service account acts for a member, and the member never logs in.
- It keeps customer passwords out of the system entirely.
- SauceDemo's six shared test accounts aren't anyone's personal credentials anyway.
- Login details are configuration: the username is chosen per run (default `standard_user`), and the password is a secret, never shown to the model or logged.
- A rejected login is an **error** that escalates, with the site's own reason attached (e.g. "locked out" vs. "username and password do not match") so it's clear and debuggable.
- We don't validate usernames against the list SauceDemo prints on its login page; a real app wouldn't display its accounts. The site accepts or rejects the login, and we report what it said.

**How the special accounts are used**:
- **Discovery only uses `standard_user`**, so the recorded flow is the correct one.
- **The others are replay-time variants: the same recorded artifact, run under different conditions.** This is our stand-in for the assignment's multi-tenant question (the same vendor product configured differently): does an artifact recorded on one variant hold up on another? Each capability gets replayed under each account, and each run should end one of four ways:
  1. **success** (the variant didn't matter);
  2. **a clear, named error at the right step**;
  3. **escalation** after one retry, with a screenshot;
  4. **a wrong answer that looks like success**, which the system should never produce, and which gets reported honestly wherever it does.
- **What each account does**, as observed by the user; expected outcomes to be confirmed by running them:
  - `locked_out_user`: can't log in. Expected: a named login error, then escalation.
  - `performance_glitch_user`: the catalog page loads slowly. Expected: success, since checkpoints wait at least 10s or 3× what discovery observed.
  - `visual_user`: the catalog shows **wrong prices** (the cart shows the right ones), and some things are placed differently. The layout differences shouldn't matter, since elements are located by role, name and id, not appearance. The prices are outcome 4: a well-formed page with wrong data, which no checkpoint or locator can notice. Browse catalog would return them as a success.
  - `problem_user`: many assorted problems. They don't need cataloguing up front; they're run and whatever happens is recorded.
  - `error_user`: not tested yet; same treatment.

**Known limit (for the REPORT's cuts)**: UI automation faithfully reads what the app shows; it can't tell that a displayed price is wrong. A possible mitigation for capabilities that go through the cart is a consistency check: the cart price must match the catalog price, or stop and escalate. That would catch `visual_user`. Not planned for now.

---

## ADR-018: Escalation During Discovery, and Incomplete Discovery Runs

**Status**: Decided and built (revised after re-reading the brief). *The `--escalate` flag referred to below no longer exists — see ADR-022; escalation is always on and the browser opens when the artifact says a person is involved.*

**Context**: Recording Place order exposed two gaps:
- **Discovery had no approval gate.** Replay asks an operator before an irreversible step (ADR-015), but discovery didn't. The model, told by the procedure that Finish "always needs a human's approval first," refused to click it, said approval was needed, and ended with `success=false`. So the recorded flow stopped at the review page. On a real app, the opposite failure would be worse: a model that didn't refuse would place a real order during discovery.
- **A discovery run that didn't complete still saved an artifact.** The truncated `place_order.json` was written anyway, and it looked valid.

**First proposal (rejected)**: a terminal y/N prompt during discovery, on the grounds that the person running the command is the operator.

**Why it was rejected**: section 3.6 of the assignment names "the agent is stuck during discovery" as one of the three cases the escalation mechanism must cover, and asks for one mechanism throughout: an intervention request with context, control of the *same* live session, and a hand back. A terminal prompt answers "may I?" and nothing else — it can't show the operator the page, can't let them take over, and doesn't exist for the stuck case at all. Two escalation mechanisms in one system would also be a design smell we'd have to defend.

**Decision**:
- **Discovery uses the same escalation channel, console and state machine as replay.** `cua discover --escalate` serves the operator console alongside the run and opens a visible browser, exactly as `cua replay --escalate` does.
- **Three triggers.** (1) Before any click the model marked `changes_data`, as a confirmation request. (2) When the agent gives up (`done(success=false)`) — the operator can take over, tell it to keep going, or call the run off. (3) When actions fail repeatedly (three in a row) or the step budget runs out; on the budget case a "keep going" grants 10 more steps, at most twice, which is a real answer to "stuck in a loop" that doesn't just kill the run.
- **Without a channel, an irreversible click is refused outright.** Discovery runs against a live application and gets one attempt at it; it never commits anything unsupervised. The model is told why, in the tool result.
- **The model is told the system handles approvals** so it marks the click and proceeds rather than refusing in prose. The gate is ours, not the model's good manners.
- **What a human does instead is recorded as a gap, not as steps.** See ADR-019.
- **No artifact from an incomplete run.** If the agent never reaches the goal, nothing is written (logs are kept), and the command exits non-zero. A half-recorded flow is worse than none: it looks like a capability and replays as a wrong one.

**Consequences**: recording an irreversible capability needs a person present, which is the point. `--escalate` becomes the normal way to record anything that commits.

---

## ADR-019: What "Record What the Human Did" Means

**Status**: Decided and built

**Context**: The brief asks us to "preserve context and evidence across the handoff, and record what the human did" (3.6), and the scope note asks us to "capture the human's actions". We were recording the page state before and after, the URLs visited, and the operator's notes. That is context, not actions: it says where they ended up, not what they did.

**Decision**:
- **Capture clicks and field entries during the handoff.** Listeners are installed on the live page and report every event to Python, which keeps only those that occur while the state machine says `HUMAN_ACTIVE`. Filtering in Python rather than toggling the listeners means a mistimed toggle can't lose an action.
- **Describe them the way the rest of the system describes elements**: by role and accessible name ('clicked button "Finish"'), never by selector. Consistent with ADR-003, and readable in evidence.
- **Redact the same way logs do.** A password field reports that it was filled, never with what; any value matching a sensitive input is masked. The handoff must not become the leak that ADR-015 closed everywhere else.
- **Typing is coalesced per field.** Browsers only fire `change` on blur, so an operator who fills the last field and immediately hands back would otherwise leave no trace; we listen to `input` as well and keep one entry per field, holding the final value.
- **Handing back states intent.** "I did the step" or "I cleared the problem, run it again" — the console won't accept a bare resume after a takeover. Replay checks the claim against the step's recorded checkpoint and, where the two disagree, stops and escalates again with the disagreement as the reason. Neither signal is trusted alone: the operator can be mistaken about what went through, and a checkpoint can be absent.
- **In discovery, a takeover is recorded as a gap** (`meta.human_assisted`), not as steps. We recorded what the human did, not how to find the controls they used; deriving locators from that is how a capability ends up clicking the wrong thing on replay. Replay warns when it loads a capability with a gap.

**Alternatives considered**: turning the human's actions into steps (rejected: we have no recorded locator for them, and a flow that only worked because a person stepped in needs a better procedure, not a recording of their clicks); recording a video or a trace (real evidence, but not readable in a result JSON and not something replay can reason about).

**Amended after live use**: capture originally kept only actions taken while the state machine said `HUMAN_ACTIVE`. The first time someone other than me used the console, they clicked Finish in the browser window and *then* went to press the console button — which is the natural order — so their click landed while the run was merely `PAUSED` and was dropped. The record then claimed a takeover with nothing in it. Now anything that happens while the run is stopped is recorded, tagged with which state was in effect (`"while": "paused" | "human"`), and acting before pressing Approve counts as a takeover — otherwise replay treats it as a plain resume and performs the irreversible step a second time. The checkpoint guard catches that today, but only for steps that have a checkpoint.

**Console fixes from live use**: the request advertises the session's CDP URL, and the console linked straight at it. That port's root path serves an empty 200, so the operator got a blank tab. Resolving the per-target inspector URL from `/json/list` fixed the blank tab but not the underlying mistake, which was answering "show me the live session" with a debugger link: the URL Chrome hands back points at a *hosted* DevTools frontend over https that then has to open a plain `ws://` to localhost, so whether it loads depends on the browser, its mixed-content rules and network access. It is also the wrong tool — DevTools inspects, it doesn't operate.

So the console now **polls a masked screenshot of the live page** (`/api/live.png`, captured on demand, never cached), which answers the actual question an operator has. DevTools is demoted to a line of instructions (`chrome://inspect` → Configure → the address), for diagnosing *why* a run stopped. Control still happens in the browser window, which is the same session — and that is the part the brief calls the real handoff.

**Known limits**: capture is per page, so a tab the operator opens themselves isn't recorded; and control transfer is cooperative — the automation is parked while the human works, but nothing physically stops a human from clicking mid-automation. Both are stated in REPORT.md.

---

## ADR-020: A Refusal by the Application Is an Answer, Not a Failure

**Status**: Decided and built

**Context**: SauceDemo's `locked_out_user` can't log in; the site says "Epic sadface: Sorry, this user has been locked out." Until now the procedures treated a rejected login as an error to escalate, so replay reported a hard failure. That is the wrong class: nothing is broken, and no human is needed. The caller asked "what do you sell?" and the honest answer is "that account is locked out" — a legitimate "no" in the sense of ADR-004, like `account_not_found`.

**The problem**: the existing way to record a business outcome (`if_missing_outcome`) keys on *an element being absent*, which the model can point at during a successful run. A refusal is the opposite: an element that only appears when things go wrong, which a successful discovery run never sees. The model can't point at a ref it has never observed, and we won't put the site's error text into the procedure — that would be writing a spec with selectors in it (ADR-011), and inventing a locator the recorder never verified (ADR-003).

**Decision**: record the outcome against *the step not working*, and read what the application says when it happens.
- The procedure states the meaning in business language: "the site rejects the login — report 'login rejected' with the reason the site shows".
- The model sets `if_rejected_outcome` / `if_rejected_message` on that click. It does not need to have seen the refusal.
- **The recorder builds the condition from the step's own checkpoint**, which it already derived from what the action changed. The handler fires when the page is announcing something (`alert_visible`) **and** the checkpoint that proved the step worked does not hold (`also_requires`, the negated checkpoint). Both, because either alone lies: a stale alert isn't a refusal, and a page that simply failed to load is a hard failure, not a business outcome.
- **The message the caller gets is the application's own.** `report_page_message` makes replay read the live alert text and append it, so the result says "Epic sadface: Sorry, this user has been locked out." rather than a phrase we guessed at record time.

**Why `alert_visible` is not app-specific knowledge**: ARIA `role="alert"` and live regions are the standard way a page announces a state change, and they are already how we observe pages at all (ADR-002). The adapter looks for them generically; a surface that announces nothing returns "" and the caller falls back to the recorded message. Nothing in the engine knows what SauceDemo is.

**Consequences**: replaying any of the three capabilities as `locked_out_user` returns `login_rejected` with the site's words and exit code 2, not a failure and not an escalation. The same mechanism covers any "the app refused and said why" case: a declined transfer, an expired session, a permission denial.

**Not covered by this**: a caller asking for a product that isn't in the catalog is still rejected up front by input validation (ADR-014) rather than by the app, because the allowed values were recorded from the catalog. Cheaper, but it means a genuinely new product would be refused until the capability is re-recorded. Noted in REPORT.md as a known limit.

---

## ADR-021: Deployment Shape — A Local Desktop Application, Plus an In-Network Headless Runner

**Status**: Decided as the envisioned product; not built (packaging and the runner are cuts)

**Context**: Everything here runs locally: the browser, the operator console, the CLI. The question was whether that is a demo shortcut standing in for a cloud service, or the actual shape of the product. It matters most for escalation, where "let the human operate the same live session" is either trivial or a distributed-systems problem depending on where the browser is.

**Decision**: local is the product, for the attended half.

- **A desktop application with a managed browser inside it**, installed on the operator's workstation, is how we'd ship discovery, approvals and takeovers. The applications we automate are reachable only from inside the institution's network; the session is the operator's own (SSO, MFA, sometimes hardware-bound); regulated page content and credentials never leave the building; there is no inbound path into the estate to justify to a security review. It is also the only way to reach a native desktop surface, which a container elsewhere cannot drive.
- **The same engine runs headless inside the institution's network** for unattended replay, because unattended replay is the actual product and cannot depend on someone's laptop being awake. Shared control plane for the capability catalog, versioning and evidence.
- **The artifact is the contract between the two modes**, which is the return on having designed it as a contract rather than a step list.

**Consequences for escalation**: on the desktop, the mechanism built here *is* the production one — the session is already on the desk of the person being asked, so there is nothing to stream. For the headless runner, escalation routes instead of pausing in place: structured interventions (approve, supply a value, disambiguate) need no session access at all, and the rare pixel takeover goes through a broker the runner connects *out* to. That inverts one property for the better — a broker mediating input can refuse events unless the state machine says `HUMAN_ACTIVE` for that operator's scoped token, which makes control transfer enforced rather than cooperative, and records the human's actions authoritatively rather than observing them from inside the page.

**Partly superseded by ADR-022**: the headless-runner-plus-broker half of this was built on the assumption that runs happen with nobody around. They don't — these are internal tools and the requester is present — so the routed-escalation design is dropped. What stands is the desktop shape and the reasoning for it.

**Not built**: no installer, no update channel, no control plane. What exists is the engine the product would be built on.

---

## ADR-022: No Escalation Flag — The Artifact Declares It, and Someone Is Always There

**Status**: Decided and built

**Context**: `--escalate` was doing three jobs: declaring that a human was available, starting the operator console, and making the browser visible. Only one of those is the caller's business, and it was the wrong one to ask about.

**What was wrong with the framing**: the flag existed because I was picturing unattended runs with nobody around — a customer-facing service shape. These are internal back-office tools. The person who asked for the work is sitting there waiting for the answer, which is the same reason we'd ship this as a desktop application (ADR-021). "Is a human available?" is always yes. Designing an availability model around a case this product doesn't have was complexity with nothing behind it.

**Decision**:
- **Escalation is always available.** No flag, no unattended mode, no operator queue or routing.
- **The artifact declares whether a person is involved.** `Capability.requires_operator` is computed from the recorded steps (any step above `safe`), so it can't drift from them, and serialized, so a calling agent — or `cua validate` — can see it without walking the steps.
- **That declaration decides whether a browser opens.** A capability that commits something opens a visible browser, because approving a page you can't see is not approving it. One that commits nothing runs headless and silently.
- **A person can always ask to watch.** `--show-browser` makes any run visible; `--headless` forces the other way. Discovery is visible by default, being a supervised authoring session.
- **The console starts on the first intervention**, not at launch, so a run that never needs anyone never stands up a web server. When a capability declares `requires_operator`, it starts up front, since it is going to ask.

**Consequence worth noting**: a capability needing approval now refuses *before* opening the browser if nothing can ask — where previously the run got as far as the review page and blocked there, having done all the work. Everything it needed to know was in the artifact from the start.

**What this replaces in the earlier design**: ADR-021's routed-escalation sketch (a broker matching a remote operator to a streamed session) solves a problem this product doesn't have. It is cut from the report rather than kept as future work, because keeping it would suggest we intend to build it.

---

## ADR-023: A Mock Legacy App, So All Three Error Classes Are Real

**Status**: Decided and built

**Context**: ADR-004 claims a three-class error taxonomy. Against SauceDemo we could only ever produce two of them. Business outcomes, yes (`login_rejected`). Hard failures, yes. But **recoverable** — a condition you clear and carry on from — needs an application that *has* such a condition, and a polished demo site doesn't have one. The class was implemented in the engine and covered by unit tests, and never once produced by discovery or seen in evidence. A taxonomy where one branch has never run is a claim, not a design.

**Decision**: add `scripts/mock_legacy_app.py`, a small back-office screen that produces the missing conditions on demand.

- **Table markup, no test ids, inputs identified by name only.** The brief's "legacy web app" in miniature, so the locator cascade has to fall through to the rungs it usually doesn't.
- **A modal notice on some page loads** that genuinely covers the form and swallows clicks. Recoverable: dismiss it, retry, carry on. A decorative banner would have proved nothing — the first version of this was a plain div and automation sailed straight past it.
- **A `--break` mode that removes the control the recording depends on**, the way a release removes a button. Every locator misses and nothing on the page explains why: a hard failure, not a refusal.

**What it changed in the system** (each of these was a gap the mock exposed):
- **A cleared blocker is recorded as a *global* recoverable handler**, not as a step and not attached to one step. Not a step, because the recording would then always click "Dismiss" and fail on the run where nothing needed dismissing. Not one step's, because a modal blocks whatever comes next — which on a slower day is a different step. This is also the first thing to populate `global_error_handlers`, which had been in the schema unused.
- **A new recovery action, `dismiss_element`**, with the target recorded like any other element. "Dismiss the dialog" only covered native dialogs; the things that actually block back-office screens are in-page modals with a close button.
- **An alert only counts as a refusal if it's new.** ADR-020 keys business outcomes on the page announcing something — and this app's modal *is* `role="alert"`, so a banner that was already on screen was about to be read as "your action was refused". Replay now records what the page was announcing before the step and requires the announcement to have changed.

**Consequences**: all three classes are demonstrated end to end in `/evidence/` (`11_replay_statement_recovered` and `12_replay_statement_hard_failure`), on a second surface, with an artifact discovery produced. The mock is a test fixture, not a product surface — it ships in `scripts/` and is not part of the capability catalog.

---

## ADR-024: Locators Are Verified Against the Page Before They Are Written

**Status**: Decided and built

**Context**: Re-recording Browse catalog produced an artifact that failed on replay with "All locators exhausted", on a page where the element was sitting in plain sight. The cause: SauceDemo ships **two elements with `id="inventory_container"`**. The recorder saw an id, wrote `[id="inventory_container"]`, and Playwright refused it as ambiguous. Worse, the structural fallback anchors on the nearest id and so collapsed onto the same dead selector — one candidate, both useless, nothing else to fall back to.

**What this exposed**: the cascade (ADR-003) was ranked by *what usually survives change* but never checked against *what actually works right now*. An attribute being present is not evidence that locating by it finds anything. We had the live page in front of us at record time and weren't using it.

**Decision**:
- **Every candidate locator is tried against the page as it is recorded**, and kept only if it resolves to exactly one element *and* that element is the one the model pointed at (compared by handle identity, not by looks).
- **An id is only used when it identifies one element**, both as a locator and as the anchor for the structural path — checked in the page, not assumed.
- If nothing verifies, the unverified list is kept with a warning rather than recording an element with no locators at all: a reviewable recording beats a blank one.

**Consequences**: the Browse catalog extract now records `[id="contents_wrapper"] > div`, which resolves. More generally, an artifact's locators are no longer claims about the page — they are locators that were observed to work on it. Cost is a handful of extra round trips per element during discovery, which is the cheapest possible place to spend them.

**Reflection**: this is the second bug in this family (the first: a positional locator that silently acted on the wrong record). Both come from writing down a way to find something without checking that it finds it. Verification at record time is the general answer, and it should have been there from the start.

---

## Open Questions

- None at the moment.
