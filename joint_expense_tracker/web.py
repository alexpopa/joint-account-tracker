from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import DATA_DIR, DEFAULT_CHASE_KEYWORDS, DEFAULT_CHASE_SENDERS, STATUSES
from .db import get_db, init_db, rows_to_dicts, set_setting, utc_now
from .importer import diagnose_messages, import_messages
from .services import classify_transaction, dashboard_data, export_month, update_transaction

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Joint Expense Tracker")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.on_event("startup")
def startup() -> None:
    init_db()


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


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
def dashboard(request: Request, month: Optional[str] = None):
    with get_db() as conn:
        data = dashboard_data(conn, month)
    return templates.TemplateResponse("dashboard.html", {"request": request, **data})


@app.post("/import")
def import_now():
    import_messages()
    return redirect("/")


@app.get("/diagnostics")
def diagnostics(request: Request):
    return templates.TemplateResponse("diagnostics.html", {"request": request, "lines": diagnose_messages()})


@app.get("/export")
def export(month: str):
    transaction_path, _ = export_month(month)
    return FileResponse(transaction_path, filename=transaction_path.name, media_type="text/csv")


@app.get("/transactions")
def transactions(
    request: Request,
    month: Optional[str] = None,
    status: Optional[str] = None,
    account: Optional[str] = None,
    q: Optional[str] = None,
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
    if q:
        clauses.append("(merchant LIKE ? OR raw_text LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with get_db() as conn:
        rows = rows_to_dicts(
            conn.execute(
                f"SELECT * FROM transactions {where} ORDER BY transaction_datetime DESC, id DESC LIMIT 500",
                params,
            ).fetchall()
        )
        all_months = rows_to_dicts(conn.execute("SELECT DISTINCT month FROM transactions ORDER BY month DESC").fetchall())
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY account_name").fetchall())
    return templates.TemplateResponse(
        "transactions.html",
        {
            "request": request,
            "transactions": rows,
            "months": [row["month"] for row in all_months],
            "accounts": accounts,
            "statuses": STATUSES,
            "filters": {"month": month or "", "status": status or "", "account": account or "", "q": q or ""},
        },
    )


@app.post("/transactions/{transaction_id}/classify")
def classify(
    transaction_id: int,
    status: str = Form(...),
    joint_amount: Optional[str] = Form(None),
    tip_percent: Optional[str] = Form(None),
    tip_amount: Optional[str] = Form(None),
    return_to: str = Form("/transactions"),
):
    classify_transaction(
        transaction_id,
        status,
        optional_float(joint_amount, "joint_amount"),
        optional_float(tip_percent, "tip_percent"),
        optional_float(tip_amount, "tip_amount"),
    )
    return redirect(return_to)


@app.get("/transactions/{transaction_id}/edit")
def edit_transaction(request: Request, transaction_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    return templates.TemplateResponse(
        "transaction_edit.html",
        {"request": request, "transaction": dict(row), "statuses": STATUSES},
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
            },
        )
    return redirect("/transactions")


@app.get("/accounts")
def accounts(request: Request):
    with get_db() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY active DESC, account_name").fetchall())
    return templates.TemplateResponse("accounts.html", {"request": request, "accounts": rows})


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
    return templates.TemplateResponse("rules.html", {"request": request, "rules": rows, "statuses": STATUSES})


@app.post("/rules")
def save_rule(
    match_text: str = Form(...),
    match_field: str = Form("merchant_or_raw"),
    default_status: str = Form(...),
    default_joint_percent: str = Form(""),
    default_joint_amount: str = Form(""),
    priority: int = Form(100),
    active: int = Form(1),
):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO rules (
                match_text, match_field, default_status, default_joint_percent,
                default_joint_amount, priority, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_text.strip(),
                match_field,
                default_status,
                float(default_joint_percent) if default_joint_percent else None,
                float(default_joint_amount) if default_joint_amount else None,
                priority,
                active,
            ),
        )
    return redirect("/rules")


@app.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return redirect("/rules")


@app.get("/settings")
def settings(request: Request):
    with get_db() as conn:
        settings_rows = rows_to_dicts(conn.execute("SELECT * FROM settings ORDER BY key").fetchall())
    settings_map = {row["key"]: row["value"] for row in settings_rows}
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings_map,
            "data_dir": DATA_DIR,
            "default_senders": ", ".join(DEFAULT_CHASE_SENDERS),
            "default_keywords": ", ".join(DEFAULT_CHASE_KEYWORDS),
        },
    )


@app.post("/settings")
def save_settings(
    messages_db_path: str = Form(...),
    chase_sender_filters: str = Form(...),
    chase_keyword_filters: str = Form(...),
    polling_interval: str = Form("manual"),
):
    with get_db() as conn:
        set_setting(conn, "messages_db_path", messages_db_path)
        set_setting(conn, "chase_sender_filters", chase_sender_filters)
        set_setting(conn, "chase_keyword_filters", chase_keyword_filters)
        set_setting(conn, "polling_interval", polling_interval or "manual")
    return redirect("/settings")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "true", "time": utc_now()}
