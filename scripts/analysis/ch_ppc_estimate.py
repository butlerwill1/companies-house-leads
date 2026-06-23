#!/usr/bin/env python3
"""
Backfill and inspect SIC-based PPC spend estimates.

This analysis helper reads current-period financial summaries from SQLite,
applies the SIC ratio rules stored by `companies_house_sqlite`, writes
`ppc_company_estimates`, and prints a small sample from a configurable monthly
spend band.

Usage:
    python -m scripts.analysis.ch_ppc_estimate --db companies-house.db --min-monthly 10000 --max-monthly 100000
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from companies_house_sqlite import init_db, refresh_all_ppc_estimates


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Backfill SIC-based PPC estimates in SQLite.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument(
        "--min-monthly",
        type=float,
        default=10000.0,
        help="Minimum estimated monthly PPC spend to include in the sample output.",
    )
    parser.add_argument(
        "--max-monthly",
        type=float,
        default=100000.0,
        help="Maximum estimated monthly PPC spend to include in the sample output.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many sample companies to print from the selected monthly spend band.",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        refreshed = refresh_all_ppc_estimates(conn)
        total_estimates = conn.execute("select count(*) from ppc_company_estimates").fetchone()[0]
        band_count = conn.execute(
            """
            select count(*)
            from ppc_company_estimates
            where estimated_monthly_ppc_spend between ? and ?
            """,
            (args.min_monthly, args.max_monthly),
        ).fetchone()[0]

        print(f"Refreshed PPC estimates for {refreshed:,} companies with current-period financial rows.")
        print(f"Total company estimates stored: {total_estimates:,}")
        print(
            f"Companies in estimated monthly PPC band "
            f"{args.min_monthly:,.0f} to {args.max_monthly:,.0f}: {band_count:,}"
        )
        print()
        print("Sample companies in band:")

        rows = conn.execute(
            """
            select
                p.company_number,
                l.company_name,
                p.sic_code,
                p.sic_label,
                p.turnover,
                p.annual_ppc_ratio,
                p.estimated_monthly_ppc_spend
            from ppc_company_estimates p
            join leads l on l.company_number = p.company_number
            where p.estimated_monthly_ppc_spend between ? and ?
            order by p.estimated_monthly_ppc_spend desc, p.company_number
            limit ?
            """,
            (args.min_monthly, args.max_monthly, args.limit),
        ).fetchall()
        for row in rows:
            print(
                f"{row[0]} | {row[1]} | SIC {row[2]} | {row[3]} | "
                f"turnover {row[4]:,} | ratio {row[5]:.2%} | "
                f"monthly PPC {row[6]:,.0f}"
            )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
