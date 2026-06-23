#!/usr/bin/env python3
"""
Load filtered Companies House leads into SQLite without calling external APIs.

This is a setup/import helper for cases where you want to populate the `leads`
table first and run enrichment later. It reuses the same schema and CSV parser
as `scripts.enrichment.ch_batch_enrich`, but stops before any Companies House
API requests are made.

Usage:
    python -m scripts.enrichment.ch_load_leads --leads-csv data/ch-leads-sample.csv --db companies-house.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from scripts.enrichment.ch_batch_enrich import init_leads_db, load_leads_csv


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Load filtered leads CSV into SQLite.")
    parser.add_argument("--leads-csv", required=True, help="Filtered leads CSV to import.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--min-score", type=int, default=70, help="Minimum score to import.")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        init_leads_db(conn)
        inserted = load_leads_csv(conn, Path(args.leads_csv), args.min_score)
        total = conn.execute("select count(*) from leads").fetchone()[0]
    finally:
        conn.close()

    print(f"Imported {inserted:,} new leads.")
    print(f"Total leads in database: {total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
