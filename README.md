# BT Workflow Engine

A **behaviour tree workflow engine** for customer service automation. Workflows are defined as YAML procedures and compiled into deterministic async behaviour trees at runtime. LLMs handle natural language (response generation, extraction, classification) while Python conditions control all routing decisions.

## Architecture

```
                  ┌─────────────────┐
                  │   YAML Files    │  procedures/*.yaml
                  │  (Procedures)   │
                  └────────┬────────┘
                           │ compile
                  ┌────────▼────────┐
                  │   BT Compiler   │  bt_engine/compiler/
                  │  (YAML → Tree)  │
                  └────────┬────────┘
                           │ produces
                  ┌────────▼────────┐
                  │  py_trees Tree  │  Sequence, Selector, Condition, etc.
                  │ (Per Session)   │
                  └────────┬────────┘
                           │ ticks
                  ┌────────▼────────┐
                  │   BT Runner     │  bt_engine/runner.py
                  │ (Execution)     │  Blackboard state, audit trail
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌─────────┐ ┌──────────┐
         │  Tools  │ │   LLM   │ │  SQLite  │
         │ (CRM,   │ │ (Gemini)│ │   (DB)   │
         │ Fraud)  │ │         │ │          │
         └─────────┘ └─────────┘ └──────────┘
```

### Key Principles

- **Deterministic routing**: All branching decisions use Python predicates, not LLM calls
- **LLM-assisted, not LLM-driven**: LLMs generate responses, extract data, and classify inputs — the behaviour tree controls flow
- **YAML-configurable**: New workflows are created by writing YAML, not Python code
- **Conversational pause points**: Tree pauses at `UserInputNode` boundaries for natural multi-turn dialogue
- **Session persistence**: Sessions survive infrastructure failures via DB-backed pause & resume
- **Cross-session memory**: Customer interaction history carries across sessions for contextual responses
- **Fresh tree per session**: Each session gets a freshly compiled tree (py_trees nodes hold mutable state)
- **Hot reload**: YAML procedures can be reloaded at runtime without restart

## Project Structure

```
bt-workflow-engine/
├── bt_engine/                    # Core engine
│   ├── nodes.py                  # Custom BT node types (10 nodes)
│   ├── runner.py                 # BT execution engine (BTRunner)
│   ├── audit.py                  # Tick-level execution tracing
│   ├── trees/                    # Hand-coded reference trees
│   │   ├── refund.py
│   │   ├── complaint.py
│   │   └── fraud_triage.py
│   └── compiler/                 # YAML-to-py_trees compiler
│       ├── __init__.py           # ProcedureCompiler (public API)
│       ├── parser.py             # YAML loading + validation
│       ├── condition_parser.py   # Condition string/object → Python predicate
│       ├── step_compilers.py     # Per-action subtree builders
│       ├── tool_registry.py      # Tool name → async function mapping
│       ├── tree_manager.py       # Runtime management, intent routing
│       ├── schemas.py            # Pydantic models for standardized format
│       ├── llm_utils.py          # Constrained decoding helpers
│       └── ingestion.py          # LLM pipeline: plain text → procedure
├── tools/                        # Async tool functions
│   ├── crm_tools.py              # Orders, refunds, cases (5 tools)
│   ├── common_tools.py           # Escalation, notes, knowledge (3 tools)
│   └── fraud_tools.py            # Alerts, transactions, devices (6 tools)
├── database/                     # SQLite layer
│   ├── db.py                     # Schema + query helpers
│   └── seed.py                   # Mock data seeding
├── procedures/                   # YAML procedure definitions
│   ├── customer_service_refund.yaml
│   ├── customer_service_complaint.yaml
│   └── fraud_ops_alert_triage.yaml
├── examples/                     # Usage examples
│   ├── sample_sop.txt            # Plain English SOP for ingestion demo
│   └── ingest_demo.py            # LLM-powered ingestion demo script
├── tests/                        # Test suite (172 tests)
│   ├── test_bt_nodes.py          # Node unit tests
│   ├── test_bt_runner.py         # Runner integration tests
│   ├── test_tools.py             # Tool function tests
│   ├── test_compiler.py          # Compiler unit + integration tests
│   ├── test_tree_equivalence.py  # Compiled vs hand-coded equivalence
│   ├── test_schemas.py           # Schema validation + predicate tests
│   ├── test_constrained.py       # Constrained decoding tests
│   └── test_ingestion.py         # Ingestion pipeline tests
├── main.py                       # FastAPI backend
├── app_ui.py                     # Shiny for Python frontend
└── config.py                     # LLM configuration (Google Gemini)
```

## Quick Start

### Prerequisites

- Python 3.11+
- Google AI API key (for LLM features)

### Setup

```bash
# Clone and install
pip install -r requirements.txt

# Set environment variables
echo "GOOGLE_API_KEY=your-key-here" > .env

# Initialize database and start server
uvicorn main:app --reload --port 8000
```

### Run Tests

```bash
# Full test suite (170 tests)
pytest tests/ -v

# Just compiler tests
pytest tests/test_compiler.py -v

# Equivalence tests (compiled vs hand-coded)
pytest tests/test_tree_equivalence.py -v

# Ingestion + schema + constrained decoding tests
pytest tests/test_schemas.py tests/test_ingestion.py tests/test_constrained.py -v
```

## Workflows

Three built-in workflows are provided as YAML procedures (all use the standardized format):

| Workflow | File | Intents | Steps |
|----------|------|---------|-------|
| **Refund** | `customer_service_refund.yaml` | refund, return, money back, cancel order | 11 |
| **Complaint** | `customer_service_complaint.yaml` | complaint, unhappy, dissatisfied | 7 |
| **Fraud Triage** | `fraud_ops_alert_triage.yaml` | fraud alert, suspicious activity | 11 |

### Creating a New Workflow

There are two ways to create a new workflow:

**Option A: Ingest from plain English** (recommended for new procedures):

Use the ingestion pipeline to convert a plain English SOP into a structured YAML procedure. The LLM pipeline handles step identification, condition structuring, tool mapping, and validation automatically. See [Ingest a Plain English SOP](#ingest-a-plain-english-sop) below, or run the demo script:

```bash
# Ingest the included sample SOP (requires GOOGLE_API_KEY)
python examples/ingest_demo.py

# Ingest your own SOP
python examples/ingest_demo.py path/to/your_sop.txt --output procedures/my_proc.yaml
```

**Option B: Write YAML directly** (recommended for precise control):

The standardized format provides structured conditions, explicit tool arg mappings, extract field descriptions, and detection keywords. See any file in `procedures/` for examples.

```yaml
procedure:
  id: my_workflow
  name: "My Custom Workflow"
  version: "2.0"
  domain: customer_service
  trigger_intents: [xyz_request]
  available_tools: [lookup_order, update_case_status]
  data_context: [order_id, customer_id]

  steps:
    - id: collect
      name: "Collect Details"
      action: collect_info
      instruction: "Ask for order details."
      extract_fields:
        - key: order_id
          description: "The order number"
          examples: ["ORD-123"]
      required_fields: [order_id]
      next_step: lookup

    - id: lookup
      name: "Look Up Order"
      action: tool_call
      instruction: "Find the order."
      tools:
        - name: lookup_order
          arg_mappings:
            - param: order_id
              source: order_id
          result_key: order_data
      on_success: check
      on_failure: end

    - id: check
      name: "Evaluate Eligibility"
      action: evaluate
      conditions:
        - condition:
            field: order_date
            operator: within_days
            value: 30
          next_step: approve
        - condition:
            field: order_date
            operator: outside_days
            value: 30
          next_step: deny

    - id: approve
      action: end
      instruction: "Approved."
    - id: deny
      action: end
      instruction: "Denied."
```

2. Reload procedures (no restart needed):

```bash
curl -X POST http://localhost:8000/api/procedures/reload
```

3. The new workflow is immediately available for intent routing.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/chat` | Send a message, get a response (creates session on first call) |
| `POST` | `/api/procedures/ingest` | Convert plain English SOP to structured YAML procedure |
| `POST` | `/api/procedures/reload` | Hot-reload all YAML procedures |
| `GET` | `/api/bt/trace/{session_id}` | Full execution trace for a session |
| `GET` | `/api/bt/trace/{session_id}/summary` | Trace summary |
| `GET` | `/api/customers` | List all customers |
| `GET` | `/api/tables/{table_name}` | Browse database tables |
| `GET` | `/api/sessions` | List active sessions |
| `GET` | `/health` | Health check with loaded workflows |

### Chat Example

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "I want a refund for my order from TechMart", "user_id": "CUST-456"}'
```

### Ingest a Plain English SOP

```bash
curl -X POST http://localhost:8000/api/procedures/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "When a customer requests a refund: 1) Collect order details 2) Look up the order 3) Check eligibility (within 30 days, delivered status) 4) Process refund or offer alternatives 5) Close the case",
    "output_format": "yaml"
  }'
```

Returns the structured procedure and writes a YAML file ready for the compiler.

## BT Compiler

The compiler (`bt_engine/compiler/`) converts YAML procedure definitions into py_trees behaviour trees. See [docs/compiler.md](docs/compiler.md) for detailed documentation.

### Compilation Pipeline

```
YAML file → parser.py (load + validate)
          → condition_parser.py (parse condition strings)
          → step_compilers.py (build subtrees per action type)
          → tool_registry.py (resolve tool functions)
          → __init__.py (recursive assembly with cycle detection)
          → py_trees.BehaviourTree
```

### Supported YAML Actions

| Action | Description | Compiled Pattern |
|--------|-------------|-----------------|
| `collect_info` | Extract info from user, ask if missing | Extract → Check → Ask → Re-extract |
| `tool_call` | Call one or more tools | Selector with success/failure paths |
| `evaluate` | Route based on conditions | ConditionNode or LLMClassifyNode routing |
| `inform` | Present info, wait for response | LLMResponse → UserInput → Option routing |
| `end` | Terminate workflow | LogNode |

### Condition Parser

The compiler parses condition strings from YAML `evaluate` steps into Python predicates:

| Pattern | Example | Behavior |
|---------|---------|----------|
| `field == value` | `severity == high` | String/numeric equality |
| `field >= N` | `risk_score >= 80` | Numeric comparison |
| `field < N` | `risk_score < 40` | Numeric comparison |
| `field in [vals]` | `order_status in [delivered, shipped]` | Membership test |
| `field within N days` | `order_date within 30 days` | `days_since_delivery <= N` |
| `field outside N days` | `order_date outside 30 days` | `days_since_delivery > N` |
| `A AND B` | Combined conditions | Logical AND |
| `A OR B` | Combined conditions | Logical OR |

Unparseable conditions (e.g., `"multiple high-confidence fraud indicators present"`) automatically fall back to `LLMClassifyNode` for LLM-based classification.

## Node Types

| Node | Purpose | Status |
|------|---------|--------|
| `LLMResponseNode` | Generate natural language via LLM | SUCCESS/FAILURE |
| `LLMExtractNode` | Extract structured JSON from text | SUCCESS/FAILURE |
| `LLMClassifyNode` | Classify input into categories | SUCCESS/FAILURE |
| `ToolActionNode` | Call async tool function | SUCCESS/FAILURE |
| `ConditionNode` | Evaluate Python predicate | SUCCESS/FAILURE |
| `UserInputNode` | Pause for user input | RUNNING → SUCCESS |
| `BlackboardWriteNode` | Write data to blackboard | SUCCESS |
| `MemoryWriteNode` | Save interaction memory to DB | SUCCESS |
| `LogNode` | Audit trail entry | SUCCESS |

## Conversational Features

### Pause Points (`await_input`)

The tree pauses at `UserInputNode` boundaries so the user sees each step's response before the workflow continues. By default, tool_call steps with `on_success` targeting another step insert a pause. Override with `await_input: true/false` in YAML:

```yaml
- id: process_refund
  action: tool_call
  await_input: true   # force pause after this step
  # ...
```

### Session Pause & Resume

Sessions are persisted to SQLite after every `run()` call. If the server restarts or the connection drops, the session resumes from where it left off:

- `BTRunner.save_session()` serializes blackboard state, conversation history, and completed steps
- `BTRunner.load_session()` restores a session from DB
- Skip-on-resume: `ToolActionNode` and `LLMResponseNode` track completed steps to avoid re-execution

### Cross-Session Customer Memory

When `save_memory: true` is set on a step, a `MemoryWriteNode` persists an interaction summary to the `customer_memories` table. On new sessions, `BTRunner.load_memories()` loads past interactions into the blackboard, and `LLMResponseNode` includes them in prompt context for personalized responses.

### Completion Guard

Once a tree reaches SUCCESS or FAILURE, the runner refuses to re-tick it and returns a completion message. This prevents accidental re-execution of workflows.

## Database

SQLite with 16 tables covering customers, orders, accounts, transactions, fraud alerts, devices, cases, escalations, refunds, knowledge articles, customer memories, and sessions. Seeded with mock data on startup.

## Configuration

Environment variables (`.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | (required) | Google AI API key |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` | Use Vertex AI backend |
| `LLM_MODEL` | `gemini-2.5-flash` | Model name |

## Testing

The test suite validates the full stack:

- **Node tests** (16): Each node type in isolation
- **Runner tests** (10): Multi-turn execution, branching, tracing
- **Tool tests** (13): All 14 tool functions against SQLite
- **Compiler tests** (47): Condition parser, tool registry, YAML parser, full compilation, tree manager
- **Equivalence tests** (17): Compiled trees produce same routing as hand-coded trees
- **Schema tests** (21): Pydantic models, structured condition predicates, serialization round-trips
- **Constrained decoding tests** (8): `generate_structured`, `classify_enum`, `LLMClassifyNode` with constrained/fallback
- **Ingestion tests** (12): Pipeline stages, validation, tool refinement, YAML output (mocked LLM)

```bash
pytest tests/ -v  # 172 tests, ~4 seconds
```

## License

MIT
