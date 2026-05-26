from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import re

from .config import DEFAULT_CHASE_KEYWORDS, DEFAULT_CHASE_SENDERS, DEFAULT_MESSAGES_DB, SNAPSHOT_DIR, ensure_data_dirs
from .db import get_db, get_setting, utc_now
from .parser import extract_amount, parse_alert
from .services import insert_alert


@dataclass
class ImportResult:
    messages_seen: int = 0
    messages_imported: int = 0
    error: str | None = None


def copy_messages_snapshot(messages_db_path: Path = DEFAULT_MESSAGES_DB) -> Path:
    ensure_data_dirs()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if not messages_db_path.exists():
        raise FileNotFoundError(f"Messages database not found: {messages_db_path}")
    for source in [messages_db_path, Path(f"{messages_db_path}-wal"), Path(f"{messages_db_path}-shm")]:
        if source.exists():
            shutil.copy2(source, SNAPSHOT_DIR / source.name)
    return SNAPSHOT_DIR / messages_db_path.name


def _csv_setting(value: str, fallback: list[str]) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or fallback


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _has_tables(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {row[0] for row in rows}
    return "message" in names and "handle" in names


def _open_snapshot_readonly(snapshot_path: Path) -> sqlite3.Connection:
    uri = f"file:{snapshot_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_attributed_body(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, bytes):
        raw = value
    else:
        return None
    if not raw:
        return None

    decoded = raw.decode("utf-8", errors="ignore")
    decoded = decoded.replace("\x00", " ")
    decoded = " ".join(decoded.split())
    if not decoded:
        return None

    chase_match = re.search(r"(Chase\s+.+?ET\.?)", decoded, re.I)
    if chase_match:
        return chase_match.group(1).strip()

    dollar_match = re.search(r"([A-Z][^$]{0,160}\$[\d,]+(?:\.\d{2})?.{0,240}?ET\.?)", decoded)
    if dollar_match:
        return dollar_match.group(1).strip()
    return decoded if "$" in decoded else None


def _message_text(row: sqlite3.Row) -> str:
    text = row["text"] if "text" in row.keys() else None
    if text:
        return str(text)
    return _decode_attributed_body(row["attributedBody"]) or ""


def _matches_filters(text: str, sender: str | None, senders: list[str], keywords: list[str]) -> bool:
    sender_value = sender or ""
    sender_match = any(item and item in sender_value for item in senders)
    keyword_match = any(item and item.lower() in text.lower() for item in keywords)
    return (sender_match or keyword_match) and extract_amount(text) is not None


def _candidate_messages(
    snapshot_conn: sqlite3.Connection,
    senders: list[str],
    keywords: list[str],
    limit: int = 1000,
) -> list[sqlite3.Row]:
    if not _has_tables(snapshot_conn):
        return []
    message_cols = _columns(snapshot_conn, "message")
    handle_cols = _columns(snapshot_conn, "handle")
    if not {"text", "date", "is_from_me"}.issubset(message_cols) or "id" not in handle_cols:
        return []

    guid_expr = "m.guid AS guid" if "guid" in message_cols else "NULL AS guid"
    body_expr = "m.attributedBody AS attributedBody" if "attributedBody" in message_cols else "NULL AS attributedBody"
    handle_join = "LEFT JOIN handle h ON h.ROWID = m.handle_id" if "handle_id" in message_cols else "LEFT JOIN handle h ON 1 = 0"
    sender_where = " OR ".join(["h.id LIKE ?" for _ in senders]) or "0"
    keyword_where = " OR ".join(["m.text LIKE ?" for _ in keywords]) or "0"
    params: list[str | int] = [f"%{keyword}%" for keyword in keywords]
    params.extend([f"%{sender}%" for sender in senders])
    params.append(limit)
    sql = f"""
        SELECT m.ROWID AS message_rowid, {guid_expr}, h.id AS sender, {body_expr},
               m.date AS date_raw, m.text AS text
        FROM message m
        {handle_join}
        WHERE m.is_from_me = 0
          AND ((m.text IS NOT NULL AND ({keyword_where})) OR ({sender_where}))
        ORDER BY m.date DESC
        LIMIT ?
    """
    return snapshot_conn.execute(sql, params).fetchall()


def import_messages() -> ImportResult:
    result = ImportResult()
    started_at = utc_now()
    with get_db() as app_conn:
        run_id = app_conn.execute(
            "INSERT INTO import_runs (started_at) VALUES (?)",
            (started_at,),
        ).lastrowid
        messages_path = Path(get_setting(app_conn, "messages_db_path", str(DEFAULT_MESSAGES_DB))).expanduser()
        senders = _csv_setting(get_setting(app_conn, "chase_sender_filters", ""), DEFAULT_CHASE_SENDERS)
        keywords = _csv_setting(get_setting(app_conn, "chase_keyword_filters", ""), DEFAULT_CHASE_KEYWORDS)
        try:
            snapshot = copy_messages_snapshot(messages_path)
            with _open_snapshot_readonly(snapshot) as snapshot_conn:
                rows = _candidate_messages(snapshot_conn, senders, keywords)
                for row in rows:
                    text = _message_text(row)
                    if not _matches_filters(text, row["sender"], senders, keywords):
                        continue
                    result.messages_seen += 1
                    parsed = parse_alert(
                        text,
                        message_date_raw=row["date_raw"],
                        source_message_id=str(row["message_rowid"]),
                        source_guid=row["guid"],
                        source_sender=row["sender"],
                    )
                    if parsed and insert_alert(app_conn, parsed):
                        result.messages_imported += 1
        except Exception as exc:  # noqa: BLE001 - importer must fail gracefully for FDA/schema issues.
            result.error = str(exc)
        finally:
            app_conn.execute(
                """
                UPDATE import_runs
                SET finished_at = ?, messages_seen = ?, messages_imported = ?, error = ?
                WHERE id = ?
                """,
                (utc_now(), result.messages_seen, result.messages_imported, result.error, run_id),
            )
    return result


def diagnose_messages() -> list[str]:
    lines: list[str] = []
    with get_db() as app_conn:
        messages_path = Path(get_setting(app_conn, "messages_db_path", str(DEFAULT_MESSAGES_DB))).expanduser()
        senders = _csv_setting(get_setting(app_conn, "chase_sender_filters", ""), DEFAULT_CHASE_SENDERS)
        keywords = _csv_setting(get_setting(app_conn, "chase_keyword_filters", ""), DEFAULT_CHASE_KEYWORDS)
    lines.append(f"Messages database: {messages_path}")
    lines.append(f"Exists: {'yes' if messages_path.exists() else 'no'}")
    try:
        snapshot = copy_messages_snapshot(messages_path)
        lines.append(f"Snapshot copy: succeeded ({snapshot})")
        with _open_snapshot_readonly(snapshot) as snapshot_conn:
            rows = _candidate_messages(snapshot_conn, senders, keywords, limit=25)
            rows = [row for row in rows if _matches_filters(_message_text(row), row["sender"], senders, keywords)]
            lines.append(f"Recent incoming dollar-message candidates: {len(rows)}")
            seen: dict[str, int] = {}
            for row in rows:
                sender = row["sender"] or "Unknown"
                seen[sender] = seen.get(sender, 0) + 1
            for sender, count in sorted(seen.items(), key=lambda item: item[1], reverse=True):
                lines.append(f"  sender={sender} count={count}")
            if not rows:
                lines.append("No candidates found. Check Chase sender/keyword filters in Settings.")
    except PermissionError as exc:
        lines.append(f"Snapshot copy: failed ({exc})")
        lines.append("Full Disk Access may be needed for Terminal, Python, or your IDE.")
    except Exception as exc:  # noqa: BLE001 - diagnostic command should explain any local failure.
        lines.append(f"Snapshot/read diagnosis failed: {exc}")
        lines.append("If this is an access error, enable Full Disk Access and retry.")
    return lines
