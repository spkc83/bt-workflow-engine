"""Refund workflow behaviour tree.

Maps the customer_service_refund.yaml procedure into a deterministic
behaviour tree with Python condition evaluation.
"""

from __future__ import annotations

from bt_engine.behaviour_tree import BehaviourTree, Selector, Sequence
from bt_engine.nodes import (
    ConditionNode,
    LLMExtractNode,
    LLMResponseNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)
from tools.crm_tools import issue_refund, lookup_order, search_orders, update_case_status
from tools.common_tools import escalate_to_supervisor


def create_refund_tree() -> BehaviourTree:
    """Build the complete refund workflow behaviour tree."""
    root = Sequence("refund_workflow", memory=True)
    root.add_children([
        _create_greet_and_collect(),
        _create_lookup_order(),
        _create_check_eligibility(),
        LogNode("refund_workflow_complete", message="Refund workflow completed"),
    ])
    return BehaviourTree(root=root)


# ---------------------------------------------------------------------------
# Subtree: greet_and_collect
# ---------------------------------------------------------------------------

def _create_greet_and_collect() -> Sequence:
    """Extract order clues from user message, or ask for them."""
    root = Sequence("greet_and_collect", memory=True)

    # Try to extract order info from the initial message
    extract = LLMExtractNode(
        "extract_order_clues",
        prompt_template=(
            "Extract any order identification details from the customer message. "
            "Look for: order ID (e.g. ORD-123), store/merchant name, item description, "
            "approximate dollar amount, or time references (last week, yesterday, etc)."
        ),
        extract_keys=["order_id", "merchant_name", "amount", "date", "item_description"],
    )

    # Check if we got enough info to proceed
    has_info = Selector("check_has_info", memory=False)

    got_order_id = ConditionNode(
        "has_order_id",
        lambda bb: bool(bb.get("order_id")),
    )
    got_clues = ConditionNode(
        "has_any_clue",
        lambda bb: any([bb.get("merchant_name"), bb.get("amount"), bb.get("item_description")]),
    )

    # If no info, ask and re-extract
    ask_sequence = Sequence("ask_for_info", memory=True)
    ask_sequence.add_children([
        LLMResponseNode(
            "ask_purchase_details",
            prompt_template=(
                "The customer wants a refund but hasn't provided enough details. "
                "Greet them warmly and ask them to describe their purchase — "
                "the store name, what they bought, and the approximate amount. "
                "Never ask for an order ID."
            ),
        ),
        UserInputNode("wait_for_details"),
        LLMExtractNode(
            "re_extract_order_clues",
            prompt_template=(
                "Extract order identification details from the customer's response. "
                "Look for: order ID, store/merchant name, item description, "
                "approximate dollar amount, or time references."
            ),
            extract_keys=["order_id", "merchant_name", "amount", "date", "item_description"],
        ),
    ])

    has_info.add_children([got_order_id, got_clues, ask_sequence])

    root.add_children([
        extract,
        has_info,
        LogNode("greet_and_collect_done", message="Order info collected"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: lookup_order
# ---------------------------------------------------------------------------

def _create_lookup_order() -> Selector:
    """Look up the order by ID or search by description."""
    root = Selector("lookup_order", memory=False)

    # Path 1: exact order ID lookup
    exact_lookup = Sequence("exact_id_lookup", memory=True)
    exact_lookup.add_children([
        ConditionNode("has_exact_order_id", lambda bb: bool(bb.get("order_id"))),
        ToolActionNode(
            "call_lookup_order",
            tool_func=lookup_order,
            arg_keys={"order_id": "order_id"},
            result_key="order_lookup_result",
        ),
        LLMResponseNode(
            "confirm_order_details",
            prompt_template=(
                "I found the customer's order. Briefly confirm the order details: "
                "order ID, merchant, items, total, and delivery status. "
                "Be concise and friendly."
            ),
        ),
    ])

    # Path 2: search by description
    search_lookup = Sequence("search_by_description", memory=True)
    search_lookup.add_children([
        ConditionNode(
            "has_search_clues",
            lambda bb: any([bb.get("merchant_name"), bb.get("amount")]),
        ),
        ToolActionNode(
            "call_search_orders",
            tool_func=search_orders,
            arg_keys={"customer_id": "customer_id"},
        ),
        LLMResponseNode(
            "confirm_search_result",
            prompt_template=(
                "I searched for the customer's order. Confirm the order found: "
                "order ID, merchant, items, total, and status. Be concise."
            ),
        ),
    ])

    # Path 3: order not found — ask again
    not_found = Sequence("order_not_found", memory=True)
    not_found.add_children([
        LLMResponseNode(
            "inform_not_found",
            prompt_template=(
                "I was unable to find an order matching the customer's description. "
                "Apologize and suggest they double-check the details or provide "
                "alternative identifiers. Be helpful and empathetic."
            ),
        ),
    ])

    root.add_children([exact_lookup, search_lookup, not_found])
    return root


# ---------------------------------------------------------------------------
# Subtree: check_eligibility (deterministic conditions)
# ---------------------------------------------------------------------------

def _create_check_eligibility() -> Selector:
    """Evaluate refund eligibility using Python conditions, not LLM."""
    root = Selector("check_eligibility", memory=False)

    # Path 1: Eligible — within 30 days + delivered/shipped
    eligible = Sequence("eligible_path", memory=True)
    eligible.add_children([
        ConditionNode(
            "within_30_days",
            lambda bb: bb.get("order_data", {}).get("days_since_delivery", 999) <= 30,
        ),
        ConditionNode(
            "delivered_or_shipped",
            lambda bb: bb.get("order_data", {}).get("status") in ("delivered", "shipped"),
        ),
        _create_process_refund(),
    ])

    # Path 2: Outside 30-day window
    outside_window = Sequence("outside_window_path", memory=True)
    outside_window.add_children([
        ConditionNode(
            "outside_30_days",
            lambda bb: bb.get("order_data", {}).get("days_since_delivery", 0) > 30,
        ),
        _create_deny_refund_window(),
    ])

    # Path 3: Order still processing — offer cancellation
    still_processing = Sequence("processing_path", memory=True)
    still_processing.add_children([
        ConditionNode(
            "status_is_processing",
            lambda bb: bb.get("order_data", {}).get("status") == "processing",
        ),
        _create_cancel_order(),
    ])

    root.add_children([eligible, outside_window, still_processing])
    return root


# ---------------------------------------------------------------------------
# Subtree: process_refund
# ---------------------------------------------------------------------------

def _create_process_refund() -> Sequence:
    root = Sequence("process_refund", memory=True)
    root.add_children([
        ToolActionNode(
            "call_issue_refund",
            tool_func=issue_refund,
            arg_keys={"order_id": "order_id"},
            fixed_args={"reason": "Customer requested refund"},
            result_key="refund_result",
        ),
        LLMResponseNode(
            "inform_refund_approved",
            prompt_template=(
                "The refund has been approved and processed. Inform the customer warmly: "
                "the refund will be credited to their original payment method within "
                "5-7 business days. Provide the refund reference number. "
                "Ask if there's anything else they need."
            ),
        ),
        _create_close_case("resolved"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: deny_refund_window
# ---------------------------------------------------------------------------

def _create_deny_refund_window() -> Sequence:
    root = Sequence("deny_refund_window", memory=True)
    root.add_children([
        LLMResponseNode(
            "inform_outside_window",
            prompt_template=(
                "The order is outside the 30-day return window and not eligible "
                "for a standard refund. Inform the customer empathetically. "
                "Offer two alternatives: (1) store credit for the full amount, "
                "or (2) escalation to a supervisor for further review. "
                "Ask which they'd prefer."
            ),
        ),
        UserInputNode("wait_for_customer_choice"),
        _create_handle_denial_response(),
    ])
    return root


def _create_handle_denial_response() -> Selector:
    """Handle customer's response to denial — escalate or close."""
    root = Selector("handle_denial_response", memory=False)

    # If customer wants escalation
    escalate_path = Sequence("customer_wants_escalation", memory=True)
    escalate_path.add_children([
        ConditionNode(
            "wants_escalation",
            lambda bb: True,  # Simplified: always offer escalation path
        ),
        _create_escalate_case(),
    ])

    root.add_children([escalate_path])
    return root


# ---------------------------------------------------------------------------
# Subtree: cancel_order
# ---------------------------------------------------------------------------

def _create_cancel_order() -> Sequence:
    root = Sequence("cancel_order", memory=True)
    root.add_children([
        LLMResponseNode(
            "inform_cancellation",
            prompt_template=(
                "The order is still processing and hasn't shipped yet. "
                "Inform the customer that you can cancel it right away and they'll "
                "receive a full refund to their original payment method within "
                "3-5 business days. Be positive and helpful."
            ),
        ),
        _create_close_case("cancelled"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: escalate_case
# ---------------------------------------------------------------------------

def _create_escalate_case() -> Sequence:
    root = Sequence("escalate_case", memory=True)
    root.add_children([
        ToolActionNode(
            "call_escalate",
            tool_func=escalate_to_supervisor,
            arg_keys={"case_id": "case_id"},
            fixed_args={"reason": "Customer requested escalation for refund outside window", "priority": "medium"},
            result_key="escalation_result",
        ),
        LLMResponseNode(
            "inform_escalation",
            prompt_template=(
                "The case has been escalated to a supervisor. Inform the customer "
                "reassuringly: provide the case reference number, expected response "
                "time, and assure them their case will get extra attention."
            ),
        ),
        _create_close_case("escalated"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: close_case
# ---------------------------------------------------------------------------

def _create_close_case(status: str) -> Sequence:
    root = Sequence(f"close_case_{status}", memory=True)
    root.add_children([
        ToolActionNode(
            f"update_case_{status}",
            tool_func=update_case_status,
            arg_keys={"case_id": "case_id"},
            fixed_args={"status": status, "notes": f"Case closed with status: {status}"},
        ),
        LogNode(f"case_closed_{status}", message=f"Case closed: {status}"),
    ])
    return root
