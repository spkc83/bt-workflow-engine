"""CRM tools for customer service operations, backed by SQLite.

Adapted from the original project: ToolContext replaced with blackboard dict.
All functions accept a `bb` (blackboard) dict for state storage.
"""

from datetime import datetime, timedelta
from typing import Optional

from database.db import execute, query_all, query_one


async def lookup_order(order_id: str, bb: dict) -> dict:
    """Look up an order by its order ID and return order details."""
    order = await query_one(
        """
        SELECT o.*, c.name AS customer_name, c.email
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        WHERE o.order_id = ?
        """,
        (order_id,),
    )
    if order is None:
        return {"error": f"Order {order_id} not found", "found": False}

    items = await query_all(
        "SELECT name, sku, qty, price FROM order_items WHERE order_id = ?",
        (order_id,),
    )

    days_since_delivery = order["days_since_delivery"] or 0
    if order["delivery_date"]:
        try:
            delivery_dt = datetime.fromisoformat(order["delivery_date"])
            days_since_delivery = (datetime.now() - delivery_dt).days
        except (ValueError, TypeError):
            pass

    result = {
        "order_id": order["order_id"],
        "customer_name": order["customer_name"],
        "customer_id": order["customer_id"],
        "email": order["email"],
        "merchant_name": order["merchant_name"],
        "items": items,
        "total": order["total"],
        "status": order["status"],
        "order_date": order["order_date"],
        "delivery_date": order["delivery_date"],
        "days_since_delivery": days_since_delivery,
        "payment_method": order["payment_method"],
        "shipping_address": order["shipping_address"],
        "found": True,
    }

    bb["order_data"] = result
    bb["customer_id"] = order["customer_id"]

    return result


async def get_customer_profile(customer_id: str, bb: dict) -> dict:
    """Look up a customer profile by customer ID."""
    profile = await query_one(
        "SELECT * FROM customers WHERE customer_id = ?",
        (customer_id,),
    )
    if profile is None:
        return {"error": f"Customer {customer_id} not found", "found": False}

    result = {**profile, "found": True}
    bb["customer_data"] = profile
    return result


async def issue_refund(order_id: str, reason: str, bb: dict) -> dict:
    """Process a refund for a given order."""
    order_data = bb.get("order_data", {})
    amount = order_data.get("total", 0)
    payment_method = order_data.get("payment_method", "original_payment_method")

    refund_id = f"REF-{order_id.split('-')[1]}-{datetime.now().strftime('%H%M%S')}"
    processed_at = datetime.now().isoformat()

    await execute(
        """
        INSERT INTO refunds (refund_id, order_id, amount, currency, status, reason, refund_method, estimated_days, processed_at)
        VALUES (?, ?, ?, 'USD', 'processed', ?, ?, '5-7 business days', ?)
        """,
        (refund_id, order_id, amount, reason, payment_method, processed_at),
    )

    result = {
        "refund_id": refund_id,
        "order_id": order_id,
        "amount": amount,
        "currency": "USD",
        "status": "processed",
        "reason": reason,
        "refund_method": payment_method,
        "estimated_days": "5-7 business days",
        "processed_at": processed_at,
    }

    bb["refund_data"] = result
    bb["workflow_status"] = "refund_processed"

    return result


async def update_case_status(case_id: str, status: str, notes: str, bb: dict) -> dict:
    """Update the status of a customer service case."""
    now = datetime.now().isoformat()

    existing = await query_one("SELECT case_id FROM cases WHERE case_id = ?", (case_id,))
    if existing:
        await execute(
            "UPDATE cases SET status = ?, notes = ?, updated_at = ? WHERE case_id = ?",
            (status, notes, now, case_id),
        )
    else:
        await execute(
            "INSERT INTO cases (case_id, status, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, status, notes, now, now),
        )

    result = {
        "case_id": case_id,
        "status": status,
        "notes": notes,
        "updated_at": now,
        "updated_by": "system",
    }

    bb["case_status"] = status
    bb["workflow_status"] = status

    return result


async def search_orders(
    customer_id: str,
    bb: dict,
    merchant_name: Optional[str] = None,
    amount: Optional[float] = None,
    date: Optional[str] = None,
) -> dict:
    """Search for orders by customer ID and optional filters."""
    conditions = ["o.customer_id = ?"]
    params: list = [customer_id]

    if merchant_name:
        conditions.append("o.merchant_name LIKE ?")
        params.append(f"%{merchant_name}%")

    if amount is not None and amount > 0:
        lower = amount * 0.9
        upper = amount * 1.1
        conditions.append("o.total BETWEEN ? AND ?")
        params.extend([lower, upper])

    if date:
        try:
            target = datetime.fromisoformat(date)
            date_lower = (target - timedelta(days=3)).strftime("%Y-%m-%d")
            date_upper = (target + timedelta(days=3)).strftime("%Y-%m-%d")
            conditions.append("o.order_date BETWEEN ? AND ?")
            params.extend([date_lower, date_upper])
        except ValueError:
            conditions.append("o.order_date = ?")
            params.append(date)

    where_clause = " AND ".join(conditions)
    sql = f"""
        SELECT o.order_id, o.merchant_name, o.total, o.status, o.order_date,
               o.delivery_date, o.days_since_delivery, o.payment_method,
               o.shipping_address, o.customer_id
        FROM orders o
        WHERE {where_clause}
        ORDER BY o.order_date DESC
    """

    rows = await query_all(sql, tuple(params))

    if not rows:
        return {"matches": [], "count": 0, "message": "No orders found matching that description."}

    matches = []
    for row in rows:
        items = await query_all(
            "SELECT name, qty, price FROM order_items WHERE order_id = ?",
            (row["order_id"],),
        )
        items_summary = ", ".join(f"{it['name']} (x{it['qty']})" for it in items) if items else "no items"

        days_since_delivery = row["days_since_delivery"] or 0
        if row["delivery_date"]:
            try:
                delivery_dt = datetime.fromisoformat(row["delivery_date"])
                days_since_delivery = (datetime.now() - delivery_dt).days
            except (ValueError, TypeError):
                pass

        matches.append({
            "order_id": row["order_id"],
            "merchant_name": row["merchant_name"],
            "total": row["total"],
            "status": row["status"],
            "order_date": row["order_date"],
            "delivery_date": row["delivery_date"],
            "days_since_delivery": days_since_delivery,
            "items_summary": items_summary,
        })

    result = {"matches": matches, "count": len(matches)}

    if len(matches) == 1:
        match = matches[0]
        order = await query_one(
            """
            SELECT o.*, c.name AS customer_name, c.email
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_id = ?
            """,
            (match["order_id"],),
        )
        items = await query_all(
            "SELECT name, sku, qty, price FROM order_items WHERE order_id = ?",
            (match["order_id"],),
        )
        order_data = {
            "order_id": order["order_id"],
            "customer_name": order["customer_name"],
            "customer_id": order["customer_id"],
            "email": order["email"],
            "merchant_name": order["merchant_name"],
            "items": items,
            "total": order["total"],
            "status": order["status"],
            "order_date": order["order_date"],
            "delivery_date": order["delivery_date"],
            "days_since_delivery": match["days_since_delivery"],
            "payment_method": order["payment_method"],
            "shipping_address": order["shipping_address"],
            "found": True,
        }
        bb["order_data"] = order_data
        bb["customer_id"] = order["customer_id"]
        result["message"] = f"Found order {match['order_id']} from {match['merchant_name']}."
    else:
        result["message"] = f"Found {len(matches)} orders. Please ask the customer to confirm which one."

    return result
