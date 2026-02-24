"""Constrained decoding helpers for Google GenAI structured output.

Provides two main utilities:
  - generate_structured(): JSON-mode generation constrained to a Pydantic schema
  - classify_enum(): Enum-mode generation constrained to an Enum class

These are used by both the ingestion pipeline and LLMClassifyNode.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TypeVar

from google.genai import types
from pydantic import BaseModel

from config import get_client, get_model_name

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


async def generate_structured(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> T:
    """Call LLM with response_schema for guaranteed JSON conformance.

    Args:
        prompt: The generation prompt.
        schema: A Pydantic BaseModel subclass. The LLM response is
                constrained to match this schema exactly.
        model: Optional model override (defaults to configured model).
        temperature: Optional temperature override.

    Returns:
        A validated instance of the schema.
    """
    client = get_client()
    model_name = model or get_model_name()

    config_kwargs: dict = {
        "response_mime_type": "application/json",
        "response_schema": schema,
    }
    if temperature is not None:
        config_kwargs["temperature"] = temperature

    config = types.GenerateContentConfig(**config_kwargs)

    response = await client.aio.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config,
    )

    text = response.text or "{}"
    logger.debug(f"Structured LLM response ({schema.__name__}): {text[:200]}")
    return schema.model_validate_json(text)


async def classify_enum(
    prompt: str,
    enum_class: type[Enum],
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> str:
    """Call LLM with text/x.enum for constrained classification.

    The response is guaranteed to be one of the enum member values.

    Args:
        prompt: The classification prompt.
        enum_class: An Enum class whose members define the allowed outputs.
        model: Optional model override.
        temperature: Optional temperature override.

    Returns:
        The enum value string (not the enum member name).
    """
    client = get_client()
    model_name = model or get_model_name()

    config_kwargs: dict = {
        "response_mime_type": "text/x.enum",
        "response_schema": enum_class,
    }
    if temperature is not None:
        config_kwargs["temperature"] = temperature

    config = types.GenerateContentConfig(**config_kwargs)

    response = await client.aio.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config,
    )

    result = (response.text or "").strip()
    logger.debug(f"Enum classification ({enum_class.__name__}): {result}")
    return result


def make_dynamic_enum(name: str, values: list[str]) -> type[Enum]:
    """Create a dynamic Enum class from a list of string values.

    Useful for LLMClassifyNode where categories are determined at compile time.
    """
    members = {v: v for v in values}
    return Enum(name, members, type=str)
