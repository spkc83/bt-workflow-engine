"""Tests for tool functions with blackboard dict instead of ToolContext."""

import pytest

import database.db as db_module
from database.seed import seed_all

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    """Point DB_PATH to a temporary file for each test."""
    original = db_module.DB_PATH
    db_module.DB_PATH = tmp_path / "test.db"
    yield
    db_module.DB_PATH = original


@pytest.fixture(autouse=True)
async def _init_and_seed(_use_temp_db):
    """Init DB and seed data before each test (after temp DB is set)."""
    await db_module.init_db()
    await seed_all()


@pytest.mark.asyncio
async def test_lookup_order_found():
    from tools.crm_tools import lookup_order
    bb = {}
    result = await lookup_order("ORD-123", bb)
    assert result["found"] is True
    assert result["order_id"] == "ORD-123"
    assert result["merchant_name"] == "TechMart Electronics"
    assert "order_data" in bb
    assert bb["customer_id"] == "CUST-456"


@pytest.mark.asyncio
async def test_lookup_order_not_found():
    from tools.crm_tools import lookup_order
    bb = {}
    result = await lookup_order("ORD-FAKE", bb)
    assert result["found"] is False


@pytest.mark.asyncio
async def test_search_orders_by_merchant():
    from tools.crm_tools import search_orders
    bb = {}
    result = await search_orders("CUST-456", bb, merchant_name="TechMart")
    assert result["count"] == 1
    assert result["matches"][0]["merchant_name"] == "TechMart Electronics"
    assert "order_data" in bb


@pytest.mark.asyncio
async def test_search_orders_no_match():
    from tools.crm_tools import search_orders
    bb = {}
    result = await search_orders("CUST-789", bb, merchant_name="TechMart")
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_issue_refund():
    from tools.crm_tools import issue_refund, lookup_order
    bb = {}
    await lookup_order("ORD-123", bb)
    result = await issue_refund("ORD-123", "test refund", bb)
    assert result["status"] == "processed"
    assert result["amount"] == 79.99
    assert bb["workflow_status"] == "refund_processed"


@pytest.mark.asyncio
async def test_update_case_status_new():
    from tools.crm_tools import update_case_status
    bb = {}
    result = await update_case_status("CASE-TEST", "resolved", "Test notes", bb)
    assert result["status"] == "resolved"
    assert bb["workflow_status"] == "resolved"


@pytest.mark.asyncio
async def test_escalate_to_supervisor():
    from tools.common_tools import escalate_to_supervisor
    bb = {}
    result = await escalate_to_supervisor("CASE-TEST", "test reason", "high", bb)
    assert result["status"] == "escalated"
    assert result["priority"] == "high"
    assert bb["workflow_status"] == "escalated"


@pytest.mark.asyncio
async def test_add_case_note():
    from tools.common_tools import add_case_note
    from tools.crm_tools import update_case_status
    bb = {}
    await update_case_status("CASE-TEST", "open", "initial", bb)
    result = await add_case_note("CASE-TEST", "Test note content", bb)
    assert result["note"] == "Test note content"
    assert len(bb["case_notes"]) == 1


@pytest.mark.asyncio
async def test_get_fraud_alert():
    from tools.fraud_tools import get_fraud_alert
    bb = {}
    result = await get_fraud_alert("FA-001", bb)
    assert result["found"] is True
    assert result["severity"] == "high"
    assert bb["account_id"] == "ACCT-1001"


@pytest.mark.asyncio
async def test_get_fraud_alert_not_found():
    from tools.fraud_tools import get_fraud_alert
    bb = {}
    result = await get_fraud_alert("FA-999", bb)
    assert result["found"] is False


@pytest.mark.asyncio
async def test_get_account_transactions():
    from tools.fraud_tools import get_account_transactions
    bb = {}
    result = await get_account_transactions("ACCT-1001", 30, bb)
    assert result["summary"]["total_transactions"] >= 3
    assert result["summary"]["flagged_count"] >= 1


@pytest.mark.asyncio
async def test_check_device_fingerprint():
    from tools.fraud_tools import check_device_fingerprint
    bb = {}
    result = await check_device_fingerprint("ACCT-1001", bb)
    assert len(result["known_devices"]) >= 2
    assert len(result["risk_indicators"]) >= 1


@pytest.mark.asyncio
async def test_flag_account():
    from tools.fraud_tools import flag_account
    bb = {}
    result = await flag_account("ACCT-1001", "test fraud", "freeze", bb)
    assert result["action_taken"] == "freeze"
    assert bb["account_flagged"] is True
