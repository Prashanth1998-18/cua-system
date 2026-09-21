"""
Core unit tests for the CUA system.

Tests the schema, safety, locator resolution, error handling,
state machine, and artifact validation — all without a browser.
"""

import asyncio
import json
from pathlib import Path

import pytest

from cua.schema.artifact import (
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
    InputParam,
    Locator,
    LocatorStrategy,
    OutputParam,
    ParamType,
    RecoveryAction,
    RiskLevel,
    SafetyPolicy,
    Step,
    SurfaceInfo,
    SurfaceType,
)
from cua.schema.results import ReplayResult, ReplayStatus, StepResult
from cua.browser.safety import SafetyGuard, SafetyViolation
from cua.replay.locator import resolve_target_templates
from cua.escalation.state_machine import (
    EscalationState,
    EscalationStateMachine,
    InvalidTransition,
)
from cua.escalation.intervention import (
    EscalationManager,
    InterventionRequest,
    InterventionResponse,
    InterventionType,
    OperatorAction,
)
from cua.config import Config
from cua.browser.adapter import SurfaceAdapter, read_table
from cua.replay.engine import ReplayEngine


# --------------------------------------------------------------------------- #
# Schema tests                                                                 #
# --------------------------------------------------------------------------- #

class TestArtifactSchema:
    """Test that the capability artifact schema validates correctly."""

    def test_load_example_artifact(self):
        """
        Load and validate a real artifact produced by `cua discover` against
        live ParaBank (evidence/logs/01_discover_browse_catalog.log has the
        run that produced it). Assertions are structural rather than pinned
        to an exact step count, since re-running discovery is not guaranteed
        to produce byte-identical output.
        """
        path = Path(__file__).parent.parent / "evidence" / "artifacts" / "browse_catalog.json"
        raw = json.loads(path.read_text())
        cap = Capability.model_validate(raw)

        assert cap.meta.name == "browse_catalog"
        assert len(cap.steps) > 0
        assert "username" in cap.contract.inputs
        assert "password" in cap.contract.inputs
        assert cap.contract.inputs["password"].sensitive is True
        assert cap.contract.inputs["sort_order"].allowed_values
        assert cap.contract.outputs["products"].type == ParamType.TABLE
        assert cap.policy.allowed_domains == ["www.saucedemo.com"]
        assert "password" in cap.policy.pii_fields

    def test_step_template_resolution(self):
        """Test that {{inputs.x}} templates resolve correctly."""
        step = Step(
            id="test_01",
            action=ActionType.TYPE,
            description="Type username",
            value="{{inputs.username}}",
        )
        resolved = step.resolve_value({"username": "john_doe"})
        assert resolved == "john_doe"

    def test_step_template_url_resolution(self):
        """Test URL template resolution."""
        step = Step(
            id="nav_01",
            action=ActionType.NAVIGATE,
            description="Go to account",
            url="https://example.com/account/{{inputs.account_id}}",
        )
        resolved = step.resolve_url({"account_id": "12345"})
        assert resolved == "https://example.com/account/12345"

    def test_step_no_template_passthrough(self):
        """Non-template values pass through unchanged."""
        step = Step(
            id="test_01",
            action=ActionType.TYPE,
            description="Type literal",
            value="literal_value",
        )
        resolved = step.resolve_value({"username": "john"})
        assert resolved == "literal_value"

    def test_minimal_capability(self):
        """A capability with only required fields validates."""
        cap = Capability(
            meta=CapabilityMeta(
                name="minimal",
                display_name="Minimal",
                description="Test",
                surface=SurfaceInfo(
                    type=SurfaceType.WEB,
                    entry_point="https://example.com",
                    app_identifier="test",
                ),
            ),
            contract=Contract(
                success_condition=Checkpoint(
                    type=CheckpointType.URL_CONTAINS,
                    value="done",
                ),
            ),
            steps=[],
            policy=SafetyPolicy(allowed_domains=["example.com"]),
        )
        assert cap.meta.version == "1.0.0"
        assert len(cap.steps) == 0

    def test_roundtrip_serialization(self):
        """Artifact survives JSON roundtrip."""
        path = Path(__file__).parent.parent / "evidence" / "artifacts" / "browse_catalog.json"
        raw = json.loads(path.read_text())
        cap = Capability.model_validate(raw)
        serialized = cap.model_dump(mode="json")
        cap2 = Capability.model_validate(serialized)
        assert cap.meta.name == cap2.meta.name
        assert len(cap.steps) == len(cap2.steps)
        assert cap.contract.inputs.keys() == cap2.contract.inputs.keys()


# --------------------------------------------------------------------------- #
# Safety guard tests                                                           #
# --------------------------------------------------------------------------- #

class TestSafetyGuard:
    """Test the safety guardrail system."""

    def _make_guard(self, domains=None, max_steps=10):
        policy = SafetyPolicy(
            allowed_domains=domains or ["example.com"],
            max_steps=max_steps,
        )
        return SafetyGuard(policy)

    def test_allowed_domain(self):
        guard = self._make_guard(["example.com"])
        # Should not raise
        guard.check_url_for_navigation("https://example.com/page")

    def test_blocked_domain(self):
        guard = self._make_guard(["example.com"])
        with pytest.raises(SafetyViolation):
            guard.check_url_for_navigation("https://evil.com/phish")

    def test_subdomain_allowed(self):
        guard = self._make_guard(["example.com"])
        # Subdomains should typically be allowed
        guard.check_url_for_navigation("https://app.example.com/page")

    def test_pii_redaction(self):
        guard = self._make_guard()
        guard.policy.pii_fields = ["password", "ssn"]
        inputs = {"username": "john", "password": "secret123", "ssn": "123-45-6789"}
        redacted = guard.redact_for_log(inputs)
        assert redacted["username"] == "john"
        assert redacted["password"] == "***REDACTED***"
        assert redacted["ssn"] == "***REDACTED***"

    def test_step_count_enforcement(self):
        guard = self._make_guard(max_steps=2)
        guard.reset_step_count()
        step = Step(id="s1", action=ActionType.CLICK, description="click")
        guard.check_action(step)
        guard.check_action(step)
        with pytest.raises(SafetyViolation):
            guard.check_action(step)  # 3rd step exceeds limit

    def test_risk_level_needs_confirmation(self):
        guard = self._make_guard()
        step = Step(
            id="s1",
            action=ActionType.CLICK,
            description="Submit transfer",
            risk_level=RiskLevel.NEEDS_CONFIRMATION,
        )
        assert guard.needs_confirmation(step) is True

    def test_safe_step_no_confirmation(self):
        guard = self._make_guard()
        step = Step(
            id="s1",
            action=ActionType.CLICK,
            description="Click next",
            risk_level=RiskLevel.SAFE,
        )
        assert guard.needs_confirmation(step) is False


# --------------------------------------------------------------------------- #
# Locator template resolution tests                                            #
# --------------------------------------------------------------------------- #

class TestLocatorResolution:
    """Test template resolution in locators."""

    def test_resolve_template_in_locator(self):
        target = ElementTarget(
            locators=[
                Locator(
                    strategy=LocatorStrategy.CSS,
                    value="#account-{{inputs.account_id}}",
                    description="Account element",
                ),
            ],
            description="Account",
        )
        resolved = resolve_target_templates(target, {"account_id": "12345"})
        assert resolved.locators[0].value == "#account-12345"

    def test_resolve_preserves_non_templates(self):
        target = ElementTarget(
            locators=[
                Locator(
                    strategy=LocatorStrategy.ROLE,
                    value="Submit",
                    role="button",
                    description="Submit button",
                ),
            ],
            description="Submit",
        )
        resolved = resolve_target_templates(target, {"anything": "value"})
        assert resolved.locators[0].value == "Submit"

    def test_original_target_unchanged(self):
        """Resolution returns a new object, doesn't mutate the original."""
        target = ElementTarget(
            locators=[
                Locator(
                    strategy=LocatorStrategy.CSS,
                    value="#user-{{inputs.id}}",
                    description="User",
                ),
            ],
            description="User element",
        )
        resolved = resolve_target_templates(target, {"id": "42"})
        assert target.locators[0].value == "#user-{{inputs.id}}"
        assert resolved.locators[0].value == "#user-42"


# --------------------------------------------------------------------------- #
# Results model tests                                                          #
# --------------------------------------------------------------------------- #

class TestResults:
    """Test replay result models."""

    def test_success_result(self):
        result = ReplayResult(
            capability_id="cap_001",
            capability_name="test",
            status=ReplayStatus.SUCCESS,
            outputs={"balance": "$1,234.56"},
            duration_ms=2500,
        )
        assert result.status == ReplayStatus.SUCCESS
        assert result.outputs["balance"] == "$1,234.56"

    def test_failure_result(self):
        result = ReplayResult(
            capability_id="cap_001",
            capability_name="test",
            status=ReplayStatus.FAILURE,
            failed_at_step="click_01",
            failure_reason="Element not found",
            duration_ms=500,
        )
        assert result.failed_at_step == "click_01"

    def test_business_outcome_result(self):
        result = ReplayResult(
            capability_id="cap_001",
            capability_name="test",
            status=ReplayStatus.BUSINESS_OUTCOME,
            outcome_key="invalid_credentials",
            outcome_message="Login failed: bad password",
            duration_ms=1200,
        )
        assert result.status == ReplayStatus.BUSINESS_OUTCOME
        assert result.outcome_key == "invalid_credentials"


# --------------------------------------------------------------------------- #
# Escalation state machine tests                                               #
# --------------------------------------------------------------------------- #

class TestEscalationStateMachine:
    """Test the escalation state machine transitions."""

    @pytest.fixture
    def sm(self):
        return EscalationStateMachine()

    @pytest.mark.asyncio
    async def test_initial_state(self, sm):
        assert sm.state == EscalationState.AUTOMATION_ACTIVE

    @pytest.mark.asyncio
    async def test_pause(self, sm):
        await sm.pause(reason="Element not found")
        assert sm.state == EscalationState.PAUSED
        assert sm.is_paused is True

    @pytest.mark.asyncio
    async def test_pause_then_resume(self, sm):
        await sm.pause()
        await sm.resume()
        assert sm.state == EscalationState.AUTOMATION_ACTIVE

    @pytest.mark.asyncio
    async def test_pause_then_human_then_resume(self, sm):
        await sm.pause()
        await sm.activate_human()
        assert sm.state == EscalationState.HUMAN_ACTIVE
        await sm.resume()
        assert sm.state == EscalationState.AUTOMATION_ACTIVE

    @pytest.mark.asyncio
    async def test_abort_from_paused(self, sm):
        await sm.pause()
        await sm.abort(reason="Operator abort")
        assert sm.state == EscalationState.ABORTED
        assert sm.is_aborted is True

    @pytest.mark.asyncio
    async def test_abort_from_active(self, sm):
        await sm.abort(reason="Emergency stop")
        assert sm.state == EscalationState.ABORTED

    @pytest.mark.asyncio
    async def test_invalid_transition(self, sm):
        # Can't go from AUTOMATION_ACTIVE to HUMAN_ACTIVE directly
        with pytest.raises(InvalidTransition):
            await sm.activate_human()

    @pytest.mark.asyncio
    async def test_abort_is_terminal(self, sm):
        await sm.abort()
        with pytest.raises(InvalidTransition):
            await sm.resume()

    @pytest.mark.asyncio
    async def test_history_recorded(self, sm):
        await sm.pause(reason="test")
        await sm.resume()
        assert len(sm.history) == 2
        assert sm.history[0]["from"] == "automation_active"
        assert sm.history[0]["to"] == "paused"
        assert sm.history[1]["to"] == "automation_active"

    @pytest.mark.asyncio
    async def test_wait_for_resolution_with_resume(self, sm):
        await sm.pause()

        async def delayed_resume():
            await asyncio.sleep(0.1)
            await sm.resume()

        asyncio.create_task(delayed_resume())
        result = await sm.wait_for_resolution(timeout=5.0)
        assert result == EscalationState.AUTOMATION_ACTIVE

    @pytest.mark.asyncio
    async def test_wait_for_resolution_timeout(self, sm):
        await sm.pause()
        result = await sm.wait_for_resolution(timeout=0.1)
        assert result == EscalationState.ABORTED


# --------------------------------------------------------------------------- #
# Escalation manager tests                                                     #
# --------------------------------------------------------------------------- #

class TestEscalationManager:
    """Test intervention request/response management."""

    def test_create_request(self):
        mgr = EscalationManager()
        req = mgr.create_request(
            intervention_type=InterventionType.HARD_FAILURE,
            reason="Element not found",
            capability_name="check_balance",
            failed_step_id="click_01",
        )
        assert mgr.pending_request is not None
        assert req.intervention_type == InterventionType.HARD_FAILURE
        assert req.capability_name == "check_balance"

    def test_resolve_request(self):
        mgr = EscalationManager()
        req = mgr.create_request(
            intervention_type=InterventionType.HARD_FAILURE,
            reason="Test",
            capability_name="test",
        )
        response = InterventionResponse(
            request_id=req.id,
            action=OperatorAction.RESUME,
            operator_notes="Fixed the element",
        )
        mgr.resolve(response)
        assert mgr.pending_request is None
        assert mgr.last_response is not None
        assert mgr.last_response.action == OperatorAction.RESUME

    def test_clear(self):
        mgr = EscalationManager()
        mgr.create_request(
            intervention_type=InterventionType.CUSTOM,
            reason="Test",
            capability_name="test",
        )
        mgr.clear()
        assert mgr.pending_request is None


# --------------------------------------------------------------------------- #
# Config tests                                                                 #
# --------------------------------------------------------------------------- #

class TestConfig:
    """Test configuration loading."""

    def test_default_config(self):
        config = Config()
        assert config.llm.model == "gpt-4o"
        assert config.browser.headless is True
        assert config.browser.viewport_width == 1280

    def test_config_from_env(self, monkeypatch):
        monkeypatch.setenv("CUA_LLM_MODEL", "gpt-4-turbo")
        monkeypatch.setenv("CUA_BROWSER_HEADLESS", "false")
        config = Config()
        assert config.llm.model == "gpt-4-turbo"
        assert config.browser.headless is False


# --------------------------------------------------------------------------- #
# CLI validation test                                                          #
# --------------------------------------------------------------------------- #

class TestCLI:
    """Test CLI commands without running the full system."""

    def test_validate_good_artifact(self):
        from click.testing import CliRunner
        from cua.cli import main

        runner = CliRunner()
        artifact_path = Path(__file__).parent.parent / "evidence" / "artifacts" / "browse_catalog.json"
        result = runner.invoke(main, ["validate", str(artifact_path)])
        assert result.exit_code == 0
        assert "Valid capability" in result.output
        # The display name is whatever the run was given; the machine name isn't.
        assert "browse_catalog" in result.output

    def test_validate_bad_json(self, tmp_path):
        from click.testing import CliRunner
        from cua.cli import main

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(bad_file)])
        assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# Replay engine: recoverable-error retry                                      #
# --------------------------------------------------------------------------- #

class FakeAdapter(SurfaceAdapter):
    """
    Minimal in-memory SurfaceAdapter for testing the replay engine's control
    flow without a real browser. `click` fails a configurable number of
    times before succeeding, so tests can drive the recovery/retry path.
    """

    def __init__(
        self,
        fail_times: int = 0,
        recoverable_condition: str = "overlay-visible",
        table_headers: list[str] | None = None,
        table_rows: list[dict[str, str]] | None = None,
        alert_text: str = "",
        url: str = "https://example.com/page",
    ):
        self.fail_times = fail_times
        self.click_attempts = 0
        self.recovery_calls = 0
        self.recoverable_condition = recoverable_condition
        self.table_headers = table_headers or []
        self.table_rows = table_rows or []
        self.alert_text = alert_text  # what the page is announcing, if anything
        self.url = url

    async def navigate(self, url: str) -> None:
        pass

    async def find_element(self, target):
        return None

    async def click(self, target: ElementTarget, wait_for_network: bool = False) -> Locator:
        self.click_attempts += 1
        if self.click_attempts <= self.fail_times:
            raise TimeoutError("element not interactable yet")
        return target.locators[0]

    async def type_text(self, target: ElementTarget, text: str) -> Locator:
        raise NotImplementedError

    async def select_option(self, target: ElementTarget, value: str) -> Locator:
        raise NotImplementedError

    async def extract_text(self, target: ElementTarget):
        raise NotImplementedError

    async def extract_table(self, target: ElementTarget):
        return self.table_headers, self.table_rows, target.locators[0]

    async def extract_cards(self, target: ElementTarget, card_selector: str):
        return self.table_headers, self.table_rows, target.locators[0]

    async def check_condition(self, condition_type: CheckpointType, value: str, locator=None) -> bool:
        if condition_type == CheckpointType.ALERT_VISIBLE:
            return bool(self.alert_text)
        if condition_type == CheckpointType.URL_CONTAINS:
            return value in self.url
        if condition_type == CheckpointType.URL_NOT_CONTAINS:
            return value not in self.url
        return value == self.recoverable_condition

    async def read_alert_text(self) -> str:
        return self.alert_text

    async def get_current_url(self) -> str:
        return self.url

    async def get_page_title(self) -> str:
        return "Example"

    async def screenshot(self, path=None) -> bytes:
        return b""

    async def accessibility_snapshot(self) -> str:
        return ""

    async def key_press(self, key: str) -> None:
        pass

    async def scroll(self, direction: str = "down", amount: int = 3) -> None:
        pass

    async def wait_for_condition(self, condition_type: CheckpointType, value: str, locator=None, timeout_ms: int = 5000) -> bool:
        return True

    async def dismiss_dialog(self) -> bool:
        self.recovery_calls += 1
        return True


def _click_step_with_recoverable_handler(max_retries: int) -> Step:
    return Step(
        id="click_01",
        action=ActionType.CLICK,
        description="Click submit",
        target=ElementTarget(
            locators=[Locator(strategy=LocatorStrategy.CSS, value="#submit")],
            description="Submit button",
        ),
        error_handlers=[
            ErrorHandler(
                condition=ErrorCondition(type=CheckpointType.ELEMENT_VISIBLE, value="overlay-visible"),
                classification=ErrorClass.RECOVERABLE,
                recovery=RecoveryAction.DISMISS_DIALOG,
                max_retries=max_retries,
            ),
        ],
    )


class TestReplayEngineRecovery:
    """
    A RECOVERABLE error handler must actually retry the step's action after
    running its recovery — otherwise 'recoverable' behaves identically to a
    hard failure, defeating the point of the error taxonomy (ADR-004).
    """

    @pytest.mark.asyncio
    async def test_recovery_retries_and_succeeds(self):
        engine = ReplayEngine()
        adapter = FakeAdapter(fail_times=1)
        guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"]))
        step = _click_step_with_recoverable_handler(max_retries=3)

        result = await engine._execute_step(
            step=step, inputs={}, adapter=adapter, guard=guard,
            session=None, global_handlers=[],
        )

        assert result.status == ReplayStatus.SUCCESS
        assert adapter.click_attempts == 2  # first failed, retry succeeded
        assert adapter.recovery_calls == 1

    @pytest.mark.asyncio
    async def test_recovery_exhausts_retries_then_fails(self):
        engine = ReplayEngine()
        adapter = FakeAdapter(fail_times=99)  # never succeeds
        guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"]))
        step = _click_step_with_recoverable_handler(max_retries=2)

        result = await engine._execute_step(
            step=step, inputs={}, adapter=adapter, guard=guard,
            session=None, global_handlers=[],
        )

        assert result.status == ReplayStatus.FAILURE
        assert result.error_class == ErrorClass.RECOVERABLE
        assert adapter.recovery_calls == 2
        assert "exhausted 2 retries" in result.error_message


# --------------------------------------------------------------------------- #
# Table outputs                                                                #
# --------------------------------------------------------------------------- #

TXN_COLUMNS = ["Date", "Transaction", "Debit (-)", "Credit (+)"]


def _table_extract_step() -> Step:
    return Step(
        id="extract_01",
        action=ActionType.EXTRACT,
        description="Read transactions",
        target=ElementTarget(
            locators=[Locator(strategy=LocatorStrategy.CSS, value="#transactionTable")],
            description="Transactions table",
        ),
        extract_output_key="transactions",
    )


def _table_output(columns: list[str] | None = TXN_COLUMNS) -> OutputParam:
    return OutputParam(type=ParamType.TABLE, extract_from_step="extract_01", columns=columns)


class _FakeTableLocator:
    def __init__(self, headers: list[str], rows: list[list[str]]):
        self._data = {"headers": headers, "rows": rows}

    async def evaluate(self, _js):
        return self._data


class TestTableOutput:
    """TABLE outputs come back as rows keyed by column name, and a changed table fails loudly."""

    @pytest.mark.asyncio
    async def test_table_rows_returned(self):
        rows = [{"Date": "12-10-2025", "Transaction": "Check # 1111", "Debit (-)": "", "Credit (+)": "$300.00"}]
        adapter = FakeAdapter(table_headers=TXN_COLUMNS, table_rows=rows)
        guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"]))

        result = await ReplayEngine()._execute_step(
            step=_table_extract_step(), inputs={}, adapter=adapter, guard=guard,
            session=None, global_handlers=[], output_param=_table_output(),
        )

        assert result.status == ReplayStatus.SUCCESS
        assert result.extracted_value == rows

    @pytest.mark.asyncio
    async def test_empty_table_is_success(self):
        """An account with no transactions is a legitimate answer, not an error."""
        adapter = FakeAdapter(table_headers=TXN_COLUMNS, table_rows=[])
        guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"]))

        result = await ReplayEngine()._execute_step(
            step=_table_extract_step(), inputs={}, adapter=adapter, guard=guard,
            session=None, global_handlers=[], output_param=_table_output(),
        )

        assert result.status == ReplayStatus.SUCCESS
        assert result.extracted_value == []

    @pytest.mark.asyncio
    async def test_changed_columns_is_hard_failure(self):
        adapter = FakeAdapter(table_headers=["Date", "Amount"], table_rows=[])
        guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"]))

        result = await ReplayEngine()._execute_step(
            step=_table_extract_step(), inputs={}, adapter=adapter, guard=guard,
            session=None, global_handlers=[], output_param=_table_output(),
        )

        assert result.status == ReplayStatus.FAILURE
        assert result.error_class == ErrorClass.HARD_FAILURE
        assert "Table columns changed" in result.error_message

    @pytest.mark.asyncio
    async def test_read_table_keys_rows_by_header(self):
        locator = _FakeTableLocator(["Date", "Amount"], [["01-01", "$5"], ["01-02", "$7"]])
        headers, rows = await read_table(locator)
        assert headers == ["Date", "Amount"]
        assert rows == [{"Date": "01-01", "Amount": "$5"}, {"Date": "01-02", "Amount": "$7"}]

    @pytest.mark.asyncio
    async def test_read_table_falls_back_when_header_count_mismatches(self):
        locator = _FakeTableLocator(["Date"], [["01-01", "$5"]])
        _, rows = await read_table(locator)
        assert rows == [{"col_0": "01-01", "col_1": "$5"}]


# --------------------------------------------------------------------------- #
# Recorder: automatic input parameterization                                  #
# --------------------------------------------------------------------------- #

class TestRecorderParameterization:
    """Input values used during discovery become placeholders, wherever they ended up."""

    def test_clicked_input_value_becomes_placeholder(self):
        from cua.discovery.recorder import ActionRecorder

        recorder = ActionRecorder(
            name="account_details",
            display_name="Account Details",
            description="test",
            entry_point="https://example.com/index.htm",
            inputs={"account_id": "13344", "month": "September"},
        )
        recorder._steps.append(Step(
            id="click_01",
            action=ActionType.CLICK,
            description="Open account",
            target=ElementTarget(
                locators=[Locator(strategy=LocatorStrategy.ROLE, value="13344", role="link")],
                description="link '13344'",
            ),
        ))
        recorder._steps.append(Step(
            id="select_02",
            action=ActionType.SELECT_OPTION,
            description="Pick month",
            target=ElementTarget(
                locators=[Locator(strategy=LocatorStrategy.CSS, value="#month")],
                description="month dropdown",
            ),
            value="September",
        ))

        cap = recorder.build()

        assert cap.steps[0].target.locators[0].value == "{{inputs.account_id}}"
        assert cap.steps[1].value == "{{inputs.month}}"
        assert "account_id" in cap.contract.inputs
        assert "month" in cap.contract.inputs

    def test_unused_input_not_added_to_contract(self):
        from cua.discovery.recorder import ActionRecorder

        recorder = ActionRecorder(
            name="x", display_name="X", description="test",
            entry_point="https://example.com/index.htm",
            inputs={"account_id": "13344"},
        )
        cap = recorder.build()
        assert "account_id" not in cap.contract.inputs


# --------------------------------------------------------------------------- #
# Dropdown inputs: allowed values                                              #
# --------------------------------------------------------------------------- #

MONTHS = ["All", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _month_capability() -> Capability:
    return Capability(
        meta=CapabilityMeta(
            name="account_details", display_name="Account Details", description="t",
            surface=SurfaceInfo(type=SurfaceType.WEB, entry_point="https://example.com", app_identifier="t"),
        ),
        contract=Contract(
            inputs={"month": InputParam(allowed_values=MONTHS)},
            success_condition=Checkpoint(type=CheckpointType.URL_CONTAINS, value="x"),
        ),
        steps=[],
        policy=SafetyPolicy(allowed_domains=["example.com"]),
    )


class TestAllowedValues:
    def test_recorder_attaches_dropdown_options_to_its_input(self):
        from cua.discovery.recorder import ActionRecorder

        recorder = ActionRecorder(
            name="t", display_name="T", description="t",
            entry_point="https://example.com/index.htm", inputs={"month": "September"},
        )
        target = ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#month")], description="month")
        recorder.record_select_option(target, "September", options=MONTHS)

        cap = recorder.build()
        assert cap.steps[0].value == "{{inputs.month}}"
        assert cap.contract.inputs["month"].allowed_values == MONTHS

    @pytest.mark.asyncio
    async def test_invalid_value_rejected_before_any_browser_work(self):
        result = await ReplayEngine().run(_month_capability(), {"month": "Fervembruary"})
        assert result.status == ReplayStatus.FAILURE
        assert result.failed_at_step == "input_validation"
        assert "'month' must be one of" in result.failure_reason
        assert "Fervembruary" in result.failure_reason

    def test_locator_descriptions_resolved_at_replay(self):
        target = ElementTarget(
            locators=[Locator(strategy=LocatorStrategy.ROLE, value="{{inputs.account_id}}", role="link",
                              description='link "{{inputs.account_id}}"')],
            description='link "{{inputs.account_id}}"',
        )
        resolved = resolve_target_templates(target, {"account_id": "13788"})
        assert resolved.description == 'link "13788"'
        assert resolved.locators[0].description == 'link "13788"'


# --------------------------------------------------------------------------- #
# Checkpoints recorded from what each action changed                           #
# --------------------------------------------------------------------------- #

LOGIN_PAGE = '- heading "Customer Login" [level=2] [ref=e1]\n- textbox [ref=e2]'
OVERVIEW_PAGE = '- heading "Accounts Overview" [level=1] [ref=f1e1]\n- link "13344" [ref=f1e2]'


def _recorder_with_click():
    from cua.discovery.recorder import ActionRecorder

    recorder = ActionRecorder(name="t", display_name="T", description="t", entry_point="https://example.com/index.htm")
    target = ElementTarget(locators=[Locator(strategy=LocatorStrategy.ROLE, value="Log In", role="button")], description="Log In")
    recorder.record_click(target, "Log in")
    return recorder


class TestCheckpoints:
    def test_new_heading_becomes_checkpoint_with_headroom(self):
        recorder = _recorder_with_click()
        recorder.add_checkpoint(recorder.last_step_id, LOGIN_PAGE, OVERVIEW_PAGE, elapsed_ms=4000)
        step = recorder.build().steps[0]
        assert step.checkpoint.locator.role == "heading"
        assert step.checkpoint.locator.value == "Accounts Overview"
        assert step.timeout_ms == 12000  # 3x what discovery saw

    def test_short_waits_get_the_minimum_timeout(self):
        recorder = _recorder_with_click()
        recorder.add_checkpoint(recorder.last_step_id, LOGIN_PAGE, OVERVIEW_PAGE, elapsed_ms=500)
        assert recorder.build().steps[0].timeout_ms == 10000

    def test_no_page_change_means_no_checkpoint(self):
        recorder = _recorder_with_click()
        recorder.add_checkpoint(recorder.last_step_id, LOGIN_PAGE, LOGIN_PAGE, elapsed_ms=500)
        assert recorder.build().steps[0].checkpoint is None

    def test_success_condition_is_the_last_checkpoint(self):
        recorder = _recorder_with_click()
        recorder.add_checkpoint(recorder.last_step_id, LOGIN_PAGE, OVERVIEW_PAGE, elapsed_ms=500)
        success = recorder.build().contract.success_condition
        assert success.type == CheckpointType.ELEMENT_VISIBLE
        assert success.locator.value == "Accounts Overview"


# --------------------------------------------------------------------------- #
# Retry once before calling it an error                                        #
# --------------------------------------------------------------------------- #

def _plain_click(risk: RiskLevel = RiskLevel.SAFE) -> Step:
    return Step(
        id="click_01", action=ActionType.CLICK, description="Click",
        target=ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#b")], description="b"),
        risk_level=risk,
    )


class TestRetryOnce:
    @pytest.mark.asyncio
    async def test_unexplained_failure_is_retried_once(self):
        adapter = FakeAdapter(fail_times=1)
        result = await ReplayEngine()._execute_step(
            step=_plain_click(), inputs={}, adapter=adapter,
            guard=SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), session=None, global_handlers=[],
        )
        assert result.status == ReplayStatus.SUCCESS
        assert adapter.click_attempts == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_one_retry(self):
        adapter = FakeAdapter(fail_times=99)
        result = await ReplayEngine()._execute_step(
            step=_plain_click(), inputs={}, adapter=adapter,
            guard=SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), session=None, global_handlers=[],
        )
        assert result.status == ReplayStatus.FAILURE
        assert adapter.click_attempts == 2
        assert "failed again after one retry" in result.error_message

    @pytest.mark.asyncio
    async def test_step_that_changes_data_is_never_retried(self):
        adapter = FakeAdapter(fail_times=1)
        result = await ReplayEngine()._execute_step(
            step=_plain_click(RiskLevel.NEEDS_CONFIRMATION), inputs={}, adapter=adapter,
            guard=SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), session=None, global_handlers=[],
        )
        assert result.status == ReplayStatus.FAILURE
        assert adapter.click_attempts == 1
        assert "not retried" in result.error_message


# --------------------------------------------------------------------------- #
# Secrets never reach the model or the logs                                    #
# --------------------------------------------------------------------------- #

class TestSecrets:
    def test_secret_filled_only_in_executed_copy(self):
        from cua.discovery.agent import with_secrets

        args = {"ref": "e37", "text": "{{inputs.password}}"}
        executed = with_secrets(args, {"password": "demo"})
        assert executed["text"] == "demo"
        assert args["text"] == "{{inputs.password}}", "the logged / model-visible args must keep the placeholder"

    def test_recorded_secret_is_a_sensitive_input(self):
        from cua.discovery.recorder import ActionRecorder

        recorder = ActionRecorder(
            name="t", display_name="T", description="t", entry_point="https://example.com/index.htm",
            inputs={"username": "john"}, secrets={"password"},
        )
        target = ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value='input[name="password"]')], description="password")
        recorder.record_type(target, "{{inputs.password}}")

        cap = recorder.build()
        assert cap.steps[0].value == "{{inputs.password}}"
        assert cap.contract.inputs["password"].sensitive is True
        assert "password" in cap.policy.pii_fields


class TestUrlCheckpointFallback:
    def test_url_change_used_when_no_heading_appears(self):
        recorder = _recorder_with_click()
        recorder.add_checkpoint(
            recorder.last_step_id, LOGIN_PAGE, LOGIN_PAGE, elapsed_ms=500,
            before_url="https://www.saucedemo.com/", after_url="https://www.saucedemo.com/inventory.html",
        )
        cp = recorder.build().steps[0].checkpoint
        assert cp.type == CheckpointType.URL_CONTAINS
        assert cp.value == "inventory.html"

    def test_heading_preferred_over_url(self):
        recorder = _recorder_with_click()
        recorder.add_checkpoint(
            recorder.last_step_id, LOGIN_PAGE, OVERVIEW_PAGE, elapsed_ms=500,
            before_url="https://x/index.htm", after_url="https://x/overview.htm",
        )
        assert recorder.build().steps[0].checkpoint.type == CheckpointType.ELEMENT_VISIBLE


# --------------------------------------------------------------------------- #
# List inputs at replay                                                        #
# --------------------------------------------------------------------------- #

PRODUCTS = ["Sauce Labs Backpack", "Sauce Labs Bike Light", "Sauce Labs Onesie"]


def _list_capability() -> Capability:
    step = _plain_click()
    step.for_each = "items"
    return Capability(
        meta=CapabilityMeta(
            name="t", display_name="T", description="t",
            surface=SurfaceInfo(type=SurfaceType.WEB, entry_point="https://example.com", app_identifier="t"),
        ),
        contract=Contract(
            inputs={
                "items": InputParam(type=ParamType.LIST, allowed_values=PRODUCTS),
                "zip": InputParam(sensitive=True, allowed_values=["10001"]),
            },
            success_condition=Checkpoint(type=CheckpointType.URL_CONTAINS, value="example.com"),
        ),
        steps=[step],
        policy=SafetyPolicy(allowed_domains=["example.com"]),
    )


class TestListInputs:
    @pytest.mark.asyncio
    async def test_repeated_step_runs_once_per_item(self):
        cap = _list_capability()
        adapter = FakeAdapter()
        result = await ReplayEngine()._execute_steps(
            cap, {"items": PRODUCTS[:2], "zip": "10001"}, adapter, SafetyGuard(cap.policy), session=None,
        )
        assert result.status == ReplayStatus.SUCCESS
        assert adapter.click_attempts == 2
        assert [r.step_id for r in result.step_results] == ["click_01[1]", "click_01[2]"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("items, message", [
        ([PRODUCTS[0], PRODUCTS[0]], "more than once"),
        (["Sauce Labs Toaster"], "unknown values"),
        ([], "non-empty list"),
        ("Sauce Labs Backpack", "non-empty list"),
    ])
    async def test_bad_lists_rejected_before_any_browser_work(self, items, message):
        result = await ReplayEngine().run(_list_capability(), {"items": items, "zip": "10001"})
        assert result.failed_at_step == "input_validation"
        assert message in result.failure_reason

    @pytest.mark.asyncio
    async def test_sensitive_value_never_echoed_in_errors(self):
        result = await ReplayEngine().run(_list_capability(), {"items": [PRODUCTS[0]], "zip": "99999"})
        assert result.failed_at_step == "input_validation"
        assert "99999" not in result.failure_reason
        assert "(hidden)" in result.failure_reason

    def test_cli_parses_json_list_inputs(self):
        from cua.cli import _parse_pairs

        parsed = _parse_pairs(('items=["Sauce Labs Backpack", "Sauce Labs Onesie"]', "username=standard_user"), "--input")
        assert parsed == {"items": ["Sauce Labs Backpack", "Sauce Labs Onesie"], "username": "standard_user"}


# --------------------------------------------------------------------------- #
# Zip code rule (ADR-016)                                                      #
# --------------------------------------------------------------------------- #

def _zip_capability() -> Capability:
    cap = _list_capability()
    cap.contract.inputs["zip"] = InputParam(sensitive=True, format="us_zip_code")
    return cap


class TestZipRule:
    @pytest.mark.parametrize("zip_code", ["10001", "90210"])
    def test_existing_zips_pass(self, zip_code):
        assert ReplayEngine._invalid_inputs(_zip_capability(), {"items": [PRODUCTS[0]], "zip": zip_code}) == []

    @pytest.mark.parametrize("zip_code", ["00000", "99999", "1234", "ABCDE", "100011"])
    def test_bad_zips_rejected_without_echoing_them(self, zip_code):
        problems = ReplayEngine._invalid_inputs(_zip_capability(), {"items": [PRODUCTS[0]], "zip": zip_code})
        assert problems == ["'zip' must be an existing 5-digit US zip code (got (hidden))"]

    def test_recorder_attaches_stated_rule_to_its_input(self):
        from cua.discovery.recorder import ActionRecorder

        recorder = ActionRecorder(
            name="t", display_name="T", description="t", entry_point="https://example.com/index.htm",
            secrets={"zip"},
        )
        target = ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#postal-code")], description="zip")
        recorder.record_type(target, "{{inputs.zip}}")
        recorder.set_input_format("zip", "us_zip_code")

        param = recorder.build().contract.inputs["zip"]
        assert param.format == "us_zip_code"
        assert param.sensitive is True


# --------------------------------------------------------------------------- #
# A refusal by the application is an answer, not a crash                       #
# --------------------------------------------------------------------------- #

def test_recorder_derives_a_rejection_handler_from_the_step_checkpoint():
    """The model says what a refusal means; the condition comes from what proved the step worked."""
    from cua.discovery.recorder import ActionRecorder

    recorder = ActionRecorder(name="t", display_name="T", description="t", entry_point="https://example.com/login")
    target = ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#login")], description="Login")
    recorder.record_click(
        target, "Log in", if_rejected=("login_rejected", "Report login rejected with the reason shown"),
    )
    recorder.add_checkpoint(
        "click_01", before="", after="", elapsed_ms=500,
        before_url="https://example.com/login", after_url="https://example.com/inventory.html",
    )

    handler = recorder._steps[-1].error_handlers[0]
    assert handler.classification == ErrorClass.BUSINESS_OUTCOME
    assert handler.outcome_key == "login_rejected"
    assert handler.condition.type == CheckpointType.ALERT_VISIBLE, "the app has to be saying something"
    assert handler.also_requires[0].type == CheckpointType.URL_NOT_CONTAINS
    assert handler.also_requires[0].value == "inventory.html", "...and the step has to not have worked"
    assert handler.report_page_message is True


@pytest.mark.asyncio
async def test_a_refused_step_is_reported_with_the_applications_own_words():
    engine = ReplayEngine()
    step = Step(
        id="click_01", action=ActionType.CLICK, description="Log in",
        target=ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#login")], description="Login"),
        checkpoint=Checkpoint(type=CheckpointType.URL_CONTAINS, value="inventory.html"),
        error_handlers=[ErrorHandler(
            condition=ErrorCondition(type=CheckpointType.ALERT_VISIBLE, value=""),
            also_requires=[ErrorCondition(type=CheckpointType.URL_NOT_CONTAINS, value="inventory.html")],
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_key="login_rejected",
            outcome_message="Report login rejected with the reason shown",
            report_page_message=True,
            max_retries=0,
        )],
    )
    adapter = FakeAdapter(
        alert_text="Epic sadface: Sorry, this user has been locked out.",
        url="https://example.com/",  # the login page, not the inventory
    )

    result = await engine._handle_step_failure(
        step, "checkpoint not met", {}, adapter, SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), [],
    )

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_key == "login_rejected"
    assert "this user has been locked out" in result.error_message


@pytest.mark.asyncio
async def test_a_step_that_fails_with_nothing_announced_is_still_a_hard_failure():
    """No alert means the page didn't refuse us -- it broke. Different class, different handling."""
    engine = ReplayEngine()
    step = Step(
        id="click_01", action=ActionType.CLICK, description="Log in",
        target=ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#login")], description="Login"),
        checkpoint=Checkpoint(type=CheckpointType.URL_CONTAINS, value="inventory.html"),
        error_handlers=[ErrorHandler(
            condition=ErrorCondition(type=CheckpointType.ALERT_VISIBLE, value=""),
            also_requires=[ErrorCondition(type=CheckpointType.URL_NOT_CONTAINS, value="inventory.html")],
            classification=ErrorClass.BUSINESS_OUTCOME,
            outcome_key="login_rejected",
            report_page_message=True,
            max_retries=0,
        )],
    )
    adapter = FakeAdapter(alert_text="", url="https://example.com/", fail_times=99)

    result = await engine._handle_step_failure(
        step, "the page never loaded", {}, adapter, SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), [],
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.error_class == ErrorClass.HARD_FAILURE


# --------------------------------------------------------------------------- #
# The run's time budget                                                        #
# --------------------------------------------------------------------------- #

def test_time_budget_is_enforced_and_excludes_waiting_for_a_person():
    """
    A run that goes on forever is a runaway; a run waiting on an operator is
    not. The policy declares a budget, and only automation time spends it.
    """
    import time as _time

    guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"], timeout_seconds=1))
    guard.reset_step_count()
    step = Step(id="s1", action=ActionType.CLICK, description="click")

    guard.check_action(step)  # within budget

    # A person takes their time. That must not spend the run's budget.
    guard.start_waiting_for_human()
    _time.sleep(1.1)
    guard.stop_waiting_for_human()
    guard.check_action(step)
    assert guard.elapsed_seconds() < 1, "the wait came off the clock"

    # Automation itself running long does spend it.
    guard._started -= 5  # five seconds of work, without sleeping for them
    with pytest.raises(SafetyViolation) as raised:
        guard.check_action(step)
    assert raised.value.violation_type == "timeout_exceeded"
    assert "excluding any wait for a person" in str(raised.value)


def test_time_budget_grows_with_list_expansion():
    """One recorded step becoming ten is work the recording never timed."""
    guard = SafetyGuard(SafetyPolicy(allowed_domains=["example.com"], timeout_seconds=120))
    guard.reset_step_count()

    guard.allow_seconds(60)
    assert guard._budget_seconds == 120, "the policy value is the floor"

    guard.allow_seconds(300)
    assert guard._budget_seconds == 300, "and a longer plan raises it"


def test_recorded_artifacts_declare_a_measured_time_budget():
    """The budget in an artifact comes from how long the flow actually took."""
    path = Path(__file__).parent.parent / "evidence" / "artifacts" / "place_order.json"
    cap = Capability.model_validate(json.loads(path.read_text()))
    step_time = sum(s.timeout_ms for s in cap.steps) / 1000
    assert cap.policy.timeout_seconds >= 120
    assert cap.policy.timeout_seconds >= step_time, "a budget under its own steps would fail every run"


def test_capability_catalog_is_callable_contracts_not_a_file_listing():
    """What an agent needs to choose and invoke: inputs, outputs, and whether a person is involved."""
    from click.testing import CliRunner
    from cua.cli import main

    artifacts = Path(__file__).parent.parent / "evidence" / "artifacts"
    result = CliRunner().invoke(main, ["capabilities", str(artifacts), "--json"])
    assert result.exit_code == 0

    catalog = {entry["name"]: entry for entry in json.loads(result.output)}
    assert {"browse_catalog", "total_with_tax", "place_order"} <= set(catalog)

    order = catalog["place_order"]
    assert order["requires_operator"] is True, "the caller must know this before invoking"
    assert order["inputs"]["zip"]["sensitive"] is True
    assert order["inputs"]["items"]["type"] == "list"
    assert "confirmation_message" in order["outputs"]
    assert "login_rejected" in order["outcomes"], "the answers it can give, not just the ones it returns"

    assert catalog["browse_catalog"]["requires_operator"] is False
    assert catalog["browse_catalog"]["inputs"]["sort_order"]["one_of"], "recorded choices are part of the contract"


@pytest.mark.asyncio
async def test_a_notice_that_was_already_there_is_not_a_refusal():
    """
    A banner with role="alert" sitting on the page before the step ran says
    nothing about the step. Treating it as one turns furniture into a
    business outcome — which the mock app in scripts/ does produce.
    """
    from cua.replay.handlers import match_error_handlers

    handler = ErrorHandler(
        condition=ErrorCondition(type=CheckpointType.ALERT_VISIBLE, value=""),
        classification=ErrorClass.BUSINESS_OUTCOME,
        outcome_key="login_rejected",
    )
    adapter = FakeAdapter(alert_text="Scheduled maintenance this weekend.")

    stale = await match_error_handlers(
        [handler], adapter, {}, alert_before="Scheduled maintenance this weekend.",
    )
    assert stale is None, "it was already saying that"

    fresh = await match_error_handlers([handler], adapter, {}, alert_before="")
    assert fresh is not None and fresh.outcome_key == "login_rejected"


@pytest.mark.asyncio
async def test_a_blocker_is_dismissed_and_the_step_retried():
    """The recoverable class, end to end: clear what's in the way, then do the step again."""
    from cua.replay.handlers import execute_recovery

    dismiss = ElementTarget(
        locators=[Locator(strategy=LocatorStrategy.ROLE, value="Dismiss", role="button")],
        description='button "Dismiss"',
    )
    step = Step(
        id="type_01", action=ActionType.CLICK, description="Look up the member",
        target=ElementTarget(locators=[Locator(strategy=LocatorStrategy.CSS, value="#go")], description="go"),
        error_handlers=[ErrorHandler(
            condition=ErrorCondition(
                type=CheckpointType.ELEMENT_VISIBLE, value="",
                locator=Locator(strategy=LocatorStrategy.ROLE, value="Dismiss", role="button"),
            ),
            classification=ErrorClass.RECOVERABLE,
            recovery=RecoveryAction.DISMISS_ELEMENT,
            recovery_target=dismiss,
            max_retries=2,
        )],
    )

    # The step has just failed with the notice up; the handler should clear
    # it and run the step again (two clicks: the dismissal, then the retry).
    adapter = FakeAdapter(recoverable_condition="")
    engine = ReplayEngine()
    result = await engine._handle_step_failure(
        step, "element not interactable", {}, adapter,
        SafetyGuard(SafetyPolicy(allowed_domains=["example.com"])), [],
    )

    assert result.status == ReplayStatus.SUCCESS, "recovered, not failed"
    assert adapter.click_attempts == 2, "the dismissal, then the step again"


def test_a_cleared_blocker_is_global_and_not_a_step():
    """
    Dismissing a modal isn't part of the flow, and it doesn't belong to one
    step either: a modal blocks whatever comes next, which on a slower day
    is a different step than it was today.
    """
    from cua.discovery.recorder import ActionRecorder

    recorder = ActionRecorder(name="t", display_name="T", description="t", entry_point="https://example.com/")
    dismiss = ElementTarget(
        locators=[Locator(strategy=LocatorStrategy.ROLE, value="Dismiss", role="button")],
        description='button "Dismiss"',
    )
    look_up = ElementTarget(
        locators=[Locator(strategy=LocatorStrategy.ROLE, value="Look up", role="button")],
        description='button "Look up"',
    )

    recorder.record_blocker(dismiss)
    recorder.record_click(look_up, "Look up the member")
    recorder.record_blocker(dismiss)  # it showed up again; still one handler

    capability = recorder.build(final_url="https://example.com/record")

    assert [s.action for s in capability.steps] == [ActionType.CLICK], "the dismissal is not a step"
    assert len(capability.global_error_handlers) == 1, "and not recorded twice"
    handler = capability.global_error_handlers[0]
    assert handler.classification == ErrorClass.RECOVERABLE
    assert handler.recovery == RecoveryAction.DISMISS_ELEMENT
    assert handler.recovery_target.description == 'button "Dismiss"'
    assert handler.max_retries == 2
