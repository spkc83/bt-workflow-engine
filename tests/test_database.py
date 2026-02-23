"""Tests for the database layer."""

import pytest

import database.db as db_module

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    """Point DB_PATH to a temporary file for each test."""
    original = db_module.DB_PATH
    db_module.DB_PATH = tmp_path / "test.db"
    yield
    db_module.DB_PATH = original


@pytest.mark.asyncio
async def test_init_db_creates_tables():
    path = await db_module.init_db()
    assert path.exists()

    tables = await db_module.query_all(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    table_names = {t["name"] for t in tables}
    assert "customers" in table_names
    assert "orders" in table_names
    assert "fraud_alerts" in table_names


@pytest.mark.asyncio
async def test_init_db_idempotent():
    await db_module.init_db()
    await db_module.init_db()  # Should not raise


@pytest.mark.asyncio
async def test_query_one_returns_none_for_missing():
    await db_module.init_db()
    result = await db_module.query_one(
        "SELECT * FROM customers WHERE customer_id = ?", ("NONEXISTENT",)
    )
    assert result is None


@pytest.mark.asyncio
async def test_execute_and_query():
    await db_module.init_db()
    await db_module.execute(
        "INSERT INTO customers (customer_id, name, email, member_since) VALUES (?, ?, ?, ?)",
        ("TEST-1", "Test User", "test@test.com", "2024-01-01"),
    )
    result = await db_module.query_one(
        "SELECT * FROM customers WHERE customer_id = ?", ("TEST-1",)
    )
    assert result is not None
    assert result["name"] == "Test User"


@pytest.mark.asyncio
async def test_query_all_returns_list():
    await db_module.init_db()
    await db_module.execute(
        "INSERT INTO customers (customer_id, name, email, member_since) VALUES (?, ?, ?, ?)",
        ("TEST-1", "User 1", "u1@test.com", "2024-01-01"),
    )
    await db_module.execute(
        "INSERT INTO customers (customer_id, name, email, member_since) VALUES (?, ?, ?, ?)",
        ("TEST-2", "User 2", "u2@test.com", "2024-01-01"),
    )
    results = await db_module.query_all("SELECT * FROM customers")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_seed_all():
    from database.seed import seed_all
    await db_module.init_db()
    await seed_all()

    customers = await db_module.query_all("SELECT * FROM customers")
    assert len(customers) >= 4

    orders = await db_module.query_all("SELECT * FROM orders")
    assert len(orders) >= 4

    alerts = await db_module.query_all("SELECT * FROM fraud_alerts")
    assert len(alerts) >= 4


@pytest.mark.asyncio
async def test_seed_all_idempotent():
    from database.seed import seed_all
    await db_module.init_db()
    await seed_all()
    await seed_all()  # Should not raise or duplicate

    customers = await db_module.query_all("SELECT * FROM customers")
    assert len(customers) == 4  # INSERT OR IGNORE
