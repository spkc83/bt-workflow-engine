"""BT execution engine — ticks the behaviour tree and manages session state.

The BTRunner:
- Accepts a user message
- Writes it to the py_trees blackboard
- Ticks the tree until RUNNING (waiting for input) or SUCCESS/FAILURE
- Collects LLM-generated responses from the blackboard
- Returns the response text and execution trace
"""

from __future__ import annotations

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
    ):
        self.tree = tree
        self.session_id = session_id or str(uuid.uuid4())
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
        self._bb.set("conversation_history", [])

        # Setup the tree (calls initialise on all nodes)
        self.tree.setup()

    def run(self, user_message: str) -> RunResult:
        """Process a user message by ticking the tree.

        Args:
            user_message: The user's input text.

        Returns:
            RunResult with the agent response, tree status, and trace.
        """
        # Reset per-turn state
        self._bb.set("user_message", user_message)
        self._bb.set("agent_response", "")
        self._bb.set("awaiting_input", False)

        # Append to conversation history
        history = _bb_get(self._bb, "conversation_history", [])
        history.append({"role": "user", "content": user_message, "timestamp": datetime.now().isoformat()})
        self._bb.set("conversation_history", history)

        # Ensure case_id exists for tools that need it
        bb_dict = _bb_get(self._bb, "bb_dict", {})
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
