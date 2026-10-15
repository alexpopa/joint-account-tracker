from joint_expense_tracker.db import backup_database


def test_daily_backup_is_written_once_and_pruned(db_path, tmp_path, monkeypatch) -> None:
    backups = tmp_path / "backups"
    monkeypatch.setattr("joint_expense_tracker.db.BACKUPS_TO_KEEP", 2)
    for stamp in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (backups / f"joint_expenses-{stamp}.sqlite").parent.mkdir(exist_ok=True)
        (backups / f"joint_expenses-{stamp}.sqlite").write_bytes(b"")

    written = backup_database(db_path, backup_dir=backups)
    assert written is not None and written.stat().st_size > 0
    assert backup_database(db_path, backup_dir=backups) is None
    assert len(list(backups.glob("joint_expenses-????-??-??.sqlite"))) == 2

