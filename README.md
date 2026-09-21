# CUA — Computer-Use Automation System

An LLM figures out how to do a job in a web application once. That run is recorded as a typed, versioned **capability artifact**. From then on, the artifact is replayed deterministically — no model in the loop — and a human is brought in when, and only when, the system shouldn't proceed alone.

```
   goal (plain English)
        │
        ▼
   cua discover ──────► capability artifact (JSON) ──────► cua replay
   LLM + Playwright         typed contract,                zero LLM calls
   observes, decides,       locator cascade,               validates inputs,
   acts, records            checkpoints, error handlers    verifies checkpoints
        │                                                       │
        └───────────────► operator console ◄────────────────────┘
                     pause · take over the live
                     session · hand back
```

**Target application:** [SauceDemo](https://www.saucedemo.com) (Swag Labs), a public e-commerce demo site. Three capabilities are recorded against it: browse the catalog, total an order with tax, and place an order. The procedures they were discovered from are in [`workflows.md`](workflows.md).

Design rationale lives in [`REPORT.md`](REPORT.md) (the seven required sections) and [`decisions.md`](decisions.md) (24 ADRs, written as the decisions were made).

## Requirements

- Python 3.11+
- An OpenAI-compatible LLM endpoint — needed **only** for `cua discover`. `replay`, `validate` and `operator` make no model calls, so everything except recording a new capability runs with no key at all.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium

cp .env.example .env     # then set CUA_LLM_API_KEY (only needed for `discover`)
```

`.env` is loaded automatically and is gitignored. Never commit it.

## Demo path

Everything below runs against the live site. The artifacts in `evidence/artifacts/` were produced by exactly these commands, so you can replay without recording anything first.

### 1. Replay a capability (no LLM, no key)

```bash
cua replay \
  --artifact evidence/artifacts/browse_catalog.json \
  --input username=standard_user \
  --input 'sort_order=Price (low to high)' \
  --secret password=secret_sauce
```

Prints each step, the locator that resolved it, and the extracted products. **Exit codes: `0`** success, **`2`** a business outcome (a legitimate "no"), **`1`** a failure.

### 2. See an exceptional state handled as an answer, not a crash

```bash
cua replay \
  --artifact evidence/artifacts/browse_catalog.json \
  --input username=locked_out_user \
  --input 'sort_order=Name (A to Z)' \
  --secret password=secret_sauce
```

```
Replay result: BUSINESS_OUTCOME
  Outcome: login_rejected
  Message: Report login rejected with the reason shown by the site:
           Epic sadface: Sorry, this user has been locked out.
exit code 2
```

Bad inputs are caught before the browser even opens — try `--secret zip=00000` or an item that isn't in the catalog on `total_with_tax.json`.

### 3. See the human in the loop

Placing an order is irreversible. The capability says so, so this needs no flag:

```bash
cua replay --artifact evidence/artifacts/place_order.json \
  --input username=standard_user \
  --input 'items=["Sauce Labs Backpack","Sauce Labs Bike Light"]' \
  --secret password=secret_sauce \
  --secret first_name=Ada --secret last_name=Lovelace --secret zip=94105
```

A browser opens, because approving something you can't see isn't approving it. The run walks the checkout, stops at the Finish step, and an intervention appears at **http://localhost:8080** with a live view of the page, redacted inputs, and why it stopped. You can:

- **Approve** — automation clicks Finish itself;
- **Take over** — click Finish yourself in that same browser window, then hand back saying *"I did the step"* or *"I cleared it, run it again"*. Replay checks your claim against the step's recorded checkpoint before doing anything, so an order never gets placed twice;
- **Abort** — the run stops and says so.

Capabilities that commit nothing — browse the catalog, total an order — run headless and silently, and no console is started. Pass `--show-browser` to watch one anyway. `cua validate <artifact>` tells you which kind you have before you run it.

`scripts/operator_takeover.py` does the takeover unattended (it drives the console API and the live session over CDP), which is how `evidence/logs/07_*` was produced.

### 4. Record a new capability

```bash
cua discover \
  --goal "$(sed -n '/## Browse catalog/,/^---/p' workflows.md)" \
  --url https://www.saucedemo.com \
  --name browse_catalog \
  --input username=standard_user \
  --input 'sort_order=Name (A to Z)' \
  --secret password=secret_sauce \
  --output evidence/artifacts/browse_catalog.json
```

Recording is supervised, so the browser is visible by default (`--headless` if you'd rather it weren't). If the agent reaches something irreversible it stops and asks you, through the same console — it never commits anything on its own say-so. A run that doesn't reach the goal writes no artifact: a half-recorded flow looks like a capability and replays as a wrong one.

Values passed with `--secret` never reach the model (it sees `{{inputs.password}}`), never appear in logs, results or artifacts, and are masked out of screenshots.

### The capability catalog

What an AI agent sees before it invokes anything — each capability as a callable contract:

```bash
cua capabilities            # human-readable
cua capabilities --json     # for an agent to consume
```

```
  place_order
    in:       username, first_name, last_name, zip, items, password
    out:      items_and_prices, item_total, tax, total, confirmation_message
    can say:  login_rejected
    approval: yes, a person decides before it commits
```

### Other commands

```bash
cua validate evidence/artifacts/place_order.json   # schema check, no browser, no LLM
cua operator --port 8080                           # the console on its own
```

## Running without live services

`discover` needs an LLM and the live site; everything else needs less than you'd think:

- **The test suite runs fully offline** — no network, no LLM, no API key. Browser tests use inline pages and a throwaway localhost server, escalation tests drive the real console app in-process, and discovery is tested against a scripted stand-in for the model.
- **`validate` and `capabilities`** read artifacts only: no browser, no network.
- **`replay`** needs no LLM and no key, but it does drive the real site. To exercise the replay engine without it, run `pytest tests/test_core.py` — the engine's control flow is tested against an in-memory surface.

## Tests

```bash
python -m pytest -q        # 122 tests, ~45s
```

No test touches the network or an LLM. The browser tests run headless against inline pages and a throwaway localhost server; the escalation tests drive the real console app over its HTTP API with a simulated operator; discovery is tested with a scripted stand-in for the model. `tests/conftest.py` isolates them from your `.env`.

## Project structure

```
cua/
  cli.py                  discover / replay / operator / validate
  config.py               env-driven config (LLM, browser, escalation)
  schema/
    artifact.py           the capability artifact — the contract between the two halves
    formats.py            named input rules (e.g. us_zip_code)
    results.py            ReplayResult / StepResult
  browser/
    session.py            Playwright lifecycle, screenshot masking
    adapter.py            SurfaceAdapter ABC + PlaywrightAdapter (locator cascade)
    network.py            "has the page stopped talking to the server yet"
    safety.py             domain allowlist, risk classification, PII redaction
  discovery/
    agent.py              the observe → decide → act loop (raw SDK, no framework)
    observer.py           accessibility snapshot → text for the model
    tools.py              the tools the model may call, executed against Playwright
    recorder.py           turns real elements into locators, actions into steps
  replay/
    engine.py             deterministic execution, input validation, escalation
    handlers.py           error-handler matching and recovery
    locator.py            {{inputs.x}} resolution
  escalation/
    state_machine.py      AUTOMATION_ACTIVE ↔ PAUSED ↔ HUMAN_ACTIVE (+ ABORTED)
    channel.py            raises an intervention, waits, records what happened
    actions.py            captures what the human did while they had control
    intervention.py       request/response models
    operator_server.py    the console (FastAPI + a plain HTML page)
  data/us_zip_codes.txt   bundled zip list (see Attribution)
scripts/
  operator_takeover.py    unattended stand-in for a person at the console
  mock_legacy_app.py      a legacy-shaped app that produces recoverable and hard failures
evidence/                 artifacts, logs and screenshots from real runs
workflows.md              the plain-English procedures discovery is given
decisions.md              24 ADRs
REPORT.md                 the design write-up
```

## Evidence

`evidence/` is the output of real runs against live SauceDemo — discovery logs with every model call, replay results, and screenshots.

| File | What it shows |
|---|---|
| `logs/01_discover_browse_catalog.log` | a genuine LLM-driven discovery run, start to finish |
| `logs/02_replay_browse_*` | the same capability replayed under two sort orders |
| `logs/03_discover_total_with_tax.log` | discovery of a flow with a variable-length item list |
| `logs/04_replay_total_*` | 1, 2 and 3 items; plus unknown product, duplicate item and bad zip, each refused before the browser opens |
| `logs/05_discover_place_order.log` | discovery pausing for approval before an irreversible click |
| `logs/06_replay_place_order_approved.*` | replay where the operator approved and automation acted |
| `logs/07_replay_place_order_takeover.*` | replay where the operator took the live session, clicked Finish themselves, and handed back — with their actions recorded |
| `logs/08`, `logs/09`, `logs/04_replay_total_locked_out.*` | a locked-out account reported as `login_rejected` with the site's own words |
| `logs/10_discover_request_statement.log` | discovery against the mock legacy app, recording a blocking modal as a recoverable condition |
| `logs/11_replay_statement_recovered.*` | that modal blocks a step on replay: matched, dismissed, step retried, run completed |
| `logs/12_replay_statement_hard_failure.*` | the control is gone: every locator misses, screenshot captured, escalated, operator aborted |

## The mock legacy app

SauceDemo is a polished site: it produces refusals and failures, but nothing you can *recover* from. So the third error class is exercised against a small back-office screen included here — table markup, no test ids, and two conditions on demand:

```bash
python scripts/mock_legacy_app.py --port 8099 --block-every 2   # a modal covers the form every other load
python scripts/mock_legacy_app.py --port 8099 --break           # the control the recording needs is gone
```

`evidence/artifacts/request_statement.json` was recorded against it. Replay it with the modal showing and the run recovers; replay it against `--break` and you get a hard failure with a screenshot and the full locator trace. See `decisions.md` ADR-023.

## Attribution

`cua/data/us_zip_codes.txt` is derived from the [GeoNames](https://www.geonames.org) postal-code dataset, used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

SauceDemo is a public demo site provided by Sauce Labs for automation practice. No real credentials, real customers or real payments are involved anywhere in this project.
