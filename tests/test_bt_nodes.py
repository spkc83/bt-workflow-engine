"""Unit tests for custom BT node types."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bt_engine.behaviour_tree import Status
from bt_engine.nodes import (
    BlackboardWriteNode,
    ConditionNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)


def _make_bb(data: dict | None = None) -> dict:
    """Create a blackboard dict with standard keys."""
    bb = {
        "user_message": "",
        "agent_response": "",
        "awaiting_input": False,
        "audit_trail": [],
        "conversation_history": [],
        "_audit_trail": [],
        "_tick_count": 1,
    }
    if data:
        bb.update(data)
    return bb


# ---------------------------------------------------------------------------
# ConditionNode tests
# ---------------------------------------------------------------------------

class TestConditionNode:
    @pytest.mark.asyncio
    async def test_true_condition_returns_success(self):
        bb = _make_bb({"days_since_delivery": 5})
        node = ConditionNode("within_30", lambda bb: bb.get("days_since_delivery", 999) <= 30)
        assert await node.tick(bb) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_false_condition_returns_failure(self):
        bb = _make_bb({"days_since_delivery": 45})
        node = ConditionNode("within_30", lambda bb: bb.get("days_since_delivery", 999) <= 30)
        assert await node.tick(bb) == Status.FAILURE

    @pytest.mark.asyncio
    async def test_missing_key_uses_default(self):
        bb = _make_bb()
        node = ConditionNode("within_30", lambda bb: bb.get("days_since_delivery", 999) <= 30)
        # 999 > 30 → FAILURE
        assert await node.tick(bb) == Status.FAILURE

    @pytest.mark.asyncio
    async def test_order_status_check(self):
        bb = _make_bb({"order_data": {"status": "delivered"}})
        node = ConditionNode(
            "is_delivered",
            lambda bb: bb.get("order_data", {}).get("status") in ("delivered", "shipped"),
        )
        assert await node.tick(bb) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_order_status_processing(self):
        bb = _make_bb({"order_data": {"status": "processing"}})
        node = ConditionNode(
            "is_delivered",
            lambda bb: bb.get("order_data", {}).get("status") in ("delivered", "shipped"),
        )
        assert await node.tick(bb) == Status.FAILURE

    @pytest.mark.asyncio
    async def test_exception_in_predicate_returns_failure(self):
        bb = _make_bb()
        node = ConditionNode("bad_pred", lambda bb: bb["nonexistent"]["nested"])
        assert await node.tick(bb) == Status.FAILURE


# ---------------------------------------------------------------------------
# UserInputNode tests
# ---------------------------------------------------------------------------

class TestUserInputNode:
    @pytest.mark.asyncio
    async def test_first_tick_returns_running(self):
        bb = _make_bb()
        node = UserInputNode("wait")
        assert await node.tick(bb) == Status.RUNNING

    @pytest.mark.asyncio
    async def test_second_tick_returns_success(self):
        bb = _make_bb()
        node = UserInputNode("wait")
        # First tick → RUNNING
        await node.tick(bb)
        # Second tick → SUCCESS
        assert await node.tick(bb) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_awaiting_input_flag_set(self):
        bb = _make_bb()
        node = UserInputNode("wait")
        await node.tick(bb)
        assert bb["awaiting_input"] is True


# ---------------------------------------------------------------------------
# BlackboardWriteNode tests
# ---------------------------------------------------------------------------

class TestBlackboardWriteNode:
    @pytest.mark.asyncio
    async def test_writes_values(self):
        bb = _make_bb({"existing": "value"})
        node = BlackboardWriteNode("write_test", lambda bb: {"new_key": "new_value"})
        assert await node.tick(bb) == Status.SUCCESS
        assert bb["new_key"] == "new_value"
        assert bb["existing"] == "value"

    @pytest.mark.asyncio
    async def test_overwrites_existing(self):
        bb = _make_bb({"key": "old"})
        node = BlackboardWriteNode("write_test", lambda bb: {"key": "new"})
        assert await node.tick(bb) == Status.SUCCESS
        assert bb["key"] == "new"


# ---------------------------------------------------------------------------
# LogNode tests
# ---------------------------------------------------------------------------

class TestLogNode:
    @pytest.mark.asyncio
    async def test_appends_to_audit_trail(self):
        bb = _make_bb()
        node = LogNode("test_log", message="Step completed")
        assert await node.tick(bb) == Status.SUCCESS
        trail = bb["audit_trail"]
        assert len(trail) == 1
        assert trail[0]["node"] == "test_log"
        assert trail[0]["message"] == "Step completed"

    @pytest.mark.asyncio
    async def test_always_returns_success(self):
        bb = _make_bb()
        node = LogNode("test_log")
        assert await node.tick(bb) == Status.SUCCESS


# ---------------------------------------------------------------------------
# ToolActionNode tests
# ---------------------------------------------------------------------------

class TestToolActionNode:
    @pytest.mark.asyncio
    async def test_calls_tool_with_correct_args(self):
        bb = _make_bb({"order_id": "ORD-123"})

        async def mock_tool(order_id: str, bb: dict) -> dict:
            return {"order_id": order_id, "found": True}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
            result_key="tool_result",
        )
        result = await node.tick(bb)
        assert result == Status.SUCCESS
        assert bb["tool_result"]["found"] is True

    @pytest.mark.asyncio
    async def test_missing_arg_skipped(self):
        bb = _make_bb()  # No order_id

        async def mock_tool(bb: dict, order_id: str = None) -> dict:
            return {"found": True}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
        )
        # None args are skipped, tool still called
        assert await node.tick(bb) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_not_found_returns_failure(self):
        bb = _make_bb({"order_id": "ORD-FAKE"})

        async def mock_tool(order_id: str, bb: dict) -> dict:
            return {"found": False, "error": "Not found"}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
        )
        assert await node.tick(bb) == Status.FAILURE

    @pytest.mark.asyncio
    async def test_fixed_args_passed(self):
        captured = {}
        bb = _make_bb({"order_id": "ORD-123"})

        async def mock_tool(order_id: str, reason: str, bb: dict) -> dict:
            captured["reason"] = reason
            return {"found": True}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
            fixed_args={"reason": "test reason"},
        )
        await node.tick(bb)
        assert captured["reason"] == "test reason"
