"""BT Compiler: converts YAML procedure definitions into behaviour trees.

Usage:
    compiler = ProcedureCompiler()
    tree = compiler.compile("procedures/customer_service_refund.yaml")
"""

from __future__ import annotations

import logging
from pathlib import Path

from bt_engine.behaviour_tree import BehaviourTree, Sequence
from bt_engine.compiler.parser import load_and_validate
from bt_engine.compiler.step_compilers import ACTION_COMPILERS
from bt_engine.compiler.tool_registry import ToolRegistry, create_default_registry

logger = logging.getLogger(__name__)


class ProcedureCompiler:
    """Compiles YAML procedure definitions into BehaviourTrees."""

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry or create_default_registry()

    def compile(self, yaml_path: str | Path) -> BehaviourTree:
        """Load a YAML file and compile it into a BehaviourTree."""
        proc = load_and_validate(yaml_path)
        return self.compile_from_dict(proc)

    def compile_from_dict(self, proc: dict) -> BehaviourTree:
        """Compile a parsed procedure dict into a BehaviourTree."""
        proc_id = proc["id"]
        steps = proc["steps"]

        # Build step lookup
        steps_by_id: dict[str, dict] = {s["id"]: s for s in steps}

        # Memoization cache for compiled subtrees (prevents duplicate compilation)
        # and tracks which steps are currently being compiled (cycle detection).
        compiled_cache: dict[str, object] = {}
        compiling_stack: set[str] = set()

        def compile_step(step_id: str, all_steps: dict[str, dict]):
            """Recursively compile a step, with memoization and cycle detection."""
            if step_id == "end":
                return Sequence("end", memory=True, children=[
                    _make_log("workflow_end", "workflow_end"),
                ])

            if step_id in compiled_cache:
                # Already compiled — return a fresh copy to avoid shared mutable state.
                pass

            if step_id in compiling_stack:
                # Back-edge detected (cycle) — this is a loop-back point.
                logger.debug(f"Back-edge detected: {step_id} (loop point)")
                return None

            step = all_steps.get(step_id)
            if step is None:
                logger.warning(f"Step '{step_id}' not found in procedure")
                return None

            action = step["action"]
            compiler_func = ACTION_COMPILERS.get(action)
            if compiler_func is None:
                logger.warning(f"No compiler for action '{action}'")
                return None

            compiling_stack.add(step_id)

            try:
                subtree = compiler_func(step, all_steps, self.registry, compile_step)
            finally:
                compiling_stack.discard(step_id)

            compiled_cache[step_id] = subtree
            return subtree

        # Compile the workflow starting from the first step
        first_step = steps[0]
        root_children = []

        first_subtree = compile_step(first_step["id"], steps_by_id)
        if first_subtree is not None:
            root_children.append(first_subtree)

        # Follow the next_step chain for linear flow
        _compile_linear_chain(first_step, steps_by_id, compile_step, root_children)

        root_children.append(
            _make_log(f"{proc_id}_complete", f"{proc['name']} completed")
        )

        root = Sequence(f"{proc_id}_workflow", memory=True)
        root.add_children(root_children)

        return BehaviourTree(root=root)


def _compile_linear_chain(
    first_step: dict,
    steps_by_id: dict[str, dict],
    compile_step,
    root_children: list,
) -> None:
    """Follow the next_step chain from the first step, compiling each into root_children."""
    visited = {first_step["id"]}
    current = first_step

    while True:
        next_id = current.get("next_step")
        if not next_id or next_id == "end" or next_id in visited:
            break

        next_step = steps_by_id.get(next_id)
        if not next_step:
            break

        visited.add(next_id)
        subtree = compile_step(next_id, steps_by_id)
        if subtree is not None:
            root_children.append(subtree)

        current = next_step


def _make_log(name: str, message: str):
    """Create a LogNode."""
    from bt_engine.nodes import LogNode
    return LogNode(name, message=message)
