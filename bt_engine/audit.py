"""Tick-level audit trail visitor for py_trees behaviour trees.

The AuditVisitor is attached to a BehaviourTree and logs every node
evaluation on every tick, producing a complete execution trace for
debugging and auditability.
"""

from __future__ import annotations

from datetime import datetime

import py_trees


def _bb_get(client, key, default=None):
    """Safe blackboard get with default for py_trees v2.4."""
    try:
        return client.get(key)
    except (KeyError, AttributeError):
        return default


class AuditVisitor(py_trees.visitors.VisitorBase):
    """Records every node's status on every tick.

    Attributes:
        trace: list of dicts, each recording a node evaluation event.
        tick_count: number of ticks observed.
    """

    def __init__(self):
        super().__init__(full=True)
        self.trace: list[dict] = []
        self.tick_count: int = 0

    def initialise(self):
        """Called once before each tick traversal."""
        self.tick_count += 1

    def run(self, behaviour: py_trees.behaviour.Behaviour):
        """Called for every node visited during a tick."""
        # Read bb_dict for a snapshot if available
        bb_snapshot = {}
        try:
            bb = py_trees.blackboard.Client(name="audit_reader")
            bb.register_key(key="bb_dict", access=py_trees.common.Access.READ)
            raw = _bb_get(bb, "bb_dict", {})
            # Only capture scalar/small values for the snapshot
            for k, v in raw.items():
                if isinstance(v, (str, int, float, bool, type(None))):
                    bb_snapshot[k] = v
                elif isinstance(v, dict) and len(str(v)) < 500:
                    bb_snapshot[k] = v
                else:
                    bb_snapshot[k] = f"<{type(v).__name__}>"
        except Exception:
            pass

        self.trace.append({
            "tick": self.tick_count,
            "timestamp": datetime.now().isoformat(),
            "node_name": behaviour.name,
            "node_type": type(behaviour).__name__,
            "status": behaviour.status.value if behaviour.status else "NONE",
            "blackboard_snapshot": bb_snapshot,
        })

    def get_trace(self) -> list[dict]:
        """Return the full trace."""
        return list(self.trace)

    def get_summary(self) -> dict:
        """Return a summary of the execution."""
        if not self.trace:
            return {"ticks": 0, "nodes_visited": 0, "unique_nodes": 0}

        unique_nodes = set()
        status_counts = {}
        for entry in self.trace:
            unique_nodes.add(entry["node_name"])
            s = entry["status"]
            status_counts[s] = status_counts.get(s, 0) + 1

        return {
            "ticks": self.tick_count,
            "nodes_visited": len(self.trace),
            "unique_nodes": len(unique_nodes),
            "status_counts": status_counts,
            "node_names": sorted(unique_nodes),
        }

    def get_execution_path(self) -> list[dict]:
        """Return only the nodes that returned SUCCESS or RUNNING (the active path)."""
        return [
            entry for entry in self.trace
            if entry["status"] in ("SUCCESS", "RUNNING")
        ]

    def clear(self):
        """Reset the trace for a new session."""
        self.trace.clear()
        self.tick_count = 0
