"""BT execution engine — ticks the behaviour tree and manages session state.

The BTRunner:
- Accepts a user message
- Writes it to the py_trees blackboard
- Ticks the tree until RUNNING (waiting for input) or SUCCESS/FAILURE
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

import py_trees

from bt_engine.audit import AuditVisitor

logger = logging.getLogger(__name__)


def _bb_get(client, key, default=None):
    """Safe blackboard get with default for py_trees v2.4."""
    try:
        return client.get(key)
    except (KeyError, AttributeError):
        return default


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
        tree: py_trees.trees.BehaviourTree,
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
        self.audit_visitor = AuditVisitor()
        self.tree.visitors.append(self.audit_visitor)

        # Set up the blackboard with a single bb_dict for all shared state
        self._bb = py_trees.blackboard.Client(name="runner")
        self._bb.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)
        self._bb.register_key(key="user_message", access=py_trees.common.Access.WRITE)
        self._bb.register_key(key="agent_response", access=py_trees.common.Access.WRITE)
        self._bb.register_key(key="awaiting_input", access=py_trees.common.Access.WRITE)
        self._bb.register_key(key="audit_trail", access=py_trees.common.Access.WRITE)
        self._bb.register_key(key="conversation_history", access=py_trees.common.Access.WRITE)

        # Initialize blackboard
        initial_state = session_state or {}
        self._bb.set("bb_dict", initial_state)
        self._bb.set("user_message", "")
        self._bb.set("agent_response", "")
        self._bb.set("awaiting_input", False)
        self._bb.set("audit_trail", [])
        self._bb.set("conversation_history", initial_state.get("_conversation_history", []))

        # Setup the tree (calls initialise on all nodes)
        self.tree.setup()

    def run(self, user_message: str) -> RunResult:
        """Process a user message by ticking the tree.

        Args:
            user_message: The user's input text.

        Returns:
            RunResult with the agent response, tree status, and trace.
        """
        # Completion guard: don't re-tick completed trees
        if self._completed:
            logger.info("Tree already completed — returning completion message")
            return RunResult(
                response="This workflow has already been completed. Please start a new session if you need further assistance.",
                status="SUCCESS",
                trace=self.audit_visitor.get_trace(),
                blackboard_state=dict(_bb_get(self._bb, "bb_dict", {})),
            )

        # Reset per-turn state
        self._bb.set("user_message", user_message)
        self._bb.set("agent_response", "")
        self._bb.set("awaiting_input", False)

        # Append to conversation history
        history = _bb_get(self._bb, "conversation_history", [])
        history.append({"role": "user", "content": user_message, "timestamp": datetime.now().isoformat()})
        self._bb.set("conversation_history", history)

        # Sync user_message into bb_dict so ConditionNode predicates can access it
        bb_dict = _bb_get(self._bb, "bb_dict", {})
        bb_dict["user_message"] = user_message
        if "case_id" not in bb_dict:
            bb_dict["case_id"] = f"CASE-{self.session_id[:8]}"
        self._bb.set("bb_dict", bb_dict)

        # Tick the tree
        max_ticks = 50  # Safety limit
        ticks = 0

        while ticks < max_ticks:
            self.tree.tick()
            ticks += 1

            # Check if a UserInputNode signaled that it needs input
            if _bb_get(self._bb, "awaiting_input", False):
                logger.info(f"Tree paused at tick {ticks} — awaiting user input")
                break

            # Check if tree completed
            root_status = self.tree.root.status
            if root_status in (py_trees.common.Status.SUCCESS, py_trees.common.Status.FAILURE):
                logger.info(f"Tree completed at tick {ticks} with status {root_status}")
                self._completed = True
                break

        # Collect response
        response = _bb_get(self._bb, "agent_response", "")

        # Determine status string
        if _bb_get(self._bb, "awaiting_input", False):
            status = "RUNNING"
        elif self.tree.root.status == py_trees.common.Status.SUCCESS:
            status = "SUCCESS"
        elif self.tree.root.status == py_trees.common.Status.FAILURE:
            status = "FAILURE"
        else:
            status = "RUNNING"

        # Append assistant response to history
        if response:
            history = _bb_get(self._bb, "conversation_history", [])
            history.append({"role": "assistant", "content": response, "timestamp": datetime.now().isoformat()})
            self._bb.set("conversation_history", history)

        return RunResult(
            response=response,
            status=status,
            trace=self.audit_visitor.get_trace(),
            blackboard_state=dict(_bb_get(self._bb, "bb_dict", {})),
        )

    def get_blackboard_state(self) -> dict:
        """Return current blackboard state for session persistence."""
        return dict(_bb_get(self._bb, "bb_dict", {}))

    def get_trace(self) -> list[dict]:
        """Return the full audit trace."""
        return self.audit_visitor.get_trace()

    def get_trace_summary(self) -> dict:
        """Return a summary of the execution trace."""
        return self.audit_visitor.get_summary()

    def get_execution_path(self) -> list[dict]:
        """Return just the active execution path."""
        return self.audit_visitor.get_execution_path()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    async def save_session(self):
        """Persist current session state to the database for pause & resume."""
        from database.db import execute

        bb_dict = _bb_get(self._bb, "bb_dict", {})
        history = _bb_get(self._bb, "conversation_history", [])

        # _completed_steps is a set — convert to list for JSON serialization
        serializable_bb = dict(bb_dict)
        completed = serializable_bb.get("_completed_steps")
        if isinstance(completed, set):
            serializable_bb["_completed_steps"] = list(completed)

        now = datetime.now().isoformat()

        # Determine tree status
        if self._completed:
            tree_status = "SUCCESS"
        elif _bb_get(self._bb, "awaiting_input", False):
            tree_status = "RUNNING"
        else:
            tree_status = "RUNNING"

        await execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, customer_id, procedure_id, intent, blackboard_state,
                conversation_history, tree_status, current_step, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                   (SELECT created_at FROM sessions WHERE session_id = ?), ?), ?)""",
            (
                self.session_id,
                bb_dict.get("customer_id"),
                self.procedure_id,
                self.intent,
                json.dumps(serializable_bb, default=str),
                json.dumps(history, default=str),
                tree_status,
                None,  # current_step — could be enhanced later
                self.session_id, now,  # COALESCE params for created_at
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

        # Deserialize JSON fields
        bb_state = json.loads(row["blackboard_state"]) if row["blackboard_state"] else {}
        history = json.loads(row["conversation_history"]) if row["conversation_history"] else []

        # Restore _completed_steps from list back to set
        if "_completed_steps" in bb_state and isinstance(bb_state["_completed_steps"], list):
            bb_state["_completed_steps"] = set(bb_state["_completed_steps"])

        # Stash conversation history in bb_state for restoration
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

            bb_dict = _bb_get(self._bb, "bb_dict", {})
            bb_dict["customer_memories"] = memories
            self._bb.set("bb_dict", bb_dict)
            logger.info(f"Loaded {len(memories)} memories for customer {customer_id}")
