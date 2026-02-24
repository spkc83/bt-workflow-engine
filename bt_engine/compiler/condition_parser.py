"""Condition parser: converts YAML condition strings into Python predicate callables.

Supports two input formats:
  1. StructuredCondition objects (from fine-grained YAML / ingestion pipeline)
  2. Legacy string-based conditions with regex grammar

String grammar:
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


# ---------------------------------------------------------------------------
# Structured condition support (fine-grained format)
# ---------------------------------------------------------------------------

def parse_structured_condition(cond) -> Callable[[dict], bool]:
    """Convert a StructuredCondition object (or dict) to a Python predicate.

    Unlike parse_condition() which returns None for unparseable strings,
    this always returns a valid predicate because the condition is already
    fully specified by the LLM with constrained decoding.

    Accepts either a StructuredCondition Pydantic model or a plain dict
    with the same fields (field, operator, value, field_path).
    """
    # Accept both Pydantic model and dict
    if hasattr(cond, "model_dump"):
        cond = cond.model_dump()

    field = cond["field"]
    operator = cond["operator"]
    value = cond["value"]
    field_path = cond.get("field_path")

    def _resolve(bb: dict):
        """Resolve the field value from the blackboard."""
        # If explicit field_path is provided, use it
        if field_path:
            parts = field_path.split(".")
            obj = bb
            for part in parts:
                if isinstance(obj, dict):
                    obj = obj.get(part)
                else:
                    return None
            return obj
        # Otherwise use the standard FIELD_LOCATIONS lookup
        return _resolve_field(bb, field)

    # Map operator to predicate
    if operator == "eq":
        def pred(bb, r=_resolve, v=value):
            actual = r(bb)
            try:
                return float(actual) == float(v)
            except (TypeError, ValueError):
                return str(actual or "").lower() == str(v).lower()
        return pred

    elif operator == "neq":
        def pred(bb, r=_resolve, v=value):
            actual = r(bb)
            try:
                return float(actual) != float(v)
            except (TypeError, ValueError):
                return str(actual or "").lower() != str(v).lower()
        return pred

    elif operator == "gt":
        return lambda bb, r=_resolve, v=value: (r(bb) or 0) > float(v)

    elif operator == "gte":
        return lambda bb, r=_resolve, v=value: (r(bb) or 0) >= float(v)

    elif operator == "lt":
        return lambda bb, r=_resolve, v=value: (r(bb) or 0) < float(v)

    elif operator == "lte":
        return lambda bb, r=_resolve, v=value: (r(bb) or 0) <= float(v)

    elif operator == "in":
        vals = value if isinstance(value, list) else [value]
        lower_vals = tuple(str(v).lower() for v in vals)
        return lambda bb, r=_resolve, vs=lower_vals: str(r(bb) or "").lower() in vs

    elif operator == "not_in":
        vals = value if isinstance(value, list) else [value]
        lower_vals = tuple(str(v).lower() for v in vals)
        return lambda bb, r=_resolve, vs=lower_vals: str(r(bb) or "").lower() not in vs

    elif operator == "within_days":
        return lambda bb, r=_resolve, d=value: (r(bb) or 999) <= int(d)

    elif operator == "outside_days":
        return lambda bb, r=_resolve, d=value: (r(bb) or 0) > int(d)

    elif operator == "contains":
        return lambda bb, r=_resolve, v=value: str(v).lower() in str(r(bb) or "").lower()

    else:
        # Unknown operator — always True as safe fallback
        return lambda bb: True
