# BT Compiler: YAML-to-py_trees Behaviour Tree Compiler

The compiler (`bt_engine/compiler/`) converts YAML procedure definitions into fully functional py_trees behaviour trees. This enables workflows to be created, modified, and deployed by editing YAML files — no Python code changes required.

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
- [YAML Procedure Format](#yaml-procedure-format)
- [Action Types](#action-types)
- [Condition Grammar](#condition-grammar)
- [Cycle Handling](#cycle-handling)
- [Adding New Tools](#adding-new-tools)
- [Design Decisions](#design-decisions)

---

## Overview

The compiler bridges the gap between declarative YAML workflow definitions and the imperative py_trees execution model:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  YAML File   │────▶│   Compiler   │────▶│  py_trees    │
│  (declare)   │     │  (compile)   │     │  (execute)   │
└──────────────┘     └──────────────┘     └──────────────┘
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

### Step Fields by Action

**`collect_info`**:
```yaml
action: collect_info
required_info: [field1, field2]  # What to extract
extract_keys: [key1, key2]      # Optional: override inferred keys
next_step: next_step_id         # Where to go after collection
```

**`tool_call`** (single tool):
```yaml
action: tool_call
tool: tool_name                  # Primary tool
on_success: step_after_success
on_failure: step_after_failure
arg_keys: {param: bb_key}       # Optional: override registry defaults
fixed_args: {param: value}      # Optional: constant arguments
result_key: my_result            # Optional: where to store result
```

**`tool_call`** (multiple tools):
```yaml
action: tool_call
tool: primary_tool
tools:                           # Tried in order with conditions
  - lookup_order                 # First: requires exact ID
  - search_orders                # Second: requires search clues
on_success: next_step
on_failure: fallback_step
```

**`evaluate`**:
```yaml
action: evaluate
conditions:
  - if: "field == value"         # Parseable → ConditionNode
    next_step: target_step
  - if: "subjective condition"   # Unparseable → LLMClassifyNode
    next_step: other_step
```

**`inform`** (with options):
```yaml
action: inform
options:
  - label: "Customer accepts"
    next_step: accept_path
  - label: "Customer requests escalation"
    next_step: escalate_path
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
