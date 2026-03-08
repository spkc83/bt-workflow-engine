"""Common tools shared across agent domains, backed by SQLite.

Adapted from the original project: ToolContext replaced with blackboard dict.
"""

import uuid
from datetime import datetime

from database.db import execute, query_all


async def escalate_to_supervisor(case_id: str, bb: dict, reason: str = "Customer requested escalation", priority: str = "medium") -> dict:
    """Escalate a case to a supervisor for further review."""
    now = datetime.now().isoformat()
    escalation_id = f"ESC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    estimated_response = "within 2 hours" if priority in ("high", "urgent") else "within 24 hours"

    await execute(
        """
        INSERT INTO escalations (escalation_id, case_id, reason, priority, assigned_to, estimated_response, escalated_at, status)
        VALUES (?, ?, ?, ?, 'Supervisor Martinez', ?, ?, 'escalated')
        """,
        (escalation_id, case_id, reason, priority, estimated_response, now),
    )

    result = {
        "escalation_id": escalation_id,
        "case_id": case_id,
        "reason": reason,
        "priority": priority,
        "assigned_to": "Supervisor Martinez",
        "estimated_response": estimated_response,
        "escalated_at": now,
        "status": "escalated",
    }

    bb["escalation_data"] = result
    bb["workflow_status"] = "escalated"

    return result


async def add_case_note(case_id: str, bb: dict, note: str = "Case note added") -> dict:
    """Add a note to a case file."""
    now = datetime.now().isoformat()
    note_id = f"NOTE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    await execute(
        "INSERT INTO case_notes (note_id, case_id, note, created_at, created_by) VALUES (?, ?, ?, ?, 'system')",
        (note_id, case_id, note, now),
    )

    result = {
        "note_id": note_id,
        "case_id": case_id,
        "note": note,
        "created_at": now,
        "created_by": "system",
    }

    existing_notes = bb.get("case_notes", [])
    existing_notes.append(result)
    bb["case_notes"] = existing_notes

    return result


async def get_knowledge_article(query: str, bb: dict) -> dict:
    """Search the knowledge base for relevant articles."""
    all_articles = await query_all(
        "SELECT article_id, title, summary, relevance_score FROM knowledge_articles ORDER BY relevance_score DESC"
    )

    query_lower = query.lower()
    matched = []
    for article in all_articles:
        if any(
            word in article["title"].lower() or word in article["summary"].lower()
            for word in query_lower.split()
        ):
            matched.append(article)

    if not matched:
        matched = all_articles[:2]

    return {"query": query, "articles": matched, "total_results": len(matched)}
