#!/usr/bin/env python3
"""
Gate A: classify every enriched company as trading / holding / dormant and
flag duplicates and passthrough vehicles, writing the result to
`company_signals`.

Reads only data already in SQLite -- no Companies House API calls, no model
calls -- so this is free to run and safe to re-run. Run it after any
enrichment batch or history backfill, since those change its inputs.

Duplicate detection compares each company's turnover against every other
company's, so it cannot run per-company inside enrichment; it needs the
whole table at once. That is why this is a separate pass rather than a hook.

Usage:
    python -m scripts.analysis.ch_company_triage --db companies-house.db --dry-run
    python -m scripts.analysis.ch_company_triage --db companies-house.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from core.companies_house_sqlite import init_db
from core.company_triage import refresh_all_company_signals, triage_rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing signals.")
    parser.add_argument(
        "--sample", type=int, default=0,
        help="Also print this many example rows per non-trading status, to sanity-check the rules.",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.execute("pragma busy_timeout=30000")
    try:
        init_db(conn)
        counts = refresh_all_company_signals(conn, dry_run=args.dry_run)

        total = sum(v for k, v in counts.items() if not k.startswith("_"))
        print(f"{'Would classify' if args.dry_run else 'Classified'} {total:,} companies:", file=sys.stderr)
        for status in sorted(k for k in counts if not k.startswith("_")):
            print(f"  {status:<12} {counts[status]:>6,}", file=sys.stderr)
        print("\nFlags (not mutually exclusive):", file=sys.stderr)
        for flag in sorted(k for k in counts if k.startswith("_")):
            print(f"  {flag.lstrip('_'):<28} {counts[flag]:>6,}", file=sys.stderr)

        if args.sample:
            by_status: dict[str, list] = {}
            for row in triage_rows(conn):
                status = row["signals"]["trading_status"]
                if status != "trading":
                    by_status.setdefault(status, []).append(row)
            for status, rows in sorted(by_status.items()):
                print(f"\n--- {status} examples ---", file=sys.stderr)
                for row in rows[: args.sample]:
                    name = conn.execute(
                        "select company_name from companies where company_number = ?",
                        (row["company_number"],),
                    ).fetchone()
                    print(
                        f"  {row['company_number']}  {(name[0] if name else '')[:38]:<38} "
                        f"{row['signals']['trading_status_reason']}",
                        file=sys.stderr,
                    )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
