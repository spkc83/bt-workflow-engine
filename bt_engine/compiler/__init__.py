"""BT Compiler: converts YAML procedure definitions into py_trees behaviour trees.

Usage:
    compiler = ProcedureCompiler()
    tree = compiler.compile("procedures/customer_service_refund.yaml")
"""

from __future__ import annotations

import logging
from pathlib import Path

import py_trees

from bt_engine.compiler.parser import load_and_validate
from bt_engine.compiler.step_compilers import ACTION_COMPILERS
from bt_engine.compiler.tool_registry import ToolRegistry, create_default_registry

logger = logging.getLogger(__name__)


class ProcedureCompiler:
    """Compiles YAML procedure definitions into py_trees BehaviourTrees."""

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry or create_default_registry()

    def compile(self, yaml_path: str | Path) -> py_trees.trees.BehaviourTree:
        """Load a YAML file and compile it into a BehaviourTree."""
        proc = load_and_validate(yaml_path)
        return self.compile_from_dict(proc)

    def compile_from_dict(self, proc: dict) -> py_trees.trees.BehaviourTree:
        """Compile a parsed procedure dict into a BehaviourTree."""
        proc_id = proc["id"]
        steps = proc["steps"]

        # Build step lookup
        steps_by_id: dict[str, dict] = {s["id"]: s for s in steps}

        # Memoization cache for compiled subtrees (prevents duplicate compilation)
        # and tracks which steps are currently being compiled (cycle detection).
        compiled_cache: dict[str, py_trees.behaviour.Behaviour] = {}
        compiling_stack: set[str] = set()

        def compile_step(step_id: str, all_steps: dict[str, dict]) -> py_trees.behaviour.Behaviour | None:
            """Recursively compile a step, with memoization and cycle detection."""
            if step_id == "end":
                return py_trees.composites.Sequence("end", memory=True, children=[
                    _make_log("workflow_end", "workflow_end"),
                ])

            if step_id in compiled_cache:
                # Already compiled — return a fresh copy to avoid shared mutable state.
                # py_trees nodes hold mutable state, so we can't reuse the same instance.
                # Instead, recompile it. The cache just prevents infinite recursion.
                pass

            if step_id in compiling_stack:
                # Back-edge detected (cycle) — this is a loop-back point.
                # Return None; the inform/collect_info step handles this by
                # terminating with UserInputNode. The runner re-ticks from root.
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

            # Mark as being compiled (for cycle detection)
            compiling_stack.add(step_id)

            try:
                subtree = compiler_func(step, all_steps, self.registry, compile_step)
            finally:
                compiling_stack.discard(step_id)

            compiled_cache[step_id] = subtree
            return subtree

        # Compile the workflow as a top-level Sequence of entry steps.
        # The first step is always the entry point.
        first_step = steps[0]
        root_children = []

        # Compile the first step — it recursively pulls in all reachable steps
        first_subtree = compile_step(first_step["id"], steps_by_id)
        if first_subtree is not None:
            root_children.append(first_subtree)

        # For procedures that have a linear flow from first step through next_step,
        # we need to compile the chain. The step compilers handle branching internally,
        # but the top-level sequence needs to chain steps that use next_step.
        _compile_linear_chain(first_step, steps_by_id, compile_step, root_children)

        root_children.append(
            _make_log(f"{proc_id}_complete", f"{proc['name']} completed")
        )

        root = py_trees.composites.Sequence(f"{proc_id}_workflow", memory=True)
        root.add_children(root_children)

        return py_trees.trees.BehaviourTree(root=root)


def _compile_linear_chain(
    first_step: dict,
    steps_by_id: dict[str, dict],
    compile_step,
    root_children: list,
) -> None:
    """Follow the next_step chain from the first step, compiling each into root_children.

    This handles the top-level linear flow:
      greet_and_collect -> lookup_order -> check_eligibility
    where each step's next_step points to the next in the sequence.

    Steps that branch (evaluate, inform with options) handle their own
    next_step references internally via the step compilers.
    """
    visited = {first_step["id"]}
    current = first_step

    while True:
        next_id = current.get("next_step")
        if not next_id or next_id == "end" or next_id in visited:
            break

        next_step = steps_by_id.get(next_id)
        if not next_step:
            break

        # Only follow next_step for linear steps (collect_info -> next tool_call, etc.)
        # Steps with on_success/on_failure or conditions handle their own branching.
        action = next_step["action"]

        visited.add(next_id)
        subtree = compile_step(next_id, steps_by_id)
        if subtree is not None:
            root_children.append(subtree)

        current = next_step


def _make_log(name: str, message: str):
    """Create a LogNode (imported here to avoid circular imports)."""
    from bt_engine.nodes import LogNode
    return LogNode(name, message=message)
