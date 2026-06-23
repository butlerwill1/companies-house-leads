#!/usr/bin/env python3
"""
Import browser-based website investigation pilot data into SQLite.

The browser pilot produces raw evidence JSON and a compact CSV report. This
script merges those two artefacts and stores the chosen domain, business model,
description, and derived website signals in the investigation tables.

Usage:
    python -m scripts.analysis.ch_website_investigations --db companies-house.db --evidence-json evidence.json --report-csv report.csv --source-label pilot_001
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

from companies_house_sqlite import init_db, upsert_website_investigation


def load_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_report_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {row["company_number"]: row for row in csv.DictReader(handle)}


def merge_report_fields(entry: dict, report_row: dict[str, str] | None) -> dict:
    merged = dict(entry)
    if not report_row:
        return merged
    merged["business_model"] = report_row.get("business_model") or merged.get("business_model")
    merged["business_description"] = report_row.get("business_description") or merged.get("business_description")
    return merged


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Import browser-based website investigations into SQLite.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--evidence-json", required=True, help="Combined browser evidence JSON path.")
    parser.add_argument("--report-csv", required=True, help="Pilot report CSV path.")
    parser.add_argument("--source-label", required=True, help="Stable label for this import batch.")
    args = parser.parse_args(argv)

    evidence_rows = load_json(Path(args.evidence_json))
    report_rows = load_report_rows(Path(args.report_csv))

    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        inserted = 0
        for entry in evidence_rows:
            merged = merge_report_fields(entry, report_rows.get(entry.get("company_number", "")))
            upsert_website_investigation(
                conn,
                merged,
                source_label=args.source_label,
                source_file=args.evidence_json,
            )
            inserted += 1
        conn.commit()
        print(
            json.dumps(
                {
                    "db": args.db,
                    "source_label": args.source_label,
                    "rows_imported": inserted,
                },
                indent=2,
            )
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
