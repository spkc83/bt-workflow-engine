"""Fraud alert triage workflow behaviour tree.

Maps the fraud_ops_alert_triage.yaml procedure into a deterministic
behaviour tree with Python condition evaluation for risk scoring.
"""

from __future__ import annotations

from bt_engine.behaviour_tree import BehaviourTree, Selector, Sequence
from bt_engine.nodes import (
    ConditionNode,
    LLMClassifyNode,
    LLMExtractNode,
    LLMResponseNode,
    LogNode,
    ToolActionNode,
    UserInputNode,
)
from tools.fraud_tools import (
    check_device_fingerprint,
    close_alert,
    flag_account,
    get_account_transactions,
    get_fraud_alert,
    submit_sar,
)
from tools.common_tools import add_case_note, escalate_to_supervisor


def create_fraud_triage_tree() -> BehaviourTree:
    """Build the complete fraud alert triage behaviour tree."""
    root = Sequence("fraud_triage_workflow", memory=True)
    root.add_children([
        _create_receive_alert(),
        _create_review_and_route(),
        LogNode("fraud_triage_complete", message="Fraud triage workflow completed"),
    ])
    return BehaviourTree(root=root)


# ---------------------------------------------------------------------------
# Subtree: receive_alert
# ---------------------------------------------------------------------------

def _create_receive_alert() -> Selector:
    """Get the fraud alert by ID."""
    root = Selector("receive_alert", memory=False)

    # Path 1: alert ID provided — look it up
    has_alert = Sequence("has_alert_id", memory=True)
    has_alert.add_children([
        LLMExtractNode(
            "extract_alert_id",
            prompt_template=(
                "Extract the fraud alert ID from the analyst's message. "
                "Alert IDs look like FA-001, FA-002, etc."
            ),
            extract_keys=["alert_id"],
        ),
        ConditionNode("check_alert_id", lambda bb: bool(bb.get("alert_id"))),
        ToolActionNode(
            "fetch_fraud_alert",
            tool_func=get_fraud_alert,
            arg_keys={"alert_id": "alert_id"},
            result_key="alert_result",
        ),
        LogNode("alert_received", message="Fraud alert retrieved"),
    ])

    # Path 2: no alert ID — ask for it
    ask_alert = Sequence("ask_for_alert_id", memory=True)
    ask_alert.add_children([
        LLMResponseNode(
            "request_alert_id",
            prompt_template=(
                "The analyst hasn't provided a fraud alert ID. "
                "Ask them to provide the alert ID (e.g. FA-001) to begin the triage."
            ),
        ),
        UserInputNode("wait_for_alert_id"),
        LLMExtractNode(
            "re_extract_alert_id",
            prompt_template="Extract the fraud alert ID from the response.",
            extract_keys=["alert_id"],
        ),
        ToolActionNode(
            "fetch_fraud_alert_retry",
            tool_func=get_fraud_alert,
            arg_keys={"alert_id": "alert_id"},
            result_key="alert_result",
        ),
    ])

    root.add_children([has_alert, ask_alert])
    return root


# ---------------------------------------------------------------------------
# Subtree: review_and_route (deterministic severity routing)
# ---------------------------------------------------------------------------

def _create_review_and_route() -> Selector:
    """Route based on alert severity and risk score using Python conditions."""
    root = Selector("review_and_route", memory=False)

    # Path 1: High severity (risk_score >= 80 or severity == high)
    high_severity = Sequence("high_severity_path", memory=True)
    high_severity.add_children([
        ConditionNode(
            "is_high_severity",
            lambda bb: (
                bb.get("alert_data", {}).get("severity") == "high"
                or bb.get("alert_data", {}).get("risk_score", 0) >= 80
            ),
        ),
        LogNode("routing_high", message="High severity alert — full investigation"),
        _create_full_investigation(),
        _create_assess_risk(),
    ])

    # Path 2: Medium severity (risk_score 40-79)
    medium_severity = Sequence("medium_severity_path", memory=True)
    medium_severity.add_children([
        ConditionNode(
            "is_medium_severity",
            lambda bb: (
                bb.get("alert_data", {}).get("severity") == "medium"
                and 40 <= bb.get("alert_data", {}).get("risk_score", 0) < 80
            ),
        ),
        LogNode("routing_medium", message="Medium severity alert — standard investigation"),
        _create_full_investigation(),
        _create_assess_risk(),
    ])

    # Path 3: Low severity (risk_score < 40)
    low_severity = Sequence("low_severity_path", memory=True)
    low_severity.add_children([
        ConditionNode(
            "is_low_severity",
            lambda bb: bb.get("alert_data", {}).get("risk_score", 100) < 40,
        ),
        LogNode("routing_low", message="Low severity alert — quick review"),
        _create_assess_risk(),
    ])

    root.add_children([high_severity, medium_severity, low_severity])
    return root


# ---------------------------------------------------------------------------
# Subtree: full_investigation (transactions + device info)
# ---------------------------------------------------------------------------

def _create_full_investigation() -> Sequence:
    """Gather transaction evidence and device fingerprint data."""
    root = Sequence("full_investigation", memory=True)
    root.add_children([
        ToolActionNode(
            "get_transactions",
            tool_func=get_account_transactions,
            arg_keys={"account_id": "account_id"},
            fixed_args={"days": 30},
            result_key="transaction_result",
        ),
        LogNode("transactions_gathered", message="Transaction evidence collected"),
        ToolActionNode(
            "check_devices",
            tool_func=check_device_fingerprint,
            arg_keys={"account_id": "account_id"},
            result_key="device_result",
        ),
        LogNode("devices_checked", message="Device fingerprint data collected"),
    ])
    return root


# ---------------------------------------------------------------------------
# Subtree: assess_risk (LLM-assisted determination + deterministic actions)
# ---------------------------------------------------------------------------

def _create_assess_risk() -> Sequence:
    """Consolidate evidence and make risk determination."""
    root = Sequence("assess_risk", memory=True)
    root.add_children([
        LLMClassifyNode(
            "classify_risk",
            prompt_template=(
                "Based on the fraud alert data, transaction history, and device information, "
                "classify the overall risk determination. Consider: "
                "- Number and severity of risk indicators "
                "- Transaction patterns (flagged transactions, amounts, geography) "
                "- Device anomalies (new devices, impossible travel) "
                "Alert data: {alert_data} "
                "Transaction data: {transaction_data} "
                "Device data: {device_data}"
            ),
            categories=["fraud_confirmed", "fraud_suspected", "false_positive"],
            result_key="risk_determination",
        ),
        _create_act_on_determination(),
    ])
    return root


def _create_act_on_determination() -> Selector:
    """Take action based on risk determination."""
    root = Selector("act_on_determination", memory=False)

    # Path 1: Fraud confirmed — flag account
    confirmed = Sequence("fraud_confirmed_action", memory=True)
    confirmed.add_children([
        ConditionNode(
            "is_fraud_confirmed",
            lambda bb: bb.get("risk_determination") == "fraud_confirmed",
        ),
        ToolActionNode(
            "flag_fraud_account",
            tool_func=flag_account,
            arg_keys={"account_id": "account_id"},
            fixed_args={"reason": "Fraud confirmed via alert triage", "action": "freeze"},
            result_key="flag_result",
        ),
        # Check SAR threshold (>= $5000)
        Selector("sar_check", memory=False, children=[
            Sequence("file_sar", memory=True, children=[
                ConditionNode(
                    "meets_sar_threshold",
                    lambda bb: bb.get("alert_data", {}).get("amount_involved", 0) >= 5000,
                ),
                ToolActionNode(
                    "submit_sar_report",
                    tool_func=submit_sar,
                    arg_keys={"account_id": "account_id", "alert_id": "alert_id"},
                    fixed_args={"findings": "Fraud confirmed — SAR threshold met"},
                    result_key="sar_result",
                ),
            ]),
            LogNode("sar_not_needed", message="SAR threshold not met — no filing required"),
        ]),
        LLMResponseNode(
            "report_fraud_confirmed",
            prompt_template=(
                "Report findings to the analyst: fraud has been confirmed. "
                "Summarize the key fraud indicators found, actions taken "
                "(account flagged, SAR filed if applicable), and next steps."
            ),
        ),
        _create_document_and_close("confirmed_fraud"),
    ])

    # Path 2: Fraud suspected — escalate to senior
    suspected = Sequence("fraud_suspected_action", memory=True)
    suspected.add_children([
        ConditionNode(
            "is_fraud_suspected",
            lambda bb: bb.get("risk_determination") == "fraud_suspected",
        ),
        ToolActionNode(
            "escalate_to_senior",
            tool_func=escalate_to_supervisor,
            arg_keys={"case_id": "alert_id"},
            fixed_args={
                "reason": "Ambiguous fraud indicators — senior analyst review required",
                "priority": "high",
            },
            result_key="escalation_result",
        ),
        LLMResponseNode(
            "report_fraud_suspected",
            prompt_template=(
                "Report to the analyst: the evidence is inconclusive and has been "
                "escalated to a senior analyst. Summarize the indicators found and "
                "the reasons for uncertainty. Provide the escalation reference."
            ),
        ),
        _create_document_and_close("escalated_pending_senior_review"),
    ])

    # Path 3: False positive — clear alert
    false_positive = Sequence("false_positive_action", memory=True)
    false_positive.add_children([
        ConditionNode(
            "is_false_positive",
            lambda bb: bb.get("risk_determination") == "false_positive",
        ),
        ToolActionNode(
            "clear_alert",
            tool_func=close_alert,
            arg_keys={"alert_id": "alert_id"},
            fixed_args={"resolution": "false_positive"},
        ),
        LLMResponseNode(
            "report_false_positive",
            prompt_template=(
                "Report to the analyst: this alert has been cleared as a false positive. "
                "Explain why the activity was determined to be legitimate. "
                "Reference specific evidence that contradicts the fraud hypothesis."
            ),
        ),
        _create_document_and_close("false_positive"),
    ])

    root.add_children([confirmed, suspected, false_positive])
    return root


# ---------------------------------------------------------------------------
# Subtree: document_and_close
# ---------------------------------------------------------------------------

def _create_document_and_close(resolution: str) -> Sequence:
    root = Sequence(f"document_and_close_{resolution}", memory=True)
    root.add_children([
        ToolActionNode(
            f"add_triage_note_{resolution}",
            tool_func=add_case_note,
            arg_keys={"case_id": "alert_id"},
            fixed_args={"note": f"Triage complete. Resolution: {resolution}"},
        ),
        ToolActionNode(
            f"close_alert_{resolution}",
            tool_func=close_alert,
            arg_keys={"alert_id": "alert_id"},
            fixed_args={"resolution": resolution},
        ),
        LogNode(f"triage_closed_{resolution}", message=f"Triage closed: {resolution}"),
    ])
    return root
