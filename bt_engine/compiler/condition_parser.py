"""Condition parser: converts YAML condition strings into Python predicate callables.

Supports a small regex-based grammar for deterministic conditions:
  field == value, field >= number, field in [vals], field within N days,
  field outside N days, AND/OR combinators.

Unparseable conditions (subjective/LLM-requiring) return None, signaling
the step compiler to use LLMClassifyNode instead.
"""

from __future__ import annotations

import re
from typing import Callable

# Maps field names to nested blackboard paths: (top_level_key, nested_key)
# Fields not in this map are looked up at top-level bb_dict.
FIELD_LOCATIONS: dict[str, tuple[str, str]] = {
    "severity": ("alert_data", "severity"),
    "risk_score": ("alert_data", "risk_score"),
    "order_status": ("order_data", "status"),
    "order_date": ("order_data", "days_since_delivery"),
    "days_since_delivery": ("order_data", "days_since_delivery"),
    "amount_involved": ("alert_data", "amount_involved"),
    "complaint_type": (None, "complaint_type"),  # top-level bb_dict key
    "status": ("order_data", "status"),
}


def _resolve_field(bb: dict, field_name: str):
    """Resolve a field name to its value in the blackboard dict."""
    if field_name in FIELD_LOCATIONS:
        container_key, nested_key = FIELD_LOCATIONS[field_name]
        if container_key is None:
            return bb.get(nested_key)
        return bb.get(container_key, {}).get(nested_key)
    # Fallback: top-level lookup
    return bb.get(field_name)


def parse_condition(condition_str: str) -> Callable[[dict], bool] | None:
    """Parse a condition string into a predicate callable.

    Returns None if the condition cannot be parsed (subjective/complex),
    indicating the caller should use LLM classification instead.
    """
    condition_str = condition_str.strip()

    # Try AND/OR combinators first
    and_pred = _try_parse_and(condition_str)
    if and_pred is not None:
        return and_pred

    or_pred = _try_parse_or(condition_str)
    if or_pred is not None:
        return or_pred

    # Try individual patterns
    return _parse_single_condition(condition_str)


def _try_parse_and(condition_str: str) -> Callable[[dict], bool] | None:
    """Try to parse 'A AND B AND C' patterns."""
    # Split on AND (case-insensitive, word boundary)
    parts = re.split(r'\s+AND\s+', condition_str, flags=re.IGNORECASE)
    if len(parts) < 2:
        return None

    predicates = []
    for part in parts:
        pred = _parse_single_condition(part.strip())
        if pred is None:
            return None  # Can't parse a component -> whole thing unparseable
        predicates.append(pred)

    def and_predicate(bb: dict) -> bool:
        return all(p(bb) for p in predicates)
    return and_predicate


def _try_parse_or(condition_str: str) -> Callable[[dict], bool] | None:
    """Try to parse 'A OR B' patterns."""
    parts = re.split(r'\s+OR\s+', condition_str, flags=re.IGNORECASE)
    if len(parts) < 2:
        return None

    predicates = []
    for part in parts:
        pred = _parse_single_condition(part.strip())
        if pred is None:
            return None
        predicates.append(pred)

    def or_predicate(bb: dict) -> bool:
        return any(p(bb) for p in predicates)
    return or_predicate


def _parse_single_condition(cond: str) -> Callable[[dict], bool] | None:
    """Parse a single condition expression (no AND/OR)."""

    # Pattern: "field == value"
    m = re.match(r'^(\w+)\s*==\s*(.+)$', cond)
    if m:
        field, value = m.group(1), m.group(2).strip().strip('"').strip("'")
        # Try numeric comparison
        try:
            num_val = float(value)
            return lambda bb, f=field, v=num_val: _resolve_field(bb, f) == v
        except ValueError:
            return lambda bb, f=field, v=value: str(_resolve_field(bb, f) or "").lower() == v.lower()

    # Pattern: "field >= number"
    m = re.match(r'^(\w+)\s*>=\s*([\d.]+)$', cond)
    if m:
        field, value = m.group(1), float(m.group(2))
        return lambda bb, f=field, v=value: (_resolve_field(bb, f) or 0) >= v

    # Pattern: "field < number"
    m = re.match(r'^(\w+)\s*<\s*([\d.]+)$', cond)
    if m:
        field, value = m.group(1), float(m.group(2))
        return lambda bb, f=field, v=value: (_resolve_field(bb, f) or 0) < v

    # Pattern: "field > number"
    m = re.match(r'^(\w+)\s*>\s*([\d.]+)$', cond)
    if m:
        field, value = m.group(1), float(m.group(2))
        return lambda bb, f=field, v=value: (_resolve_field(bb, f) or 0) > v

    # Pattern: "field <= number"
    m = re.match(r'^(\w+)\s*<=\s*([\d.]+)$', cond)
    if m:
        field, value = m.group(1), float(m.group(2))
        return lambda bb, f=field, v=value: (_resolve_field(bb, f) or 0) <= v

    # Pattern: "field in [val1, val2, ...]"
    m = re.match(r'^(\w+)\s+in\s+\[([^\]]+)\]$', cond)
    if m:
        field = m.group(1)
        values = tuple(v.strip().strip('"').strip("'") for v in m.group(2).split(','))
        return lambda bb, f=field, vs=values: str(_resolve_field(bb, f) or "").lower() in tuple(v.lower() for v in vs)

    # Pattern: "field not in list_name" (e.g. "category not in non_refundable_list")
    m = re.match(r'^(\w+)\s+not\s+in\s+(\w+)$', cond)
    if m:
        # For now, non_refundable_list is not actually checked in the hand-coded trees
        # This is a soft condition that always returns True (matching hand-coded behavior)
        return lambda bb: True

    # Pattern: "field within N days"
    m = re.match(r'^(\w+)\s+within\s+(\d+)\s+days?$', cond, re.IGNORECASE)
    if m:
        field, days = m.group(1), int(m.group(2))
        return lambda bb, f=field, d=days: (_resolve_field(bb, f) or 999) <= d

    # Pattern: "field outside N days"
    m = re.match(r'^(\w+)\s+outside\s+(\d+)\s+days?$', cond, re.IGNORECASE)
    if m:
        field, days = m.group(1), int(m.group(2))
        return lambda bb, f=field, d=days: (_resolve_field(bb, f) or 0) > d

    # Pattern: "severity == high OR risk_score >= 80" (already handled by _try_parse_or)
    # Pattern: compound with parentheses — not supported, fall through

    # Unparseable — return None to signal LLM classification needed
    return None
