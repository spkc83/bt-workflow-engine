"""BT execution engine — ticks the behaviour tree and manages session state.

The BTRunner:
- Accepts a user message
- Writes it to the blackboard (a plain dict)
- Ticks the tree once (async — processes until RUNNING or completion)
- Collects LLM-generated responses from the blackboard
- Returns the response text and execution trace
- Supports session save/restore for pause & resume
- Loads cross-session customer memories from the database
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from bt_engine.audit import AuditCollector
from bt_engine.behaviour_tree import BehaviourTree, Status

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Result of a single BTRunner.run() call."""
    response: str
    status: str  # "RUNNING", "SUCCESS", "FAILURE"
    trace: list[dict] = field(default_factory=list)
    blackboard_state: dict = field(default_factory=dict)


class BTRunner:
    """Execution engine for a behaviour tree workflow.

    Each BTRunner is associated with a single session and tree instance.
    The tree persists across multiple run() calls (multi-turn conversation).
    """

    def __init__(
        self,
        tree: BehaviourTree,
        session_state: dict | None = None,
        session_id: str | None = None,
        procedure_id: str | None = None,
        intent: str | None = None,
    ):
        self.tree = tree
        self.session_id = session_id or str(uuid.uuid4())
        self.procedure_id = procedure_id
        self.intent = intent
        self._completed = False  # Completion guard
        self.audit = AuditCollector()

        # Initialize blackboard as a plain dict
        self._bb: dict = {}
        if session_state:
            self._bb.update(session_state)

        # Ensure standard keys exist
        self._bb.setdefault("user_message", "")
        self._bb.setdefault("agent_response", "")
        self._bb.setdefault("awaiting_input", False)
        self._bb.setdefault("audit_trail", [])
        self._bb.setdefault("_audit_trail", [])
        self._bb.setdefault("_tick_count", 0)

        # Restore conversation history from session state if present
        if "_conversation_history" in self._bb:
            self._bb.setdefault("conversation_history", self._bb.pop("_conversation_history"))
        else:
            self._bb.setdefault("conversation_history", [])

    async def run(self, user_message: str) -> RunResult:
        """Process a user message by ticking the tree.

        A single await tree.tick(bb) call processes the entire tree until
        a UserInputNode returns RUNNING or the tree completes. No tick
        loop needed — the async composites handle full traversal.
        """
        # Completion guard: don't re-tick completed trees
        if self._completed:
            logger.info("Tree already completed — returning completion message")
            return RunResult(
                response="This workflow has already been completed. Please start a new session if you need further assistance.",
                status="SUCCESS",
                trace=self.get_trace(),
                blackboard_state=self._get_public_state(),
            )

        # Reset per-turn state
        self._bb["user_message"] = user_message
        self._bb["agent_response"] = ""
        self._bb["awaiting_input"] = False

        # Append to conversation history
        history = self._bb.setdefault("conversation_history", [])
        history.append({
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now().isoformat(),
        })

        # Ensure case_id exists
        if "case_id" not in self._bb:
            self._bb["case_id"] = f"CASE-{self.session_id[:8]}"

        # Tick the tree — single async call handles full traversal
        self._bb["_tick_count"] = self._bb.get("_tick_count", 0) + 1
        self.audit.tick_count = self._bb["_tick_count"]

        status = await self.tree.tick(self._bb)

        # Check if tree completed
        if status in (Status.SUCCESS, Status.FAILURE):
            logger.info(f"Tree completed with status {status}")
            self._completed = True

        # Collect response
        response = self._bb.get("agent_response", "")
        response = _sanitize_response(response)

        # Determine status string
        status_str = status.value

        # Append assistant response to history
        if response:
            history.append({
                "role": "assistant",
                "content": response,
                "timestamp": datetime.now().isoformat(),
            })

        return RunResult(
            response=response,
            status=status_str,
            trace=self.get_trace(),
            blackboard_state=self._get_public_state(),
        )

    def _get_public_state(self) -> dict:
        """Return blackboard state without internal underscore keys."""
        return {k: v for k, v in self._bb.items() if not k.startswith("_")}

    def get_blackboard_state(self) -> dict:
        """Return current blackboard state for session persistence."""
        return self._get_public_state()

    def get_trace(self) -> list[dict]:
        """Return the full audit trace."""
        return self.audit.get_trace(self._bb)

    def get_trace_summary(self) -> dict:
        """Return a summary of the execution trace."""
        return self.audit.get_summary(self._bb)

    def get_execution_path(self) -> list[dict]:
        """Return just the active execution path."""
        return self.audit.get_execution_path(self._bb)

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    async def save_session(self):
        """Persist current session state to the database for pause & resume."""
        from database.db import execute

        bb = self._bb
        history = bb.get("conversation_history", [])

        # Build serializable state — exclude transient internal keys
        serializable_bb = {}
        for key, val in bb.items():
            if key in ("_audit_trail", "_tick_count"):
                continue  # Transient, don't persist
            if key == "_completed_steps" and isinstance(val, set):
                serializable_bb[key] = list(val)
            elif key.startswith("_"):
                continue
            else:
                serializable_bb[key] = val

        # Preserve _completed_steps for resume
        completed = bb.get("_completed_steps")
        if isinstance(completed, set):
            serializable_bb["_completed_steps"] = list(completed)

        now = datetime.now().isoformat()
        tree_status = "SUCCESS" if self._completed else "RUNNING"

        await execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, customer_id, procedure_id, intent, blackboard_state,
                conversation_history, tree_status, current_step, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                   (SELECT created_at FROM sessions WHERE session_id = ?), ?), ?)""",
            (
                self.session_id,
                bb.get("customer_id"),
                self.procedure_id,
                self.intent,
                json.dumps(serializable_bb, default=str),
                json.dumps(history, default=str),
                tree_status,
                None,
                self.session_id, now,
                now,
            ),
        )
        logger.info(f"Session {self.session_id} saved (status={tree_status})")

    @staticmethod
    async def load_session(session_id: str) -> dict | None:
        """Load a saved session from the database. Returns None if not found."""
        from database.db import query_one

        row = await query_one(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        if row is None:
            return None

        bb_state = json.loads(row["blackboard_state"]) if row["blackboard_state"] else {}
        history = json.loads(row["conversation_history"]) if row["conversation_history"] else []

        # Restore _completed_steps from list back to set
        if "_completed_steps" in bb_state and isinstance(bb_state["_completed_steps"], list):
            bb_state["_completed_steps"] = set(bb_state["_completed_steps"])

        # Stash conversation history for restoration
        bb_state["_conversation_history"] = history

        return {
            "session_id": row["session_id"],
            "customer_id": row["customer_id"],
            "procedure_id": row["procedure_id"],
            "intent": row["intent"],
            "blackboard_state": bb_state,
            "tree_status": row["tree_status"],
        }

    # ------------------------------------------------------------------
    # Customer memory
    # ------------------------------------------------------------------

    async def load_memories(self, customer_id: str):
        """Load cross-session memories for a customer into the blackboard."""
        from database.db import query_all

        rows = await query_all(
            """SELECT memory_id, memory_type, summary, data, created_at
               FROM customer_memories
               WHERE customer_id = ?
               ORDER BY created_at DESC LIMIT 10""",
            (customer_id,),
        )

        if rows:
            memories = []
            for row in rows:
                mem = dict(row)
                if mem.get("data"):
                    try:
                        mem["data"] = json.loads(mem["data"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                memories.append(mem)

            self._bb["customer_memories"] = memories
            logger.info(f"Loaded {len(memories)} memories for customer {customer_id}")


def _sanitize_response(response: str) -> str:
    if not response:
        return response

    cleaned_lines = []
    for line in response.splitlines():
        lowered = line.lower()
        if "\"tool_code\"" in lowered or "tool_code" in lowered:
            continue
        if "print(" in lowered and "_orders" in lowered:
            continue
        if "print(" in lowered and "_order" in lowered:
            continue
        if "print(" in lowered and "_alert" in lowered:
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    return cleaned
