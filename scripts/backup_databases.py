#!/usr/bin/env python3
"""Back up companies-house.db and the MLflow server database into OneDrive.

Uses SQLite's online backup API rather than a raw file copy, so a consistent
snapshot is produced even while a file is open (the MLflow server holds
mlflow.db open continuously; companies-house.db may be mid-write during an
enrichment run). Writes a dated copy per run and deletes copies in the
destination folder older than --keep-days, to bound how much space
accumulates in OneDrive over time.

Usage:
    python scripts/backup_databases.py
    python scripts/backup_databases.py --keep-days 30
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# MLflow is served only by the mlflow-local Docker Compose stack, whose backend
# store lives outside this repository. Backing up any other mlflow.db captures a
# database no server writes to.
MLFLOW_DB = Path.home() / "mlflow-server" / "data" / "mlflow.db"

SOURCES = {
    "companies-house": REPO_ROOT / "companies-house.db",
    "mlflow": MLFLOW_DB,
}

DEFAULT_DEST = Path.home() / "OneDrive" / "Backups" / "companies-house-leads"


def backup_one(source: Path, dest_dir: Path, label: str) -> Path | None:
    if not source.exists():
        print(f"skip {label}: {source} does not exist")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{label}-{date.today():%Y%m%d}.db"
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    print(f"backed up {label}: {source} ({source.stat().st_size:,} bytes) -> {dest}")
    return dest


def prune_old(dest_dir: Path, keep_days: int) -> None:
    if not dest_dir.exists():
        return
    cutoff = time.time() - keep_days * 86400
    for path in dest_dir.glob("*.db"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            print(f"pruned old backup: {path}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="Backup destination folder.")
    parser.add_argument("--keep-days", type=int, default=14, help="Delete backups older than this many days.")
    args = parser.parse_args(argv)

    for label, source in SOURCES.items():
        backup_one(source, args.dest, label)
    prune_old(args.dest, args.keep_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
