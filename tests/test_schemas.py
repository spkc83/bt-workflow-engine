"""Tests for the fine-grained Pydantic procedure schemas."""

import json

import pytest

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
from bt_engine.compiler.condition_parser import parse_structured_condition


# ---------------------------------------------------------------------------
# StructuredCondition tests
# ---------------------------------------------------------------------------

class TestStructuredCondition:
    """Test StructuredCondition model validation and predicate conversion."""

    def test_eq_string(self):
        cond = StructuredCondition(field="severity", operator=ConditionOperator.eq, value="high")
        assert cond.field == "severity"
        assert cond.operator == ConditionOperator.eq
        assert cond.value == "high"

    def test_gte_numeric(self):
        cond = StructuredCondition(field="risk_score", operator=ConditionOperator.gte, value=80)
        assert cond.value == 80

    def test_in_list(self):
        cond = StructuredCondition(
            field="order_status", operator=ConditionOperator.in_list, value=["delivered", "shipped"]
        )
        assert isinstance(cond.value, list)
        assert len(cond.value) == 2

    def test_with_field_path(self):
        cond = StructuredCondition(
            field="severity",
            operator=ConditionOperator.eq,
            value="high",
            field_path="alert_data.severity",
        )
        assert cond.field_path == "alert_data.severity"

    def test_within_days(self):
        cond = StructuredCondition(
            field="order_date", operator=ConditionOperator.within_days, value=30
        )
        assert cond.operator == ConditionOperator.within_days

    def test_serialization_roundtrip(self):
        cond = StructuredCondition(
            field="risk_score", operator=ConditionOperator.gte, value=80
        )
        json_str = cond.model_dump_json()
        restored = StructuredCondition.model_validate_json(json_str)
        assert restored.field == cond.field
        assert restored.operator == cond.operator
        assert restored.value == cond.value


# ---------------------------------------------------------------------------
# StructuredCondition -> predicate tests
# ---------------------------------------------------------------------------

class TestStructuredConditionPredicate:
    """Test parse_structured_condition() converts to working predicates."""

    def test_eq_string_predicate(self):
        cond = StructuredCondition(field="severity", operator=ConditionOperator.eq, value="high")
        pred = parse_structured_condition(cond)
        bb = {"alert_data": {"severity": "high", "risk_score": 90}}
        assert pred(bb) is True
        bb["alert_data"]["severity"] = "low"
        assert pred(bb) is False

    def test_gte_numeric_predicate(self):
        cond = StructuredCondition(field="risk_score", operator=ConditionOperator.gte, value=80)
        pred = parse_structured_condition(cond)
        assert pred({"alert_data": {"risk_score": 90}}) is True
        assert pred({"alert_data": {"risk_score": 80}}) is True
        assert pred({"alert_data": {"risk_score": 50}}) is False

    def test_lt_predicate(self):
        cond = StructuredCondition(field="risk_score", operator=ConditionOperator.lt, value=40)
        pred = parse_structured_condition(cond)
        assert pred({"alert_data": {"risk_score": 30}}) is True
        assert pred({"alert_data": {"risk_score": 50}}) is False

    def test_in_list_predicate(self):
        cond = StructuredCondition(
            field="order_status", operator=ConditionOperator.in_list, value=["delivered", "shipped"]
        )
        pred = parse_structured_condition(cond)
        assert pred({"order_data": {"status": "delivered"}}) is True
        assert pred({"order_data": {"status": "processing"}}) is False

    def test_within_days_predicate(self):
        cond = StructuredCondition(
            field="order_date", operator=ConditionOperator.within_days, value=30
        )
        pred = parse_structured_condition(cond)
        assert pred({"order_data": {"days_since_delivery": 15}}) is True
        assert pred({"order_data": {"days_since_delivery": 45}}) is False

    def test_outside_days_predicate(self):
        cond = StructuredCondition(
            field="order_date", operator=ConditionOperator.outside_days, value=30
        )
        pred = parse_structured_condition(cond)
        assert pred({"order_data": {"days_since_delivery": 45}}) is True
        assert pred({"order_data": {"days_since_delivery": 15}}) is False

    def test_contains_predicate(self):
        cond = StructuredCondition(field="complaint_type", operator=ConditionOperator.contains, value="damage")
        pred = parse_structured_condition(cond)
        assert pred({"complaint_type": "product_damage"}) is True
        assert pred({"complaint_type": "wrong_item"}) is False

    def test_neq_predicate(self):
        cond = StructuredCondition(field="severity", operator=ConditionOperator.neq, value="low")
        pred = parse_structured_condition(cond)
        assert pred({"alert_data": {"severity": "high"}}) is True
        assert pred({"alert_data": {"severity": "low"}}) is False

    def test_not_in_predicate(self):
        cond = StructuredCondition(
            field="order_status", operator=ConditionOperator.not_in, value=["cancelled", "returned"]
        )
        pred = parse_structured_condition(cond)
        assert pred({"order_data": {"status": "delivered"}}) is True
        assert pred({"order_data": {"status": "cancelled"}}) is False

    def test_field_path_predicate(self):
        cond = StructuredCondition(
            field="severity",
            operator=ConditionOperator.eq,
            value="critical",
            field_path="alert_data.severity",
        )
        pred = parse_structured_condition(cond)
        assert pred({"alert_data": {"severity": "critical"}}) is True
        assert pred({"alert_data": {"severity": "low"}}) is False

    def test_from_dict(self):
        """parse_structured_condition accepts plain dicts too."""
        cond_dict = {"field": "risk_score", "operator": "gte", "value": 80}
        pred = parse_structured_condition(cond_dict)
        assert pred({"alert_data": {"risk_score": 90}}) is True


# ---------------------------------------------------------------------------
# ProcedureStep tests
# ---------------------------------------------------------------------------

class TestProcedureStep:
    """Test ProcedureStep model."""

    def test_minimal_step(self):
        step = ProcedureStep(id="step1", action=ActionType.end)
        assert step.id == "step1"
        assert step.action == ActionType.end
        assert step.instruction == ""
        assert step.extract_fields == []

    def test_collect_info_step(self):
        step = ProcedureStep(
            id="collect",
            action=ActionType.collect_info,
            instruction="Ask for order details",
            extract_fields=[
                ExtractField(key="order_id", description="The order number", examples=["ORD-123"]),
                ExtractField(key="amount", description="Dollar amount"),
            ],
            required_fields=["order_id"],
            next_step="lookup",
        )
        assert len(step.extract_fields) == 2
        assert step.required_fields == ["order_id"]
        assert step.next_step == "lookup"

    def test_tool_call_step(self):
        step = ProcedureStep(
            id="lookup",
            action=ActionType.tool_call,
            tools=[
                ToolConfig(
                    name="lookup_order",
                    arg_mappings=[ToolArgMapping(param="order_id", source="order_id")],
                    result_key="order_data",
                ),
            ],
            on_success="evaluate",
            on_failure="not_found",
        )
        assert len(step.tools) == 1
        assert step.tools[0].arg_mappings[0].param == "order_id"

    def test_evaluate_step_with_structured_conditions(self):
        step = ProcedureStep(
            id="check",
            action=ActionType.evaluate,
            conditions=[
                ConditionBranch(
                    condition=StructuredCondition(
                        field="order_date", operator=ConditionOperator.within_days, value=30
                    ),
                    next_step="approve",
                ),
                ConditionBranch(
                    condition=None,
                    condition_description="Customer seems upset",
                    next_step="escalate",
                ),
            ],
            classify_categories=["approve", "escalate"],
            classify_result_key="check_result",
        )
        assert step.conditions[0].condition is not None
        assert step.conditions[1].condition is None
        assert len(step.classify_categories) == 2

    def test_inform_step_with_options(self):
        step = ProcedureStep(
            id="offer",
            action=ActionType.inform,
            instruction="Would you like store credit or escalation?",
            options=[
                InformOption(
                    label="Store credit",
                    next_step="credit",
                    detection_keywords=["credit", "store", "accept", "yes"],
                ),
                InformOption(
                    label="Escalation",
                    next_step="escalate",
                    detection_keywords=["escalat", "supervisor", "manager"],
                ),
            ],
        )
        assert len(step.options) == 2
        assert step.options[0].detection_keywords == ["credit", "store", "accept", "yes"]


# ---------------------------------------------------------------------------
# Procedure tests
# ---------------------------------------------------------------------------

class TestProcedure:
    """Test full Procedure model."""

    def test_minimal_procedure(self):
        proc = Procedure(
            id="test_proc",
            name="Test Procedure",
            steps=[ProcedureStep(id="end", action=ActionType.end)],
        )
        assert proc.id == "test_proc"
        assert proc.version == "1.0"
        assert proc.domain == ""

    def test_full_procedure(self):
        proc = Procedure(
            id="cs_refund",
            name="Customer Service - Refund",
            description="Handle refund requests",
            domain="customer_service",
            trigger_intents=["refund", "return"],
            available_tools=["lookup_order", "issue_refund"],
            data_context=["order_id", "customer_id"],
            steps=[
                ProcedureStep(id="collect", action=ActionType.collect_info, next_step="end"),
                ProcedureStep(id="end", action=ActionType.end),
            ],
        )
        assert len(proc.steps) == 2
        assert proc.trigger_intents == ["refund", "return"]

    def test_serialization_roundtrip(self):
        """Full serialization round-trip: model -> JSON -> model."""
        proc = Procedure(
            id="test",
            name="Test",
            steps=[
                ProcedureStep(
                    id="eval",
                    action=ActionType.evaluate,
                    conditions=[
                        ConditionBranch(
                            condition=StructuredCondition(
                                field="risk_score",
                                operator=ConditionOperator.gte,
                                value=80,
                            ),
                            next_step="high_risk",
                        ),
                    ],
                ),
                ProcedureStep(id="high_risk", action=ActionType.end),
            ],
        )
        json_str = proc.model_dump_json()
        restored = Procedure.model_validate_json(json_str)
        assert restored.id == proc.id
        assert restored.steps[0].conditions[0].condition.value == 80


# ---------------------------------------------------------------------------
# ProcedureOverview tests (ingestion intermediate schema)
# ---------------------------------------------------------------------------

class TestProcedureOverview:
    """Test intermediate schema for ingestion Pass 1."""

    def test_overview(self):
        overview = ProcedureOverview(
            id="refund_proc",
            name="Refund Procedure",
            domain="customer_service",
            steps=[
                StepOverview(id="collect", name="Collect Info", action=ActionType.collect_info),
                StepOverview(id="end", name="End", action=ActionType.end),
            ],
        )
        assert len(overview.steps) == 2
        assert overview.steps[0].action == ActionType.collect_info
