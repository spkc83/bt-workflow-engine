"""Tests for constrained decoding utilities and LLMClassifyNode integration.

Uses mocked LLM responses to test the constrained decoding pathway
without requiring actual API calls.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bt_engine.compiler.llm_utils import classify_enum, generate_structured, make_dynamic_enum
from bt_engine.compiler.schemas import StructuredCondition, ConditionOperator


# ---------------------------------------------------------------------------
# make_dynamic_enum tests
# ---------------------------------------------------------------------------

class TestMakeDynamicEnum:
    """Test dynamic enum creation utility."""

    def test_creates_enum(self):
        MyEnum = make_dynamic_enum("TestCat", ["fraud_confirmed", "false_positive", "needs_review"])
        assert issubclass(MyEnum, Enum)
        assert len(MyEnum) == 3

    def test_enum_values(self):
        MyEnum = make_dynamic_enum("Cat", ["low", "medium", "high"])
        assert MyEnum["low"].value == "low"
        assert MyEnum["high"].value == "high"

    def test_single_value(self):
        MyEnum = make_dynamic_enum("Single", ["only_option"])
        assert len(MyEnum) == 1

    def test_used_as_type(self):
        """Dynamic enums can be used for isinstance checks."""
        MyEnum = make_dynamic_enum("Status", ["active", "inactive"])
        val = MyEnum["active"]
        assert isinstance(val, MyEnum)


# ---------------------------------------------------------------------------
# generate_structured tests (mocked)
# ---------------------------------------------------------------------------

class TestGenerateStructured:
    """Test structured generation with mocked LLM."""

    @pytest.mark.asyncio
    async def test_returns_valid_model(self):
        mock_response = MagicMock()
        mock_response.text = '{"field": "risk_score", "operator": "gte", "value": 80}'

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        with patch("bt_engine.compiler.llm_utils.get_genai_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="gemini-2.5-flash"):
            result = await generate_structured("test prompt", StructuredCondition)

        assert isinstance(result, StructuredCondition)
        assert result.field == "risk_score"
        assert result.operator == ConditionOperator.gte
        assert result.value == 80

    @pytest.mark.asyncio
    async def test_passes_schema_to_config(self):
        mock_response = MagicMock()
        mock_response.text = '{"field": "severity", "operator": "eq", "value": "high"}'

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        with patch("bt_engine.compiler.llm_utils.get_genai_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="gemini-2.5-flash"):
            await generate_structured("test", StructuredCondition)

        # Verify generate_content was called with a config containing response_schema
        call_kwargs = mock_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert config.response_mime_type == "application/json"


# ---------------------------------------------------------------------------
# classify_enum tests (mocked)
# ---------------------------------------------------------------------------

class TestClassifyEnum:
    """Test enum classification with mocked LLM."""

    @pytest.mark.asyncio
    async def test_returns_valid_enum_value(self):
        mock_response = MagicMock()
        mock_response.text = "high"

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        SeverityEnum = make_dynamic_enum("Severity", ["low", "medium", "high"])

        with patch("bt_engine.compiler.llm_utils.get_genai_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="gemini-2.5-flash"):
            result = await classify_enum("classify severity", SeverityEnum)

        assert result == "high"

    @pytest.mark.asyncio
    async def test_passes_enum_to_config(self):
        mock_response = MagicMock()
        mock_response.text = "fraud_confirmed"

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        CatEnum = make_dynamic_enum("Category", ["fraud_confirmed", "false_positive"])

        with patch("bt_engine.compiler.llm_utils.get_genai_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="gemini-2.5-flash"):
            await classify_enum("test", CatEnum)

        call_kwargs = mock_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert config.response_mime_type == "text/x.enum"


# ---------------------------------------------------------------------------
# LLMClassifyNode constrained decoding tests (mocked)
# ---------------------------------------------------------------------------

class TestLLMClassifyNodeConstrained:
    """Test that LLMClassifyNode uses constrained decoding."""

    def _make_bb(self) -> dict:
        """Create a blackboard dict for testing."""
        return {
            "user_message": "This is clearly fraud",
            "agent_response": "",
            "awaiting_input": False,
            "audit_trail": [],
            "_audit_trail": [],
            "_tick_count": 1,
        }

    @pytest.mark.asyncio
    async def test_classify_node_uses_constrained(self):
        """LLMClassifyNode should attempt constrained enum decoding."""
        from bt_engine.behaviour_tree import Status
        from bt_engine.nodes import LLMClassifyNode

        bb = self._make_bb()
        node = LLMClassifyNode(
            name="test_classify",
            prompt_template="Classify the alert outcome",
            categories=["fraud_confirmed", "false_positive", "needs_review"],
            result_key="classification",
        )

        # Mock the constrained classify call
        mock_response = MagicMock()
        mock_response.text = "fraud_confirmed"

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        with patch("bt_engine.compiler.llm_utils.get_genai_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="gemini-2.5-flash"):
            status = await node.tick(bb)

        assert status == Status.SUCCESS
        assert bb["classification"] == "fraud_confirmed"

    @pytest.mark.asyncio
    async def test_classify_node_fallback_on_error(self):
        """LLMClassifyNode falls back to free-text if constrained fails."""
        from bt_engine.behaviour_tree import Status
        from bt_engine.nodes import LLMClassifyNode

        bb = self._make_bb()
        node = LLMClassifyNode(
            name="test_classify_fallback",
            prompt_template="Classify this",
            categories=["approve", "deny"],
            result_key="result",
        )

        # Make constrained path raise, then free-text returns valid result
        mock_response = MagicMock()
        mock_response.text = "approve"

        mock_client = MagicMock()
        # First call (constrained) fails, second call (free-text) succeeds
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=[Exception("constrained not supported"), mock_response]
        )

        with patch("bt_engine.compiler.llm_utils.get_genai_client", return_value=mock_client), \
             patch("bt_engine.compiler.llm_utils.get_model_name", return_value="gemini-2.5-flash"), \
             patch("bt_engine.nodes.get_genai_client", return_value=mock_client), \
             patch("bt_engine.nodes.get_model_name", return_value="gemini-2.5-flash"):
            status = await node.tick(bb)

        assert status == Status.SUCCESS
        assert bb["result"] == "approve"
