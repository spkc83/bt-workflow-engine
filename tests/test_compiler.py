"""Tests for the BT compiler: condition parser, tool registry, parser, step compilers, and full compilation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Condition parser tests
# ---------------------------------------------------------------------------

from bt_engine.compiler.condition_parser import parse_condition


class TestConditionParser:
    """Test the regex-based condition parser."""

    def test_equality_string(self):
        pred = parse_condition("severity == high")
        assert pred is not None
        assert pred({"alert_data": {"severity": "high"}}) is True
        assert pred({"alert_data": {"severity": "low"}}) is False

    def test_equality_case_insensitive(self):
        pred = parse_condition("severity == HIGH")
        assert pred is not None
        assert pred({"alert_data": {"severity": "high"}}) is True

    def test_equality_processing(self):
        pred = parse_condition("order_status == processing")
        assert pred is not None
        assert pred({"order_data": {"status": "processing"}}) is True
        assert pred({"order_data": {"status": "delivered"}}) is False

    def test_gte(self):
        pred = parse_condition("risk_score >= 80")
        assert pred is not None
        assert pred({"alert_data": {"risk_score": 85}}) is True
        assert pred({"alert_data": {"risk_score": 80}}) is True
        assert pred({"alert_data": {"risk_score": 50}}) is False

    def test_lt(self):
        pred = parse_condition("risk_score < 40")
        assert pred is not None
        assert pred({"alert_data": {"risk_score": 30}}) is True
        assert pred({"alert_data": {"risk_score": 40}}) is False

    def test_in_list(self):
        pred = parse_condition("order_status in [delivered, shipped]")
        assert pred is not None
        assert pred({"order_data": {"status": "delivered"}}) is True
        assert pred({"order_data": {"status": "shipped"}}) is True
        assert pred({"order_data": {"status": "processing"}}) is False

    def test_in_list_complaint_type(self):
        pred = parse_condition("complaint_type in [product_quality, delivery]")
        assert pred is not None
        assert pred({"complaint_type": "product_quality"}) is True
        assert pred({"complaint_type": "service"}) is False

    def test_within_days(self):
        pred = parse_condition("order_date within 30 days")
        assert pred is not None
        assert pred({"order_data": {"days_since_delivery": 15}}) is True
        assert pred({"order_data": {"days_since_delivery": 30}}) is True
        assert pred({"order_data": {"days_since_delivery": 45}}) is False

    def test_outside_days(self):
        pred = parse_condition("order_date outside 30 days")
        assert pred is not None
        assert pred({"order_data": {"days_since_delivery": 45}}) is True
        assert pred({"order_data": {"days_since_delivery": 30}}) is False
        assert pred({"order_data": {"days_since_delivery": 15}}) is False

    def test_and_combinator(self):
        pred = parse_condition(
            "order_date within 30 days AND order_status in [delivered, shipped]"
        )
        assert pred is not None
        # Both true
        assert pred({"order_data": {"days_since_delivery": 10, "status": "delivered"}}) is True
        # One false
        assert pred({"order_data": {"days_since_delivery": 45, "status": "delivered"}}) is False
        assert pred({"order_data": {"days_since_delivery": 10, "status": "processing"}}) is False

    def test_or_combinator(self):
        pred = parse_condition("severity == high OR risk_score >= 80")
        assert pred is not None
        assert pred({"alert_data": {"severity": "high", "risk_score": 50}}) is True
        assert pred({"alert_data": {"severity": "low", "risk_score": 90}}) is True
        assert pred({"alert_data": {"severity": "low", "risk_score": 50}}) is False

    def test_not_in_list(self):
        pred = parse_condition("category not in non_refundable_list")
        assert pred is not None
        # This is a soft condition — always True in current implementation
        assert pred({}) is True

    def test_unparseable_returns_none(self):
        """Subjective/complex conditions should return None."""
        result = parse_condition(
            "multiple high-confidence fraud indicators present (new device + geo anomaly + velocity + large transaction)"
        )
        assert result is None

    def test_unparseable_subjective(self):
        result = parse_condition("some indicators present but not conclusive")
        assert result is None

    def test_missing_field_defaults(self):
        """Missing fields should use safe defaults."""
        pred = parse_condition("risk_score >= 80")
        assert pred is not None
        # Missing alert_data entirely
        assert pred({}) is False

        pred2 = parse_condition("order_date within 30 days")
        assert pred2 is not None
        # Missing order_data -> default 999 -> not within 30
        assert pred2({}) is False

    def test_zero_days_since_delivery(self):
        """days_since_delivery=0 should be 'within' any window (not treated as falsy)."""
        pred_within = parse_condition("order_date within 30 days")
        assert pred_within is not None
        # 0 days since delivery = just delivered today -> within 30 days
        assert pred_within({"order_data": {"days_since_delivery": 0}}) is True

        pred_outside = parse_condition("order_date outside 30 days")
        assert pred_outside is not None
        # 0 days since delivery -> NOT outside 30 days
        assert pred_outside({"order_data": {"days_since_delivery": 0}}) is False

    def test_zero_risk_score(self):
        """risk_score=0 should not be treated as missing."""
        pred = parse_condition("risk_score >= 0")
        assert pred is not None
        assert pred({"alert_data": {"risk_score": 0}}) is True


# ---------------------------------------------------------------------------
# Tool registry tests
# ---------------------------------------------------------------------------

from bt_engine.compiler.tool_registry import ToolRegistry, _infer_arg_keys, create_default_registry


class TestToolRegistry:
    """Test the tool registry."""

    def test_register_and_get(self):
        registry = ToolRegistry()

        async def my_tool(order_id: str, bb: dict):
            return {"ok": True}

        registry.register("my_tool", my_tool)
        entry = registry.get("my_tool")
        assert entry is not None
        assert entry.func is my_tool
        assert entry.arg_keys == {"order_id": "order_id"}

    def test_infer_arg_keys_skips_bb(self):
        async def tool(order_id: str, reason: str, bb: dict):
            pass

        keys = _infer_arg_keys(tool)
        assert keys == {"order_id": "order_id", "reason": "reason"}
        assert "bb" not in keys

    def test_infer_arg_keys_skips_defaults(self):
        async def tool(customer_id: str, bb: dict, merchant_name: str = None, amount: float = None):
            pass

        keys = _infer_arg_keys(tool)
        assert keys == {"customer_id": "customer_id"}

    def test_custom_arg_keys(self):
        registry = ToolRegistry()

        async def tool(x: str, bb: dict):
            pass

        registry.register("tool", tool, arg_keys={"x": "custom_key"})
        entry = registry.get("tool")
        assert entry.arg_keys == {"x": "custom_key"}

    def test_fixed_args(self):
        registry = ToolRegistry()

        async def tool(x: str, bb: dict):
            pass

        registry.register("tool", tool, fixed_args={"extra": "value"})
        entry = registry.get("tool")
        assert entry.fixed_args == {"extra": "value"}

    def test_get_missing(self):
        registry = ToolRegistry()
        assert registry.get("nonexistent") is None

    def test_has(self):
        registry = ToolRegistry()

        async def tool(bb: dict):
            pass

        registry.register("exists", tool)
        assert registry.has("exists") is True
        assert registry.has("nope") is False

    def test_default_registry_completeness(self):
        """The default registry should have all 15 tools registered."""
        registry = create_default_registry()
        expected_tools = [
            "lookup_order", "search_orders", "get_customer_profile",
            "issue_refund", "issue_store_credit", "update_case_status",
            "escalate_to_supervisor", "add_case_note", "get_knowledge_article",
            "get_fraud_alert", "get_account_transactions", "check_device_fingerprint",
            "flag_account", "submit_sar", "close_alert",
        ]
        for name in expected_tools:
            assert registry.has(name), f"Missing tool: {name}"

        assert len(registry.tool_names) == len(expected_tools)


# ---------------------------------------------------------------------------
# YAML parser tests
# ---------------------------------------------------------------------------

from bt_engine.compiler.parser import load_and_validate, ProcedureValidationError


class TestParser:
    """Test YAML loading and validation."""

    def test_load_refund(self):
        proc = load_and_validate("procedures/customer_service_refund.yaml")
        assert proc["id"] == "cs_refund"
        assert len(proc["steps"]) > 0

    def test_load_complaint(self):
        proc = load_and_validate("procedures/customer_service_complaint.yaml")
        assert proc["id"] == "cs_complaint"

    def test_load_fraud(self):
        proc = load_and_validate("procedures/fraud_ops_alert_triage.yaml")
        assert proc["id"] == "fraud_alert_triage"

    def test_steps_normalized(self):
        proc = load_and_validate("procedures/customer_service_refund.yaml")
        for step in proc["steps"]:
            assert "instruction" in step
            assert "id" in step
            assert "action" in step

    def test_invalid_file(self, tmp_path):
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("not a procedure")
        with pytest.raises(ProcedureValidationError):
            load_and_validate(bad_file)

    def test_missing_file(self):
        with pytest.raises(ProcedureValidationError):
            load_and_validate("/nonexistent/path.yaml")

    def test_missing_steps(self, tmp_path):
        bad_file = tmp_path / "no_steps.yaml"
        bad_file.write_text("procedure:\n  id: test\n  name: Test\n")
        with pytest.raises(ProcedureValidationError):
            load_and_validate(bad_file)

    def test_duplicate_step_ids(self, tmp_path):
        bad_file = tmp_path / "dup.yaml"
        bad_file.write_text(
            "procedure:\n  id: test\n  name: Test\n  steps:\n"
            "    - id: step1\n      action: end\n"
            "    - id: step1\n      action: end\n"
        )
        with pytest.raises(ProcedureValidationError, match="Duplicate"):
            load_and_validate(bad_file)


# ---------------------------------------------------------------------------
# Full compilation tests
# ---------------------------------------------------------------------------

import py_trees
from bt_engine.compiler import ProcedureCompiler


class TestFullCompilation:
    """Test that each YAML procedure compiles into a valid BehaviourTree."""

    @pytest.fixture
    def compiler(self):
        return ProcedureCompiler()

    def test_compile_refund(self, compiler):
        tree = compiler.compile("procedures/customer_service_refund.yaml")
        assert isinstance(tree, py_trees.trees.BehaviourTree)
        assert tree.root is not None
        assert "cs_refund" in tree.root.name

    def test_compile_complaint(self, compiler):
        tree = compiler.compile("procedures/customer_service_complaint.yaml")
        assert isinstance(tree, py_trees.trees.BehaviourTree)
        assert tree.root is not None
        assert "cs_complaint" in tree.root.name

    def test_compile_fraud(self, compiler):
        tree = compiler.compile("procedures/fraud_ops_alert_triage.yaml")
        assert isinstance(tree, py_trees.trees.BehaviourTree)
        assert tree.root is not None
        assert "fraud_alert_triage" in tree.root.name

    def test_compiled_tree_has_children(self, compiler):
        tree = compiler.compile("procedures/customer_service_refund.yaml")
        # Root should be a Sequence with children
        assert hasattr(tree.root, "children")
        assert len(tree.root.children) > 0

    def test_fresh_compilation_per_call(self, compiler):
        """Each compile call should return a distinct tree instance."""
        tree1 = compiler.compile("procedures/customer_service_refund.yaml")
        tree2 = compiler.compile("procedures/customer_service_refund.yaml")
        assert tree1 is not tree2
        assert tree1.root is not tree2.root


# ---------------------------------------------------------------------------
# Tree manager tests
# ---------------------------------------------------------------------------

from bt_engine.compiler.tree_manager import TreeManager


class TestTreeManager:
    """Test runtime tree management."""

    @pytest.fixture
    def manager(self):
        mgr = TreeManager(procedures_dir="procedures")
        mgr.load_all()
        return mgr

    def test_load_all(self, manager):
        assert len(manager.get_all_intents()) > 0
        assert len(manager.get_all_procedures()) == 4

    def test_get_tree_factory_refund(self, manager):
        factory = manager.get_tree_factory("refund")
        assert factory is not None
        tree = factory()
        assert isinstance(tree, py_trees.trees.BehaviourTree)

    def test_get_tree_factory_complaint(self, manager):
        factory = manager.get_tree_factory("complaint")
        assert factory is not None
        tree = factory()
        assert isinstance(tree, py_trees.trees.BehaviourTree)

    def test_get_tree_factory_fraud(self, manager):
        factory = manager.get_tree_factory("fraud_alert")
        assert factory is not None
        tree = factory()
        assert isinstance(tree, py_trees.trees.BehaviourTree)

    def test_unknown_intent(self, manager):
        factory = manager.get_tree_factory("unknown_workflow")
        assert factory is None

    def test_intent_normalization(self, manager):
        # Various trigger phrasings should map to the same factory
        assert manager.get_tree_factory("refund") is not None
        assert manager.get_tree_factory("return") is not None
        assert manager.get_tree_factory("money back") is not None

    def test_fresh_tree_per_factory_call(self, manager):
        factory = manager.get_tree_factory("refund")
        tree1 = factory()
        tree2 = factory()
        assert tree1 is not tree2

    def test_reload(self, manager):
        """Reload should clear and re-populate."""
        manager.reload_all()
        assert len(manager.get_all_procedures()) == 4

    def test_all_procedures_metadata(self, manager):
        procs = manager.get_all_procedures()
        for proc in procs:
            assert "id" in proc
            assert "name" in proc
            assert "trigger_intents" in proc
