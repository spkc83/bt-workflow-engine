"""Unit tests for custom BT node types."""

import asyncio
from unittest.mock import AsyncMock, patch

import py_trees
import pytest

from bt_engine.nodes import (
    BlackboardWriteNode,
    ConditionNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)


@pytest.fixture(autouse=True)
def _clear_blackboard():
    """Clear the py_trees blackboard before each test."""
    py_trees.blackboard.Blackboard.enable_activity_stream()
    yield
    py_trees.blackboard.Blackboard.clear()


def _setup_bb_dict(data: dict):
    """Helper to set up the bb_dict on the blackboard."""
    client = py_trees.blackboard.Client(name="test_setup")
    client.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)
    client.register_key(key="user_message", access=py_trees.common.Access.WRITE)
    client.register_key(key="agent_response", access=py_trees.common.Access.WRITE)
    client.register_key(key="awaiting_input", access=py_trees.common.Access.WRITE)
    client.register_key(key="audit_trail", access=py_trees.common.Access.WRITE)
    client.register_key(key="conversation_history", access=py_trees.common.Access.WRITE)
    client.set("bb_dict", data)
    client.set("user_message", "")
    client.set("agent_response", "")
    client.set("awaiting_input", False)
    client.set("audit_trail", [])
    client.set("conversation_history", [])
    return client


# ---------------------------------------------------------------------------
# ConditionNode tests
# ---------------------------------------------------------------------------

class TestConditionNode:
    def test_true_condition_returns_success(self):
        _setup_bb_dict({"days_since_delivery": 5})
        node = ConditionNode("within_30", lambda bb: bb.get("days_since_delivery", 999) <= 30)
        node.initialise()
        assert node.update() == py_trees.common.Status.SUCCESS

    def test_false_condition_returns_failure(self):
        _setup_bb_dict({"days_since_delivery": 45})
        node = ConditionNode("within_30", lambda bb: bb.get("days_since_delivery", 999) <= 30)
        node.initialise()
        assert node.update() == py_trees.common.Status.FAILURE

    def test_missing_key_uses_default(self):
        _setup_bb_dict({})
        node = ConditionNode("within_30", lambda bb: bb.get("days_since_delivery", 999) <= 30)
        node.initialise()
        # 999 > 30 → FAILURE
        assert node.update() == py_trees.common.Status.FAILURE

    def test_order_status_check(self):
        _setup_bb_dict({"order_data": {"status": "delivered"}})
        node = ConditionNode(
            "is_delivered",
            lambda bb: bb.get("order_data", {}).get("status") in ("delivered", "shipped"),
        )
        node.initialise()
        assert node.update() == py_trees.common.Status.SUCCESS

    def test_order_status_processing(self):
        _setup_bb_dict({"order_data": {"status": "processing"}})
        node = ConditionNode(
            "is_delivered",
            lambda bb: bb.get("order_data", {}).get("status") in ("delivered", "shipped"),
        )
        node.initialise()
        assert node.update() == py_trees.common.Status.FAILURE

    def test_exception_in_predicate_returns_failure(self):
        _setup_bb_dict({})
        node = ConditionNode("bad_pred", lambda bb: bb["nonexistent"]["nested"])
        node.initialise()
        assert node.update() == py_trees.common.Status.FAILURE


# ---------------------------------------------------------------------------
# UserInputNode tests
# ---------------------------------------------------------------------------

class TestUserInputNode:
    def test_first_tick_returns_running(self):
        _setup_bb_dict({})
        node = UserInputNode("wait")
        node.initialise()
        assert node.update() == py_trees.common.Status.RUNNING

    def test_second_tick_returns_success(self):
        bb = _setup_bb_dict({})
        node = UserInputNode("wait")
        node.initialise()
        # First tick → RUNNING
        node.update()
        # Second tick → SUCCESS
        assert node.update() == py_trees.common.Status.SUCCESS

    def test_awaiting_input_flag_set(self):
        bb = _setup_bb_dict({})
        node = UserInputNode("wait")
        node.initialise()
        node.update()
        assert bb.get("awaiting_input") is True


# ---------------------------------------------------------------------------
# BlackboardWriteNode tests
# ---------------------------------------------------------------------------

class TestBlackboardWriteNode:
    def test_writes_values(self):
        bb = _setup_bb_dict({"existing": "value"})
        node = BlackboardWriteNode("write_test", lambda bb: {"new_key": "new_value"})
        node.initialise()
        assert node.update() == py_trees.common.Status.SUCCESS
        assert bb.get("bb_dict")["new_key"] == "new_value"
        assert bb.get("bb_dict")["existing"] == "value"

    def test_overwrites_existing(self):
        bb = _setup_bb_dict({"key": "old"})
        node = BlackboardWriteNode("write_test", lambda bb: {"key": "new"})
        node.initialise()
        assert node.update() == py_trees.common.Status.SUCCESS
        assert bb.get("bb_dict")["key"] == "new"


# ---------------------------------------------------------------------------
# LogNode tests
# ---------------------------------------------------------------------------

class TestLogNode:
    def test_appends_to_audit_trail(self):
        bb = _setup_bb_dict({})
        node = LogNode("test_log", message="Step completed")
        node.initialise()
        assert node.update() == py_trees.common.Status.SUCCESS
        trail = bb.get("audit_trail")
        assert len(trail) == 1
        assert trail[0]["node"] == "test_log"
        assert trail[0]["message"] == "Step completed"

    def test_always_returns_success(self):
        _setup_bb_dict({})
        node = LogNode("test_log")
        node.initialise()
        assert node.update() == py_trees.common.Status.SUCCESS


# ---------------------------------------------------------------------------
# ToolActionNode tests
# ---------------------------------------------------------------------------

class TestToolActionNode:
    def test_calls_tool_with_correct_args(self):
        bb = _setup_bb_dict({"order_id": "ORD-123"})

        async def mock_tool(order_id: str, bb: dict) -> dict:
            return {"order_id": order_id, "found": True}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
            result_key="tool_result",
        )
        node.initialise()
        result = node.update()
        assert result == py_trees.common.Status.SUCCESS
        assert bb.get("bb_dict")["tool_result"]["found"] is True

    def test_missing_arg_returns_failure(self):
        _setup_bb_dict({})  # No order_id

        async def mock_tool(order_id: str, bb: dict) -> dict:
            return {"found": True}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
        )
        node.initialise()
        assert node.update() == py_trees.common.Status.FAILURE

    def test_not_found_returns_failure(self):
        _setup_bb_dict({"order_id": "ORD-FAKE"})

        async def mock_tool(order_id: str, bb: dict) -> dict:
            return {"found": False, "error": "Not found"}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
        )
        node.initialise()
        assert node.update() == py_trees.common.Status.FAILURE

    def test_fixed_args_passed(self):
        captured = {}
        _setup_bb_dict({"order_id": "ORD-123"})

        async def mock_tool(order_id: str, reason: str, bb: dict) -> dict:
            captured["reason"] = reason
            return {"found": True}

        node = ToolActionNode(
            "call_tool",
            tool_func=mock_tool,
            arg_keys={"order_id": "order_id"},
            fixed_args={"reason": "test reason"},
        )
        node.initialise()
        node.update()
        assert captured["reason"] == "test reason"
