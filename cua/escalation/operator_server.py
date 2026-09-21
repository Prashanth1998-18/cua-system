"""
Operator API server: FastAPI endpoint for human intervention.

Provides a minimal REST API (and optional HTML UI) that an operator
can use to view escalated interventions and respond with resume/abort.

In production, this would be a full dashboard with WebSocket push.
For the demo, it's a simple polling API with an HTML page.

Endpoints:
    GET  /                    - HTML operator UI
    GET  /api/status          - Current escalation state and pending request
    POST /api/intervene       - Operator submits their decision
    GET  /api/cdp             - CDP connection info for live browser access
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from typing import Awaitable, Callable

from .intervention import (
    EscalationManager,
    HandBack,
    InterventionResponse,
    OperatorAction,
)
from .state_machine import EscalationState, EscalationStateMachine

logger = logging.getLogger(__name__)


def _cdp_targets(cdp_url: str) -> list[dict[str, Any]]:
    """Ask the browser what's open. Blocking; called off the event loop."""
    with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=5) as response:
        return json.load(response)


class OperatorDecision(BaseModel):
    """Request body for the /api/intervene endpoint."""
    action: OperatorAction
    # Required when handing control back after a takeover: what the operator
    # did while they held it. Checked against the step's checkpoint before
    # automation acts on it.
    hand_back: HandBack | None = None
    notes: str = ""


def create_operator_app(
    state_machine: EscalationStateMachine,
    escalation_manager: EscalationManager,
    cdp_url: str | None = None,
    live_view: Callable[[], Awaitable[bytes]] | None = None,
) -> FastAPI:
    """
    Create the FastAPI application for the operator UI.

    Args:
        state_machine: The escalation state machine for the current run
        escalation_manager: Manages intervention requests/responses
        cdp_url: CDP remote debugging URL for live browser access
        live_view: returns a masked screenshot of the live session, taken now
    """
    app = FastAPI(
        title="CUA Operator Console",
        description="Human escalation interface for Computer-Use Automation",
    )

    @app.get("/", response_class=HTMLResponse)
    async def operator_ui():
        """Serve the operator HTML interface."""
        return _OPERATOR_HTML

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """Get current escalation state and pending intervention."""
        pending = escalation_manager.pending_request
        return {
            "state": state_machine.state.value,
            "is_paused": state_machine.is_paused,
            "pending_intervention": pending.model_dump(mode="json") if pending else None,
            "cdp_url": cdp_url if state_machine.is_paused else None,
            "live_view": live_view is not None,
            "history": state_machine.history,
        }

    @app.post("/api/intervene")
    async def intervene(decision: OperatorDecision) -> dict[str, str]:
        """Operator submits their decision (resume, abort, or takeover)."""
        pending = escalation_manager.pending_request
        if not pending:
            raise HTTPException(
                status_code=409,
                detail="No pending intervention to respond to",
            )

        if not state_machine.is_paused:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot intervene in state: {state_machine.state.value}",
            )

        handing_back = (
            decision.action == OperatorAction.RESUME
            and state_machine.state == EscalationState.HUMAN_ACTIVE
        )
        if handing_back and decision.hand_back is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Say what you did before handing control back: "
                    "'completed' (you did the step) or 'retry' (you cleared the problem)"
                ),
            )

        response = InterventionResponse(
            request_id=pending.id,
            action=decision.action,
            hand_back=decision.hand_back if handing_back else None,
            operator_notes=decision.notes,
        )
        # Taking over doesn't close the request: the operator still has to hand
        # control back (resume) or abort when they're done.
        if decision.action == OperatorAction.TAKE_OVER:
            escalation_manager.note(response)
        else:
            escalation_manager.resolve(response)

        match decision.action:
            case OperatorAction.RESUME:
                await state_machine.resume()
                if handing_back:
                    said = (
                        "You completed the step; automation will verify that and carry on."
                        if decision.hand_back == HandBack.COMPLETED
                        else "Automation will run the step again."
                    )
                    return {"status": "resumed", "message": f"Control handed back. {said}"}
                return {"status": "resumed", "message": "Approved. Automation will continue."}

            case OperatorAction.ABORT:
                await state_machine.abort(reason=f"Operator abort: {decision.notes}")
                return {"status": "aborted", "message": "Run aborted by operator"}

            case OperatorAction.TAKE_OVER:
                if state_machine.state == EscalationState.HUMAN_ACTIVE:
                    raise HTTPException(status_code=409, detail="You already have control")
                await state_machine.activate_human()
                return {
                    "status": "takeover",
                    "message": "You have control of the live browser. Click Resume to hand it back.",
                    "cdp_url": cdp_url or "CDP not available",
                }

    @app.get("/api/screenshot")
    async def get_screenshot():
        """The screenshot taken when the current intervention was raised."""
        pending = escalation_manager.pending_request
        if not pending or not pending.screenshot_path or not Path(pending.screenshot_path).exists():
            raise HTTPException(status_code=404, detail="No screenshot for the current intervention")
        return FileResponse(pending.screenshot_path, media_type="image/png")

    @app.get("/api/live.png")
    async def live_screenshot():
        """
        What the session looks like right now, masked the same way evidence
        screenshots are. Polled by the console, so an operator can watch the
        page without attaching anything to the browser.
        """
        if live_view is None:
            raise HTTPException(status_code=404, detail="This run has no live session to show")
        try:
            image = await live_view()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not capture the live session: {e}")
        return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/devtools")
    async def open_devtools():
        """
        Send the operator to DevTools attached to the live page.

        The CDP port's root path serves an empty 200 -- following it gets
        you a blank tab. The inspector URL has to be looked up per target
        in /json/list, so this resolves it and redirects.
        """
        if not cdp_url:
            raise HTTPException(status_code=404, detail="This run has no live browser to attach to")
        try:
            targets = await asyncio.to_thread(_cdp_targets, cdp_url)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach the browser's debugging port at {cdp_url}: {e}",
            )

        pages = [t for t in targets if t.get("type") == "page" and t.get("devtoolsFrontendUrl")]
        # Prefer a page that is actually somewhere, over a blank tab.
        pages.sort(key=lambda t: t.get("url", "") in ("", "about:blank"))
        if not pages:
            raise HTTPException(status_code=404, detail="The live browser has no inspectable page")
        return RedirectResponse(pages[0]["devtoolsFrontendUrl"])

    @app.get("/api/cdp")
    async def get_cdp_info() -> dict[str, Any]:
        """Get CDP connection details for live browser access."""
        return {
            "cdp_url": cdp_url,
            "state": state_machine.state.value,
            "instructions": (
                "Connect to this URL with Chrome DevTools or a CDP client "
                "to interact with the live browser session. "
                "The browser is shared — your actions are visible to the automation."
            ),
        }

    return app


# --------------------------------------------------------------------------- #
# Inline HTML for the operator UI                                              #
# --------------------------------------------------------------------------- #

_OPERATOR_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CUA Operator Console</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f1117;
            color: #e1e4e8;
            padding: 2rem;
        }
        h1 { color: #58a6ff; margin-bottom: 1.5rem; }
        .status-bar {
            display: flex; align-items: center; gap: 1rem;
            padding: 1rem; background: #161b22; border-radius: 8px;
            margin-bottom: 1.5rem;
        }
        .state-badge {
            padding: 0.3rem 0.8rem; border-radius: 4px;
            font-weight: 600; font-size: 0.85rem; text-transform: uppercase;
        }
        .state-automation_active { background: #238636; color: #fff; }
        .state-paused { background: #d29922; color: #000; }
        .state-human_active { background: #da3633; color: #fff; }
        .state-aborted { background: #6e7681; color: #fff; }

        .intervention-card {
            background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            padding: 1.5rem; margin-bottom: 1.5rem;
        }
        .intervention-card h2 { color: #f0883e; margin-bottom: 1rem; }
        .detail { margin-bottom: 0.5rem; }
        .detail .label { color: #8b949e; font-size: 0.85rem; }
        .detail .value { color: #e1e4e8; }

        .actions { display: flex; gap: 0.75rem; margin-top: 1.5rem; }
        .btn {
            padding: 0.6rem 1.5rem; border: none; border-radius: 6px;
            font-size: 0.9rem; font-weight: 600; cursor: pointer;
        }
        .btn-resume { background: #238636; color: #fff; }
        .btn-resume:hover { background: #2ea043; }
        .btn-takeover { background: #1f6feb; color: #fff; }
        .btn-takeover:hover { background: #388bfd; }
        .btn-abort { background: #da3633; color: #fff; }
        .btn-abort:hover { background: #f85149; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }

        .notes-input {
            width: 100%; padding: 0.5rem; margin-top: 0.75rem;
            background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
            color: #e1e4e8; font-size: 0.85rem;
        }
        .cdp-link {
            display: inline-block; margin-top: 0.5rem;
            color: #58a6ff; text-decoration: none;
        }
        #message {
            margin-top: 1rem; padding: 0.75rem; border-radius: 4px;
            display: none;
        }
        .msg-success { background: #1b4332; color: #6ee7b7; display: block !important; }
        .msg-error { background: #4c1d1d; color: #fca5a5; display: block !important; }

        .no-intervention {
            text-align: center; padding: 3rem; color: #8b949e;
        }
    </style>
</head>
<body>
    <h1>&#x1F916; CUA Operator Console</h1>

    <div class="status-bar">
        <span>State:</span>
        <span id="state-badge" class="state-badge">loading...</span>
        <span style="margin-left: auto; color: #8b949e; font-size: 0.85rem;">
            Auto-refreshes every 2s
        </span>
    </div>

    <div id="intervention-area">
        <div class="no-intervention">
            <p>No pending interventions. Automation is running normally.</p>
        </div>
    </div>

    <div id="message"></div>

    <script>
        let polling = null;
        let lastRenderKey = null;

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                updateUI(data);
            } catch (e) {
                console.error('Failed to fetch status:', e);
            }
        }

        function updateUI(data) {
            // Update state badge
            const badge = document.getElementById('state-badge');
            badge.textContent = data.state.replace('_', ' ');
            badge.className = 'state-badge state-' + data.state;

            const area = document.getElementById('intervention-area');

            // Redraw only when the state or the request changes, so the
            // operator's half-typed notes aren't wiped by the 2s refresh.
            const key = data.state + '|' + (data.pending_intervention ? data.pending_intervention.id : '');
            if (key === lastRenderKey) return;
            lastRenderKey = key;

            if (data.pending_intervention) {
                const req = data.pending_intervention;
                area.innerHTML = `
                    <div class="intervention-card">
                        <h2>&#x26A0;&#xFE0F; Intervention Required</h2>
                        <div class="detail">
                            <div class="label">Type</div>
                            <div class="value">${req.intervention_type}</div>
                        </div>
                        <div class="detail">
                            <div class="label">Reason</div>
                            <div class="value">${req.reason}</div>
                        </div>
                        <div class="detail">
                            <div class="label">Capability</div>
                            <div class="value">${req.capability_display_name || req.capability_name}</div>
                        </div>
                        ${req.failed_step_id ? `
                        <div class="detail">
                            <div class="label">Failed Step</div>
                            <div class="value">${req.failed_step_id}: ${req.failed_step_description || ''}</div>
                        </div>` : ''}
                        ${req.error_message ? `
                        <div class="detail">
                            <div class="label">Error</div>
                            <div class="value">${req.error_message}</div>
                        </div>` : ''}
                        <div class="detail">
                            <div class="label">Current URL</div>
                            <div class="value">${req.current_url}</div>
                        </div>
                        ${data.live_view ? `
                        <div class="detail">
                            <div class="label">The live session (updates every 2s)</div>
                            <img id="live-view" src="/api/live.png?t=${Date.now()}"
                                 style="max-width:100%;border:1px solid #30363d;border-radius:4px;margin-top:0.5rem">
                            <div class="value" style="opacity:.6;margin-top:0.4rem">
                                This is a view, not a control. To act on the page yourself, use the
                                browser window the automation opened — it is the same session, and it
                                is waiting for you.
                            </div>
                        </div>` : (req.screenshot_path ? `
                        <div class="detail">
                            <div class="label">Screenshot when it stopped</div>
                            <img src="/api/screenshot?id=${req.id}" style="max-width:100%;border:1px solid #30363d;border-radius:4px;margin-top:0.5rem">
                        </div>` : '')}
                        ${data.cdp_url ? `
                        <div class="detail">
                            <div class="label">Attach DevTools (optional)</div>
                            <div class="value" style="opacity:.75">
                                In Chrome, open <code>chrome://inspect</code> → Configure… → add
                                <code>${data.cdp_url.replace('http://', '')}</code>, then click
                                <em>inspect</em> on this page. Useful for working out <em>why</em> the
                                automation stopped; not needed to approve or take over.
                            </div>
                        </div>` : ''}
                        <input type="text" class="notes-input" id="operator-notes"
                               placeholder="Optional notes...">
                        <div class="actions">
                            ${data.state === 'human_active' ? `
                            <button class="btn btn-resume" onclick="decide('resume', 'completed')">
                                &#x2714; I did the step &mdash; hand back
                            </button>
                            <button class="btn btn-resume" onclick="decide('resume', 'retry')">
                                &#x21BB; I cleared it &mdash; run the step again
                            </button>` : `
                            <button class="btn btn-resume" onclick="decide('resume')">
                                ${req.intervention_type === 'confirmation'
                                    ? '&#x2714; Approve &mdash; automation performs it'
                                    : '&#x25B6; Resume'}
                            </button>
                            <button class="btn btn-takeover" onclick="decide('takeover')">
                                &#x1F3AE; Take Over
                            </button>`}
                            <button class="btn btn-abort" onclick="decide('abort')">
                                &#x23F9; Abort
                            </button>
                        </div>
                        ${data.state === 'human_active' ? `
                        <div class="detail">
                            <div class="label">You have control of the live browser window</div>
                            <div class="value">Do what's needed there, then choose how to hand back.
                            Your clicks and entries are being recorded as evidence.</div>
                        </div>` : ''}
                    </div>
                `;
            } else if (data.state === 'aborted') {
                area.innerHTML = `
                    <div class="no-intervention">
                        <p>Run has been aborted.</p>
                    </div>`;
            } else {
                area.innerHTML = `
                    <div class="no-intervention">
                        <p>No pending interventions. Automation is running normally.</p>
                    </div>`;
            }
        }

        async function decide(action, handBack) {
            const notes = document.getElementById('operator-notes')?.value || '';
            const msgEl = document.getElementById('message');
            try {
                const res = await fetch('/api/intervene', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ action, notes, hand_back: handBack || null }),
                });
                const data = await res.json();
                if (res.ok) {
                    msgEl.textContent = data.message;
                    msgEl.className = 'msg-success';
                } else {
                    msgEl.textContent = data.detail || 'Error';
                    msgEl.className = 'msg-error';
                }
            } catch (e) {
                msgEl.textContent = 'Network error: ' + e.message;
                msgEl.className = 'msg-error';
            }
            // Refresh immediately
            await fetchStatus();
        }

        // Start polling
        fetchStatus();
        polling = setInterval(fetchStatus, 2000);
    </script>
</body>
</html>
"""
