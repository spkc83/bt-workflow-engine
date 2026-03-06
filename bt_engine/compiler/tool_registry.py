"""Tool registry: maps YAML tool name strings to async Python functions.

Each tool registration includes the function, arg_keys (param -> bb key mapping),
and optional fixed_args. Signature introspection auto-generates arg_keys when
not explicitly provided.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolEntry:
    """A registered tool with its function and argument mapping."""
    func: Callable
    arg_keys: dict[str, str]  # {param_name: blackboard_key}
    fixed_args: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Registry mapping tool name strings to async functions with arg mappings."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        name: str,
        func: Callable,
        arg_keys: dict[str, str] | None = None,
        fixed_args: dict[str, Any] | None = None,
    ) -> None:
        """Register a tool function.

        If arg_keys is not provided, it is inferred from the function signature:
        each parameter (except 'bb') maps to a blackboard key of the same name.
        """
        if arg_keys is None:
            arg_keys = _infer_arg_keys(func)
        self._tools[name] = ToolEntry(
            func=func,
            arg_keys=arg_keys,
            fixed_args=fixed_args or {},
        )

    def get(self, name: str) -> ToolEntry | None:
        """Look up a registered tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


def _infer_arg_keys(func: Callable) -> dict[str, str]:
    """Introspect function signature to build arg_keys mapping.

    Every parameter except 'bb' (the blackboard dict) is mapped to a
    blackboard key of the same name. Parameters with defaults are skipped
    (they're treated as optional/fixed).
    """
    sig = inspect.signature(func)
    arg_keys = {}
    for name, param in sig.parameters.items():
        if name == "bb":
            continue
        # Only map required params (no default) to blackboard keys
        if param.default is inspect.Parameter.empty:
            arg_keys[name] = name
    return arg_keys


def create_default_registry() -> ToolRegistry:
    """Create a registry pre-populated with all project tools."""
    from tools.crm_tools import (
        get_customer_profile,
        issue_refund,
        issue_store_credit,
        lookup_order,
        search_orders,
        update_case_status,
    )
    from tools.common_tools import (
        add_case_note,
        escalate_to_supervisor,
        get_knowledge_article,
    )
    from tools.fraud_tools import (
        check_device_fingerprint,
        close_alert,
        flag_account,
        get_account_transactions,
        get_fraud_alert,
        submit_sar,
    )

    registry = ToolRegistry()

    # CRM tools
    registry.register("lookup_order", lookup_order)
    registry.register("search_orders", search_orders)
    registry.register("get_customer_profile", get_customer_profile)
    registry.register("issue_refund", issue_refund)
    registry.register("issue_store_credit", issue_store_credit)
    registry.register("update_case_status", update_case_status)

    # Common tools
    registry.register("escalate_to_supervisor", escalate_to_supervisor)
    registry.register("add_case_note", add_case_note)
    registry.register("get_knowledge_article", get_knowledge_article)

    # Fraud tools
    registry.register("get_fraud_alert", get_fraud_alert)
    registry.register("get_account_transactions", get_account_transactions)
    registry.register("check_device_fingerprint", check_device_fingerprint)
    registry.register("flag_account", flag_account)
    registry.register("submit_sar", submit_sar)
    registry.register("close_alert", close_alert)

    return registry
