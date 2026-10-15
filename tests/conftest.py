"""Point all app data at a throwaway directory before the package is imported.

Paths in `joint_expense_tracker.config` are resolved at import time, so this must
run before any test module imports the package. Pytest loads conftest.py first.
"""

import os
import tempfile
from pathlib import Path

_TEST_HOME = Path(tempfile.mkdtemp(prefix="joint-expense-tracker-tests-"))
os.environ["JOINT_EXPENSE_TRACKER_HOME"] = str(_TEST_HOME)

from joint_expense_tracker import config  # noqa: E402

assert config.DATA_DIR == _TEST_HOME, "tests must never use the real ~/.joint-expense-tracker data directory"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from joint_expense_tracker.db import get_db, init_db  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path: Path):
    """An open connection to a fresh, migrated database."""
    with get_db(db_path) as connection:
        yield connection


@pytest.fixture
def client():
    """HTTP client for the web app, backed by a fresh database at the test DB_PATH."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{config.DB_PATH}{suffix}").unlink(missing_ok=True)
    from joint_expense_tracker.web import app

    with TestClient(app) as test_client:
        yield test_client
