"""Complaint handling workflow behaviour tree.

Maps the customer_service_complaint.yaml procedure into a deterministic
py_trees behaviour tree.
"""

from __future__ import annotations

import py_trees

from bt_engine.nodes import (
    ConditionNode,
    LLMClassifyNode,
    LLMExtractNode,
    LLMResponseNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)
from tools.crm_tools import lookup_order, search_orders, update_case_status
from tools.common_tools import add_case_note, escalate_to_supervisor


def create_complaint_tree() -> py_trees.trees.BehaviourTree:
    """Build the complete complaint handling behaviour tree."""
    root = py_trees.composites.Sequence("complaint_workflow", memory=True)
    root.add_children([
        _create_greet_and_collect(),
        _create_identify_issue(),
        _create_route_by_type(),
        LogNode("complaint_workflow_complete", message="Complaint workflow completed"),
    ])
    return py_trees.trees.BehaviourTree(root=root)


# ---------------------------------------------------------------------------
# Subtree: greet_and_collect
# ---------------------------------------------------------------------------

def _create_greet_and_collect() -> py_trees.behaviour.Behaviour:
    """Extract complaint details and any order clues from user message."""
    root = py_trees.composites.Sequence("greet_and_collect", memory=True)
    root.add_children([
        LLMExtractNode(
            "extract_complaint_details",
            prompt_template=(
                "Extract complaint details and any order identification from the customer message. "
                "Look for: complaint description, order ID, store/merchant name, item description, "
                "approximate amount, and time references."
            ),
            extract_keys=[
                "order_id", "merchant_name", "amount", "date",
                "item_description", "complaint_description",
            ],
        ),
        LogNode("complaint_info_collected", message="Complaint details extracted"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: identify_issue
# ---------------------------------------------------------------------------

def _create_identify_issue() -> py_trees.behaviour.Behaviour:
    """Classify the complaint into a category using LLM."""
    return LLMClassifyNode(
        "classify_complaint",
        prompt_template=(
            "Classify this customer complaint into one of the following categories based on "
            "what the customer is complaining about. Choose the most appropriate category."
        ),
        categories=["product_quality", "delivery", "service", "billing"],
        result_key="complaint_type",
    )


# ---------------------------------------------------------------------------
# Subtree: route_by_type
# ---------------------------------------------------------------------------

def _create_route_by_type() -> py_trees.behaviour.Behaviour:
    """Route to appropriate handling based on complaint type."""
    root = py_trees.composites.Selector("route_by_complaint_type", memory=False)

    # Path 1: product_quality or delivery — need order lookup first
    order_related = py_trees.composites.Sequence("order_related_complaint", memory=True)
    order_related.add_children([
        ConditionNode(
            "is_order_related",
            lambda bb: bb.get("complaint_type") in ("product_quality", "delivery"),
        ),
        _create_lookup_context(),
        _create_attempt_resolution(),
    ])

    # Path 2: service or billing — proceed directly to resolution
    service_related = py_trees.composites.Sequence("service_related_complaint", memory=True)
    service_related.add_children([
        ConditionNode(
            "is_service_related",
            lambda bb: bb.get("complaint_type") in ("service", "billing"),
        ),
        _create_attempt_resolution(),
    ])

    # Path 3: fallback — treat as general complaint
    fallback = _create_attempt_resolution()

    root.add_children([order_related, service_related, fallback])
    return root


# ---------------------------------------------------------------------------
# Subtree: lookup_context
# ---------------------------------------------------------------------------

def _create_lookup_context() -> py_trees.behaviour.Behaviour:
    """Look up order context for product/delivery complaints."""
    root = py_trees.composites.Selector("lookup_context", memory=False)

    # Try exact ID lookup
    exact = py_trees.composites.Sequence("exact_complaint_lookup", memory=True)
    exact.add_children([
        ConditionNode("has_complaint_order_id", lambda bb: bool(bb.get("order_id"))),
        ToolActionNode(
            "complaint_lookup_order",
            tool_func=lookup_order,
            arg_keys={"order_id": "order_id"},
            result_key="order_lookup_result",
        ),
    ])

    # Try search by description
    search = py_trees.composites.Sequence("search_complaint_order", memory=True)
    search.add_children([
        ConditionNode(
            "has_complaint_clues",
            lambda bb: any([bb.get("merchant_name"), bb.get("amount")]),
        ),
        ToolActionNode(
            "complaint_search_orders",
            tool_func=search_orders,
            arg_keys={"customer_id": "customer_id"},
        ),
    ])

    # Fallback: proceed without order data
    no_order = LogNode("no_order_context", message="Proceeding without order context")

    root.add_children([exact, search, no_order])
    return root


# ---------------------------------------------------------------------------
# Subtree: attempt_resolution
# ---------------------------------------------------------------------------

def _create_attempt_resolution() -> py_trees.behaviour.Behaviour:
    """Offer resolution based on complaint type, then handle response."""
    root = py_trees.composites.Sequence("attempt_resolution", memory=True)
    root.add_children([
        LLMResponseNode(
            "offer_resolution",
            prompt_template=(
                "Based on the complaint type ({complaint_type}), offer an appropriate resolution. "
                "For product_quality: offer replacement or full refund. "
                "For delivery: offer reship, carrier trace, or refund. "
                "For service: sincerely apologize, offer store credit as goodwill. "
                "For billing: explain or initiate correction. "
                "Be warm, solution-focused, and give the customer options."
            ),
        ),
        UserInputNode("wait_for_resolution_response"),
        _create_handle_resolution_response(),
    ])
    return root


def _create_handle_resolution_response() -> py_trees.behaviour.Behaviour:
    """Route based on customer satisfaction after resolution offer."""
    root = py_trees.composites.Selector("handle_resolution_response", memory=False)

    # Path 1: Customer accepts — document and close
    accept_path = py_trees.composites.Sequence("customer_accepts", memory=True)
    accept_path.add_children([
        ConditionNode(
            "check_accepts",
            # Simplified: if not escalation keywords, assume acceptance
            lambda bb: not any(
                kw in bb.get("user_message", "").lower()
                for kw in ["escalate", "supervisor", "manager", "not satisfied", "unacceptable"]
            ),
        ),
        _create_document_and_close(),
    ])

    # Path 2: Customer wants escalation
    escalate_path = py_trees.composites.Sequence("customer_escalates", memory=True)
    escalate_path.add_children([
        _create_escalate_complaint(),
    ])

    root.add_children([accept_path, escalate_path])
    return root


# ---------------------------------------------------------------------------
# Subtree: document_and_close
# ---------------------------------------------------------------------------

def _create_document_and_close() -> py_trees.behaviour.Behaviour:
    root = py_trees.composites.Sequence("document_and_close", memory=True)
    root.add_children([
        ToolActionNode(
            "add_complaint_note",
            tool_func=add_case_note,
            arg_keys={"case_id": "case_id"},
            fixed_args={"note": "Complaint resolved — customer accepted proposed resolution."},
        ),
        ToolActionNode(
            "close_complaint_case",
            tool_func=update_case_status,
            arg_keys={"case_id": "case_id"},
            fixed_args={"status": "resolved", "notes": "Complaint resolved to customer satisfaction."},
        ),
        LLMResponseNode(
            "complaint_closing_message",
            prompt_template=(
                "Wrap up the complaint interaction positively. Summarize what was done, "
                "confirm the resolution, apologize again for the experience, "
                "and ask if there's anything else. Be genuine and appreciative."
            ),
        ),
        LogNode("complaint_resolved", message="Complaint resolved and case closed"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: escalate_complaint
# ---------------------------------------------------------------------------

def _create_escalate_complaint() -> py_trees.behaviour.Behaviour:
    root = py_trees.composites.Sequence("escalate_complaint", memory=True)
    root.add_children([
        ToolActionNode(
            "escalate_complaint_case",
            tool_func=escalate_to_supervisor,
            arg_keys={"case_id": "case_id"},
            fixed_args={
                "reason": "Customer unsatisfied with proposed complaint resolution",
                "priority": "medium",
            },
            result_key="escalation_result",
        ),
        LLMResponseNode(
            "inform_complaint_escalation",
            prompt_template=(
                "The complaint has been escalated to a supervisor. Inform the customer "
                "respectfully: provide the case reference number and expected response "
                "time. Apologize that the initial resolution wasn't sufficient."
            ),
        ),
        ToolActionNode(
            "close_escalated_complaint",
            tool_func=update_case_status,
            arg_keys={"case_id": "case_id"},
            fixed_args={"status": "escalated", "notes": "Complaint escalated at customer request."},
        ),
        LogNode("complaint_escalated", message="Complaint escalated to supervisor"),
    ])
    return root
