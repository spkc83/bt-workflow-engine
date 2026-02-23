"""SQLite database connection, schema creation, and query helpers."""

from pathlib import Path

import aiosqlite

DB_PATH: Path = Path("data/workflow.db")

_SCHEMA = """
-- Customers
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    account_status TEXT DEFAULT 'active',
    loyalty_tier TEXT DEFAULT 'bronze',
    total_orders INTEGER DEFAULT 0,
    member_since TEXT NOT NULL
);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    merchant_name TEXT NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    order_date TEXT NOT NULL,
    delivery_date TEXT,
    days_since_delivery INTEGER DEFAULT 0,
    payment_method TEXT,
    shipping_address TEXT
);

-- Order Items
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    name TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL
);

-- Accounts (for fraud domain)
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email TEXT,
    status TEXT DEFAULT 'active'
);

-- Transactions
CREATE TABLE IF NOT EXISTS transactions (
    txn_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    amount REAL NOT NULL,
    merchant TEXT NOT NULL,
    date TEXT NOT NULL,
    location TEXT,
    is_flagged INTEGER DEFAULT 0,
    type TEXT DEFAULT 'purchase'
);

-- Fraud Alerts
CREATE TABLE IF NOT EXISTS fraud_alerts (
    alert_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    customer_name TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    description TEXT,
    amount_involved REAL,
    transactions_flagged INTEGER,
    status TEXT DEFAULT 'open'
);

-- Devices
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    type TEXT NOT NULL,
    os TEXT,
    first_seen TEXT,
    trusted INTEGER DEFAULT 0
);

-- Login History
CREATE TABLE IF NOT EXISTS login_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    device TEXT NOT NULL,
    device_id TEXT,
    location TEXT,
    timestamp TEXT NOT NULL,
    is_new INTEGER DEFAULT 0,
    ip TEXT
);

-- Risk Indicators
CREATE TABLE IF NOT EXISTS risk_indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    indicator TEXT NOT NULL
);

-- Cases
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    customer_id TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    notes TEXT
);

-- Case Notes
CREATE TABLE IF NOT EXISTS case_notes (
    note_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT DEFAULT 'system'
);

-- Escalations
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    priority TEXT NOT NULL,
    assigned_to TEXT,
    estimated_response TEXT,
    escalated_at TEXT NOT NULL,
    status TEXT DEFAULT 'escalated'
);

-- Refunds
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    status TEXT DEFAULT 'processed',
    reason TEXT,
    refund_method TEXT,
    estimated_days TEXT,
    processed_at TEXT NOT NULL
);

-- Knowledge Articles
CREATE TABLE IF NOT EXISTS knowledge_articles (
    article_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT,
    relevance_score REAL DEFAULT 0.0
);
"""


async def init_db() -> Path:
    """Create all tables if they don't exist. Returns the DB path."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
    return DB_PATH


def get_db():
    """Return an aiosqlite connection context manager."""
    return aiosqlite.connect(DB_PATH)


async def query_one(sql: str, params: tuple = ()) -> dict | None:
    """Fetch a single row as a dict, or None if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)


async def query_all(sql: str, params: tuple = ()) -> list[dict]:
    """Fetch all rows as a list of dicts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def execute(sql: str, params: tuple = ()) -> int:
    """Execute an insert/update and return lastrowid."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cursor:
            await db.commit()
            return cursor.lastrowid
