"""FastAPI backend for the Behaviour Tree Workflow Engine."""

import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

from database import init_db, query_all, seed_all
from bt_engine.runner import BTRunner
from bt_engine.compiler.tree_manager import TreeManager
from config import get_client, get_model_name

# ---------------------------------------------------------------------------
# Session store: maps session_id -> BTRunner instance
# ---------------------------------------------------------------------------

_sessions: dict[str, BTRunner] = {}

# Tree manager — compiles YAML procedures into py_trees trees
_tree_manager = TreeManager(procedures_dir="procedures")

# Allowed tables for data browser
_ALLOWED_TABLES = {
    "customers", "orders", "order_items", "accounts", "transactions",
    "fraud_alerts", "devices", "login_history", "risk_indicators",
    "cases", "case_notes", "escalations", "refunds", "knowledge_articles",
}


# ---------------------------------------------------------------------------
# Intent classifier
# ---------------------------------------------------------------------------

async def classify_intent(message: str) -> str:
    """Classify user message into a workflow intent using LLM.

    Dynamically builds the intent list from loaded procedures.
    """
    intents = _tree_manager.get_all_intents()
    if not intents:
        return "general"

    # Build intent descriptions for the prompt
    intent_lines = []
    for proc in _tree_manager.get_all_procedures():
        triggers = ", ".join(proc.get("trigger_intents", []))
        # Use the canonical intent key for the procedure
        for intent in proc.get("trigger_intents", []):
            canonical = TreeManager._normalize_intent(intent)
            intent_lines.append(f"- {canonical}: {proc['description']} (triggers: {triggers})")
            break  # one line per procedure

    intent_lines.append("- general: none of the above")
    intent_list = "\n".join(intent_lines)

    prompt = f"""Classify this customer message into exactly ONE of these categories:
{intent_list}

Message: "{message}"

Return ONLY the category name, nothing else."""

    try:
        client = get_client()
        response = await client.aio.models.generate_content(
            model=get_model_name(),
            contents=prompt,
        )
        text = (response.text or "").strip().lower()
        # Check each known intent
        for intent_key in intents:
            if intent_key in text:
                return intent_key
        return "general"
    except Exception:
        return "general"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB, seed data, and load procedures on startup."""
    await init_db()
    await seed_all()
    _tree_manager.load_all()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BT Workflow Engine",
    description="Behaviour Tree workflow engine for customer service CRM.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    status: str
    intent: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Send a message to the BT workflow engine and get a response."""
    session_id = request.session_id or str(uuid.uuid4())
    intent = None

    # Get or create runner for this session
    runner = _sessions.get(session_id)

    if runner is None:
        # New session — classify intent and create appropriate tree
        intent = await classify_intent(request.message)

        if intent == "general":
            # No matching workflow — respond with a simple LLM call
            try:
                client = get_client()
                response = await client.aio.models.generate_content(
                    model=get_model_name(),
                    contents=(
                        "You are a helpful customer service agent. "
                        "Answer this general question concisely:\n\n"
                        f"{request.message}"
                    ),
                )
                return ChatResponse(
                    response=response.text or "I'm sorry, I couldn't process that.",
                    session_id=session_id,
                    status="SUCCESS",
                    intent=intent,
                )
            except Exception as e:
                return ChatResponse(
                    response=f"Sorry, I encountered an error: {e}",
                    session_id=session_id,
                    status="FAILURE",
                    intent=intent,
                )

        # Create the tree and runner
        factory = _tree_manager.get_tree_factory(intent)
        if factory is None:
            raise HTTPException(status_code=400, detail=f"Unknown intent: {intent}")

        tree = factory()
        initial_state = {"customer_id": request.user_id}
        runner = BTRunner(tree=tree, session_state=initial_state, session_id=session_id)
        _sessions[session_id] = runner

    # Run the tree with the user message
    result = runner.run(request.message)

    return ChatResponse(
        response=result.response or "I'm processing your request...",
        session_id=session_id,
        status=result.status,
        intent=intent,
    )


@app.get("/api/bt/trace/{session_id}")
async def get_bt_trace(session_id: str) -> dict:
    """Return the full BT execution trace for a session."""
    runner = _sessions.get(session_id)
    if runner is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    return {
        "session_id": session_id,
        "trace": runner.get_trace(),
        "summary": runner.get_trace_summary(),
        "execution_path": runner.get_execution_path(),
        "blackboard_state": runner.get_blackboard_state(),
    }


@app.get("/api/bt/trace/{session_id}/summary")
async def get_bt_trace_summary(session_id: str) -> dict:
    """Return just the trace summary for a session."""
    runner = _sessions.get(session_id)
    if runner is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    return {
        "session_id": session_id,
        "summary": runner.get_trace_summary(),
        "blackboard_state": runner.get_blackboard_state(),
    }


@app.get("/api/customers")
async def list_customers() -> dict:
    """Return all customers for the UI customer selector."""
    rows = await query_all("SELECT customer_id, name FROM customers")
    return {"customers": rows}


@app.get("/api/tables/{table_name}")
async def get_table_data(table_name: str) -> dict:
    """Get all rows from an allowed table for the data browser."""
    if table_name not in _ALLOWED_TABLES:
        raise HTTPException(status_code=400, detail=f"Table '{table_name}' is not allowed")
    rows = await query_all(f"SELECT * FROM {table_name}")  # noqa: S608
    return {"table": table_name, "rows": rows, "count": len(rows)}


@app.get("/api/sessions")
async def list_sessions() -> dict:
    """List all active sessions."""
    sessions = []
    for sid, runner in _sessions.items():
        bb = runner.get_blackboard_state()
        sessions.append({
            "session_id": sid,
            "status": runner.tree.root.status.value if runner.tree.root.status else "NONE",
            "workflow_status": bb.get("workflow_status", ""),
        })
    return {"sessions": sessions}


@app.post("/api/procedures/reload")
async def reload_procedures() -> dict:
    """Reload all YAML procedures (hot reload)."""
    _tree_manager.reload_all()
    return {
        "status": "reloaded",
        "procedures": _tree_manager.get_all_procedures(),
        "intents": _tree_manager.get_all_intents(),
    }


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {
        "status": "ok",
        "engine": "behaviour_tree",
        "workflows": _tree_manager.get_all_intents(),
        "procedures": _tree_manager.get_all_procedures(),
        "active_sessions": len(_sessions),
    }
