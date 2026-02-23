"""Shiny for Python chat UI for the BT Workflow Engine.

Includes BT trace visualization panel showing execution path,
condition evaluations, and node statuses.
"""

import json

import pandas as pd
import httpx
from shiny import App, reactive, render, ui

API_BASE = "http://localhost:8000"

# --- Test scenario definitions ---

TEST_SCENARIOS = {
    "positive": [
        {
            "label": "Refund eligible order (natural)",
            "customer_id": "CUST-456",
            "message": "I bought some headphones from TechMart about a week ago and I'd like a refund",
            "description": "Natural language - search_orders finds ORD-123",
        },
        {
            "label": "Complaint about product (natural)",
            "customer_id": "CUST-456",
            "message": "I'm unhappy with the quality of the wireless headphones I got from TechMart",
            "description": "Natural language complaint flow",
        },
        {
            "label": "Fraud alert triage",
            "customer_id": None,
            "message": "I need to investigate fraud alert FA-001",
            "description": "High severity alert",
        },
    ],
    "negative": [
        {
            "label": "Refund outside window (natural)",
            "customer_id": "CUST-345",
            "message": "I want to return a laptop stand I bought from HomeOffice Supplies last month",
            "description": "Natural language - outside 30-day window",
        },
        {
            "label": "Order still processing",
            "customer_id": "CUST-012",
            "message": "I want a refund for order ORD-789",
            "description": "Status: processing, not delivered",
        },
        {
            "label": "General question (no procedure)",
            "customer_id": "guest",
            "message": "What is your return policy?",
            "description": "No matching procedure",
        },
    ],
    "multi_turn": [
        {
            "label": "Escalation path",
            "customer_id": "CUST-345",
            "message": "I want a refund for order ORD-999",
            "description": "Will be denied -> user can request escalation",
        },
        {
            "label": "Complaint -> resolution",
            "customer_id": "CUST-789",
            "message": "My order ORD-456 hasn't arrived yet and it's been 3 days",
            "description": "Shipped, not delivered -> delivery complaint",
        },
    ],
}


def _scenario_button(scenario, idx, category):
    btn_id = f"scenario_{category}_{idx}"
    return ui.tags.div(
        ui.input_action_button(
            btn_id,
            scenario["label"],
            class_="btn-outline-secondary btn-sm w-100 mb-1",
        ),
        ui.tags.small(scenario["description"], class_="text-muted d-block mb-2"),
    )


def _scenario_section(title, scenarios, category):
    buttons = [_scenario_button(s, i, category) for i, s in enumerate(scenarios)]
    return ui.tags.div(
        ui.tags.h6(title, class_="mt-2"),
        *buttons,
    )


app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.h4("Customer"),
        ui.output_ui("customer_selector"),
        ui.hr(),
        ui.h4("Session"),
        ui.output_text("session_id_display"),
        ui.input_action_button("new_session", "New Session", class_="btn-outline-primary w-100 mb-2"),
        ui.hr(),
        ui.h4("BT Status"),
        ui.output_ui("bt_status_display"),
        ui.hr(),
        ui.p(ui.tags.small("Backend: ", ui.tags.code(API_BASE))),
        width=300,
    ),
    ui.navset_tab(
        ui.nav_panel(
            "Chat",
            ui.chat_ui("chat"),
        ),
        ui.nav_panel(
            "BT Trace",
            ui.layout_columns(
                ui.card(
                    ui.card_header("Execution Path"),
                    ui.output_ui("bt_trace_path"),
                ),
                ui.card(
                    ui.card_header("Trace Summary"),
                    ui.output_ui("bt_trace_summary"),
                ),
                col_widths=[8, 4],
            ),
            ui.card(
                ui.card_header("Full Trace Log"),
                ui.output_data_frame("bt_trace_table"),
            ),
        ),
        ui.nav_panel(
            "Test Scenarios",
            ui.layout_columns(
                ui.card(
                    ui.card_header("Positive Scenarios (Happy Path)"),
                    _scenario_section("", TEST_SCENARIOS["positive"], "positive"),
                ),
                ui.card(
                    ui.card_header("Negative Scenarios (Edge Cases)"),
                    _scenario_section("", TEST_SCENARIOS["negative"], "negative"),
                ),
                ui.card(
                    ui.card_header("Multi-Turn Flow Scenarios"),
                    _scenario_section("", TEST_SCENARIOS["multi_turn"], "multi_turn"),
                ),
                col_widths=[4, 4, 4],
            ),
        ),
        ui.nav_panel(
            "Data Browser",
            ui.layout_columns(
                ui.card(
                    ui.card_header("Browse Tables"),
                    ui.input_select(
                        "table_select",
                        "Select table:",
                        choices=[
                            "customers", "orders", "order_items", "accounts",
                            "transactions", "fraud_alerts", "devices",
                            "login_history", "risk_indicators", "cases",
                            "case_notes", "escalations", "refunds",
                            "knowledge_articles",
                        ],
                    ),
                    ui.input_action_button("load_table", "Load", class_="btn-primary"),
                ),
                col_widths=[12],
            ),
            ui.output_data_frame("table_data"),
        ),
    ),
    title="BT Workflow Engine",
    fillable=True,
)


def server(input, output, session):
    session_id = reactive.value(None)
    selected_customer = reactive.value("CUST-456")
    table_rows = reactive.value([])
    bt_trace_data = reactive.value({})

    chat = ui.Chat("chat")

    # --- Customer selector ---

    @render.ui
    async def customer_selector():
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{API_BASE}/api/customers")
                resp.raise_for_status()
                data = resp.json()
                choices = {c["customer_id"]: f"{c['name']} ({c['customer_id']})" for c in data["customers"]}
                choices["guest"] = "Guest (no account)"
                return ui.input_select(
                    "customer_select",
                    "Select customer:",
                    choices=choices,
                    selected=selected_customer(),
                )
        except Exception:
            return ui.p("Could not load customers", class_="text-muted")

    # --- Chat ---

    def _get_user_id():
        cid = input.customer_select() if hasattr(input, "customer_select") else selected_customer()
        return cid if cid else "guest"

    @chat.on_user_submit
    async def on_user_message():
        user_msg = chat.user_input()
        if not user_msg:
            return

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                payload = {
                    "message": user_msg,
                    "user_id": _get_user_id(),
                }
                sid = session_id()
                if sid is not None:
                    payload["session_id"] = sid

                resp = await client.post(f"{API_BASE}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()

                session_id.set(data["session_id"])

                # Show intent classification for new sessions
                intent = data.get("intent")
                status = data.get("status", "")
                status_badge = f" [{status}]" if status else ""
                if intent:
                    await chat.append_message({
                        "role": "assistant",
                        "content": f"*Intent: {intent}{status_badge}*\n\n{data['response']}",
                    })
                else:
                    await chat.append_message({
                        "role": "assistant",
                        "content": f"{data['response']}",
                    })

                await _refresh_bt_trace()
        except httpx.HTTPStatusError as e:
            await chat.append_message({"role": "assistant", "content": f"Error: {e.response.status_code} - {e.response.text}"})
        except httpx.ConnectError:
            await chat.append_message({"role": "assistant", "content": "Cannot connect to backend. Is the FastAPI server running on port 8000?"})

    async def _refresh_bt_trace():
        sid = session_id()
        if sid is None:
            bt_trace_data.set({})
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{API_BASE}/api/bt/trace/{sid}")
                resp.raise_for_status()
                bt_trace_data.set(resp.json())
        except Exception:
            bt_trace_data.set({})

    # --- BT Status display ---

    @render.ui
    def bt_status_display():
        data = bt_trace_data()
        if not data:
            return ui.p("No active workflow", class_="text-muted small")

        summary = data.get("summary", {})
        bb = data.get("blackboard_state", {})

        items = []
        if summary.get("ticks"):
            items.append(ui.tags.div(
                ui.tags.strong("Ticks: ", class_="small"),
                ui.tags.span(str(summary["ticks"]), class_="small"),
            ))
        if summary.get("unique_nodes"):
            items.append(ui.tags.div(
                ui.tags.strong("Nodes visited: ", class_="small"),
                ui.tags.span(str(summary["unique_nodes"]), class_="small"),
            ))
        if bb.get("workflow_status"):
            items.append(ui.tags.div(
                ui.tags.strong("Status: ", class_="small"),
                ui.tags.span(bb["workflow_status"], class_="small"),
            ))

        return ui.tags.div(*items) if items else ui.p("No active workflow", class_="text-muted small")

    # --- BT Trace visualization ---

    @render.ui
    def bt_trace_path():
        data = bt_trace_data()
        if not data:
            return ui.p("No trace data. Send a message first.", class_="text-muted")

        path = data.get("execution_path", [])
        if not path:
            return ui.p("No execution path recorded.", class_="text-muted")

        # Show execution path as colored nodes
        items = []
        for entry in path:
            status = entry.get("status", "NONE")
            node_type = entry.get("node_type", "")
            node_name = entry.get("node_name", "")

            color_class = {
                "SUCCESS": "text-success",
                "RUNNING": "text-warning",
                "FAILURE": "text-danger",
            }.get(status, "text-muted")

            badge_class = {
                "SUCCESS": "bg-success",
                "RUNNING": "bg-warning",
                "FAILURE": "bg-danger",
            }.get(status, "bg-secondary")

            items.append(ui.tags.div(
                ui.tags.span(status, class_=f"badge {badge_class} me-2"),
                ui.tags.strong(node_name, class_=f"{color_class} me-2"),
                ui.tags.small(f"({node_type})", class_="text-muted"),
                class_="mb-1 py-1 border-bottom",
            ))

        return ui.tags.div(*items, style="max-height: 400px; overflow-y: auto;")

    @render.ui
    def bt_trace_summary():
        data = bt_trace_data()
        if not data:
            return ui.p("No trace data.", class_="text-muted")

        summary = data.get("summary", {})
        bb = data.get("blackboard_state", {})

        items = []
        for key, label in [
            ("ticks", "Total Ticks"),
            ("nodes_visited", "Nodes Visited"),
            ("unique_nodes", "Unique Nodes"),
        ]:
            if key in summary:
                items.append(ui.tags.div(
                    ui.tags.strong(f"{label}: "),
                    ui.tags.span(str(summary[key])),
                    class_="mb-1",
                ))

        # Status counts
        status_counts = summary.get("status_counts", {})
        if status_counts:
            items.append(ui.tags.hr())
            items.append(ui.tags.strong("Status Counts:"))
            for s, c in status_counts.items():
                items.append(ui.tags.div(
                    ui.tags.span(f"  {s}: {c}", class_="small"),
                ))

        # Key blackboard values
        if bb:
            items.append(ui.tags.hr())
            items.append(ui.tags.strong("Blackboard State:"))
            for k, v in bb.items():
                if isinstance(v, (str, int, float, bool)) and v:
                    items.append(ui.tags.div(
                        ui.tags.small(f"  {k}: {v}"),
                    ))

        return ui.tags.div(*items)

    @render.data_frame
    def bt_trace_table():
        data = bt_trace_data()
        if not data or not data.get("trace"):
            return pd.DataFrame({"message": ["No trace data. Send a message first."]})

        trace = data["trace"]
        rows = []
        for entry in trace:
            rows.append({
                "tick": entry.get("tick", ""),
                "node_name": entry.get("node_name", ""),
                "node_type": entry.get("node_type", ""),
                "status": entry.get("status", ""),
                "timestamp": entry.get("timestamp", "")[:19],
            })
        return pd.DataFrame(rows)

    # --- Session management ---

    @reactive.effect
    @reactive.event(input.new_session)
    async def _new_session():
        session_id.set(None)
        bt_trace_data.set({})
        await chat.clear_messages()
        await chat.append_message({"role": "assistant", "content": "New session started. Type a message to begin."})

    @render.text
    def session_id_display():
        sid = session_id()
        return f"ID: {sid[:12]}..." if sid else "No active session"

    # --- Test scenario handlers ---

    async def _run_scenario(scenario):
        cid = scenario["customer_id"] or "guest"
        selected_customer.set(cid)
        session_id.set(None)
        bt_trace_data.set({})
        await chat.clear_messages()
        await chat.append_message({
            "role": "assistant",
            "content": f"**Test scenario:** {scenario['label']}\n\n*{scenario['description']}*\n\nCustomer: {cid}",
        })
        await chat.append_message({"role": "user", "content": scenario["message"]})
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                payload = {
                    "message": scenario["message"],
                    "user_id": cid,
                }
                resp = await client.post(f"{API_BASE}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                session_id.set(data["session_id"])
                intent = data.get("intent", "")
                await chat.append_message({
                    "role": "assistant",
                    "content": f"*Intent: {intent}*\n\n{data['response']}",
                })
                await _refresh_bt_trace()
        except httpx.HTTPStatusError as e:
            await chat.append_message({"role": "assistant", "content": f"Error: {e.response.status_code} - {e.response.text}"})
        except httpx.ConnectError:
            await chat.append_message({"role": "assistant", "content": "Cannot connect to backend."})

    def _make_scenario_handler(scenario, category, idx):
        btn_id = f"scenario_{category}_{idx}"

        @reactive.effect
        @reactive.event(getattr(input, btn_id))
        async def _handler():
            await _run_scenario(scenario)

    for category, scenarios in TEST_SCENARIOS.items():
        for idx, scenario in enumerate(scenarios):
            _make_scenario_handler(scenario, category, idx)

    # --- Data browser ---

    @reactive.effect
    @reactive.event(input.load_table)
    async def _load_table():
        table_name = input.table_select()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{API_BASE}/api/tables/{table_name}")
                resp.raise_for_status()
                data = resp.json()
                table_rows.set(data["rows"])
        except Exception:
            table_rows.set([])

    @render.data_frame
    def table_data():
        rows = table_rows()
        if not rows:
            return pd.DataFrame({"message": ["No data. Click Load to fetch a table."]})
        return pd.DataFrame(rows)


app = App(app_ui, server)
