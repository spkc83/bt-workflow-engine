"""YAML parser: loads and validates procedure YAML files.

Validates required fields, normalizes optional fields with defaults,
and returns a clean dict ready for compilation.

Supports both legacy format (string conditions, flat required_info) and
fine-grained format (structured conditions, extract_fields, arg_mappings).
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ProcedureValidationError(Exception):
    """Raised when a YAML procedure fails validation."""
    pass


def load_and_validate(yaml_path: str | Path) -> dict:
    """Load a YAML procedure file and validate its structure.

    Returns the validated procedure dict (the inner 'procedure' key).
    Raises ProcedureValidationError on invalid input.
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise ProcedureValidationError(f"File not found: {yaml_path}")

    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ProcedureValidationError(f"Expected a YAML mapping, got {type(raw).__name__}")

    # The YAML wraps everything under 'procedure:'
    proc = raw.get("procedure", raw)

    _validate_procedure(proc, yaml_path)
    _normalize_steps(proc)

    return proc


def _validate_procedure(proc: dict, source: Path) -> None:
    """Validate top-level procedure fields."""
    for field in ("id", "name", "steps"):
        if field not in proc:
            raise ProcedureValidationError(
                f"Missing required field '{field}' in {source}"
            )

    if not isinstance(proc["steps"], list) or len(proc["steps"]) == 0:
        raise ProcedureValidationError(f"'steps' must be a non-empty list in {source}")

    step_ids = set()
    for i, step in enumerate(proc["steps"]):
        _validate_step(step, i, source)
        if step["id"] in step_ids:
            raise ProcedureValidationError(
                f"Duplicate step id '{step['id']}' in {source}"
            )
        step_ids.add(step["id"])


def _validate_step(step: dict, index: int, source: Path) -> None:
    """Validate a single step has required fields."""
    if not isinstance(step, dict):
        raise ProcedureValidationError(
            f"Step {index} must be a mapping in {source}"
        )

    for field in ("id", "action"):
        if field not in step:
            raise ProcedureValidationError(
                f"Step {index} missing required field '{field}' in {source}"
            )

    action = step["action"]
    valid_actions = ("collect_info", "tool_call", "evaluate", "inform", "end")
    if action not in valid_actions:
        raise ProcedureValidationError(
            f"Step '{step['id']}' has unknown action '{action}' in {source}. "
            f"Valid actions: {valid_actions}"
        )

    # Action-specific validation
    if action == "tool_call" and "tool" not in step and "tools" not in step:
        raise ProcedureValidationError(
            f"Step '{step['id']}' (tool_call) must have 'tool' or 'tools' in {source}"
        )

    if action == "evaluate" and "conditions" not in step:
        # Fine-grained format may use classify_categories instead of conditions
        if "classify_categories" not in step:
            raise ProcedureValidationError(
                f"Step '{step['id']}' (evaluate) must have 'conditions' or 'classify_categories' in {source}"
            )


def _normalize_steps(proc: dict) -> None:
    """Normalize optional fields with sensible defaults.

    Handles both legacy format and fine-grained format fields.
    """
    for step in proc["steps"]:
        # Ensure instruction has a default
        step.setdefault("instruction", "")

        # Normalize tool_call steps
        if step["action"] == "tool_call":
            step.setdefault("on_success", None)
            step.setdefault("on_failure", None)
            step.setdefault("result_key", None)
            step.setdefault("arg_keys", None)
            step.setdefault("fixed_args", None)
            # Ensure 'tools' list exists even for single-tool steps
            if "tools" not in step and "tool" in step:
                step.setdefault("tools", [step["tool"]])
            # Fine-grained: tool_configs (list of ToolConfig dicts)
            step.setdefault("tool_configs", None)

        # Normalize collect_info steps
        if step["action"] == "collect_info":
            step.setdefault("required_info", [])
            step.setdefault("next_step", None)
            step.setdefault("extract_keys", None)
            # Fine-grained fields
            step.setdefault("extract_fields", None)
            step.setdefault("required_fields", None)

        # Normalize evaluate steps
        if step["action"] == "evaluate":
            step.setdefault("classify", None)
            # Fine-grained fields
            step.setdefault("classify_categories", None)
            step.setdefault("classify_result_key", None)

        # Normalize inform steps
        if step["action"] == "inform":
            step.setdefault("options", None)
            step.setdefault("next_step", None)

        # Universal
        step.setdefault("next_step", None)
