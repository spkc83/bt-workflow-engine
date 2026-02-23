"""Integration tests for the BT runner with mock LLM responses."""

from unittest.mock import AsyncMock, MagicMock, patch

import py_trees
import pytest

from bt_engine.nodes import (
    ConditionNode,
    LLMResponseNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)
from bt_engine.runner import BTRunner


@pytest.fixture(autouse=True)
def _clear_blackboard():
    py_trees.blackboard.Blackboard.enable_activity_stream()
    yield
    py_trees.blackboard.Blackboard.clear()


def _make_simple_tree():
    """A minimal tree: condition -> log."""
    root = py_trees.composites.Sequence("simple", memory=True)
    root.add_children([
        ConditionNode("always_true", lambda bb: True),
        LogNode("done", message="Completed"),
    ])
    return py_trees.trees.BehaviourTree(root=root)


def _make_user_input_tree():
    """A tree that pauses for user input."""
    root = py_trees.composites.Sequence("with_input", memory=True)
    root.add_children([
        LogNode("step1", message="Step 1"),
        UserInputNode("wait_for_user"),
        LogNode("step2", message="Step 2 after input"),
    ])
    return py_trees.trees.BehaviourTree(root=root)


def _make_branching_tree():
    """A tree with deterministic branching based on a value."""
    root = py_trees.composites.Selector("branching", memory=False)

    path_a = py_trees.composites.Sequence("path_a", memory=True)
    path_a.add_children([
        ConditionNode("check_a", lambda bb: bb.get("choice") == "a"),
        LogNode("chose_a", message="Path A taken"),
    ])

    path_b = py_trees.composites.Sequence("path_b", memory=True)
    path_b.add_children([
        ConditionNode("check_b", lambda bb: bb.get("choice") == "b"),
        LogNode("chose_b", message="Path B taken"),
    ])

    root.add_children([path_a, path_b])
    return py_trees.trees.BehaviourTree(root=root)


class TestBTRunner:
    def test_simple_tree_completes(self):
        tree = _make_simple_tree()
        runner = BTRunner(tree=tree)
        result = runner.run("hello")
        assert result.status == "SUCCESS"

    def test_user_input_tree_pauses(self):
        tree = _make_user_input_tree()
        runner = BTRunner(tree=tree)

        # First run — should pause at UserInputNode
        result1 = runner.run("first message")
        assert result1.status == "RUNNING"

        # Second run — should complete
        result2 = runner.run("second message")
        assert result2.status == "SUCCESS"

    def test_branching_path_a(self):
        tree = _make_branching_tree()
        runner = BTRunner(tree=tree, session_state={"choice": "a"})
        result = runner.run("go")
        assert result.status == "SUCCESS"

        # Verify path A was taken
        trace_nodes = [e["node_name"] for e in result.trace]
        assert "chose_a" in trace_nodes

    def test_branching_path_b(self):
        tree = _make_branching_tree()
        runner = BTRunner(tree=tree, session_state={"choice": "b"})
        result = runner.run("go")
        assert result.status == "SUCCESS"

        trace_nodes = [e["node_name"] for e in result.trace]
        assert "chose_b" in trace_nodes

    def test_trace_is_populated(self):
        tree = _make_simple_tree()
        runner = BTRunner(tree=tree)
        result = runner.run("hello")
        assert len(result.trace) > 0
        assert all("node_name" in e for e in result.trace)
        assert all("status" in e for e in result.trace)

    def test_blackboard_state_returned(self):
        tree = _make_simple_tree()
        runner = BTRunner(tree=tree, session_state={"customer_id": "CUST-123"})
        result = runner.run("hello")
        assert result.blackboard_state["customer_id"] == "CUST-123"

    def test_case_id_auto_generated(self):
        tree = _make_simple_tree()
        runner = BTRunner(tree=tree)
        result = runner.run("hello")
        assert "case_id" in result.blackboard_state
        assert result.blackboard_state["case_id"].startswith("CASE-")

    def test_conversation_history_tracked(self):
        tree = _make_user_input_tree()
        runner = BTRunner(tree=tree)
        runner.run("first")
        runner.run("second")

        bb = py_trees.blackboard.Client(name="test_reader")
        bb.register_key(key="conversation_history", access=py_trees.common.Access.READ)
        try:
            history = bb.get("conversation_history")
        except (KeyError, AttributeError):
            history = []
        user_messages = [h for h in history if h["role"] == "user"]
        assert len(user_messages) == 2

    def test_trace_summary(self):
        tree = _make_simple_tree()
        runner = BTRunner(tree=tree)
        runner.run("hello")
        summary = runner.get_trace_summary()
        assert summary["ticks"] >= 1
        assert summary["unique_nodes"] >= 1

    def test_determinism_same_input_same_path(self):
        """Same input should always produce the same tree path."""
        for _ in range(3):
            py_trees.blackboard.Blackboard.clear()

            tree = _make_branching_tree()
            runner = BTRunner(tree=tree, session_state={"choice": "a"})
            result = runner.run("go")
            trace_nodes = [e["node_name"] for e in result.trace if e["status"] == "SUCCESS"]
            assert "chose_a" in trace_nodes
            assert "chose_b" not in trace_nodes
