"""Seed all mock data into the SQLite database."""

from datetime import datetime, timedelta

import aiosqlite

import database.db as _db


async def seed_all() -> None:
    """Insert all mock data into the database. Idempotent via INSERT OR IGNORE."""
    async with aiosqlite.connect(_db.DB_PATH) as db:
        await _seed_customers(db)
        await _seed_orders(db)
        await _seed_order_items(db)
        await _seed_accounts(db)
        await _seed_transactions(db)
        await _seed_fraud_alerts(db)
        await _seed_devices(db)
        await _seed_login_history(db)
        await _seed_risk_indicators(db)
        await _seed_knowledge_articles(db)
        await db.commit()


async def _seed_customers(db: aiosqlite.Connection) -> None:
    customers = [
        ("CUST-456", "Jane Smith", "jane.smith@email.com", "+1-555-0123", "active", "gold", 15, "2022-03-15"),
        ("CUST-789", "Bob Johnson", "bob.j@email.com", "+1-555-0456", "active", "silver", 7, "2023-06-20"),
        ("CUST-012", "Alice Williams", "alice.w@email.com", "+1-555-0789", "active", "bronze", 3, "2024-01-10"),
        ("CUST-345", "Carol Davis", "carol.d@email.com", "+1-555-0345", "active", "bronze", 5, "2023-09-01"),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO customers (customer_id, name, email, phone, account_status, loyalty_tier, total_orders, member_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        customers,
    )


async def _seed_orders(db: aiosqlite.Connection) -> None:
    now = datetime.now()
    orders = [
        ("ORD-123", "CUST-456", "TechMart Electronics", 79.99, "delivered", (now - timedelta(days=10)).strftime("%Y-%m-%d"), (now - timedelta(days=5)).strftime("%Y-%m-%d"), 5, "credit_card_ending_4242", "123 Main St, Springfield, IL 62701"),
        ("ORD-456", "CUST-789", "SportZone", 155.97, "shipped", (now - timedelta(days=3)).strftime("%Y-%m-%d"), None, 0, "paypal", "456 Oak Ave, Portland, OR 97201"),
        ("ORD-789", "CUST-012", "GadgetWorld", 249.99, "processing", now.strftime("%Y-%m-%d"), None, 0, "credit_card_ending_1234", "789 Pine Rd, Austin, TX 78701"),
        ("ORD-999", "CUST-345", "HomeOffice Supplies", 49.99, "delivered", (now - timedelta(days=45)).strftime("%Y-%m-%d"), (now - timedelta(days=40)).strftime("%Y-%m-%d"), 40, "debit_card_ending_5678", "321 Elm St, Denver, CO 80201"),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO orders (order_id, customer_id, merchant_name, total, status, order_date, delivery_date, days_since_delivery, payment_method, shipping_address) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        orders,
    )


async def _seed_order_items(db: aiosqlite.Connection) -> None:
    async with db.execute("SELECT COUNT(*) FROM order_items") as cur:
        count = (await cur.fetchone())[0]
    if count > 0:
        return

    items = [
        ("ORD-123", "Wireless Headphones", "WH-100", 1, 79.99),
        ("ORD-456", "Running Shoes", "RS-200", 1, 129.99),
        ("ORD-456", "Athletic Socks", "AS-050", 2, 12.99),
        ("ORD-789", "Smart Watch", "SW-300", 1, 249.99),
        ("ORD-999", "Laptop Stand", "LS-400", 1, 49.99),
    ]
    await db.executemany(
        "INSERT INTO order_items (order_id, name, sku, qty, price) VALUES (?, ?, ?, ?, ?)",
        items,
    )


async def _seed_accounts(db: aiosqlite.Connection) -> None:
    accounts = [
        ("ACCT-1001", "Michael Chen", "michael.chen@email.com", "active"),
        ("ACCT-2002", "Sarah Parker", "sarah.parker@email.com", "active"),
        ("ACCT-3003", "David Lee", "david.lee@email.com", "active"),
        ("ACCT-4004", "Emily Brown", "emily.brown@email.com", "active"),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO accounts (account_id, customer_name, email, status) VALUES (?, ?, ?, ?)",
        accounts,
    )


async def _seed_transactions(db: aiosqlite.Connection) -> None:
    now = datetime.now()
    transactions = [
        ("TXN-5001", "ACCT-1001", 1500.00, "ElectroMart Online", (now - timedelta(hours=2)).isoformat(), "New York, NY", 1, "purchase"),
        ("TXN-5002", "ACCT-1001", 2000.00, "TechGear Pro", (now - timedelta(hours=1, minutes=45)).isoformat(), "Los Angeles, CA", 1, "purchase"),
        ("TXN-5003", "ACCT-1001", 1000.00, "Digital World", (now - timedelta(hours=1, minutes=30)).isoformat(), "Chicago, IL", 1, "purchase"),
        ("TXN-5004", "ACCT-1001", 55.00, "Coffee Shop", (now - timedelta(days=1)).isoformat(), "New York, NY", 0, "purchase"),
        ("TXN-5005", "ACCT-1001", 120.00, "Grocery Store", (now - timedelta(days=2)).isoformat(), "New York, NY", 0, "purchase"),
        ("TXN-6001", "ACCT-2002", 2899.99, "Premium Electronics", (now - timedelta(hours=1)).isoformat(), "Miami, FL", 1, "purchase"),
        ("TXN-6002", "ACCT-2002", 45.00, "Local Restaurant", (now - timedelta(days=1)).isoformat(), "Seattle, WA", 0, "purchase"),
        ("TXN-6003", "ACCT-2002", 89.99, "Streaming Service", (now - timedelta(days=5)).isoformat(), "Online", 0, "subscription"),
        ("TXN-7001", "ACCT-3003", 899.99, "TechHub Store", (now - timedelta(hours=6)).isoformat(), "Portland, OR", 1, "purchase"),
        ("TXN-7002", "ACCT-3003", 32.50, "Bookstore", (now - timedelta(days=2)).isoformat(), "Portland, OR", 0, "purchase"),
        ("TXN-7003", "ACCT-3003", 15.99, "Music Streaming", (now - timedelta(days=7)).isoformat(), "Online", 0, "subscription"),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO transactions (txn_id, account_id, amount, merchant, date, location, is_flagged, type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        transactions,
    )


async def _seed_fraud_alerts(db: aiosqlite.Connection) -> None:
    now = datetime.now()
    alerts = [
        ("FA-001", "card_not_present", "high", 87, "ACCT-1001", "Michael Chen", (now - timedelta(hours=2)).isoformat(), "Multiple high-value transactions from new device in different geographic locations within 30 minutes", 4500.00, 3, "open"),
        ("FA-002", "account_takeover", "high", 92, "ACCT-2002", "Sarah Parker", (now - timedelta(hours=1)).isoformat(), "Password changed followed by shipping address update and large purchase from unrecognized device", 2899.99, 1, "open"),
        ("FA-003", "unusual_activity", "medium", 55, "ACCT-3003", "David Lee", (now - timedelta(hours=6)).isoformat(), "Purchase pattern deviation - electronics purchase significantly above average order value", 899.99, 1, "open"),
        ("FA-004", "unusual_activity", "low", 25, "ACCT-4004", "Emily Brown", (now - timedelta(hours=12)).isoformat(), "Minor velocity check trigger - 3 small transactions in quick succession at same merchant", 45.97, 3, "open"),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO fraud_alerts (alert_id, type, severity, risk_score, account_id, customer_name, triggered_at, description, amount_involved, transactions_flagged, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        alerts,
    )


async def _seed_devices(db: aiosqlite.Connection) -> None:
    devices = [
        ("DEV-001", "ACCT-1001", "iPhone 15", "iOS 17", "2023-06-15", 1),
        ("DEV-002", "ACCT-1001", "MacBook Pro", "macOS 14", "2023-06-15", 1),
        ("DEV-003", "ACCT-2002", "Samsung Galaxy S24", "Android 14", "2024-01-10", 1),
        ("DEV-005", "ACCT-3003", "Pixel 8", "Android 14", "2024-03-01", 1),
        ("DEV-006", "ACCT-3003", "Chromebook", "ChromeOS", "2024-03-01", 1),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO devices (device_id, account_id, type, os, first_seen, trusted) VALUES (?, ?, ?, ?, ?, ?)",
        devices,
    )


async def _seed_login_history(db: aiosqlite.Connection) -> None:
    async with db.execute("SELECT COUNT(*) FROM login_history") as cur:
        count = (await cur.fetchone())[0]
    if count > 0:
        return

    now = datetime.now()
    logins = [
        ("ACCT-1001", "Unknown Android Device", "DEV-NEW-1", "Los Angeles, CA", (now - timedelta(hours=2)).isoformat(), 1, "192.168.1.100"),
        ("ACCT-1001", "iPhone 15", "DEV-001", "New York, NY", (now - timedelta(days=1)).isoformat(), 0, "10.0.0.1"),
        ("ACCT-2002", "Windows Desktop", "DEV-NEW-2", "Miami, FL", (now - timedelta(hours=1, minutes=30)).isoformat(), 1, "203.0.113.42"),
        ("ACCT-2002", "Samsung Galaxy S24", "DEV-003", "Seattle, WA", (now - timedelta(days=1)).isoformat(), 0, "10.0.0.2"),
        ("ACCT-3003", "Pixel 8", "DEV-005", "Portland, OR", (now - timedelta(hours=6)).isoformat(), 0, "10.0.0.3"),
    ]
    await db.executemany(
        "INSERT INTO login_history (account_id, device, device_id, location, timestamp, is_new, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        logins,
    )


async def _seed_risk_indicators(db: aiosqlite.Connection) -> None:
    async with db.execute("SELECT COUNT(*) FROM risk_indicators") as cur:
        count = (await cur.fetchone())[0]
    if count > 0:
        return

    indicators = [
        ("ACCT-1001", "new_device_login"),
        ("ACCT-1001", "geographic_anomaly"),
        ("ACCT-1001", "multiple_locations_short_timeframe"),
        ("ACCT-2002", "new_device_login"),
        ("ACCT-2002", "password_recently_changed"),
        ("ACCT-2002", "shipping_address_changed"),
        ("ACCT-2002", "geographic_anomaly"),
    ]
    await db.executemany(
        "INSERT INTO risk_indicators (account_id, indicator) VALUES (?, ?)",
        indicators,
    )


async def _seed_knowledge_articles(db: aiosqlite.Connection) -> None:
    articles = [
        ("KB-001", "Refund Policy Overview", "Our standard refund policy allows returns within 30 days of delivery. Items must be in original condition. Non-refundable categories include: personalized items, digital downloads, and clearance items.", None, 0.95),
        ("KB-002", "Escalation Procedures", "Cases should be escalated when: customer requests supervisor, issue exceeds agent authority, complaint involves safety concerns, or case has been open for more than 48 hours.", None, 0.85),
        ("KB-003", "Fraud Investigation Guidelines", "When investigating fraud alerts: gather all available evidence before making a determination, check device fingerprints and transaction patterns, document all findings, and follow the SAR submission timeline requirements.", None, 0.80),
        ("KB-004", "Store Credit Policy", "Store credit can be issued for orders outside the refund window or for non-refundable items at supervisor discretion. Store credit expires after 12 months and cannot be converted to cash.", None, 0.75),
    ]
    await db.executemany(
        "INSERT OR IGNORE INTO knowledge_articles (article_id, title, summary, content, relevance_score) VALUES (?, ?, ?, ?, ?)",
        articles,
    )
