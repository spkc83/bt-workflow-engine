# BT Compiler: YAML-to-BehaviourTree Compiler

The compiler (`bt_engine/compiler/`) converts YAML procedure definitions into fully functional async behaviour trees. This enables workflows to be created, modified, and deployed by editing YAML files — no Python code changes required.

## Table of Contents

- [Overview](#overview)
- [Compilation Pipeline](#compilation-pipeline)
- [Module Reference](#module-reference)
  - [ProcedureCompiler](#procedurecompiler)
  - [YAML Parser](#yaml-parser)
  - [Condition Parser](#condition-parser)
  - [Step Compilers](#step-compilers)
  - [Tool Registry](#tool-registry)
  - [Tree Manager](#tree-manager)
  - [Pydantic Schemas](#pydantic-schemas)
  - [Constrained Decoding Helpers](#constrained-decoding-helpers)
  - [Ingestion Pipeline](#ingestion-pipeline)
- [YAML Procedure Format](#yaml-procedure-format)
  - [Standardized Format](#standardized-format)
- [Action Types](#action-types)
- [Condition Grammar](#condition-grammar)
  - [String Conditions](#string-conditions)
  - [Structured Conditions](#structured-conditions)
- [Cycle Handling](#cycle-handling)
- [Adding New Tools](#adding-new-tools)
- [Ingesting Plain English SOPs](#ingesting-plain-english-sops)
- [Design Decisions](#design-decisions)

---

## Overview

The compiler bridges the gap between declarative YAML workflow definitions and the imperative py_trees execution model:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  YAML File   │────▶│   Compiler   │────▶│  py_trees    │
│  (declare)   │     │  (compile)   │     │  (execute)   │
└──────────────┘     └──────────────┘     └──────────────┘
       ▲
       │ generates
┌──────────────┐
│  Plain Text  │────▶ Ingestion Pipeline (LLM + constrained decoding)
│  (SOP/Runbook│     → Structure extraction → Step detailing
│   in English)│     → Condition/tool refinement → Validation loop
└──────────────┘
```

Each YAML step maps to a **subtree pattern** — a composition of py_trees nodes (Sequence, Selector, ConditionNode, ToolActionNode, LLMResponseNode, etc.) that implements the step's behavior.

---

## Compilation Pipeline

```
1. load_and_validate(yaml_path)     # parser.py
   → Parsed procedure dict with normalized fields

2. For each step, determine action type:
   → collect_info | tool_call | evaluate | inform | end

3. Call the action's compiler function:   # step_compilers.py
   → parse_condition(cond_str)            # condition_parser.py
   → registry.get(tool_name)              # tool_registry.py
   → compile_step(next_step_id) [recursive]

4. Assemble into root Sequence:           # __init__.py
   → Follow next_step chain for linear flow
   → Cycle detection via compiling_stack

5. Return py_trees.trees.BehaviourTree
```

---

## Module Reference

### ProcedureCompiler

**File**: `bt_engine/compiler/__init__.py`

The main public API for compilation.

```python
from bt_engine.compiler import ProcedureCompiler

compiler = ProcedureCompiler()

# Compile from file
tree = compiler.compile("procedures/customer_service_refund.yaml")

# Compile from parsed dict
proc = {"id": "test", "name": "Test", "steps": [...]}
tree = compiler.compile_from_dict(proc)
```

**Key behaviors**:

- **Recursive compilation**: `compile_step(step_id)` is called recursively as steps reference each other via `next_step`, `on_success`, `on_failure`, and condition branches.
- **Cycle detection**: A `compiling_stack` set tracks steps currently being compiled. If a step references a step already on the stack (back-edge), compilation returns `None` instead of recursing infinitely.
- **Memoization**: A `compiled_cache` prevents duplicate compilation of the same step within a single compile pass.
- **Linear chain following**: After compiling the first step, `_compile_linear_chain()` follows the `next_step` references to build the top-level Sequence.

### YAML Parser

**File**: `bt_engine/compiler/parser.py`

Loads YAML files and validates structure.

```python
from bt_engine.compiler.parser import load_and_validate

proc = load_and_validate("procedures/my_workflow.yaml")
# proc = {"id": "...", "name": "...", "steps": [...]}
```

**Validation rules**:
- Procedure must have `id`, `name`, `steps`
- Steps must be a non-empty list
- Each step must have `id` and `action`
- Action must be one of: `collect_info`, `tool_call`, `evaluate`, `inform`, `end`
- `tool_call` must have `tool` or `tools`
- `evaluate` must have `conditions`
- No duplicate step IDs

**Normalization**: Optional fields are set to defaults:
- `instruction` defaults to `""`
- `on_success`, `on_failure` default to `None`
- `tools` list is populated from single `tool` if not present

### Condition Parser

**File**: `bt_engine/compiler/condition_parser.py`

Converts condition strings from YAML `evaluate` steps into Python predicate callables.

```python
from bt_engine.compiler.condition_parser import parse_condition

pred = parse_condition("risk_score >= 80")
assert pred({"alert_data": {"risk_score": 92}}) is True

# Unparseable conditions return None
pred = parse_condition("multiple high-confidence fraud indicators")
assert pred is None  # Signals: use LLMClassifyNode instead
```

**Field resolution**: The `FIELD_LOCATIONS` dict maps field names to nested blackboard paths:

```python
FIELD_LOCATIONS = {
    "severity":    ("alert_data", "severity"),
    "risk_score":  ("alert_data", "risk_score"),
    "order_status": ("order_data", "status"),
    "order_date":  ("order_data", "days_since_delivery"),
    "complaint_type": (None, "complaint_type"),  # top-level
    ...
}
```

Fields not in the map are looked up at the top-level `bb_dict`.

### Step Compilers

**File**: `bt_engine/compiler/step_compilers.py`

Each YAML action type has a dedicated compiler function that returns a py_trees subtree.

See [Action Types](#action-types) below for the generated patterns.

### Tool Registry

**File**: `bt_engine/compiler/tool_registry.py`

Maps YAML tool name strings to actual Python async functions.

```python
from bt_engine.compiler.tool_registry import create_default_registry

registry = create_default_registry()
entry = registry.get("lookup_order")
# entry.func = tools.crm_tools.lookup_order
# entry.arg_keys = {"order_id": "order_id"}
# entry.fixed_args = {}
```

**Signature introspection**: When `arg_keys` is not explicitly provided, `_infer_arg_keys()` inspects the function signature. Each parameter (except `bb`) with no default value maps to a blackboard key of the same name. Parameters with defaults are skipped (treated as optional).

**Default registry**: `create_default_registry()` pre-registers all 14 tools:

| Tool | Module | Inferred arg_keys |
|------|--------|--------------------|
| `lookup_order` | crm_tools | `{order_id: order_id}` |
| `search_orders` | crm_tools | `{customer_id: customer_id}` |
| `get_customer_profile` | crm_tools | `{customer_id: customer_id}` |
| `issue_refund` | crm_tools | `{order_id: order_id, reason: reason}` |
| `update_case_status` | crm_tools | `{case_id: case_id, status: status, notes: notes}` |
| `escalate_to_supervisor` | common_tools | `{case_id: case_id, reason: reason, priority: priority}` |
| `add_case_note` | common_tools | `{case_id: case_id, note: note}` |
| `get_knowledge_article` | common_tools | `{query: query}` |
| `get_fraud_alert` | fraud_tools | `{alert_id: alert_id}` |
| `get_account_transactions` | fraud_tools | `{account_id: account_id, days: days}` |
| `check_device_fingerprint` | fraud_tools | `{account_id: account_id}` |
| `flag_account` | fraud_tools | `{account_id: account_id, reason: reason, action: action}` |
| `submit_sar` | fraud_tools | `{account_id: account_id, alert_id: alert_id, findings: findings}` |
| `close_alert` | fraud_tools | `{alert_id: alert_id, resolution: resolution}` |

### Tree Manager

**File**: `bt_engine/compiler/tree_manager.py`

Runtime management of compiled procedure trees.

```python
from bt_engine.compiler.tree_manager import TreeManager

manager = TreeManager(procedures_dir="procedures")
manager.load_all()

# Get a tree factory for an intent
factory = manager.get_tree_factory("refund")
tree = factory()  # Fresh tree per session

# List all registered intents
intents = manager.get_all_intents()  # ["refund", "complaint", "fraud_alert"]

# Hot reload
manager.reload_file("procedures/customer_service_refund.yaml")
manager.reload_all()
```

**Intent normalization**: Various trigger phrasings map to canonical keys:
- `"refund"`, `"return"`, `"money back"`, `"cancel order"` → `"refund"`
- `"complaint"`, `"unhappy"`, `"dissatisfied"` → `"complaint"`
- `"fraud alert"`, `"suspicious activity"` → `"fraud_alert"`

### Pydantic Schemas

**File**: `bt_engine/compiler/schemas.py`

Defines the standardized procedure format as Pydantic models. These serve triple duty: constrained decoding schema for LLM output, validation, and documentation.

**Key models**:

| Model | Purpose |
|-------|---------|
| `Procedure` | Top-level procedure with metadata, tools, and steps |
| `ProcedureStep` | A single step with action-specific standardized fields |
| `StructuredCondition` | Machine-readable condition: `{field, operator, value}` |
| `ConditionBranch` | An evaluate branch: structured condition or LLM-classified |
| `ToolConfig` | Tool with explicit `arg_mappings` and `guard_condition` |
| `ExtractField` | Field to extract: `{key, description, examples}` |
| `InformOption` | User option with `detection_keywords` for deterministic routing |
| `ProcedureOverview` / `StepOverview` | Intermediate schemas for ingestion Pass 1 |

**Enums**: `ActionType` (collect_info, tool_call, evaluate, inform, end), `ConditionOperator` (eq, neq, gt, gte, lt, lte, in, not_in, within_days, outside_days, contains).

### Constrained Decoding Helpers

**File**: `bt_engine/compiler/llm_utils.py`

Shared utilities for constrained LLM calls using Google GenAI structured output.

```python
from bt_engine.compiler.llm_utils import generate_structured, classify_enum, make_dynamic_enum

# JSON-constrained generation: response matches Pydantic schema exactly
condition = await generate_structured("Parse this condition...", StructuredCondition)

# Enum-constrained classification: response is guaranteed to be one of the values
CategoryEnum = make_dynamic_enum("Cat", ["fraud_confirmed", "false_positive"])
result = await classify_enum("Classify this alert...", CategoryEnum)
```

- `generate_structured(prompt, schema)` — Uses `response_mime_type="application/json"` with `response_schema` set to the Pydantic model
- `classify_enum(prompt, enum_class)` — Uses `response_mime_type="text/x.enum"` with `response_schema` set to the Enum
- `make_dynamic_enum(name, values)` — Creates Enum classes at runtime from a list of strings

### Ingestion Pipeline

**File**: `bt_engine/compiler/ingestion.py`

Multi-pass LLM pipeline that converts plain English SOPs into validated `Procedure` objects.

```python
from bt_engine.compiler.ingestion import ProcedureIngester
from bt_engine.compiler.tool_registry import create_default_registry

ingester = ProcedureIngester(registry=create_default_registry())

# Plain text → validated Procedure object
procedure = await ingester.ingest("When a customer requests a refund, first collect...")

# Plain text → YAML file
path = await ingester.ingest_to_yaml("When a customer...", "procedures/my_proc.yaml")
```

**Pipeline stages**:

| Pass | Input | Output | Method |
|------|-------|--------|--------|
| 1. Structure Extraction | Raw text | `ProcedureOverview` (IDs, names, action types) | Constrained JSON |
| 2. Step Detailing | Text + overview | `ProcedureStep` per step (full details) | Constrained JSON per step |
| 3. Condition & Tool Refinement | Detailed steps | Structured conditions, validated tools | Constrained JSON + registry validation |
| 4. Validation & Refinement Loop | Assembled procedure | Error-free `Procedure` | Pydantic validation + LLM fixes (up to N rounds) |

See [Ingesting Plain English SOPs](#ingesting-plain-english-sops) for usage details.

---

## YAML Procedure Format

```yaml
procedure:
  id: unique_identifier          # Required: used as workflow ID
  name: "Human-Readable Name"    # Required: display name
  description: "What this does"  # Optional: used in intent classification
  version: "1.0"                 # Optional: for tracking
  trigger_intents:               # Optional: intent strings that route here
    - refund
    - return

  steps:
    - id: step_id                # Required: unique within procedure
      instruction: >             # The LLM prompt / task description
        Detailed instructions for this step...
      action: collect_info       # Required: one of the action types
      # ... action-specific fields
```

### Standardized Format

All procedure YAML files use a single standardized format with structured, machine-readable fields.

**`collect_info`** — extract fields with descriptions:
```yaml
action: collect_info
extract_fields:
  - key: order_id
    description: "The order number (e.g., ORD-123)"
    examples: ["ORD-123", "ORD-789"]
  - key: merchant_name
    description: "Store or brand name"
    examples: ["TechMart", "SportZone"]
required_fields: [order_id]
next_step: next_step_id
```

**`tool_call`** (single tool) — explicit arg_mappings:
```yaml
action: tool_call
tools:
  - name: lookup_order
    arg_mappings:
      - param: order_id
        source: order_id
    result_key: order_data
on_success: step_after_success
on_failure: step_after_failure
```

**`tool_call`** (multiple tools) — guard conditions select the tool:
```yaml
action: tool_call
tools:
  - name: lookup_order
    arg_mappings:
      - param: order_id
        source: order_id
    result_key: order_data
    guard_condition:
      field: order_id
      operator: neq
      value: ""
  - name: search_orders
    arg_mappings:
      - param: customer_id
        source: customer_id
      - param: merchant_name
        source: merchant_name
    result_key: search_result
on_success: next_step
on_failure: fallback_step
```

**`evaluate`** (deterministic) — structured conditions:
```yaml
action: evaluate
conditions:
  - condition:
      field: order_date
      operator: within_days
      value: 30
    condition_description: "Order within 30-day return window"
    next_step: process_refund
  - condition:
      field: order_status
      operator: eq
      value: processing
    condition_description: "Order still processing"
    next_step: cancel_order
```

**`evaluate`** (subjective) — LLM classification with explicit categories:
```yaml
action: evaluate
classify_categories: [fraud_confirmed, false_positive, needs_review]
classify_result_key: triage_result
conditions:
  - condition_description: "Evidence strongly suggests fraud"
    next_step: flag_account
  - condition_description: "Appears to be a false positive"
    next_step: close_alert
```

**`inform`** (with options) — detection keywords for routing:
```yaml
action: inform
options:
  - label: "Customer accepts store credit"
    next_step: offer_store_credit
    detection_keywords: ["credit", "store", "accept", "yes", "fine", "ok"]
  - label: "Customer requests escalation"
    next_step: escalate_case
    detection_keywords: ["escalat", "supervisor", "manager", "not satisf"]
```

**`inform`** (simple loop-back):
```yaml
action: inform
next_step: earlier_step          # Back-edge: runner re-ticks from root
```

**`end`**:
```yaml
action: end
```

| Feature | Description |
|---------|-------------|
| Structured conditions | `condition: {field, operator, value}` — compiled to ConditionNode predicates |
| Explicit tool arg_mappings | `arg_mappings: [{param, source}]` — no reliance on signature inference |
| Rich extract_fields | `extract_fields: [{key, description, examples}]` — guides LLM extraction |
| Detection keywords | `detection_keywords: [kw1, kw2]` — deterministic inform option routing |
| Explicit classification | `classify_categories` + `classify_result_key` — for subjective conditions |
| Guard conditions | `guard_condition: {field, operator, value}` — selects which tool to invoke |

---

## Action Types

### `collect_info` → Extract-Check-Ask Pattern

```
Sequence(step_id, memory=True):
  LLMExtractNode(extract fields from user message)
  Selector(check_has_info):
    ConditionNode(has ID?)
    ConditionNode(has descriptive clues?)
    Sequence(ask_for_info):
      LLMResponseNode(ask using instruction)
      UserInputNode(wait for response)
      LLMExtractNode(re-extract from response)
  LogNode(step done)
```

### `tool_call` (single) → Success/Failure Branching

```
Selector(step_id):
  Sequence(success_path):
    ToolActionNode(call tool)
    LLMResponseNode(confirm using instruction)
    [compiled on_success subtree]
  Sequence(failure_path):
    [compiled on_failure subtree]
```

### `tool_call` (multiple) → Condition-Guarded Tool Selection

```
Selector(step_id):
  Sequence(tool_1_path):
    ConditionNode(has exact ID?) → ToolActionNode(tool_1)
  Sequence(tool_2_path):
    ConditionNode(has search clues?) → ToolActionNode(tool_2)
  [fallback: LLMResponseNode or compiled on_failure]
```

### `evaluate` (parseable) → Deterministic ConditionNode Routing

```
Selector(step_id):
  Sequence(cond_0): ConditionNode(parsed predicate) → [compiled next_step]
  Sequence(cond_1): ConditionNode(parsed predicate) → [compiled next_step]
  ...
```

### `evaluate` (unparseable) → LLM Classification + Routing

```
Sequence(step_id):
  LLMClassifyNode(categories, result_key)
  Selector(route):
    Sequence(cat_0): ConditionNode(result == cat_0) → [compiled next_step]
    Sequence(cat_1): ConditionNode(result == cat_1) → [compiled next_step]
    ...
```

### `inform` (with options) → Present + Wait + Route

```
Sequence(step_id):
  LLMResponseNode(instruction)
  UserInputNode(wait)
  Selector(route):
    Sequence(opt_0): ConditionNode(keyword match) → [compiled next_step]
    Sequence(opt_1): ConditionNode(keyword match) → [compiled next_step]
```

### `end` → Terminal LogNode

```
LogNode("workflow_end")
```

---

## Condition Grammar

The condition parser supports these patterns via regex matching:

| Pattern | Regex | Example |
|---------|-------|---------|
| Equality | `^(\w+)\s*==\s*(.+)$` | `severity == high` |
| Greater/equal | `^(\w+)\s*>=\s*([\d.]+)$` | `risk_score >= 80` |
| Less than | `^(\w+)\s*<\s*([\d.]+)$` | `risk_score < 40` |
| Greater than | `^(\w+)\s*>\s*([\d.]+)$` | `days > 30` |
| Less/equal | `^(\w+)\s*<=\s*([\d.]+)$` | `score <= 50` |
| In list | `^(\w+)\s+in\s+\[([^\]]+)\]$` | `status in [delivered, shipped]` |
| Not in | `^(\w+)\s+not\s+in\s+(\w+)$` | `category not in non_refundable_list` |
| Within days | `^(\w+)\s+within\s+(\d+)\s+days?$` | `order_date within 30 days` |
| Outside days | `^(\w+)\s+outside\s+(\d+)\s+days?$` | `order_date outside 30 days` |
| AND | `\s+AND\s+` split | `A AND B AND C` |
| OR | `\s+OR\s+` split | `A OR B` |

**Unparseable fallback**: Any condition that doesn't match these patterns returns `None`. The step compiler then uses `LLMClassifyNode` instead, with categories derived from the condition's `next_step` values. This handles subjective conditions like fraud risk assessment.

### Structured Conditions

The standardized format uses `StructuredCondition` objects instead of strings. These are already parsed — no regex needed.

```python
from bt_engine.compiler.condition_parser import parse_structured_condition

# From a Pydantic model
from bt_engine.compiler.schemas import StructuredCondition, ConditionOperator
cond = StructuredCondition(field="risk_score", operator=ConditionOperator.gte, value=80)
pred = parse_structured_condition(cond)
assert pred({"alert_data": {"risk_score": 92}}) is True

# From a plain dict (e.g., loaded from YAML)
pred = parse_structured_condition({"field": "severity", "operator": "eq", "value": "high"})
assert pred({"alert_data": {"severity": "high"}}) is True
```

**Supported operators**: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `within_days`, `outside_days`, `contains`.

**Field resolution**: Uses `field_path` if provided (e.g., `"alert_data.severity"`), otherwise falls back to `FIELD_LOCATIONS` lookup, then top-level `bb_dict`.

**Compiler behavior**: The step compiler checks each condition branch for a `condition` object first. If found, it uses `parse_structured_condition()`. If not, it falls back to `parse_condition()` on an `if` string (for backward compatibility).

---

## Cycle Handling

YAML procedures can contain cycles (e.g., `order_not_found → greet_and_collect`). The compiler handles these via **back-edge detection**:

1. A `compiling_stack` set tracks steps currently being compiled
2. When `compile_step(X)` is called and `X` is already on the stack, it's a back-edge
3. The compiler returns `None` for back-edges instead of recursing
4. The `inform` step at the cycle point terminates with `UserInputNode`
5. On the next user message, the runner re-ticks from root with updated blackboard

This matches the hand-coded tree pattern where loop-back points produce output, pause for input, and the runner starts fresh from root.

---

## Adding New Tools

1. Create the async function in the appropriate tools module:

```python
# tools/crm_tools.py
async def my_new_tool(order_id: str, action: str, bb: dict) -> dict:
    # ... implementation
    bb["my_result"] = result
    return result
```

2. Register it in `tool_registry.py`:

```python
def create_default_registry() -> ToolRegistry:
    from tools.crm_tools import my_new_tool
    # ...
    registry.register("my_new_tool", my_new_tool)
    # Inferred arg_keys: {order_id: order_id, action: action}
```

3. Use it in YAML:

```yaml
- id: my_step
  action: tool_call
  tool: my_new_tool
  fixed_args:
    action: "process"
  on_success: next_step
```

---

## Ingesting Plain English SOPs

The ingestion pipeline (`bt_engine/compiler/ingestion.py`) converts plain English procedure documents into validated, standardized YAML procedures using a multi-pass LLM pipeline with constrained decoding.

### Usage

**Python API**:
```python
from bt_engine.compiler.ingestion import ProcedureIngester
from bt_engine.compiler.tool_registry import create_default_registry

ingester = ProcedureIngester(registry=create_default_registry())

# Ingest to Procedure object
procedure = await ingester.ingest("""
When a customer contacts us about a refund:
1. First, collect their order information - order number, store name, or item description
2. Look up the order in our system
3. Check if the order is eligible for a refund (within 30 days, delivered status)
4. If eligible, process the refund
5. If outside the return window, offer store credit or escalation
6. Close the case with a summary
""")

# Ingest directly to YAML file
path = await ingester.ingest_to_yaml(plain_text, "procedures/new_refund.yaml")
```

**REST API**:
```bash
curl -X POST http://localhost:8000/api/procedures/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "When a customer contacts us about a refund...",
    "output_format": "yaml"
  }'
```

### Pipeline Details

**Pass 1 — Structure Extraction**: Identifies the procedure's ID, name, domain, trigger intents, available tools, and a list of steps with their IDs, names, and action types. Uses `text/x.enum` for action type classification.

**Pass 2 — Step Detailing**: For each step, extracts full details appropriate to its action type — extract_fields for collect_info, tool configs for tool_call, condition branches for evaluate, options for inform.

**Pass 3 — Condition & Tool Refinement**: Attempts to convert natural language conditions into `StructuredCondition` objects. Validates tool names against the registry and auto-populates arg_mappings from function signatures.

**Pass 4 — Validation & Refinement Loop**: Validates the assembled procedure (step references, tool names, condition fields). If errors are found, feeds them back to the LLM for correction. Loops up to `max_refinement_rounds` (default: 3).

### Constrained Decoding

All LLM calls in the pipeline use Google GenAI's constrained decoding:

- **`response_mime_type="application/json"` + `response_schema`**: Forces the LLM output to match a Pydantic schema exactly. Used for all structured extraction (procedure overview, step details, conditions).
- **`response_mime_type="text/x.enum"` + `response_schema`**: Forces the LLM output to be one of the enum values. Used for action type classification and `LLMClassifyNode`.

This eliminates hallucination risks — the LLM cannot produce invalid field names, unknown operators, or malformed JSON.

---

## Design Decisions

### Why regex-based condition parsing?

The condition grammar is small (~15 patterns). A regex parser has zero external dependencies (`lark`, `pyparsing`), is fast, and handles all conditions used in the three existing procedures. It can be upgraded to a proper parser later if the grammar grows.

### Why fresh compilation per session?

py_trees nodes hold mutable state (`_waiting` flags, internal counters). Sharing a tree between sessions would cause state corruption. Compiling a fresh tree per session takes microseconds of object construction and guarantees clean state.

### Why back-edge → None instead of loop nodes?

The hand-coded trees use the same pattern: loop-back points terminate the tree's tick, and the runner re-ticks from root on the next message. The compiled trees match this exactly, ensuring behavioral equivalence.

### Why LLMClassifyNode for unparseable conditions?

Fraud risk assessment conditions like "multiple high-confidence fraud indicators present" are inherently subjective. Rather than forcing these into a rigid grammar, the compiler delegates to LLM classification — matching what the hand-coded trees already do.

### Why keep hand-coded trees?

The hand-coded trees in `bt_engine/trees/` serve as **reference implementations** for equivalence testing. They're not used in production (main.py uses the compiler), but they validate that compiled trees route identically for all test scenarios.

### Why constrained decoding instead of free-text + parsing?

Free-text LLM output requires fuzzy string matching, JSON repair, and schema validation — all error-prone. Google GenAI's `response_schema` (Pydantic) and `text/x.enum` guarantee valid output at the API level. The LLM literally cannot produce an invalid operator or unknown action type. This eliminates an entire class of bugs.

### Why a multi-pass ingestion pipeline?

A single-pass "generate the whole procedure" approach produces lower quality output because it asks the LLM to do too many things at once. Multi-pass allows each stage to focus on one concern (structure, details, refinement) with targeted constrained schemas. It also enables targeted error correction — Pass 4 can fix specific validation errors without regenerating the whole procedure.

### Why keep backward-compatible parsing?

The compiler checks for standardized structured fields first (`condition` object, `extract_fields`, tool `arg_mappings`, `detection_keywords`), then falls back to simpler parsing (`if` strings, `required_info`, tool name strings, label-based routing). This ensures older or externally-generated YAML files still compile correctly.

### Why `LLMClassifyNode` has a free-text fallback?

Not all environments support `text/x.enum` constrained decoding (older model versions, alternative providers). The node tries constrained decoding first for maximum reliability, then falls back to the original free-text-with-string-matching approach if it fails. This maintains compatibility while improving accuracy where possible.
