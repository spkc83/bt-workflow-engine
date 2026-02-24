"""Step compilers: convert each YAML action type into a py_trees subtree.

Each compile_* function takes a step dict, the full steps list, a ToolRegistry,
and a recursive compile_step callback, and returns a py_trees Behaviour (subtree).
"""

from __future__ import annotations

from typing import Callable

import py_trees

from bt_engine.nodes import (
    ConditionNode,
    LLMClassifyNode,
    LLMExtractNode,
    LLMResponseNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)
from bt_engine.compiler.condition_parser import parse_condition, parse_structured_condition
from bt_engine.compiler.tool_registry import ToolRegistry


def compile_collect_info(
    step: dict,
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile a collect_info step.

    Pattern:
      Sequence(step_id, memory=True):
        LLMExtractNode(extract fields from user message)
        Selector(check_has_info):
          ConditionNode(has required info -> proceed)
          Sequence(ask_for_info):
            LLMResponseNode(ask using instruction)
            UserInputNode(wait)
            LLMExtractNode(re-extract)
        LogNode(step done)
    """
    step_id = step["id"]

    # Fine-grained: use extract_fields if available
    extract_fields = step.get("extract_fields")
    if extract_fields and isinstance(extract_fields, list) and len(extract_fields) > 0:
        # Fine-grained format: extract_fields is a list of dicts with key/description/examples
        if isinstance(extract_fields[0], dict):
            extract_keys = [ef["key"] for ef in extract_fields]
            field_descriptions = ", ".join(
                f"{ef['key']} ({ef.get('description', '')})" for ef in extract_fields
            )
            extract_prompt = (
                f"Extract the following fields from the customer message: {field_descriptions}. "
                f"{step['instruction']}"
            )
        else:
            # extract_fields is already a list of strings (legacy-compatible)
            extract_keys = extract_fields
            extract_prompt = (
                f"Extract relevant details from the customer message. "
                f"Look for: order ID, store/merchant name, item description, "
                f"approximate dollar amount, time references, and any other identifiers. "
                f"{step['instruction']}"
            )
    else:
        # Legacy format
        extract_keys = step.get("extract_keys")
        if not extract_keys:
            extract_keys = _infer_extract_keys(step)
        extract_prompt = (
            f"Extract relevant details from the customer message. "
            f"Look for: order ID, store/merchant name, item description, "
            f"approximate dollar amount, time references, and any other identifiers. "
            f"{step['instruction']}"
        )

    root = py_trees.composites.Sequence(step_id, memory=True)

    # Initial extraction
    extract = LLMExtractNode(
        f"extract_{step_id}",
        prompt_template=extract_prompt,
        extract_keys=extract_keys,
    )

    # Check if we have enough info
    has_info = py_trees.composites.Selector(f"check_has_info_{step_id}", memory=False)

    # Check for order_id
    got_id = ConditionNode(
        f"has_id_{step_id}",
        lambda bb: bool(bb.get("order_id") or bb.get("alert_id")),
    )

    # Check for any descriptive clues
    got_clues = ConditionNode(
        f"has_clues_{step_id}",
        lambda bb: any([
            bb.get("merchant_name"),
            bb.get("amount"),
            bb.get("item_description"),
            bb.get("complaint_description"),
        ]),
    )

    # Ask for info if nothing found
    ask_seq = py_trees.composites.Sequence(f"ask_for_info_{step_id}", memory=True)
    ask_seq.add_children([
        LLMResponseNode(
            f"ask_{step_id}",
            prompt_template=step["instruction"],
        ),
        UserInputNode(f"wait_{step_id}"),
        LLMExtractNode(
            f"re_extract_{step_id}",
            prompt_template=(
                f"Extract relevant details from the customer's response. "
                f"Look for any identifiers or descriptions."
            ),
            extract_keys=extract_keys,
        ),
    ])

    has_info.add_children([got_id, got_clues, ask_seq])

    root.add_children([
        extract,
        has_info,
        LogNode(f"{step_id}_done", message=f"Step '{step_id}' completed"),
    ])

    return root


def compile_tool_call(
    step: dict,
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile a tool_call step.

    Supports fine-grained tool_configs (list of ToolConfig dicts with
    explicit arg_mappings) or legacy format (tool name strings).

    Single tool: Selector with success/failure paths.
    Multiple tools: Selector with condition-guarded paths per tool.
    """
    step_id = step["id"]

    # Fine-grained format: tool_configs with explicit arg_mappings
    tool_configs = step.get("tool_configs")
    if tool_configs and isinstance(tool_configs, list) and len(tool_configs) > 0:
        # Convert tool_configs to legacy format for compilation,
        # but apply explicit arg_mappings and fixed_args
        for tc in tool_configs:
            if isinstance(tc, dict) and tc.get("arg_mappings"):
                # Override step-level arg_keys with explicit mappings
                step["arg_keys"] = {
                    m["param"]: m["source"] for m in tc["arg_mappings"]
                }
            if isinstance(tc, dict) and tc.get("fixed_args"):
                step.setdefault("fixed_args", {})
                step["fixed_args"].update(tc["fixed_args"])
            if isinstance(tc, dict) and tc.get("result_key"):
                step["result_key"] = tc["result_key"]

        tools_list = [
            tc["name"] if isinstance(tc, dict) else tc
            for tc in tool_configs
        ]
    else:
        tools_list = step.get("tools", [step["tool"]] if step.get("tool") else [])

    if len(tools_list) > 1:
        return _compile_multi_tool(step, tools_list, all_steps, registry, compile_step)
    else:
        return _compile_single_tool(step, tools_list[0], all_steps, registry, compile_step)


def _compile_single_tool(
    step: dict,
    tool_name: str,
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile a single-tool tool_call step."""
    step_id = step["id"]
    entry = registry.get(tool_name)

    if entry is None:
        # Unknown tool - create a LogNode placeholder
        return LogNode(f"{step_id}_unknown_tool", message=f"Unknown tool: {tool_name}")

    # Build arg_keys and fixed_args, merging YAML overrides with registry defaults
    arg_keys = step.get("arg_keys") or dict(entry.arg_keys)
    fixed_args = dict(entry.fixed_args)
    if step.get("fixed_args"):
        fixed_args.update(step["fixed_args"])

    result_key = step.get("result_key") or f"{tool_name}_result"

    tool_node = ToolActionNode(
        f"call_{tool_name}_{step_id}",
        tool_func=entry.func,
        arg_keys=arg_keys,
        fixed_args=fixed_args,
        result_key=result_key,
    )

    on_success = step.get("on_success")
    on_failure = step.get("on_failure")

    # If both on_success and on_failure point somewhere, build a Selector
    if on_success or on_failure:
        root = py_trees.composites.Selector(step_id, memory=False)

        # Success path
        success_seq = py_trees.composites.Sequence(f"{step_id}_success", memory=True)
        success_children = [tool_node]

        # Add an LLM response if there's an instruction
        if step.get("instruction"):
            success_children.append(
                LLMResponseNode(
                    f"respond_{step_id}",
                    prompt_template=step["instruction"],
                )
            )

        # Compile the on_success next step inline
        if on_success and on_success != "end":
            next_subtree = compile_step(on_success, all_steps)
            if next_subtree is not None:
                success_children.append(next_subtree)
        elif on_success == "end":
            success_children.append(LogNode(f"{step_id}_end", message="workflow_end"))

        success_seq.add_children(success_children)

        # Failure path
        failure_children = []
        if on_failure and on_failure != on_success:
            if on_failure == "end":
                failure_children.append(LogNode(f"{step_id}_fail_end", message="workflow_end"))
            else:
                fail_subtree = compile_step(on_failure, all_steps)
                if fail_subtree is not None:
                    failure_children.append(fail_subtree)

        if failure_children:
            failure_seq = py_trees.composites.Sequence(f"{step_id}_failure", memory=True)
            failure_seq.add_children(failure_children)
            root.add_children([success_seq, failure_seq])
        else:
            # on_failure same as on_success or not specified — just the success path
            root.add_children([success_seq])

        return root
    else:
        # Simple tool call, no branching
        root = py_trees.composites.Sequence(step_id, memory=True)
        children = [tool_node]
        if step.get("instruction"):
            children.append(
                LLMResponseNode(f"respond_{step_id}", prompt_template=step["instruction"])
            )
        root.add_children(children)
        return root


def _compile_multi_tool(
    step: dict,
    tools_list: list[str],
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile a multi-tool tool_call step (e.g., lookup_order + search_orders).

    Pattern: Selector where each tool path is guarded by a condition.
    """
    step_id = step["id"]
    root = py_trees.composites.Selector(step_id, memory=False)

    for i, tool_name in enumerate(tools_list):
        entry = registry.get(tool_name)
        if entry is None:
            continue

        arg_keys = dict(entry.arg_keys)
        fixed_args = dict(entry.fixed_args)
        result_key = f"{tool_name}_result"

        path = py_trees.composites.Sequence(f"{step_id}_{tool_name}", memory=True)
        children = []

        # First tool (exact lookup) — guarded by having exact ID
        if i == 0:
            children.append(ConditionNode(
                f"has_exact_id_{step_id}",
                lambda bb: bool(bb.get("order_id") or bb.get("alert_id")),
            ))
        # Second tool (search) — guarded by having clues
        elif i == 1:
            children.append(ConditionNode(
                f"has_clues_{step_id}",
                lambda bb: any([bb.get("merchant_name"), bb.get("amount")]),
            ))

        children.append(ToolActionNode(
            f"call_{tool_name}_{step_id}",
            tool_func=entry.func,
            arg_keys=arg_keys,
            fixed_args=fixed_args,
            result_key=result_key,
        ))

        # Add confirmation response
        if step.get("instruction"):
            children.append(LLMResponseNode(
                f"confirm_{tool_name}_{step_id}",
                prompt_template=step["instruction"],
            ))

        path.add_children(children)
        root.add_children([path])

    # Fallback: not found
    on_failure = step.get("on_failure")
    if on_failure:
        fail_subtree = compile_step(on_failure, all_steps)
        if fail_subtree is not None:
            root.add_children([fail_subtree])
    else:
        root.add_children([
            LLMResponseNode(
                f"not_found_{step_id}",
                prompt_template=(
                    "I was unable to find matching information. "
                    "Apologize and ask the customer to provide more details."
                ),
            ),
        ])

    return root


def compile_evaluate(
    step: dict,
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile an evaluate step.

    Supports three formats:
    1. Fine-grained structured conditions (StructuredCondition objects/dicts)
    2. Legacy string conditions that are parseable -> ConditionNode routing
    3. Unparseable conditions -> LLMClassifyNode + ConditionNode routing

    Also supports explicit classify_categories for LLM-classified steps.
    """
    step_id = step["id"]
    conditions = step.get("conditions", [])

    # Check for explicit classify_categories (fine-grained format)
    classify_categories = step.get("classify_categories")
    if classify_categories and isinstance(classify_categories, list) and len(classify_categories) > 0:
        return _compile_evaluate_with_classify(step_id, step, conditions, all_steps, compile_step)

    # Try to parse all conditions
    parsed = []
    all_parseable = True
    for cond in conditions:
        # Fine-grained format: condition is a structured object
        structured = cond.get("condition")
        if structured and isinstance(structured, dict) and "field" in structured:
            predicate = parse_structured_condition(structured)
            parsed.append((cond, predicate))
            continue

        # Legacy format: condition is a string
        cond_str = cond.get("if", "")
        predicate = parse_condition(cond_str)
        parsed.append((cond, predicate))
        if predicate is None:
            all_parseable = False

    if all_parseable:
        return _compile_evaluate_deterministic(step_id, parsed, all_steps, compile_step)
    else:
        return _compile_evaluate_with_classify(step_id, step, conditions, all_steps, compile_step)


def _compile_evaluate_deterministic(
    step_id: str,
    parsed_conditions: list[tuple[dict, Callable]],
    all_steps: dict[str, dict],
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile evaluate with all-parseable conditions -> pure ConditionNode routing."""
    root = py_trees.composites.Selector(step_id, memory=False)

    for i, (cond, predicate) in enumerate(parsed_conditions):
        next_step_id = cond.get("next_step")
        if not next_step_id:
            continue

        path = py_trees.composites.Sequence(f"{step_id}_cond_{i}", memory=True)
        children = [
            ConditionNode(f"{step_id}_check_{i}", predicate),
        ]

        # Compile the target step
        next_subtree = compile_step(next_step_id, all_steps)
        if next_subtree is not None:
            children.append(next_subtree)

        path.add_children(children)
        root.add_children([path])

    return root


def _compile_evaluate_with_classify(
    step_id: str,
    step: dict,
    conditions: list[dict],
    all_steps: dict[str, dict],
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile evaluate with unparseable conditions -> LLMClassifyNode + routing."""
    root = py_trees.composites.Sequence(step_id, memory=True)

    # Derive categories from the condition next_steps
    categories = []
    category_to_next = {}
    for cond in conditions:
        next_step_id = cond.get("next_step", "")
        if next_step_id and next_step_id not in categories:
            categories.append(next_step_id)
            category_to_next[next_step_id] = next_step_id

    result_key = f"{step_id}_result"

    # Determine classify prompt from step instruction or conditions
    classify_prompt = step.get("instruction", "")
    if not classify_prompt:
        cond_descriptions = [c.get("if", "") for c in conditions]
        classify_prompt = (
            f"Based on the available evidence, classify the outcome. "
            f"Consider: {'; '.join(cond_descriptions)}"
        )

    classify_node = LLMClassifyNode(
        f"classify_{step_id}",
        prompt_template=classify_prompt,
        categories=categories,
        result_key=result_key,
    )

    # Route based on classification result
    router = py_trees.composites.Selector(f"route_{step_id}", memory=False)

    for category in categories:
        next_step_id = category_to_next[category]

        path = py_trees.composites.Sequence(f"{step_id}_{category}", memory=True)
        cat = category  # capture for lambda
        path_children = [
            ConditionNode(
                f"is_{category}_{step_id}",
                lambda bb, c=cat, rk=result_key: bb.get(rk) == c,
            ),
        ]

        next_subtree = compile_step(next_step_id, all_steps)
        if next_subtree is not None:
            path_children.append(next_subtree)

        path.add_children(path_children)
        router.add_children([path])

    root.add_children([classify_node, router])
    return root


def compile_inform(
    step: dict,
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile an inform step.

    With options: present info, wait for response, route by option.
    Simple next_step: present info, wait for input.
    """
    step_id = step["id"]
    options = step.get("options")

    root = py_trees.composites.Sequence(step_id, memory=True)

    # Always present the information
    root.add_children([
        LLMResponseNode(f"inform_{step_id}", prompt_template=step["instruction"]),
        UserInputNode(f"wait_{step_id}"),
    ])

    if options and len(options) > 1:
        # Multiple options — route by customer response
        router = py_trees.composites.Selector(f"route_{step_id}", memory=False)

        for i, opt in enumerate(options):
            next_step_id = opt.get("next_step")
            if not next_step_id:
                continue

            opt_path = py_trees.composites.Sequence(f"{step_id}_opt_{i}", memory=True)

            # Fine-grained: use detection_keywords if provided
            detection_keywords = opt.get("detection_keywords")
            if detection_keywords and isinstance(detection_keywords, list) and len(detection_keywords) > 0:
                opt_path.add_children([
                    ConditionNode(
                        f"detect_{step_id}_opt_{i}",
                        lambda bb, kws=detection_keywords: any(
                            kw.lower() in (bb.get("user_message", "") or "").lower() for kw in kws
                        ),
                    ),
                ])
            else:
                # Legacy: use keyword matching for option routing (matching hand-coded pattern)
                label = opt.get("label", "").lower()
                escalation_keywords = ["escalat", "supervisor", "manager", "not satisf", "unacceptable"]
                if any(kw in label for kw in escalation_keywords):
                    opt_path.add_children([
                        ConditionNode(
                            f"wants_{step_id}_opt_{i}",
                            lambda bb, kws=escalation_keywords: any(
                                kw in (bb.get("user_message", "") or "").lower() for kw in kws
                            ),
                        ),
                    ])
                else:
                    opt_path.add_children([
                        ConditionNode(
                            f"accepts_{step_id}_opt_{i}",
                            lambda bb, kws=escalation_keywords: not any(
                                kw in (bb.get("user_message", "") or "").lower() for kw in kws
                            ),
                        ),
                    ])

            next_subtree = compile_step(next_step_id, all_steps)
            if next_subtree is not None:
                opt_path.add_children([next_subtree])

            router.add_children([opt_path])

        root.add_children([router])

    elif step.get("next_step"):
        # Simple next_step (e.g., loop back) — this is a back-edge terminal
        # Don't compile the next step (it would create a cycle)
        # The runner re-ticks from root on next message
        pass

    return root


def compile_end(
    step: dict,
    all_steps: dict[str, dict],
    registry: ToolRegistry,
    compile_step: Callable,
) -> py_trees.behaviour.Behaviour:
    """Compile an end step -> simple LogNode."""
    return LogNode(f"{step['id']}", message="workflow_end")


def _infer_extract_keys(step: dict) -> list[str]:
    """Infer extraction keys from step context."""
    required = step.get("required_info", [])
    # Default extract keys for common patterns
    base_keys = ["order_id", "merchant_name", "amount", "date", "item_description"]

    if any("complaint" in r for r in required):
        base_keys.append("complaint_description")
    if any("alert" in r for r in required):
        base_keys = ["alert_id"]

    return base_keys


# Map action types to their compiler functions
ACTION_COMPILERS: dict[str, Callable] = {
    "collect_info": compile_collect_info,
    "tool_call": compile_tool_call,
    "evaluate": compile_evaluate,
    "inform": compile_inform,
    "end": compile_end,
}
