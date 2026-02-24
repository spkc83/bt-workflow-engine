"""LLM ingestion pipeline: converts plain English SOPs into validated Procedure objects.

Multi-pass pipeline using constrained decoding:
  Pass 1 — Structure Extraction: identify steps, action types, metadata
  Pass 2 — Step Detailing: extract full details per step
  Pass 3 — Condition & Tool Refinement: structured conditions, tool validation
  Pass 4 — Validation & Refinement Loop: fix errors iteratively
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from bt_engine.compiler.llm_utils import classify_enum, generate_structured
from bt_engine.compiler.schemas import (
    ActionType,
    ConditionBranch,
    ConditionOperator,
    ExtractField,
    InformOption,
    Procedure,
    ProcedureOverview,
    ProcedureStep,
    StructuredCondition,
    ToolArgMapping,
    ToolConfig,
)
from bt_engine.compiler.condition_parser import FIELD_LOCATIONS
from bt_engine.compiler.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


class ProcedureIngester:
    """Convert plain English SOPs into validated Procedure objects via LLM pipeline."""

    def __init__(
        self,
        registry: ToolRegistry,
        max_refinement_rounds: int = 3,
        model: str | None = None,
    ):
        self.registry = registry
        self.max_refinement_rounds = max_refinement_rounds
        self.model = model

    async def ingest(self, plain_text: str) -> Procedure:
        """Full pipeline: plain text -> validated Procedure."""
        # Pass 1: Extract high-level structure
        overview = await self._pass1_structure(plain_text)
        logger.info(f"Pass 1 complete: {overview.id} with {len(overview.steps)} steps")

        # Pass 2: Detail each step
        detailed_steps = await self._pass2_detail_steps(plain_text, overview)
        logger.info(f"Pass 2 complete: {len(detailed_steps)} steps detailed")

        # Pass 3: Refine conditions and tools
        refined_steps = await self._pass3_refine(detailed_steps, overview)
        logger.info("Pass 3 complete: conditions and tools refined")

        # Assemble procedure
        procedure = Procedure(
            id=overview.id,
            name=overview.name,
            description=overview.description,
            domain=overview.domain,
            trigger_intents=overview.trigger_intents,
            available_tools=overview.available_tools,
            data_context=overview.data_context,
            steps=refined_steps,
        )

        # Pass 4: Validate and refine
        procedure = await self._pass4_validate(procedure)
        logger.info("Pass 4 complete: validation passed")

        return procedure

    async def ingest_to_yaml(self, plain_text: str, output_path: str | Path) -> Path:
        """Full pipeline + write YAML file."""
        procedure = await self.ingest(plain_text)
        output_path = Path(output_path)

        # Convert to YAML-compatible dict
        yaml_dict = {"procedure": procedure.model_dump(mode="json", exclude_none=True)}

        # Clean up empty lists/strings for cleaner YAML
        _clean_empty_fields(yaml_dict["procedure"])

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            yaml.dump(yaml_dict, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        logger.info(f"Wrote procedure YAML to {output_path}")
        return output_path

    async def refine(self, procedure: Procedure, errors: list[str]) -> Procedure:
        """Single refinement pass: fix validation errors."""
        error_list = "\n".join(f"  - {e}" for e in errors)
        proc_json = procedure.model_dump_json(indent=2)

        prompt = f"""You are fixing a structured procedure definition that has validation errors.

Current procedure (JSON):
{proc_json}

Validation errors found:
{error_list}

Fix ALL the errors above. Return the corrected procedure as JSON.
Keep all existing fields that are correct. Only modify what's needed to fix the errors.
Ensure all step IDs referenced in next_step, on_success, on_failure exist or are "end"."""

        return await generate_structured(prompt, Procedure, model=self.model)

    # ------------------------------------------------------------------
    # Pass 1: Structure Extraction
    # ------------------------------------------------------------------

    async def _pass1_structure(self, plain_text: str) -> ProcedureOverview:
        """Extract high-level procedure structure from plain text."""
        available_tools = ", ".join(self.registry.tool_names)

        prompt = f"""Analyze this plain English procedure/SOP and extract its structure.

PROCEDURE TEXT:
---
{plain_text}
---

Available tools in the system: {available_tools}

Extract:
1. A short snake_case ID for the procedure
2. A human-readable name
3. A description
4. The domain (e.g. "customer_service", "fraud_ops")
5. Trigger intents (keywords that would start this procedure)
6. Which of the available tools this procedure would use
7. Data context keys (blackboard data this procedure works with)
8. A list of steps, each with:
   - A short snake_case step ID
   - A human-readable name
   - The action type (collect_info, tool_call, evaluate, inform, or end)
   - A brief instruction describing what the step does

Order the steps in logical workflow order. Include an "end" step as the final step."""

        return await generate_structured(prompt, ProcedureOverview, model=self.model)

    # ------------------------------------------------------------------
    # Pass 2: Step Detailing
    # ------------------------------------------------------------------

    async def _pass2_detail_steps(
        self, plain_text: str, overview: ProcedureOverview
    ) -> list[ProcedureStep]:
        """Extract full details for each step."""
        detailed_steps = []
        step_ids = [s.id for s in overview.steps]
        available_tools = ", ".join(self.registry.tool_names)

        for step_overview in overview.steps:
            prompt = self._build_step_detail_prompt(
                plain_text, step_overview, step_ids, available_tools, overview
            )
            detailed = await generate_structured(prompt, ProcedureStep, model=self.model)
            detailed_steps.append(detailed)

        return detailed_steps

    def _build_step_detail_prompt(
        self,
        plain_text: str,
        step: Any,
        step_ids: list[str],
        available_tools: str,
        overview: ProcedureOverview,
    ) -> str:
        """Build the prompt for detailing a single step."""
        valid_targets = ", ".join(step_ids + ["end"])

        base = f"""Extract full details for this step from the procedure.

PROCEDURE TEXT:
---
{plain_text}
---

STEP TO DETAIL:
- ID: {step.id}
- Name: {step.name}
- Action: {step.action.value}
- Brief: {step.instruction}

All valid step IDs for next_step references: {valid_targets}
Available tools: {available_tools}
"""

        if step.action == ActionType.collect_info:
            base += """
For this collect_info step, provide:
- instruction: detailed prompt for the agent
- extract_fields: list of fields to extract, each with key, description, and examples
- required_fields: which extract_fields keys are required before proceeding
- next_step: the step to go to after collecting info"""

        elif step.action == ActionType.tool_call:
            base += """
For this tool_call step, provide:
- instruction: what to tell the user about the tool result
- tools: list of tool configs, each with name (from available tools), arg_mappings (param->source), result_key
- on_success: step ID if tool succeeds
- on_failure: step ID if tool fails"""

        elif step.action == ActionType.evaluate:
            base += f"""
For this evaluate step, provide:
- instruction: context for evaluation
- conditions: list of condition branches. For each:
  - If the condition is objective/deterministic, provide a structured condition with field, operator, and value.
    Valid operators: eq, neq, gt, gte, lt, lte, in, not_in, within_days, outside_days, contains
    Known fields: {', '.join(FIELD_LOCATIONS.keys())}
  - If the condition is subjective, set condition to null and provide condition_description
  - Always provide next_step
- If ALL conditions are subjective, also set classify_categories and classify_result_key"""

        elif step.action == ActionType.inform:
            base += """
For this inform step, provide:
- instruction: message template for the user
- options: list of options if the user has choices, each with label, description, next_step, and detection_keywords
- next_step: if there's a single next step (no options)"""

        elif step.action == ActionType.end:
            base += """
For this end step, just provide a brief instruction summarizing the procedure completion."""

        return base

    # ------------------------------------------------------------------
    # Pass 3: Condition & Tool Refinement
    # ------------------------------------------------------------------

    async def _pass3_refine(
        self, steps: list[ProcedureStep], overview: ProcedureOverview
    ) -> list[ProcedureStep]:
        """Refine conditions and validate tools against registry."""
        refined = []
        for step in steps:
            if step.action == ActionType.evaluate:
                step = await self._refine_evaluate_step(step)
            elif step.action == ActionType.tool_call:
                step = self._refine_tool_step(step)
            refined.append(step)
        return refined

    async def _refine_evaluate_step(self, step: ProcedureStep) -> ProcedureStep:
        """Refine conditions: try to make them structured where possible."""
        refined_conditions = []
        has_subjective = False

        for branch in step.conditions:
            if branch.condition is not None:
                # Already structured — validate operator
                refined_conditions.append(branch)
            elif branch.condition_description:
                # Try to parse into structured condition via LLM
                structured = await self._try_structure_condition(branch.condition_description)
                if structured:
                    refined_conditions.append(ConditionBranch(
                        condition=structured,
                        condition_description=branch.condition_description,
                        next_step=branch.next_step,
                    ))
                else:
                    has_subjective = True
                    refined_conditions.append(branch)
            else:
                has_subjective = True
                refined_conditions.append(branch)

        # If any conditions are subjective, set up classify_categories
        if has_subjective and not step.classify_categories:
            step = step.model_copy(update={
                "conditions": refined_conditions,
                "classify_categories": [b.next_step for b in refined_conditions],
                "classify_result_key": f"{step.id}_result",
            })
        else:
            step = step.model_copy(update={"conditions": refined_conditions})

        return step

    async def _try_structure_condition(self, description: str) -> StructuredCondition | None:
        """Try to convert a natural language condition into a StructuredCondition."""
        known_fields = ", ".join(FIELD_LOCATIONS.keys())
        operators = ", ".join(op.value for op in ConditionOperator)

        prompt = f"""Convert this condition description into a structured condition if possible.

Condition: "{description}"

Known fields: {known_fields}
Valid operators: {operators}

If the condition is objective and can be expressed with a field, operator, and value, return a structured condition.
If the condition is subjective or requires human judgment, return a condition with field="subjective", operator="eq", value="true".
"""
        try:
            result = await generate_structured(prompt, StructuredCondition, model=self.model)
            if result.field == "subjective":
                return None
            return result
        except Exception:
            return None

    def _refine_tool_step(self, step: ProcedureStep) -> ProcedureStep:
        """Validate tool names and arg_mappings against the registry."""
        refined_tools = []
        for tool_config in step.tools:
            if not self.registry.has(tool_config.name):
                logger.warning(f"Unknown tool '{tool_config.name}' in step '{step.id}', skipping")
                continue

            # Validate arg_mappings against tool signature
            entry = self.registry.get(tool_config.name)
            if entry and not tool_config.arg_mappings:
                # Auto-populate from registry defaults
                tool_config = tool_config.model_copy(update={
                    "arg_mappings": [
                        ToolArgMapping(param=k, source=v)
                        for k, v in entry.arg_keys.items()
                    ],
                })
            if entry and not tool_config.result_key:
                tool_config = tool_config.model_copy(update={
                    "result_key": f"{tool_config.name}_result",
                })

            refined_tools.append(tool_config)

        return step.model_copy(update={"tools": refined_tools})

    # ------------------------------------------------------------------
    # Pass 4: Validation & Refinement Loop
    # ------------------------------------------------------------------

    async def _pass4_validate(self, procedure: Procedure) -> Procedure:
        """Validate and iteratively fix errors."""
        for round_num in range(self.max_refinement_rounds):
            errors = self._validate_procedure(procedure)
            if not errors:
                logger.info(f"Validation passed on round {round_num + 1}")
                return procedure

            logger.info(f"Validation round {round_num + 1}: {len(errors)} errors found")
            procedure = await self.refine(procedure, errors)

        # Final validation check
        errors = self._validate_procedure(procedure)
        if errors:
            logger.warning(f"Procedure still has {len(errors)} errors after {self.max_refinement_rounds} rounds")

        return procedure

    def _validate_procedure(self, procedure: Procedure) -> list[str]:
        """Validate structural integrity of the procedure."""
        errors = []
        step_ids = {s.id for s in procedure.steps}
        step_ids.add("end")

        for step in procedure.steps:
            # Check next_step references
            if step.next_step and step.next_step not in step_ids:
                errors.append(
                    f"Step '{step.id}' references unknown next_step '{step.next_step}'"
                )

            # Check on_success / on_failure
            if step.on_success and step.on_success not in step_ids:
                errors.append(
                    f"Step '{step.id}' references unknown on_success '{step.on_success}'"
                )
            if step.on_failure and step.on_failure not in step_ids:
                errors.append(
                    f"Step '{step.id}' references unknown on_failure '{step.on_failure}'"
                )

            # Check condition branch targets
            for branch in step.conditions:
                if branch.next_step not in step_ids:
                    errors.append(
                        f"Step '{step.id}' condition branch references unknown next_step '{branch.next_step}'"
                    )

            # Check inform option targets
            for opt in step.options:
                if opt.next_step not in step_ids:
                    errors.append(
                        f"Step '{step.id}' option references unknown next_step '{opt.next_step}'"
                    )

            # Check tool names against registry
            for tool_config in step.tools:
                if not self.registry.has(tool_config.name):
                    errors.append(
                        f"Step '{step.id}' references unknown tool '{tool_config.name}'"
                    )

            # Check available_tools
            for tool_name in procedure.available_tools:
                if not self.registry.has(tool_name):
                    errors.append(
                        f"Procedure declares unknown available_tool '{tool_name}'"
                    )

        return errors


def _clean_empty_fields(d: dict | list) -> None:
    """Recursively remove empty strings, empty lists, and None values for cleaner YAML."""
    if isinstance(d, dict):
        keys_to_remove = []
        for k, v in d.items():
            if v is None or v == "" or v == []:
                keys_to_remove.append(k)
            elif isinstance(v, (dict, list)):
                _clean_empty_fields(v)
        for k in keys_to_remove:
            del d[k]
    elif isinstance(d, list):
        for item in d:
            if isinstance(item, (dict, list)):
                _clean_empty_fields(item)
