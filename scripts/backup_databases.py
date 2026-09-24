#!/usr/bin/env python3
"""Back up companies-house.db (and the parked MLflow database) into OneDrive.

Uses SQLite's online backup API rather than a raw file copy, so a consistent
snapshot is produced even while a file is open (companies-house.db may be
mid-write during an enrichment run).

Only the latest copy of each database is kept: every run overwrites the
previous one, and nothing is dated. Because that leaves no older copy to fall
back on, each backup is written to a temporary file beside its destination
and only moved into place once it is complete and readable -- an interrupted
or failed run leaves the last good backup untouched rather than truncating it.

MLflow is retired -- nothing writes to it now -- but
~/Documents/mlflow-server-2026-08-27/ is kept as a rollback until ~2026-10;
its database is still snapshotted here until then, after which the "mlflow"
source below can be removed.

Langfuse's own stores (Postgres metadata, ClickHouse trace history, the MinIO
media bucket) are backed up alongside these by ~/langfuse-server/backup.ps1,
on the same latest-copy-only basis.

Usage:
    python scripts/backup_databases.py
    python scripts/backup_databases.py --dest <some other folder>
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The parked MLflow store (retired 2026-09; kept as a rollback until ~2026-10).
# Its Docker Compose stack lives at ~/Documents/mlflow-server-2026-08-27/.
MLFLOW_DB = Path.home() / "Documents" / "mlflow-server-2026-08-27" / "data" / "mlflow.db"

SOURCES = {
    "companies-house": REPO_ROOT / "companies-house.db",
    "mlflow": MLFLOW_DB,
}

DEFAULT_DEST = Path.home() / "OneDrive" / "Backups" / "companies-house-leads"


def discard_temp(temp: Path) -> None:
    """Remove a staged snapshot and any SQLite sidecars sitting beside it."""
    for path in (temp, Path(f"{temp}-wal"), Path(f"{temp}-shm")):
        path.unlink(missing_ok=True)


def backup_one(source: Path, dest_dir: Path, label: str) -> Path | None:
    """Snapshot one SQLite database to <dest_dir>/<label>.db, replacing any
    previous copy only once the new one is known to be good."""
    if not source.exists():
        print(f"skip {label}: {source} does not exist")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{label}.db"
    temp = dest_dir / f"{label}.db.tmp"
    discard_temp(temp)

    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(temp)
    try:
        src_conn.backup(dest_conn)
        # companies-house.db runs in WAL mode and the backup API copies that
        # setting across, so the snapshot would otherwise be left behind with
        # -wal/-shm sidecars beside it in OneDrive. Checkpointing back to a
        # rollback journal makes the copied .db self-contained.
        dest_conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        dest_conn.close()
        src_conn.close()

    # Cheap proof the snapshot is a readable database before it replaces the
    # only copy we have. A corrupt or empty file raises here and leaves the
    # previous backup in place.
    try:
        check_conn = sqlite3.connect(f"file:{temp}?mode=ro", uri=True)
        try:
            tables = check_conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        finally:
            check_conn.close()
        if tables == 0:
            raise sqlite3.DatabaseError("snapshot contains no tables")
    except sqlite3.DatabaseError as exc:
        discard_temp(temp)
        print(f"FAILED {label}: {exc}; kept previous backup at {dest}")
        return None

    os.replace(temp, dest)
    discard_temp(temp)
    print(
        f"backed up {label}: {source} ({source.stat().st_size:,} bytes, "
        f"{tables} tables) -> {dest}"
    )
    return dest


def report_dated_leftovers(dest_dir: Path) -> None:
    """This script used to write one dated file per run. Those are no longer
    produced or pruned, so point out any that are still taking up space."""
    leftovers = sorted(
        path
        for path in dest_dir.glob("*.db")
        if path.stem not in SOURCES and not path.name.endswith(".tmp")
    )
    if not leftovers:
        return
    total = sum(path.stat().st_size for path in leftovers)
    print(
        f"\nnote: {len(leftovers)} dated backup(s) from the old retention scheme "
        f"are still in {dest_dir} ({total:,} bytes). Delete them by hand if you "
        f"no longer want them:"
    )
    for path in leftovers:
        print(f"  {path.name} ({path.stat().st_size:,} bytes)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", type=Path, default=DEFAULT_DEST, help="Backup destination folder."
    )
    args = parser.parse_args(argv)

    for label, source in SOURCES.items():
        backup_one(source, args.dest, label)
    report_dated_leftovers(args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
