"""
CLI entry point for the Computer-Use Automation System.

Commands:
    cua discover   - Run LLM-driven discovery to create a capability artifact
    cua replay     - Execute a capability artifact deterministically (no LLM)
    cua operator   - Start the operator console for human escalation
    cua validate   - Validate a capability artifact JSON file
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from .config import Config


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """CUA: Computer-Use Automation System.

    Discover, record, and replay UI workflows.
    """
    _setup_logging(verbose)


# --------------------------------------------------------------------------- #
# discover                                                                     #
# --------------------------------------------------------------------------- #

@main.command()
@click.option("--goal", "-g", required=True, help="Natural language goal (e.g., 'Log in and check balance')")
@click.option("--url", "-u", required=True, help="Entry point URL")
@click.option("--name", "-n", required=True, help="Capability name (snake_case)")
@click.option("--display-name", "-d", default=None, help="Human-readable display name")
@click.option("--description", default=None, help="Capability description")
@click.option("--output", "-o", default=None, help="Output JSON file path (default: <name>.json)")
@click.option("--max-steps", default=30, help="Maximum agent steps")
@click.option("--max-seconds", default=900, type=float,
              help="Give up after this long (time spent waiting for you doesn't count)")
@click.option("--allowed-domain", multiple=True, help="Allowed domains (can repeat)")
@click.option("--input", "input_pairs", multiple=True, help="Input values as key=value (can repeat)")
@click.option("--secret", "secret_pairs", multiple=True,
              help="Sensitive inputs as key=value (e.g. password). Never shown to the LLM or written to logs.")
@click.option("--show-browser/--headless", "show_browser", default=True,
              help="Watch the agent work. On by default: recording is something you supervise.")
@click.option("--operator-port", default=8080, help="Port for the operator console, when one is needed")
@click.option("--escalation-timeout", default=None, type=int,
              help="Seconds to wait for you before stopping (default from config: 300)")
def discover(
    goal: str,
    url: str,
    name: str,
    display_name: str | None,
    description: str | None,
    output: str | None,
    max_steps: int,
    max_seconds: float,
    allowed_domain: tuple[str, ...],
    input_pairs: tuple[str, ...],
    secret_pairs: tuple[str, ...],
    show_browser: bool,
    operator_port: int,
    escalation_timeout: int | None,
) -> None:
    """Run LLM-driven discovery to create a capability artifact.

    The agent will navigate to the URL, achieve the goal by interacting
    with the page, and record its actions as a replayable capability.

    Example:
        cua discover \\
            --goal "Log in and check the balance" \\
            --url https://parabank.parasoft.com/parabank/index.htm \\
            --name check_balance \\
            --input username=john --secret password=demo
    """
    config = Config()
    inputs = _parse_pairs(input_pairs, "--input")
    secrets = _parse_pairs(secret_pairs, "--secret")

    # Determine output path
    output_path = Path(output) if output else Path(f"{name}.json")

    click.echo(f"Starting discovery: {goal}")
    click.echo(f"  Entry point: {url}")
    click.echo(f"  Capability:  {name}")
    if inputs:
        click.echo(f"  Inputs:      {inputs}")
    if secrets:
        click.echo(f"  Secrets:     {list(secrets)} (values withheld from the LLM and logs)")

    from .discovery.agent import DiscoveryAgent

    if escalation_timeout is not None:
        config.escalation.escalation_timeout_seconds = escalation_timeout

    agent = DiscoveryAgent(config)
    run = dict(
        goal=goal,
        entry_point=url,
        capability_name=name,
        display_name=display_name,
        description=description,
        allowed_domains=list(allowed_domain) or None,
        max_steps=max_steps,
        max_seconds=max_seconds,
        inputs=inputs or None,
        secrets=secrets or None,
    )

    # Recording is supervised by whoever asked for the capability, and the
    # agent may need their approval part-way through, so the browser is
    # visible unless you say otherwise.
    config.browser.headless = not show_browser
    result = asyncio.run(_discover_with_operator(config, agent, run, operator_port))

    capability = result.capability
    for intervention in result.interventions:
        click.echo(f"\n  Intervention: {intervention['type']} -> {intervention['operator_action']}")
        for line in intervention.get("what_the_human_did", []):
            click.echo(f"    Operator {line}")

    if not result.success:
        # A flow that stops in the middle of itself is worse than no flow:
        # it looks like a capability and replays as a wrong one.
        click.echo(f"\nDiscovery did not complete: {result.summary}", err=True)
        click.echo(f"  {len(capability.steps)} steps were recorded but no artifact was saved.", err=True)
        click.echo(f"  Fix what stopped it (or adjust the procedure) and run discovery again.", err=True)
        sys.exit(1)

    # Save the artifact
    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_json = capability.model_dump(mode="json")
    output_path.write_text(json.dumps(artifact_json, indent=2))

    click.echo(f"\nDiscovery complete!")
    click.echo(f"  Steps recorded: {len(capability.steps)}")
    click.echo(f"  Inputs:         {list(capability.contract.inputs.keys())}")
    click.echo(f"  Outputs:        {list(capability.contract.outputs.keys())}")
    click.echo(f"  Artifact saved: {output_path}")
    for assist in capability.meta.human_assisted:
        click.echo(
            f"  Warning: a human took over after {assist.after_step_id} ({assist.reason}). "
            f"Those actions are not in the recording, so replay has a gap there."
        )


async def _discover_with_operator(config: Config, agent, run: dict, port: int):
    """Run discovery, which can stop and ask before it commits anything."""
    channel, _start, stop = _operator_channel(config, port)
    try:
        return await agent.discover(**run, escalation=channel)
    finally:
        await stop()


def _parse_pairs(pairs: tuple[str, ...], flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            click.echo(f"Error: {flag} must be key=value, got: {pair}", err=True)
            sys.exit(1)
        key, value = pair.split("=", 1)
        if value.lstrip().startswith("["):
            # A list input, written as JSON: items='["Sauce Labs Backpack", "Sauce Labs Bike Light"]'
            try:
                value = json.loads(value)
            except json.JSONDecodeError as e:
                click.echo(f"Error: {flag} {key}= looks like a list but isn't valid JSON: {e}", err=True)
                sys.exit(1)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                click.echo(f"Error: {flag} {key}= must be a JSON list of strings", err=True)
                sys.exit(1)
        parsed[key] = value
    return parsed


# --------------------------------------------------------------------------- #
# replay                                                                       #
# --------------------------------------------------------------------------- #

@main.command()
@click.option("--artifact", "-a", required=True, type=click.Path(exists=True), help="Path to capability artifact JSON")
@click.option("--input", "input_pairs", multiple=True, help="Input values as key=value (can repeat)")
@click.option("--secret", "secret_pairs", multiple=True, help="Sensitive inputs as key=value (never logged)")
@click.option("--output", "-o", default=None, help="Save replay result JSON to this path")
@click.option("--show-browser/--headless", "show_browser", default=None,
              help="Watch it run. A capability that needs your approval opens a browser anyway.")
@click.option("--operator-port", default=8080, help="Port for the operator console, when one is needed")
@click.option("--escalation-timeout", default=None, type=int,
              help="Seconds to wait for you before stopping (default from config: 300)")
def replay(
    artifact: str,
    input_pairs: tuple[str, ...],
    secret_pairs: tuple[str, ...],
    output: str | None,
    show_browser: bool | None,
    operator_port: int,
    escalation_timeout: int | None,
) -> None:
    """Replay a capability artifact deterministically (no LLM calls).

    Executes the recorded steps with the given inputs and reports results.

    Example:
        cua replay \\
            --artifact check_balance.json \\
            --input username=john --secret password=demo
    """
    config = Config()

    # Load the artifact
    artifact_path = Path(artifact)
    from .schema.artifact import Capability
    raw = json.loads(artifact_path.read_text())
    capability = Capability.model_validate(raw)

    click.echo(f"Replaying: {capability.meta.display_name}")
    click.echo(f"  Steps: {len(capability.steps)}")

    secrets = _parse_pairs(secret_pairs, "--secret")
    inputs = {**_parse_pairs(input_pairs, "--input"), **secrets}

    # Check required inputs
    missing = [
        name for name, param in capability.contract.inputs.items()
        if param.required and name not in inputs
    ]
    if missing:
        click.echo(f"Error: missing required inputs: {', '.join(missing)}", err=True)
        click.echo(f"  Required: {list(capability.contract.inputs.keys())}")
        sys.exit(1)

    sensitive = {n for n, param in capability.contract.inputs.items() if param.sensitive} | set(secrets)
    click.echo(f"  Inputs:  { {k: ('***' if k in sensitive else v) for k, v in inputs.items()} }")

    if escalation_timeout is not None:
        config.escalation.escalation_timeout_seconds = escalation_timeout

    # These are internal back-office systems: the person who asked for the
    # work is right there, so there is always someone to ask. What varies is
    # whether they need to see anything. A capability that asks for approval
    # opens a browser, because approving something you can't see is not
    # approving it; anything else runs out of sight unless you ask to watch.
    if show_browser is None:
        show_browser = capability.requires_operator
    config.browser.headless = not show_browser
    if capability.requires_operator:
        click.echo("  This capability needs your approval before it commits anything.")

    result = asyncio.run(_replay_with_operator(config, capability, inputs, operator_port))

    # Display results
    click.echo(f"\nReplay result: {result.status.value.upper()}")
    click.echo(f"  Duration: {result.duration_ms}ms")

    if result.outputs:
        click.echo("  Outputs:")
        for k, v in result.outputs.items():
            if isinstance(v, list):
                click.echo(f"    {k}: table, {len(v)} rows")
                for row in v:
                    click.echo(f"      {row}")
            else:
                click.echo(f"    {k}: {v}")

    if result.outcome_key:
        click.echo(f"  Outcome: {result.outcome_key}")
        if result.outcome_message:
            click.echo(f"  Message: {result.outcome_message}")

    if result.failure_reason:
        click.echo(f"  Failure: {result.failure_reason}")
        click.echo(f"  Failed at: {result.failed_at_step}")

    if result.screenshot_path:
        click.echo(f"  Screenshot: {result.screenshot_path}")

    for i, iv in enumerate(result.interventions, 1):
        took = ", operator took over" if iv["operator_took_over"] else ""
        click.echo(f"  Intervention {i}: {iv['type']} at {iv['step_id']} -> {iv['operator_action']}{took}")
        if iv.get("operator_notes"):
            click.echo(f"    Notes: {iv['operator_notes']}")
        if iv.get("operator_says"):
            click.echo(f"    Operator said: {iv['operator_says']}")
        if iv.get("pages_visited_by_operator"):
            click.echo(f"    Operator visited: {iv['pages_visited_by_operator']}")
        for line in iv.get("what_the_human_did", []):
            click.echo(f"    Operator {line}")

    # Step trace
    if result.step_results:
        click.echo(f"\n  Step trace ({len(result.step_results)} steps):")
        for sr in result.step_results:
            status_icon = "✓" if sr.status.value == "success" else "✗"
            line = f"    {status_icon} {sr.step_id}"
            if sr.locator_used:
                line += f" (via {sr.locator_used})"
            if isinstance(sr.extracted_value, list):
                line += f" → table ({len(sr.extracted_value)} rows)"
            elif sr.extracted_value:
                line += f" → {sr.extracted_value}"
            if sr.duration_ms:
                line += f" [{sr.duration_ms}ms]"
            click.echo(line)

    # Save result
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result.model_dump(mode="json"), indent=2))
        click.echo(f"\n  Result saved: {output_path}")

    # Exit code: 0 success, 2 business outcome (a legitimate "no"), 1 failure
    sys.exit({"success": 0, "business_outcome": 2}.get(result.status.value, 1))


def _operator_channel(config: Config, port: int):
    """
    An escalation channel whose console comes up the moment it's needed.

    Starting a web server for a run that never asks anything would be noise,
    so the console is stood up on the first intervention and the address is
    printed then — which is also the moment someone has a reason to open it.
    """
    import uvicorn

    from .escalation.channel import EscalationChannel
    from .escalation.operator_server import create_operator_app

    channel = EscalationChannel(
        cdp_url=f"http://127.0.0.1:{config.browser.cdp_port}",
        timeout_seconds=config.escalation.escalation_timeout_seconds,
    )
    app = create_operator_app(
        channel.state_machine, channel.manager, channel.cdp_url,
        live_view=lambda: channel.live_view() if channel.live_view else None,
    )
    host = config.escalation.operator_host
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    state: dict[str, Any] = {"task": None}

    async def start() -> None:
        if state["task"] is None:
            state["task"] = asyncio.create_task(server.serve())
            await asyncio.sleep(0.2)  # let it bind before anyone is told about it
            click.echo(f"\n  Waiting for you: http://{host}:{port}")

    async def stop() -> None:
        if state["task"] is not None:
            server.should_exit = True
            await state["task"]

    channel.on_first_request = start
    return channel, start, stop


async def _replay_with_operator(config: Config, capability, inputs: dict[str, str], port: int):
    """Run a replay that can stop and ask, with the console ready to serve when it does."""
    from .replay.engine import ReplayEngine

    channel, start, stop = _operator_channel(config, port)
    if capability.requires_operator:
        # It is going to ask, so give them the address up front.
        await start()
    try:
        return await ReplayEngine(config, channel).run(capability, inputs)
    finally:
        await stop()


# --------------------------------------------------------------------------- #
# operator                                                                     #
# --------------------------------------------------------------------------- #

@main.command()
@click.option("--port", "-p", default=8080, help="Port for the operator console")
@click.option("--host", default="0.0.0.0", help="Host to bind to")
def operator(port: int, host: str) -> None:
    """Start the operator console for human escalation.

    This launches a web UI where operators can view pending interventions
    and take action (resume, take over via CDP, or abort).

    Example:
        cua operator --port 8080
    """
    import uvicorn

    from .escalation.state_machine import EscalationStateMachine
    from .escalation.intervention import EscalationManager
    from .escalation.operator_server import create_operator_app

    click.echo(f"Starting operator console on http://{host}:{port}")
    click.echo("Press Ctrl+C to stop")

    sm = EscalationStateMachine()
    manager = EscalationManager()
    app = create_operator_app(sm, manager)

    uvicorn.run(app, host=host, port=port, log_level="info")


# --------------------------------------------------------------------------- #
# capabilities                                                                 #
# --------------------------------------------------------------------------- #

@main.command()
@click.argument("directory", type=click.Path(exists=True), default="evidence/artifacts")
@click.option("--json", "as_json", is_flag=True, help="Emit the catalog as JSON, for an agent to consume")
def capabilities(directory: str, as_json: bool) -> None:
    """List the recorded capabilities as a catalog an agent can call.

    Each entry is a callable contract: what it needs, what it returns, and
    whether running it involves a person. That last one matters before
    invoking, not halfway through.

        cua capabilities
        cua capabilities --json
    """
    from .schema.artifact import Capability

    catalog = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            capability = Capability.model_validate(json.loads(path.read_text()))
        except Exception as e:
            click.echo(f"  (skipping {path.name}: {e})", err=True)
            continue
        catalog.append({
            "name": capability.meta.name,
            "description": capability.meta.description.strip().splitlines()[0][:120],
            "artifact": str(path),
            "requires_operator": capability.requires_operator,
            "inputs": {
                name: {
                    "type": param.type.value,
                    "required": param.required,
                    **({"sensitive": True} if param.sensitive else {}),
                    **({"one_of": param.allowed_values} if param.allowed_values else {}),
                    **({"format": param.format} if param.format else {}),
                }
                for name, param in capability.contract.inputs.items()
            },
            "outputs": {
                name: {"type": param.type.value, **({"columns": param.columns} if param.columns else {})}
                for name, param in capability.contract.outputs.items()
            },
            "outcomes": sorted({
                handler.outcome_key
                for step in capability.steps
                for handler in step.error_handlers
                if handler.outcome_key
            }),
        })

    if as_json:
        click.echo(json.dumps(catalog, indent=2))
        return

    if not catalog:
        click.echo(f"No capabilities in {directory}")
        return

    click.echo(f"{len(catalog)} capabilities in {directory}:\n")
    for entry in catalog:
        click.echo(f"  {entry['name']}")
        click.echo(f"    {entry['description']}")
        click.echo(f"    in:       {', '.join(entry['inputs']) or '(none)'}")
        click.echo(f"    out:      {', '.join(entry['outputs']) or '(none)'}")
        if entry["outcomes"]:
            click.echo(f"    can say:  {', '.join(entry['outcomes'])}")
        click.echo(f"    approval: {'yes, a person decides before it commits' if entry['requires_operator'] else 'no'}")
        click.echo("")


# --------------------------------------------------------------------------- #
# validate                                                                     #
# --------------------------------------------------------------------------- #

@main.command()
@click.argument("artifact_path", type=click.Path(exists=True))
def validate(artifact_path: str) -> None:
    """Validate a capability artifact JSON file.

    Checks that the artifact conforms to the schema and reports any issues.

    Example:
        cua validate check_balance.json
    """
    from .schema.artifact import Capability

    path = Path(artifact_path)
    try:
        raw = json.loads(path.read_text())
        capability = Capability.model_validate(raw)
    except json.JSONDecodeError as e:
        click.echo(f"Invalid JSON: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Schema validation failed: {e}", err=True)
        sys.exit(1)

    click.echo(f"Valid capability: {capability.meta.display_name}")
    click.echo(f"  Name:       {capability.meta.name}")
    click.echo(f"  Version:    {capability.meta.version}")
    click.echo(f"  Steps:      {len(capability.steps)}")
    click.echo(f"  Inputs:     {list(capability.contract.inputs.keys())}")
    click.echo(f"  Outputs:    {list(capability.contract.outputs.keys())}")
    click.echo(f"  Domains:    {capability.policy.allowed_domains}")
    click.echo(f"  PII fields: {capability.policy.pii_fields}")
    click.echo(
        "  Approval:   needed before this commits anything (a browser opens)"
        if capability.requires_operator
        else "  Approval:   not needed; nothing here commits anything"
    )

    # Warn about potential issues
    warnings = []
    if not capability.steps:
        warnings.append("No steps defined")
    if not capability.contract.inputs:
        warnings.append("No input parameters (fully hardcoded workflow)")
    if not capability.contract.outputs:
        warnings.append("No output parameters (nothing will be extracted)")
    if not capability.policy.allowed_domains:
        warnings.append("No allowed domains (navigation unrestricted)")

    if warnings:
        click.echo(f"\nWarnings:")
        for w in warnings:
            click.echo(f"  ⚠ {w}")
    else:
        click.echo(f"\nNo issues found.")


if __name__ == "__main__":
    main()
