"""Tests for the LLM ingestion pipeline.

Uses mocked LLM responses to test pipeline stages without API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bt_engine.compiler.ingestion import ProcedureIngester, _clean_empty_fields
from bt_engine.compiler.schemas import (
    ActionType,
    ConditionBranch,
    ConditionOperator,
    ExtractField,
    InformOption,
    Procedure,
    ProcedureOverview,
    ProcedureStep,
    StepOverview,
    StructuredCondition,
    ToolArgMapping,
    ToolConfig,
)
from bt_engine.compiler.tool_registry import create_default_registry


@pytest.fixture
def registry():
    return create_default_registry()


@pytest.fixture
def ingester(registry):
    return ProcedureIngester(registry=registry, max_refinement_rounds=2)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestProcedureValidation:
    """Test the internal validation logic."""

    def test_valid_procedure(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(id="step1", action=ActionType.collect_info, next_step="step2"),
                ProcedureStep(id="step2", action=ActionType.end),
            ],
        )
        errors = ingester._validate_procedure(proc)
        assert errors == []

    def test_dangling_next_step(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(id="step1", action=ActionType.collect_info, next_step="nonexistent"),
            ],
        )
        errors = ingester._validate_procedure(proc)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_dangling_on_success(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(
                    id="tool_step",
                    action=ActionType.tool_call,
                    on_success="missing_step",
                    on_failure="end",
                ),
            ],
        )
        errors = ingester._validate_procedure(proc)
        assert any("on_success" in e for e in errors)

    def test_unknown_tool(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(
                    id="step1",
                    action=ActionType.tool_call,
                    tools=[ToolConfig(name="nonexistent_tool")],
                ),
            ],
        )
        errors = ingester._validate_procedure(proc)
        assert any("nonexistent_tool" in e for e in errors)

    def test_end_is_valid_target(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(id="step1", action=ActionType.tool_call, on_success="end", on_failure="end"),
            ],
        )
        errors = ingester._validate_procedure(proc)
        # "end" should be recognized as valid
        assert not any("on_success" in e for e in errors)
        assert not any("on_failure" in e for e in errors)

    def test_condition_branch_validation(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(
                    id="eval",
                    action=ActionType.evaluate,
                    conditions=[
                        ConditionBranch(next_step="missing_target"),
                    ],
                ),
            ],
        )
        errors = ingester._validate_procedure(proc)
        assert any("missing_target" in e for e in errors)

    def test_inform_option_validation(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(
                    id="inform",
                    action=ActionType.inform,
                    options=[InformOption(label="Go", next_step="nowhere")],
                ),
            ],
        )
        errors = ingester._validate_procedure(proc)
        assert any("nowhere" in e for e in errors)

    def test_available_tools_validation(self, ingester):
        proc = Procedure(
            id="test",
            name="Test",
            available_tools=["lookup_order", "fake_tool"],
            steps=[ProcedureStep(id="end", action=ActionType.end)],
        )
        errors = ingester._validate_procedure(proc)
        assert any("fake_tool" in e for e in errors)
        # lookup_order is valid and shouldn't cause an error
        assert not any("lookup_order" in e for e in errors)


# ---------------------------------------------------------------------------
# Tool refinement tests
# ---------------------------------------------------------------------------

class TestToolRefinement:
    """Test tool validation and arg_mapping auto-population."""

    def test_auto_populate_arg_mappings(self, ingester):
        step = ProcedureStep(
            id="lookup",
            action=ActionType.tool_call,
            tools=[ToolConfig(name="lookup_order")],
        )
        refined = ingester._refine_tool_step(step)
        assert len(refined.tools) == 1
        assert len(refined.tools[0].arg_mappings) > 0
        param_names = [m.param for m in refined.tools[0].arg_mappings]
        assert "order_id" in param_names

    def test_unknown_tool_removed(self, ingester):
        step = ProcedureStep(
            id="bad",
            action=ActionType.tool_call,
            tools=[
                ToolConfig(name="lookup_order"),
                ToolConfig(name="totally_fake"),
            ],
        )
        refined = ingester._refine_tool_step(step)
        assert len(refined.tools) == 1
        assert refined.tools[0].name == "lookup_order"

    def test_auto_populate_result_key(self, ingester):
        step = ProcedureStep(
            id="fetch",
            action=ActionType.tool_call,
            tools=[ToolConfig(name="get_fraud_alert")],
        )
        refined = ingester._refine_tool_step(step)
        assert refined.tools[0].result_key == "get_fraud_alert_result"

    def test_preserves_existing_arg_mappings(self, ingester):
        step = ProcedureStep(
            id="lookup",
            action=ActionType.tool_call,
            tools=[ToolConfig(
                name="lookup_order",
                arg_mappings=[ToolArgMapping(param="order_id", source="custom_key")],
            )],
        )
        refined = ingester._refine_tool_step(step)
        assert refined.tools[0].arg_mappings[0].source == "custom_key"


# ---------------------------------------------------------------------------
# Pipeline pass tests (mocked LLM)
# ---------------------------------------------------------------------------

class TestIngestionPipeline:
    """Test the full ingestion pipeline with mocked LLM."""

    def _make_mock_overview(self) -> str:
        """Return JSON for a ProcedureOverview."""
        overview = ProcedureOverview(
            id="simple_refund",
            name="Simple Refund",
            description="Handle refund requests",
            domain="customer_service",
            trigger_intents=["refund"],
            available_tools=["lookup_order", "issue_refund"],
            data_context=["order_id"],
            steps=[
                StepOverview(id="collect", name="Collect Info", action=ActionType.collect_info, instruction="Get order info"),
                StepOverview(id="lookup", name="Lookup Order", action=ActionType.tool_call, instruction="Find the order"),
                StepOverview(id="check", name="Check Eligibility", action=ActionType.evaluate, instruction="Check refund eligibility"),
                StepOverview(id="refund", name="Process Refund", action=ActionType.tool_call, instruction="Issue refund"),
                StepOverview(id="done", name="End", action=ActionType.end, instruction="Done"),
            ],
        )
        return overview.model_dump_json()

    def _make_mock_step(self, step_id: str) -> str:
        """Return JSON for a detailed ProcedureStep based on ID."""
        steps = {
            "collect": ProcedureStep(
                id="collect",
                action=ActionType.collect_info,
                instruction="Ask for order details",
                extract_fields=[ExtractField(key="order_id", description="Order number")],
                required_fields=["order_id"],
                next_step="lookup",
            ),
            "lookup": ProcedureStep(
                id="lookup",
                action=ActionType.tool_call,
                instruction="Looking up your order",
                tools=[ToolConfig(name="lookup_order", arg_mappings=[ToolArgMapping(param="order_id", source="order_id")])],
                on_success="check",
                on_failure="done",
            ),
            "check": ProcedureStep(
                id="check",
                action=ActionType.evaluate,
                instruction="Check eligibility",
                conditions=[
                    ConditionBranch(
                        condition=StructuredCondition(field="order_date", operator=ConditionOperator.within_days, value=30),
                        next_step="refund",
                    ),
                    ConditionBranch(
                        condition=StructuredCondition(field="order_date", operator=ConditionOperator.outside_days, value=30),
                        next_step="done",
                    ),
                ],
            ),
            "refund": ProcedureStep(
                id="refund",
                action=ActionType.tool_call,
                instruction="Processing refund",
                tools=[ToolConfig(name="issue_refund")],
                on_success="done",
                on_failure="done",
            ),
            "done": ProcedureStep(
                id="done",
                action=ActionType.end,
                instruction="Complete",
            ),
        }
        return steps[step_id].model_dump_json()

    @pytest.mark.asyncio
    async def test_pass1_structure(self, ingester):
        """Pass 1 extracts procedure overview."""
        mock_response = MagicMock()
        mock_response.text = self._make_mock_overview()

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        with patch("bt_engine.compiler.llm_utils.get_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="test"):
            result = await ingester._pass1_structure("Handle refund requests for customers.")

        assert result.id == "simple_refund"
        assert len(result.steps) == 5

    @pytest.mark.asyncio
    async def test_full_pipeline_mocked(self, ingester):
        """Full pipeline with all LLM calls mocked."""
        call_count = [0]
        overview_json = self._make_mock_overview()
        step_ids = ["collect", "lookup", "check", "refund", "done"]

        def mock_generate(model, contents, config=None):
            resp = MagicMock()
            idx = call_count[0]
            call_count[0] += 1

            if idx == 0:
                # Pass 1: overview
                resp.text = overview_json
            elif 1 <= idx <= 5:
                # Pass 2: step details
                step_id = step_ids[idx - 1]
                resp.text = self._make_mock_step(step_id)
            elif 6 <= idx <= 7:
                # Pass 3: condition refinement (structured conditions already provided)
                # These may not be called since conditions are already structured
                resp.text = '{"field": "order_date", "operator": "within_days", "value": 30}'
            else:
                # Pass 4: validation (shouldn't need refinement)
                resp.text = "{}"

            return resp

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=mock_generate)

        with patch("bt_engine.compiler.llm_utils.get_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="test"):
            result = await ingester.ingest("Handle refund requests.")

        assert isinstance(result, Procedure)
        assert result.id == "simple_refund"
        assert len(result.steps) == 5

    @pytest.mark.asyncio
    async def test_ingest_to_yaml(self, ingester, tmp_path):
        """Pipeline writes valid YAML output."""
        call_count = [0]
        overview_json = self._make_mock_overview()
        step_ids = ["collect", "lookup", "check", "refund", "done"]

        def mock_generate(model, contents, config=None):
            resp = MagicMock()
            idx = call_count[0]
            call_count[0] += 1

            if idx == 0:
                resp.text = overview_json
            elif 1 <= idx <= 5:
                step_id = step_ids[idx - 1]
                resp.text = self._make_mock_step(step_id)
            else:
                resp.text = "{}"
            return resp

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=mock_generate)

        output_path = tmp_path / "test_proc.yaml"

        with patch("bt_engine.compiler.llm_utils.get_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="test"):
            path = await ingester.ingest_to_yaml("Handle refunds.", str(output_path))

        assert path.exists()
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "procedure" in data
        assert data["procedure"]["id"] == "simple_refund"

    @pytest.mark.asyncio
    async def test_refinement_fixes_errors(self, ingester):
        """Refinement pass attempts to fix validation errors."""
        # Procedure with a dangling reference
        bad_proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(id="step1", action=ActionType.collect_info, next_step="nonexistent"),
                ProcedureStep(id="end", action=ActionType.end),
            ],
        )

        # Mock the refinement LLM call to return a fixed version
        fixed_proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(id="step1", action=ActionType.collect_info, next_step="end"),
                ProcedureStep(id="end", action=ActionType.end),
            ],
        )

        mock_response = MagicMock()
        mock_response.text = fixed_proc.model_dump_json()

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        with patch("bt_engine.compiler.llm_utils.get_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="test"):
            result = await ingester.refine(bad_proc, ["next_step 'nonexistent' not found"])

        assert result.steps[0].next_step == "end"


# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------

class TestCleanEmptyFields:
    """Test the YAML cleanup utility."""

    def test_removes_empty_strings(self):
        d = {"name": "test", "description": "", "id": "x"}
        _clean_empty_fields(d)
        assert "description" not in d
        assert d["name"] == "test"

    def test_removes_empty_lists(self):
        d = {"tools": [], "steps": [{"id": "a"}]}
        _clean_empty_fields(d)
        assert "tools" not in d
        assert "steps" in d

    def test_removes_none(self):
        d = {"field": None, "other": "ok"}
        _clean_empty_fields(d)
        assert "field" not in d

    def test_recursive(self):
        d = {"nested": {"empty": "", "value": "keep"}}
        _clean_empty_fields(d)
        assert d == {"nested": {"value": "keep"}}
