# Conversational BT Architecture Design

**Date**: 2026-03-05
**Status**: Approved

## Problem

The BT workflow engine ticks through the entire workflow in a single burst,
producing a wall of concatenated LLM responses. There are no conversational
pause points, no session persistence, and no cross-session memory. The result
is a system that processes workflows correctly but fails as a conversational
agent.

## Goal

Create a conversational customer service, fraud, and claims processing AI agent
whose behaviour is strictly controlled by a behaviour tree while maintaining
natural, human-like conversational capability powered by LLMs. The structured
YAML procedures remain human-readable and auditable.

## Design

### 1. Hybrid Conversational Pause Points

Add `await_input` field to YAML step schema (optional bool).

**Default behavior** (when `await_input` is not specified):
- `tool_call` steps with `on_success` -> pause (decision points needing user confirmation)
- `collect_info` steps -> already pause when info is missing
- `inform` steps with options -> already pause for user choice
- `evaluate` steps -> no pause (internal routing)
- `tool_call` steps without `on_success` (terminal) -> no pause

**Override**: `await_input: true` forces a pause; `await_input: false` suppresses it.

**Implementation**: Step compilers insert `UserInputNode` after the step's
`LLMResponseNode` when a pause is needed. The tree returns RUNNING, the runner
sends the accumulated response to the user, and on the next message the tree
resumes from where it left off.

**Multi-tool pattern** (lookup_order):
```
Sequence(lookup_order):
  Selector(tools):          <- find order via exact ID or search
    [tool path 1]
    [tool path 2]
  LLMResponseNode           <- "I found your order, please confirm"
  UserInputNode             <- PAUSE here
  [on_success subtree]      <- check_eligibility -> process_refund -> close_case
```

### 2. Completion Guard

When the tree reaches SUCCESS:
- Mark the session as completed in blackboard state (`bb_dict["_workflow_completed"] = True`)
- On subsequent messages, don't re-tick the tree
- Return a graceful completion response or route to a new workflow

### 3. Customer Memory Module

**Storage**: `customer_memories` table in SQLite:
```sql
CREATE TABLE customer_memories (
    memory_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    session_id TEXT,
    memory_type TEXT,          -- 'workflow_outcome', 'fact', 'unresolved'
    summary TEXT,
    data JSON,
    created_at TEXT,
    expires_at TEXT
);
```

**Write**: At workflow completion, `MemoryWriteNode` summarizes the interaction
(LLM call on bb_dict) and persists to the table.

**Read**: At session start, `BTRunner.__init__` loads the customer's memories
into `bb_dict["customer_memories"]`. LLM nodes naturally reference them via
the existing bb_dict context.

**New node**: `MemoryWriteNode` in `bt_engine/nodes.py`.

**YAML integration**: `save_memory: true` on steps, or auto-added at workflow
completion by the compiler.

### 4. Session Pause & Resume

**Storage**: `sessions` table in SQLite:
```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    customer_id TEXT,
    procedure_id TEXT,
    intent TEXT,
    blackboard_state JSON,
    conversation_history JSON,
    tree_status TEXT,
    current_step TEXT,
    created_at TEXT,
    updated_at TEXT,
    expires_at TEXT
);
```

**Save**: After every `BTRunner.run()` call, serialize bb_dict, conversation
history, tree status, and current step to SQLite.

**Restore**: Client reconnects with `session_id` -> check in-memory, then
SQLite -> recompile tree, restore bb_dict, fast-forward via skip-on-resume.

**Skip-on-resume mechanism**:
- `bb_dict["_completed_steps"]` tracks finished step IDs
- `ToolActionNode` checks `result_key` in bb_dict -> SUCCESS without re-executing
- `LLMResponseNode` checks completed marker -> skips
- `ConditionNode` evaluates normally (deterministic, no side effects)
- `UserInputNode` at saved position -> RUNNING (pauses correctly)

## Files to Modify

| File | Change |
|------|--------|
| `bt_engine/compiler/step_compilers.py` | UserInputNode at pause points; MemoryWriteNode at completion |
| `bt_engine/compiler/schemas.py` | `await_input` and `save_memory` fields on ProcedureStep |
| `bt_engine/compiler/parser.py` | Pass through new fields |
| `bt_engine/nodes.py` | MemoryWriteNode; skip-on-resume on ToolActionNode, LLMResponseNode |
| `bt_engine/runner.py` | Completion guard; load memories; session save/restore; step tracking |
| `database/db.py` | customer_memories + sessions tables |
| `tools/crm_tools.py` | save_memory / load_memories functions |
| `main.py` | Session restore from DB in chat endpoint |
| `procedures/*.yaml` | await_input overrides where needed |
