"""Pydantic models for the fine-grained procedure format.

These serve triple duty:
  (a) Constrained decoding schema for LLM output (via Google GenAI response_schema)
  (b) Validation of ingested procedures
  (c) Self-documenting procedure specification

Backward compatible: the compiler checks for structured fields first,
falls back to legacy string-based parsing.
"""

from __future__ import annotations

from enum import Enum
from typing import Union

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    collect_info = "collect_info"
    tool_call = "tool_call"
    evaluate = "evaluate"
    inform = "inform"
    end = "end"


class ConditionOperator(str, Enum):
    eq = "eq"
    neq = "neq"
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"
    in_list = "in"
    not_in = "not_in"
    within_days = "within_days"
    outside_days = "outside_days"
    contains = "contains"


# ---------------------------------------------------------------------------
# Condition models
# ---------------------------------------------------------------------------

class StructuredCondition(BaseModel):
    """A machine-readable condition that can be compiled to a Python predicate."""
    field: str
    operator: ConditionOperator
    value: Union[str, int, float, list]
    field_path: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def coerce_value(cls, v):
        """Allow flexible value types from LLM output."""
        return v


class ConditionBranch(BaseModel):
    """A single branch in an evaluate step.

    If condition is None and classify_categories is populated on the parent step,
    the branch is routed via LLM classification instead.
    """
    condition: StructuredCondition | None = None
    condition_description: str = ""
    next_step: str


# ---------------------------------------------------------------------------
# Tool models
# ---------------------------------------------------------------------------

class ToolArgMapping(BaseModel):
    """Maps a tool function parameter to a blackboard key."""
    param: str
    source: str


class ToolConfig(BaseModel):
    """Fine-grained tool configuration for tool_call steps."""
    name: str
    arg_mappings: list[ToolArgMapping] = []
    fixed_args: dict[str, Union[str, int, float, bool]] = {}
    result_key: str = ""
    guard_condition: StructuredCondition | None = None


# ---------------------------------------------------------------------------
# Field extraction models
# ---------------------------------------------------------------------------

class ExtractField(BaseModel):
    """Describes a field to extract from user input in collect_info steps."""
    key: str
    description: str
    examples: list[str] = []


class InformOption(BaseModel):
    """An option presented to the user in inform steps."""
    label: str
    description: str = ""
    next_step: str
    detection_keywords: list[str] = []


# ---------------------------------------------------------------------------
# Step and Procedure models
# ---------------------------------------------------------------------------

class ProcedureStep(BaseModel):
    """A single step in a procedure, with optional fine-grained fields."""
    id: str
    name: str = ""
    action: ActionType
    instruction: str = ""

    # collect_info fields
    extract_fields: list[ExtractField] = []
    required_fields: list[str] = []

    # tool_call fields
    tools: list[ToolConfig] = []
    on_success: str = ""
    on_failure: str = ""

    # evaluate fields
    conditions: list[ConditionBranch] = []
    classify_categories: list[str] = []
    classify_result_key: str = ""

    # inform fields
    options: list[InformOption] = []

    # universal navigation
    next_step: str = ""


class Procedure(BaseModel):
    """Top-level procedure definition with optional fine-grained metadata."""
    id: str
    name: str
    description: str = ""
    version: str = "1.0"
    domain: str = ""
    trigger_intents: list[str] = []
    available_tools: list[str] = []
    data_context: list[str] = []
    steps: list[ProcedureStep]


# ---------------------------------------------------------------------------
# Intermediate schemas for ingestion pipeline passes
# ---------------------------------------------------------------------------

class StepOverview(BaseModel):
    """Pass 1 output: high-level step identification."""
    id: str
    name: str
    action: ActionType
    instruction: str = ""


class ProcedureOverview(BaseModel):
    """Pass 1 output: high-level procedure structure."""
    id: str
    name: str
    description: str = ""
    domain: str = ""
    trigger_intents: list[str] = []
    available_tools: list[str] = []
    data_context: list[str] = []
    steps: list[StepOverview]
