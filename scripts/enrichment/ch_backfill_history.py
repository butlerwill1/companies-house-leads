#!/usr/bin/env python3
"""
Backdate multi-year financial history for already-enriched companies.

Forward enrichment (ch_batch_enrich.py) only ever fetches the latest
accounts filing, so financial_period_summaries has exactly two rows per
company (current + comparative period) from one filing. This script walks
each selected company's filing history further back and inserts one row per
historical filing, using the same persistence path as forward enrichment
(upsert_extractor_payload), so a company ends up with several years of
history instead of one.

Intended pipeline shape:
    1. ch_batch_enrich.py       -- get the latest filing for many companies
    2. (screen by account_category / turnover / entity triage)
    3. ch_backfill_history.py   -- backdate history for the companies that
                                    are worth it

Company selection (choose one):
    --company NUMBER_OR_NAME [--company ...]
        Explicit companies, by Companies House number or by name (matched
        against the local companies/leads tables; errors on an ambiguous
        name rather than guessing).

    --turnover-band MIN:MAX --sample N [--seed N]
        A random sample of N companies whose current-period turnover falls
        in [MIN, MAX], restricted to companies with both turnover and
        profit_after_tax on their current period. This is the "companies
        big enough to matter, no ranking needed, just a sample" case.

History depth: --years (default 5) is a floor on how far back to walk;
--max-filings (default 4) is a hard cap on API cost per company. Each
filing supplies both a "current" and "previous" period, so 4 filings cover
roughly 5 distinct financial years.

Resume-safe: a filing already present in `documents` (by transaction_id) is
skipped without any API call, so re-running only fetches what's missing.

Usage:
    python -m scripts.enrichment.ch_backfill_history --db companies-house.db --company 01407612
    python -m scripts.enrichment.ch_backfill_history --db companies-house.db --turnover-band 5000000:20000000 --sample 100 --dry-run
    python -m scripts.enrichment.ch_backfill_history --db companies-house.db --turnover-band 5000000:20000000 --sample 100
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from core.companies_house_extractor import CompaniesHouseExtractor, load_dotenv
from core.companies_house_sqlite import compute_comparative_overlap, init_db, upsert_extractor_payload, utc_now
from scripts.enrichment.ch_batch_enrich import RateLimiter, configure_sqlite_connection, open_sqlite_connection


def resolve_company(conn: sqlite3.Connection, identifier: str) -> str:
    """Resolve a --company argument (a CH number or a company name) to a
    single company_number already present in the local database."""
    identifier = identifier.strip()
    exact = conn.execute(
        "select company_number from companies where company_number = ?", (identifier.upper(),)
    ).fetchone()
    if exact:
        return exact[0]

    matches = conn.execute(
        "select company_number, company_name from companies where company_name like ? order by company_name",
        (f"%{identifier}%",),
    ).fetchall()
    if not matches:
        raise SystemExit(f"No company found matching '{identifier}' (checked company_number and company_name).")
    if len(matches) > 1:
        listed = "\n".join(f"  {number}  {name}" for number, name in matches[:20])
        raise SystemExit(f"'{identifier}' matches {len(matches)} companies, pick one by number instead:\n{listed}")
    return matches[0][0]


def select_turnover_band_sample(
    conn: sqlite3.Connection,
    min_turnover: float,
    max_turnover: float,
    sample_size: int,
    seed: int,
) -> list[str]:
    """Companies with both turnover and profit data on their current period
    whose turnover falls in [min_turnover, max_turnover], sampled (not
    ranked)."""
    candidates = conn.execute(
        """
        select distinct company_number
        from financial_period_summaries
        where period_type = 'current'
          and turnover between ? and ?
          and profit_after_tax is not null
        """,
        (min_turnover, max_turnover),
    ).fetchall()
    company_numbers = [row[0] for row in candidates]
    if len(company_numbers) <= sample_size:
        return company_numbers
    return random.Random(seed).sample(company_numbers, sample_size)


def existing_transaction_ids(conn: sqlite3.Connection, company_number: str) -> set[str]:
    rows = conn.execute(
        "select transaction_id from documents where company_number = ? and transaction_id is not null",
        (company_number,),
    ).fetchall()
    return {row[0] for row in rows}


def backfill_company(
    extractor: CompaniesHouseExtractor,
    limiter: RateLimiter,
    conn: sqlite3.Connection,
    company_number: str,
    *,
    years: int,
    max_filings: int,
    dry_run: bool,
) -> dict[str, int]:
    counts = {"inserted": 0, "skipped_existing": 0, "no_xhtml": 0, "error": 0}

    profile_row = conn.execute(
        "select profile_payload from companies where company_number = ?", (company_number,)
    ).fetchone()
    if not profile_row:
        counts["error"] += 1
        print(f"  {company_number}: not in companies table, run forward enrichment first", file=sys.stderr)
        return counts
    company_profile = json.loads(profile_row[0])

    known_transactions = existing_transaction_ids(conn, company_number)

    limiter.wait()
    try:
        history = extractor.get_accounts_history(company_number, years=years, max_filings=max_filings)
    except Exception as exc:
        counts["error"] += 1
        print(f"  {company_number}: filing-history fetch failed: {exc}", file=sys.stderr)
        return counts

    for filing in history:
        transaction_id = filing.get("transaction_id")
        if transaction_id in known_transactions:
            counts["skipped_existing"] += 1
            continue

        period_end = (filing.get("description_values") or {}).get("made_up_date") or filing.get("action_date")
        if dry_run:
            print(f"  {company_number}: would fetch {filing.get('date')} (period end {period_end})")
            counts["inserted"] += 1
            continue

        try:
            limiter.wait()
            document_urls = extractor.get_document_urls(company_number, filing)
            if not document_urls.get("xhtml"):
                counts["no_xhtml"] += 1
                continue

            limiter.wait()
            xhtml_data = extractor.fetch_document(document_urls["xhtml"], content_type="application/xhtml+xml")
            xhtml_text = xhtml_data.decode("utf-8", errors="ignore")
            accounts_extract = extractor.parse_xhtml_accounts(xhtml_text)

            payload: dict[str, Any] = {
                "generated_at": utc_now(),
                "label": company_profile.get("company_name"),
                "company_number": company_number,
                "source_mode": "public_api",
                "company_profile": company_profile,
                "latest_accounts_filing": filing,
                "document_urls": document_urls,
                "downloaded_files": {},
                "accounts_extract": accounts_extract,
            }
            result = upsert_extractor_payload(conn, payload)
            if result.get("document_id"):
                compute_comparative_overlap(conn, company_number, result["document_id"])
                conn.commit()
            counts["inserted"] += 1
        except Exception as exc:
            counts["error"] += 1
            print(f"  {company_number}: {filing.get('date')}: {exc}", file=sys.stderr)

    return counts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument(
        "--company", action="append", default=None,
        help="A company number or name to backfill. Repeatable.",
    )
    parser.add_argument(
        "--turnover-band", default=None, metavar="MIN:MAX",
        help="Select companies by current-period turnover range, e.g. 5000000:20000000.",
    )
    parser.add_argument("--sample", type=int, default=None, help="Sample size for --turnover-band.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for --turnover-band sampling (default 42).")
    parser.add_argument("--years", type=int, default=5, help="How many years of history to walk back (default 5).")
    parser.add_argument(
        "--max-filings", type=int, default=4,
        help="Hard cap on filings fetched per company (default 4; ~5 years including comparatives).",
    )
    parser.add_argument("--rate", type=int, default=2, help="API calls per second (default 2, CH limit is ~2/sec).")
    parser.add_argument("--dry-run", action="store_true", help="List what would be fetched without writing.")
    args = parser.parse_args(argv)

    if not args.company and not args.turnover_band:
        parser.error("Specify either --company (repeatable) or --turnover-band MIN:MAX --sample N.")
    if args.turnover_band and not args.sample:
        parser.error("--turnover-band requires --sample N.")

    load_dotenv(Path(".env"))
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        print("ERROR: COMPANIES_HOUSE_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    conn = open_sqlite_connection(args.db)
    init_db(conn)

    if args.company:
        company_numbers = [resolve_company(conn, identifier) for identifier in args.company]
    else:
        min_turnover, _, max_turnover = args.turnover_band.partition(":")
        company_numbers = select_turnover_band_sample(conn, float(min_turnover), float(max_turnover), args.sample, args.seed)
        print(f"Selected {len(company_numbers):,} companies with turnover in [{min_turnover}, {max_turnover}].", file=sys.stderr)

    if not company_numbers:
        print("No companies selected.", file=sys.stderr)
        conn.close()
        return 0

    extractor = CompaniesHouseExtractor(api_key=api_key)
    limiter = RateLimiter(rate=args.rate, period=1.0)

    totals = {"inserted": 0, "skipped_existing": 0, "no_xhtml": 0, "error": 0}
    start = time.monotonic()
    for i, company_number in enumerate(company_numbers, 1):
        counts = backfill_company(
            extractor, limiter, conn, company_number,
            years=args.years, max_filings=args.max_filings, dry_run=args.dry_run,
        )
        for key, value in counts.items():
            totals[key] += value

        if i % 5 == 0 or i == len(company_numbers):
            elapsed = time.monotonic() - start
            rate = i / elapsed if elapsed > 0 else 0
            remaining = len(company_numbers) - i
            eta_sec = int(remaining / rate) if rate > 0 else 0
            print(
                f"  [{i:>4}/{len(company_numbers):,}]  "
                + "  ".join(f"{k}:{v}" for k, v in sorted(totals.items()))
                + f"  ETA {eta_sec // 60}m {eta_sec % 60}s",
                file=sys.stderr,
            )

    overlap_rows = conn.execute(
        "select comparative_overlap_status, count(*) from financial_period_summaries "
        "where comparative_overlap_status is not null group by 1"
    ).fetchall()
    if overlap_rows:
        print("\nComparative-overlap agreement (previous-period reading vs. adjacent filing's current reading):", file=sys.stderr)
        for status, count in overlap_rows:
            print(f"  {status:<10} {count:>6,}", file=sys.stderr)

    print(f"\nDone: {totals}", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
