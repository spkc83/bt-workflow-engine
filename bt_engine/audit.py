"""Audit trail for behaviour tree execution.

Provides query methods over the structured audit trail stored in the
blackboard's _audit_trail list. Each node appends an entry when ticked.

For banking/regulatory compliance, the audit trail captures:
- Every node evaluation with timestamp
- Node type and status (SUCCESS/FAILURE/RUNNING)
- Tick count for ordering events within a run() call
"""

from __future__ import annotations

from datetime import datetime


class AuditCollector:
    """Reads and queries audit trail data from the blackboard.

    The audit trail is stored in bb["_audit_trail"] as a list of dicts,
    each with: tick, timestamp, node_name, node_type, status.

    This class wraps the raw list with query/summary methods used by
    the runner and API endpoints.
    """

    def __init__(self):
        self.tick_count: int = 0

    def get_trace(self, bb: dict) -> list[dict]:
        """Return the full audit trace."""
        return list(bb.get("_audit_trail", []))

    def get_summary(self, bb: dict) -> dict:
        """Return a summary of the execution."""
        trail = bb.get("_audit_trail", [])
        if not trail:
            return {"ticks": 0, "nodes_visited": 0, "unique_nodes": 0}

        unique_nodes = set()
        status_counts: dict[str, int] = {}
        for entry in trail:
            unique_nodes.add(entry["node_name"])
            s = entry["status"]
            status_counts[s] = status_counts.get(s, 0) + 1

        return {
            "ticks": self.tick_count,
            "nodes_visited": len(trail),
            "unique_nodes": len(unique_nodes),
            "status_counts": status_counts,
            "node_names": sorted(unique_nodes),
        }

    def get_execution_path(self, bb: dict) -> list[dict]:
        """Return only the nodes that returned SUCCESS or RUNNING."""
        return [
            entry for entry in bb.get("_audit_trail", [])
            if entry["status"] in ("SUCCESS", "RUNNING")
        ]

    def clear(self, bb: dict):
        """Reset the audit trail."""
        bb["_audit_trail"] = []
        self.tick_count = 0
