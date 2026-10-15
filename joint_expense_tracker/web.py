from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .charts import stacked_columns
from .config import (
    CATEGORIZED_STATUSES,
    DATA_DIR,
    DEFAULT_CHASE_KEYWORDS,
    DEFAULT_CHASE_SENDERS,
    STATUSES,
    status_label,
)
from .db import categories, delete_category, get_db, init_db, rows_to_dicts, save_category, set_setting, utc_now
from .importer import diagnose_messages, import_messages
from .rules import normalize_status
from .services import (
    classify_transaction,
    create_manual_transaction,
    dashboard_data,
    export_month,
    spending_data,
    update_transaction,
)

BASE_DIR = Path(__file__).resolve().parent
TRANSACTIONS_PAGE_LIMIT = 500


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Joint Expense Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["status_label"] = status_label
templates.env.filters["money"] = lambda value: f"${float(value or 0):,.2f}"
templates.env.globals["categorized_statuses"] = sorted(CATEGORIZED_STATUSES)


def render(request: Request, name: str, context: dict[str, Any]):
    return templates.TemplateResponse(request, name, context)


def redirect(path: str) -> RedirectResponse:
    # Only follow same-site paths from form fields like return_to.
    return RedirectResponse(path if path.startswith("/") and not path.startswith("//") else "/", status_code=303)


def optional_float(value: str | float | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid number") from exc


@app.get("/")
def dashboard(request: Request, month: str | None = None):
    with get_db() as conn:
        data = dashboard_data(conn, month)
        data["categories"] = categories(conn)
    return render(request, "dashboard.html", data)


@app.get("/spending")
def spending(request: Request, month: str | None = None, category: str | None = None):
    with get_db() as conn:
        data = spending_data(conn, month, category or None)
    data["chart"] = stacked_columns(data["trend"], ["joint", "personal"])
    return render(request, "spending.html", data)


@app.get("/analytics")
def analytics():
    return redirect("/spending")


@app.post("/import")
def import_now():
    import_messages()
    return redirect("/")


@app.get("/diagnostics")
def diagnostics(request: Request):
    return render(request, "diagnostics.html", {"lines": diagnose_messages()})


@app.get("/export")
def export(month: str):
    transaction_path, _ = export_month(month)
    return FileResponse(transaction_path, filename=transaction_path.name, media_type="text/csv")


@app.get("/transactions")
def transactions(
    request: Request,
    month: str | None = None,
    status: str | None = None,
    account: str | None = None,
    q: str | None = None,
    category: str | None = None,
):
    clauses = []
    params: list[str] = []
    if month:
        clauses.append("month = ?")
        params.append(month)
    if status:
        clauses.append("status = ?")
        params.append(status)
    else:
        clauses.append("status != 'Ignored'")
    if account:
        clauses.append("(card_last4 = ? OR account_name = ?)")
        params.extend([account, account])
    if category:
        clauses.append("subcategory = ? COLLATE NOCASE")
        params.append(category)
    if q:
        clauses.append("(merchant LIKE ? OR raw_text LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where = "WHERE " + " AND ".join(clauses)
    with get_db() as conn:
        rows = rows_to_dicts(
            conn.execute(
                f"SELECT * FROM transactions {where} ORDER BY transaction_datetime DESC, id DESC LIMIT ?",
                [*params, TRANSACTIONS_PAGE_LIMIT],
            ).fetchall()
        )
        total = conn.execute(f"SELECT COUNT(*) FROM transactions {where}", params).fetchone()[0]
        all_months = [
            row["month"] for row in conn.execute("SELECT DISTINCT month FROM transactions ORDER BY month DESC")
        ]
        accounts = rows_to_dicts(
            conn.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY account_name").fetchall()
        )
        category_names = categories(conn)
    filters = {
        "month": month or "",
        "status": status or "",
        "account": account or "",
        "q": q or "",
        "category": category or "",
    }
    return render(
        request,
        "transactions.html",
        {
            "transactions": rows,
            "total_matching": total,
            "months": all_months,
            "accounts": accounts,
            "statuses": STATUSES,
            "categories": category_names,
            "today": date.today().isoformat(),
            "filters": filters,
            "return_to": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
        },
    )


@app.post("/transactions")
def create_transaction(
    transaction_date: str = Form(...),
    merchant: str = Form(...),
    amount: str = Form(...),
    card_last4: str = Form(""),
    status: str = Form("Review"),
    joint_amount: str = Form(""),
    note: str = Form(""),
    category: str = Form(""),
):
    try:
        date.fromisoformat(transaction_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="transaction_date must be a valid date") from exc
    if not merchant.strip():
        raise HTTPException(status_code=422, detail="merchant is required")
    parsed_amount = optional_float(amount, "amount")
    if parsed_amount is None or parsed_amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be greater than zero")
    with get_db() as conn:
        create_manual_transaction(
            conn,
            transaction_date,
            merchant,
            parsed_amount,
            card_last4,
            status,
            optional_float(joint_amount, "joint_amount"),
            note,
            category,
        )
    return redirect("/transactions")


@app.post("/transactions/{transaction_id}/classify")
def classify(
    transaction_id: int,
    status: str = Form(...),
    joint_amount: str | None = Form(None),
    tip_percent: str | None = Form(None),
    tip_amount: str | None = Form(None),
    category: str | None = Form(None),
    return_to: str = Form("/transactions"),
):
    with get_db() as conn:
        classify_transaction(
            conn,
            transaction_id,
            status,
            optional_float(joint_amount, "joint_amount"),
            optional_float(tip_percent, "tip_percent"),
            optional_float(tip_amount, "tip_amount"),
            category,
        )
    return redirect(return_to)


@app.get("/transactions/{transaction_id}/edit")
def edit_transaction(request: Request, transaction_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found")
        category_names = categories(conn)
    return render(
        request,
        "transaction_edit.html",
        {"transaction": dict(row), "statuses": STATUSES, "categories": category_names},
    )


@app.post("/transactions/{transaction_id}/edit")
def save_transaction(
    transaction_id: int,
    merchant: str = Form(...),
    account_name: str = Form(""),
    card_last4: str = Form(""),
    status: str = Form(...),
    joint_amount: float = Form(0),
    tip_percent: str = Form(""),
    tip_amount: str = Form(""),
    note: str = Form(""),
    category: str = Form(""),
):
    with get_db() as conn:
        update_transaction(
            conn,
            transaction_id,
            {
                "merchant": merchant,
                "account_name": account_name,
                "card_last4": card_last4,
                "status": status,
                "joint_amount": joint_amount,
                "tip_percent": tip_percent,
                "tip_amount": tip_amount,
                "note": note,
                "category": category,
            },
        )
    return redirect("/transactions")


@app.get("/accounts")
def accounts(request: Request):
    with get_db() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY active DESC, account_name").fetchall())
    return render(request, "accounts.html", {"accounts": rows})


@app.post("/accounts")
def save_account(card_last4: str = Form(...), account_name: str = Form(...), active: int = Form(1)):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO accounts (card_last4, account_name, active) VALUES (?, ?, ?)
            ON CONFLICT(card_last4) DO UPDATE SET account_name = excluded.account_name, active = excluded.active
            """,
            (card_last4.strip(), account_name.strip(), active),
        )
        conn.execute(
            "UPDATE transactions SET account_name = ? WHERE card_last4 = ?",
            (account_name.strip(), card_last4.strip()),
        )
    return redirect("/accounts")


@app.get("/rules")
def rules(request: Request):
    with get_db() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM rules ORDER BY active DESC, priority ASC, id ASC").fetchall())
        category_names = categories(conn)
    return render(request, "rules.html", {"rules": rows, "statuses": STATUSES, "categories": category_names})


@app.post("/rules")
def save_rule(
    match_text: str = Form(...),
    match_field: str = Form("merchant_or_raw"),
    default_status: str = Form(...),
    category: str = Form(""),
    default_joint_percent: str = Form(""),
    default_joint_amount: str = Form(""),
    priority: int = Form(100),
    active: int = Form(1),
):
    default_status = normalize_status(default_status)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO rules (
                match_text, match_field, default_status, default_joint_percent,
                default_joint_amount, priority, active, default_subcategory
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_text.strip(),
                match_field,
                default_status,
                optional_float(default_joint_percent, "default_joint_percent"),
                optional_float(default_joint_amount, "default_joint_amount"),
                priority,
                active,
                save_category(conn, default_status, category),
            ),
        )
    return redirect("/rules")


@app.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return redirect("/rules")


@app.post("/rules/{rule_id}/category")
def set_rule_category(rule_id: int, category: str = Form("")):
    with get_db() as conn:
        rule = conn.execute("SELECT default_status FROM rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        conn.execute(
            "UPDATE rules SET default_subcategory = ? WHERE id = ?",
            (save_category(conn, rule["default_status"], category), rule_id),
        )
    return redirect("/rules")


@app.get("/settings")
def settings(request: Request):
    with get_db() as conn:
        settings_map = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM settings")}
        usage = {
            row["name"]: row["uses"]
            for row in conn.execute(
                """
                SELECT c.name, COUNT(t.id) AS uses
                FROM personal_subcategories c
                LEFT JOIN transactions t ON t.subcategory = c.name COLLATE NOCASE
                GROUP BY c.name
                """
            )
        }
        category_names = categories(conn)
    return render(
        request,
        "settings.html",
        {
            "settings": settings_map,
            "data_dir": DATA_DIR,
            "default_senders": ", ".join(DEFAULT_CHASE_SENDERS),
            "default_keywords": ", ".join(DEFAULT_CHASE_KEYWORDS),
            "categories": [{"name": name, "uses": usage.get(name, 0)} for name in category_names],
        },
    )


@app.post("/settings")
def save_settings(
    messages_db_path: str = Form(...),
    chase_sender_filters: str = Form(...),
    chase_keyword_filters: str = Form(...),
):
    with get_db() as conn:
        set_setting(conn, "messages_db_path", messages_db_path)
        set_setting(conn, "chase_sender_filters", chase_sender_filters)
        set_setting(conn, "chase_keyword_filters", chase_keyword_filters)
    return redirect("/settings")


@app.post("/categories")
def add_category(name: str = Form(...)):
    with get_db() as conn:
        save_category(conn, "Personal", name)
    return redirect("/settings")


@app.post("/categories/delete")
def remove_category(name: str = Form(...)):
    with get_db() as conn:
        delete_category(conn, name)
    return redirect("/settings")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "true", "time": utc_now()}
