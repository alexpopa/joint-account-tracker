from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "Joint Expense Tracker"

# JOINT_EXPENSE_TRACKER_HOME relocates all app data; the test suite uses it to keep away from the real database.
DATA_DIR = Path(os.environ.get("JOINT_EXPENSE_TRACKER_HOME", Path.home() / ".joint-expense-tracker"))
DB_PATH = DATA_DIR / "joint_expenses.sqlite"
SNAPSHOT_DIR = DATA_DIR / "messages_snapshot"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"
BACKUPS_TO_KEEP = 30
DEFAULT_MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Chase alerts are written in Eastern time; dates and months are bucketed in this zone.
APP_TIMEZONE = ZoneInfo("America/New_York")

DEFAULT_CHASE_SENDERS = ["24273", "28107", "33172", "72166", "74869"]
DEFAULT_CHASE_KEYWORDS = ["Chase", "purchase", "card", "ending", "was charged", "alert"]

STATUSES = ["Joint", "Personal", "Venmo", "Split", "Review", "Ignored"]
# Presents for the partner are Personal spending with this category. "Present" is also
# accepted as a status shortcut and stored as Personal + Present (see services.resolve_status).
PRESENT = "Present"
# Tagging buttons on the Dashboard's review cards, in order.
REVIEW_CHOICES = ["Joint", "Personal", PRESENT, "Venmo", "Ignored"]
# Statuses whose spending can carry a category (stored in the `subcategory` column).
CATEGORIZED_STATUSES = {"Joint", "Split", "Personal"}
STARTER_CATEGORIES = [
    "Dining",
    "Entertainment",
    "Groceries",
    "Health",
    "Household",
    "Present",
    "Shopping",
    "Subscriptions",
    "Transport",
    "Travel",
    "Utilities",
    "Video games",
]


def ensure_data_dirs() -> None:
    for directory in (DATA_DIR, SNAPSHOT_DIR, EXPORT_DIR, BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
