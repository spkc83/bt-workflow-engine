"""Tests for the BT audit trail."""

import pytest

from bt_engine.audit import AuditCollector
from bt_engine.behaviour_tree import BehaviourTree, Selector, Sequence, Status
from bt_engine.nodes import ConditionNode, LogNode


def _make_bb(data: dict | None = None) -> dict:
    """Create a blackboard dict with standard keys."""
    bb = {
        "user_message": "",
        "agent_response": "",
        "awaiting_input": False,
        "audit_trail": [],
        "_audit_trail": [],
        "_tick_count": 0,
    }
    if data:
        bb.update(data)
    return bb


class TestAuditCollector:
    @pytest.mark.asyncio
    async def test_records_node_visits(self):
        bb = _make_bb({"value": True})
        collector = AuditCollector()

        root = Sequence("test_seq", memory=False)
        root.add_children([
            ConditionNode("cond1", lambda bb: bb.get("value", False)),
            LogNode("log1", message="Done"),
        ])

        tree = BehaviourTree(root=root)
        bb["_tick_count"] = 1
        collector.tick_count = 1
        await tree.tick(bb)

        trace = collector.get_trace(bb)
        assert len(trace) > 0
        node_names = [e["node_name"] for e in trace]
        assert "cond1" in node_names
        assert "log1" in node_names

    @pytest.mark.asyncio
    async def test_tick_count_increments(self):
        bb = _make_bb()
        collector = AuditCollector()

        root = LogNode("simple_log")
        tree = BehaviourTree(root=root)

        bb["_tick_count"] = 1
        collector.tick_count = 1
        await tree.tick(bb)

        bb["_tick_count"] = 2
        collector.tick_count = 2
        await tree.tick(bb)

        assert collector.tick_count == 2

    @pytest.mark.asyncio
    async def test_summary_has_expected_fields(self):
        bb = _make_bb({"value": True})
        collector = AuditCollector()
        collector.tick_count = 1

        root = ConditionNode("cond", lambda bb: True)
        tree = BehaviourTree(root=root)
        bb["_tick_count"] = 1
        await tree.tick(bb)

        summary = collector.get_summary(bb)
        assert "ticks" in summary
        assert "nodes_visited" in summary
        assert "unique_nodes" in summary
        assert summary["ticks"] == 1

    @pytest.mark.asyncio
    async def test_execution_path_filters_success_running(self):
        bb = _make_bb({"value": True})
        collector = AuditCollector()
        collector.tick_count = 1

        root = Selector("sel", memory=False)
        root.add_children([
            ConditionNode("fail_cond", lambda bb: False),
            ConditionNode("pass_cond", lambda bb: True),
        ])

        tree = BehaviourTree(root=root)
        bb["_tick_count"] = 1
        await tree.tick(bb)

        path = collector.get_execution_path(bb)
        statuses = [e["status"] for e in path]
        assert all(s in ("SUCCESS", "RUNNING") for s in statuses)

    @pytest.mark.asyncio
    async def test_clear_resets_trace(self):
        bb = _make_bb()
        collector = AuditCollector()
        collector.tick_count = 1

        root = LogNode("log")
        tree = BehaviourTree(root=root)
        bb["_tick_count"] = 1
        await tree.tick(bb)

        assert len(collector.get_trace(bb)) > 0
        collector.clear(bb)
        assert len(collector.get_trace(bb)) == 0
        assert collector.tick_count == 0
