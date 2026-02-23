"""Tests for the BT audit trail visitor."""

import py_trees
import pytest

from bt_engine.audit import AuditVisitor
from bt_engine.nodes import ConditionNode, LogNode


@pytest.fixture(autouse=True)
def _clear_blackboard():
    py_trees.blackboard.Blackboard.enable_activity_stream()
    yield
    py_trees.blackboard.Blackboard.clear()


def _setup_bb_dict(data: dict):
    client = py_trees.blackboard.Client(name="test_setup")
    client.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)
    client.register_key(key="audit_trail", access=py_trees.common.Access.WRITE)
    client.set("bb_dict", data)
    client.set("audit_trail", [])


class TestAuditVisitor:
    def test_records_node_visits(self):
        _setup_bb_dict({"value": True})
        visitor = AuditVisitor()

        root = py_trees.composites.Sequence("test_seq", memory=False)
        root.add_children([
            ConditionNode("cond1", lambda bb: bb.get("value", False)),
            LogNode("log1", message="Done"),
        ])

        tree = py_trees.trees.BehaviourTree(root=root)
        tree.visitors.append(visitor)
        tree.setup()
        tree.tick()

        trace = visitor.get_trace()
        assert len(trace) > 0
        node_names = [e["node_name"] for e in trace]
        assert "cond1" in node_names
        assert "log1" in node_names

    def test_tick_count_increments(self):
        _setup_bb_dict({})
        visitor = AuditVisitor()

        root = LogNode("simple_log")
        tree = py_trees.trees.BehaviourTree(root=root)
        tree.visitors.append(visitor)
        tree.setup()

        tree.tick()
        tree.tick()

        assert visitor.tick_count == 2

    def test_summary_has_expected_fields(self):
        _setup_bb_dict({"value": True})
        visitor = AuditVisitor()

        root = ConditionNode("cond", lambda bb: True)
        tree = py_trees.trees.BehaviourTree(root=root)
        tree.visitors.append(visitor)
        tree.setup()
        tree.tick()

        summary = visitor.get_summary()
        assert "ticks" in summary
        assert "nodes_visited" in summary
        assert "unique_nodes" in summary
        assert summary["ticks"] == 1

    def test_execution_path_filters_success_running(self):
        _setup_bb_dict({"value": True})
        visitor = AuditVisitor()

        root = py_trees.composites.Selector("sel", memory=False)
        root.add_children([
            ConditionNode("fail_cond", lambda bb: False),
            ConditionNode("pass_cond", lambda bb: True),
        ])

        tree = py_trees.trees.BehaviourTree(root=root)
        tree.visitors.append(visitor)
        tree.setup()
        tree.tick()

        path = visitor.get_execution_path()
        statuses = [e["status"] for e in path]
        assert all(s in ("SUCCESS", "RUNNING") for s in statuses)

    def test_clear_resets_trace(self):
        _setup_bb_dict({})
        visitor = AuditVisitor()

        root = LogNode("log")
        tree = py_trees.trees.BehaviourTree(root=root)
        tree.visitors.append(visitor)
        tree.setup()
        tree.tick()

        assert len(visitor.get_trace()) > 0
        visitor.clear()
        assert len(visitor.get_trace()) == 0
        assert visitor.tick_count == 0
