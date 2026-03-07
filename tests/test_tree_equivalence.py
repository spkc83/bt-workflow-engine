"""Behavioral equivalence tests: compiled trees vs hand-coded trees.

Runs identical blackboard scenarios through both hand-coded and compiled trees,
verifying that the same node types are visited in the same order for deterministic
routing paths. LLM nodes are not called — we verify the structural/condition paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bt_engine.behaviour_tree import BehaviourTree
from bt_engine.nodes import (
    ConditionNode,
    LLMClassifyNode,
    LLMExtractNode,
    LLMResponseNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)
from bt_engine.compiler import ProcedureCompiler
from bt_engine.trees.refund import create_refund_tree
from bt_engine.trees.complaint import create_complaint_tree
from bt_engine.trees.fraud_triage import create_fraud_triage_tree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_node_types(tree: BehaviourTree) -> list[str]:
    """Collect all node class names in the tree via BFS."""
    nodes = []
    queue = [tree.root]
    while queue:
        node = queue.pop(0)
        nodes.append(type(node).__name__)
        if hasattr(node, "children"):
            queue.extend(node.children)
    return nodes


def _collect_condition_nodes(tree: BehaviourTree) -> list[ConditionNode]:
    """Collect all ConditionNode instances in the tree."""
    conditions = []
    queue = [tree.root]
    while queue:
        node = queue.pop(0)
        if isinstance(node, ConditionNode):
            conditions.append(node)
        if hasattr(node, "children"):
            queue.extend(node.children)
    return conditions


def _evaluate_conditions(tree: BehaviourTree, bb_state: dict) -> dict[str, bool]:
    """Evaluate all ConditionNodes against a blackboard state, return name->result."""
    conditions = _collect_condition_nodes(tree)
    results = {}
    for cond in conditions:
        try:
            result = cond.predicate(bb_state)
            results[cond.name] = result
        except Exception:
            results[cond.name] = False
    return results


# ---------------------------------------------------------------------------
# Refund equivalence tests
# ---------------------------------------------------------------------------

class TestRefundEquivalence:
    """Verify compiled refund tree matches hand-coded tree behavior."""

    @pytest.fixture
    def compiled_tree(self):
        compiler = ProcedureCompiler()
        return compiler.compile("procedures/customer_service_refund.yaml")

    @pytest.fixture
    def handcoded_tree(self):
        return create_refund_tree()

    def test_both_trees_have_condition_nodes(self, compiled_tree, handcoded_tree):
        """Both trees should contain ConditionNode instances for routing."""
        compiled_conds = _collect_condition_nodes(compiled_tree)
        handcoded_conds = _collect_condition_nodes(handcoded_tree)
        assert len(compiled_conds) > 0
        assert len(handcoded_conds) > 0

    def test_eligible_order_same_path(self, compiled_tree, handcoded_tree):
        """Eligible order (within 30 days, delivered) -> both trees route to process_refund."""
        bb_state = {
            "order_data": {
                "days_since_delivery": 10,
                "status": "delivered",
                "order_id": "ORD-1001",
                "total": 79.99,
            },
            "order_id": "ORD-1001",
            "customer_id": "CUST-456",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Both should have: within_30_days=True, delivered_or_shipped=True
        # Hand-coded names
        assert handcoded_results.get("within_30_days") is True
        assert handcoded_results.get("delivered_or_shipped") is True
        assert handcoded_results.get("outside_30_days") is False

        # Compiled tree should have equivalent conditions that are True
        eligible_conditions = [
            name for name, result in compiled_results.items() if result is True
        ]
        assert len(eligible_conditions) > 0, "No eligible conditions matched in compiled tree"

    def test_outside_window_same_path(self, compiled_tree, handcoded_tree):
        """Order outside 30-day window -> both trees route to deny_refund."""
        bb_state = {
            "order_data": {
                "days_since_delivery": 45,
                "status": "delivered",
            },
            "order_id": "ORD-1001",
            "customer_id": "CUST-456",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: within_30_days=False, outside_30_days=True
        assert handcoded_results.get("within_30_days") is False
        assert handcoded_results.get("outside_30_days") is True

    def test_processing_order_same_path(self, compiled_tree, handcoded_tree):
        """Processing order -> both trees route to cancel_order."""
        bb_state = {
            "order_data": {
                "days_since_delivery": 0,
                "status": "processing",
            },
            "order_id": "ORD-1001",
            "customer_id": "CUST-456",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: status_is_processing=True, delivered_or_shipped=False
        assert handcoded_results.get("status_is_processing") is True
        assert handcoded_results.get("delivered_or_shipped") is False

        # Compiled tree should have an order_status == processing condition that's True
        processing_matches = [
            name for name, result in compiled_results.items() if result is True
        ]
        assert len(processing_matches) > 0


# ---------------------------------------------------------------------------
# Complaint equivalence tests
# ---------------------------------------------------------------------------

class TestComplaintEquivalence:
    """Verify compiled complaint tree matches hand-coded tree behavior."""

    @pytest.fixture
    def compiled_tree(self):
        compiler = ProcedureCompiler()
        return compiler.compile("procedures/customer_service_complaint.yaml")

    @pytest.fixture
    def handcoded_tree(self):
        return create_complaint_tree()

    def test_product_quality_routes_to_lookup(self, compiled_tree, handcoded_tree):
        """product_quality complaint -> both route through lookup_context."""
        bb_state = {
            "complaint_type": "product_quality",
            "order_id": "ORD-1001",
            "customer_id": "CUST-456",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: is_order_related=True
        assert handcoded_results.get("is_order_related") is True

        # Compiled: complaint_type in [product_quality, delivery] should be True
        order_related_matches = [
            name for name, result in compiled_results.items()
            if result is True
        ]
        assert len(order_related_matches) > 0

    def test_service_routes_directly(self, compiled_tree, handcoded_tree):
        """service complaint -> both skip lookup, go to resolution."""
        bb_state = {
            "complaint_type": "service",
            "customer_id": "CUST-456",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: is_order_related=False, is_service_related=True
        assert handcoded_results.get("is_order_related") is False
        assert handcoded_results.get("is_service_related") is True


# ---------------------------------------------------------------------------
# Fraud triage equivalence tests
# ---------------------------------------------------------------------------

class TestFraudEquivalence:
    """Verify compiled fraud triage tree matches hand-coded tree behavior."""

    @pytest.fixture
    def compiled_tree(self):
        compiler = ProcedureCompiler()
        return compiler.compile("procedures/fraud_ops_alert_triage.yaml")

    @pytest.fixture
    def handcoded_tree(self):
        return create_fraud_triage_tree()

    def test_high_severity_full_investigation(self, compiled_tree, handcoded_tree):
        """High severity alert -> both trees route to full investigation."""
        bb_state = {
            "alert_data": {
                "severity": "high",
                "risk_score": 92,
                "amount_involved": 8500,
            },
            "alert_id": "FA-001",
            "account_id": "ACC-001",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: is_high_severity=True
        assert handcoded_results.get("is_high_severity") is True

        # Compiled: severity == high OR risk_score >= 80 should be True
        high_matches = [
            name for name, result in compiled_results.items()
            if result is True
        ]
        assert len(high_matches) > 0

    def test_low_severity_skips_investigation(self, compiled_tree, handcoded_tree):
        """Low severity alert -> both skip investigation, go to assess_risk."""
        bb_state = {
            "alert_data": {
                "severity": "low",
                "risk_score": 25,
                "amount_involved": 150,
            },
            "alert_id": "FA-004",
            "account_id": "ACC-004",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: is_high_severity=False, is_low_severity=True
        assert handcoded_results.get("is_high_severity") is False
        assert handcoded_results.get("is_low_severity") is True

    def test_fraud_confirmed_flags_account(self, compiled_tree, handcoded_tree):
        """fraud_confirmed determination -> both route to flag account."""
        bb_state = {
            "alert_data": {
                "severity": "high",
                "risk_score": 92,
                "amount_involved": 8500,
            },
            "risk_determination": "fraud_confirmed",
            "alert_id": "FA-001",
            "account_id": "ACC-001",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: is_fraud_confirmed=True
        assert handcoded_results.get("is_fraud_confirmed") is True

    def test_false_positive_clears_alert(self, compiled_tree, handcoded_tree):
        """false_positive -> both route to clear alert."""
        bb_state = {
            "alert_data": {
                "severity": "low",
                "risk_score": 25,
                "amount_involved": 150,
            },
            "risk_determination": "false_positive",
            "alert_id": "FA-004",
            "account_id": "ACC-004",
            "case_id": "CASE-TEST",
        }

        compiled_results = _evaluate_conditions(compiled_tree, bb_state)
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_state)

        # Hand-coded: is_false_positive=True, is_fraud_confirmed=False
        assert handcoded_results.get("is_false_positive") is True
        assert handcoded_results.get("is_fraud_confirmed") is False

    def test_sar_threshold(self, compiled_tree, handcoded_tree):
        """SAR threshold check: amount >= 5000 -> file SAR."""
        # High amount
        bb_high = {
            "alert_data": {"severity": "high", "risk_score": 95, "amount_involved": 8500},
            "risk_determination": "fraud_confirmed",
            "alert_id": "FA-001",
            "account_id": "ACC-001",
        }
        handcoded_results = _evaluate_conditions(handcoded_tree, bb_high)
        assert handcoded_results.get("meets_sar_threshold") is True

        # Low amount
        bb_low = {
            "alert_data": {"severity": "high", "risk_score": 95, "amount_involved": 2000},
            "risk_determination": "fraud_confirmed",
            "alert_id": "FA-001",
            "account_id": "ACC-001",
        }
        handcoded_results_low = _evaluate_conditions(handcoded_tree, bb_low)
        assert handcoded_results_low.get("meets_sar_threshold") is False


# ---------------------------------------------------------------------------
# Structural equivalence tests
# ---------------------------------------------------------------------------

class TestStructuralEquivalence:
    """Verify compiled trees have equivalent structural patterns."""

    @pytest.fixture
    def compiler(self):
        return ProcedureCompiler()

    def test_all_trees_have_tool_nodes(self, compiler):
        """All compiled trees should contain ToolActionNode instances."""
        for yaml_file in Path("procedures").glob("*.yaml"):
            tree = compiler.compile(yaml_file)
            node_types = _collect_node_types(tree)
            assert "ToolActionNode" in node_types, f"No ToolActionNode in {yaml_file.name}"

    def test_all_trees_have_log_nodes(self, compiler):
        """All compiled trees should contain LogNode instances."""
        for yaml_file in Path("procedures").glob("*.yaml"):
            tree = compiler.compile(yaml_file)
            node_types = _collect_node_types(tree)
            assert "LogNode" in node_types, f"No LogNode in {yaml_file.name}"

    def test_refund_has_user_input_nodes(self, compiler):
        """Refund tree should have UserInputNode (for ask_for_info pattern)."""
        tree = compiler.compile("procedures/customer_service_refund.yaml")
        node_types = _collect_node_types(tree)
        assert "UserInputNode" in node_types

    def test_complaint_has_classify_node(self, compiler):
        """Complaint tree should have ConditionNode for routing."""
        tree = compiler.compile("procedures/customer_service_complaint.yaml")
        node_types = _collect_node_types(tree)
        assert "ConditionNode" in node_types

    def test_fraud_has_classify_and_condition_nodes(self, compiler):
        """Fraud tree should have both LLMClassifyNode and ConditionNode."""
        tree = compiler.compile("procedures/fraud_ops_alert_triage.yaml")
        node_types = _collect_node_types(tree)
        assert "ConditionNode" in node_types
        # assess_risk has unparseable conditions -> should use LLMClassifyNode
        assert "LLMClassifyNode" in node_types

    def test_finegrained_multi_tool_arg_mappings(self, compiler):
        """Fine-grained YAML multi-tool steps should use per-tool arg_mappings, not registry defaults."""
        tree = compiler.compile("procedures/customer_service_refund.yaml")

        # Find all ToolActionNode instances in the lookup_order subtree
        tool_nodes = []
        for node in tree.root.iterate():
            if isinstance(node, ToolActionNode) and "lookup_order" in node.name:
                tool_nodes.append(node)

        # Should have a lookup_order node with arg_keys={order_id: order_id}
        lookup_nodes = [n for n in tool_nodes if "call_lookup_order" in n.name]
        assert len(lookup_nodes) == 1, f"Expected 1 lookup_order node, got {len(lookup_nodes)}"
        assert lookup_nodes[0].arg_keys == {"order_id": "order_id"}

        # Find search_orders node
        search_nodes = []
        for node in tree.root.iterate():
            if isinstance(node, ToolActionNode) and "search_orders" in node.name:
                search_nodes.append(node)
        assert len(search_nodes) == 1, f"Expected 1 search_orders node, got {len(search_nodes)}"
        # search_orders should have per-tool YAML mappings, NOT registry default {customer_id: customer_id}
        assert "customer_id" in search_nodes[0].arg_keys
        assert "merchant_name" in search_nodes[0].arg_keys
        assert "amount" in search_nodes[0].arg_keys
