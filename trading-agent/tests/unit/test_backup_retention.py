from datetime import date, timedelta

from trading_agent.storage.backups import retain


def test_retention_keeps_seven_days_four_weeks_and_never_deletes_unrelated_files(tmp_path):
    start = date(2026, 1, 1)
    for index in range(32):
        day = (start + timedelta(days=index)).isoformat()
        for prefix in ("daily", "weekly"):
            (tmp_path / f"{prefix}-{day}.dump").write_bytes(b"fixture")
            (tmp_path / f"{prefix}-{day}.sha256").write_text("fixture")
    unrelated = tmp_path / "operator-notes.dump"
    unrelated.write_text("retain")
    (tmp_path / "daily-2020-01-01.dump").symlink_to(unrelated)
    retain(tmp_path, "daily", 7)
    retain(tmp_path, "weekly", 4)
    assert len([p for p in tmp_path.glob("daily-*.dump") if not p.is_symlink()]) == 7
    assert len(list(tmp_path.glob("weekly-*.dump"))) == 4
    assert unrelated.read_text() == "retain"
    assert (tmp_path / "daily-2020-01-01.dump").is_symlink()
