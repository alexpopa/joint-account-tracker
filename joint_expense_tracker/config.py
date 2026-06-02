from __future__ import annotations

from pathlib import Path

APP_NAME = "Joint Expense Tracker"
DATA_DIR = Path.home() / ".joint-expense-tracker"
DB_PATH = DATA_DIR / "joint_expenses.sqlite"
SNAPSHOT_DIR = DATA_DIR / "messages_snapshot"
EXPORT_DIR = DATA_DIR / "exports"
DEFAULT_MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"

DEFAULT_CHASE_SENDERS = ["24273", "28107", "33172", "72166", "74869"]
DEFAULT_CHASE_KEYWORDS = ["Chase", "purchase", "card", "ending", "was charged", "alert"]

STATUSES = ["Joint", "Personal", "Her", "Split", "Review", "Ignored"]


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
