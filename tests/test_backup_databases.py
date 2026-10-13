from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.backup_databases import backup_one, report_dated_leftovers


def make_db(path: Path, rows: list[str]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute("create table if not exists notes (body text)")
        conn.executemany("insert into notes (body) values (?)", [(row,) for row in rows])
        conn.commit()
    finally:
        conn.close()
    return path


def read_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute("select body from notes order by body")]
    finally:
        conn.close()


def test_backup_writes_an_undated_latest_copy(tmp_path: Path):
    source = make_db(tmp_path / "source.db", ["first"])
    dest_dir = tmp_path / "backups"

    dest = backup_one(source, dest_dir, "companies-house")

    assert dest == dest_dir / "companies-house.db"
    assert read_rows(dest) == ["first"]


def test_rerunning_overwrites_rather_than_accumulating(tmp_path: Path):
    source = make_db(tmp_path / "source.db", ["first"])
    dest_dir = tmp_path / "backups"

    backup_one(source, dest_dir, "companies-house")
    make_db(source, ["second"])
    backup_one(source, dest_dir, "companies-house")

    assert [path.name for path in dest_dir.iterdir()] == ["companies-house.db"]
    assert read_rows(dest_dir / "companies-house.db") == ["first", "second"]


def test_a_source_with_no_tables_leaves_the_previous_backup_intact(tmp_path: Path):
    source = make_db(tmp_path / "source.db", ["keep me"])
    dest_dir = tmp_path / "backups"
    backup_one(source, dest_dir, "companies-house")

    # An empty database snapshots cleanly but carries nothing: the guard should
    # reject it rather than replace the only copy we have with an empty file.
    source.unlink()
    sqlite3.connect(source).close()

    assert backup_one(source, dest_dir, "companies-house") is None
    assert read_rows(dest_dir / "companies-house.db") == ["keep me"]
    assert not (dest_dir / "companies-house.db.tmp").exists()


def test_a_wal_source_leaves_no_sidecars_in_the_destination(tmp_path: Path):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("pragma journal_mode = wal")
        conn.execute("create table notes (body text)")
        conn.execute("insert into notes (body) values ('walled')")
        conn.commit()
    finally:
        conn.close()

    dest_dir = tmp_path / "backups"
    backup_one(source, dest_dir, "companies-house")

    assert sorted(path.name for path in dest_dir.iterdir()) == ["companies-house.db"]
    assert read_rows(dest_dir / "companies-house.db") == ["walled"]


def test_a_missing_source_is_skipped(tmp_path: Path):
    dest_dir = tmp_path / "backups"

    assert backup_one(tmp_path / "absent.db", dest_dir, "companies-house") is None


def test_dated_leftovers_are_reported_but_never_deleted(tmp_path: Path, capsys):
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    stale = make_db(dest_dir / "companies-house-20260828.db", ["old"])
    current = make_db(dest_dir / "companies-house.db", ["current"])

    report_dated_leftovers(dest_dir)

    out = capsys.readouterr().out
    assert "companies-house-20260828.db" in out
    assert "companies-house.db (" not in out
    assert stale.exists()
    assert current.exists()
