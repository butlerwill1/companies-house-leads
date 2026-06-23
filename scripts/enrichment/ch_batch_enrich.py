#!/usr/bin/env python3
"""
Batch enrichment pipeline for filtered Companies House leads.

This is the normal short-run worker. It imports a filtered lead CSV into the
SQLite `leads` table, checks each pending company through the Companies House
API, fetches the latest accounts document metadata, parses XHTML/iXBRL
financials when available, and stores the structured extractor payload.

Use this when:
- you are loading a fresh lead CSV
- you want a bounded enrichment run with `--limit`
- you want to backfill narrative sections for already-enriched companies

Rate limit: Companies House API allows 600 requests per 5 minutes (2/sec).
Each company costs 2 API calls if no XHTML, 3 if XHTML found.

Resume-safe: tracks status per company in the `leads` table. Re-running
skips companies already in a terminal state (done / no_xhtml / error).

Usage:
    python -m scripts.enrichment.ch_batch_enrich --leads-csv data/ch-leads-sample.csv --db companies-house.db
    python -m scripts.enrichment.ch_batch_enrich --leads-csv data/ch-leads-sample.csv --db companies-house.db --limit 100
    python -m scripts.enrichment.ch_batch_enrich --leads-csv data/ch-leads-sample.csv --db companies-house.db --min-score 70

Backfill qualitative narrative sections for companies already enriched (1 API call each):
    python -m scripts.enrichment.ch_batch_enrich --db companies-house.db --backfill-narrative --limit 50
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from companies_house_extractor import CompaniesHouseExtractor, parse_xhtml_narrative, pick_latest_accounts_filing
from companies_house_sqlite import init_db, insert_narrative_payload, upsert_extractor_payload


# ---------------------------------------------------------------------------
# Schema additions — leads tracking table
# ---------------------------------------------------------------------------

LEADS_SCHEMA = """
create table if not exists leads (
    company_number   text primary key,
    company_name     text,
    sic_1            text,
    sic_2            text,
    sic_3            text,
    sic_4            text,
    incorporation_date text,
    last_accounts_date text,
    account_category text,
    post_town        text,
    post_code        text,
    lead_score       integer,
    score_reasons    text,
    -- enrichment tracking
    status           text not null default 'pending',
    -- pending | done | no_xhtml | no_filing | error
    xhtml_available  integer,   -- 1/0/null
    filing_date      text,
    filing_type      text,
    error_message    text,
    processed_at     text
);

create index if not exists idx_leads_status on leads(status);
create index if not exists idx_leads_score  on leads(lead_score desc);
"""


def init_leads_db(conn: sqlite3.Connection) -> None:
    configure_sqlite_connection(conn)
    init_db(conn)
    conn.executescript(LEADS_SCHEMA)
    conn.commit()


def configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma busy_timeout=30000")
    conn.execute("pragma synchronous=NORMAL")


def open_sqlite_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    configure_sqlite_connection(conn)
    return conn


def load_leads_csv(conn: sqlite3.Connection, csv_path: Path, min_score: int) -> int:
    """Import filtered leads CSV into the leads table. Skips existing rows."""
    inserted = 0
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            score = int(row.get("score", 0))
            if score < min_score:
                continue
            conn.execute(
                """
                insert into leads (
                    company_number, company_name, sic_1, sic_2, sic_3, sic_4,
                    incorporation_date, last_accounts_date, account_category,
                    post_town, post_code, lead_score, score_reasons
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(company_number) do nothing
                """,
                (
                    row["company_number"],
                    row["company_name"],
                    row["sic_1"],
                    row["sic_2"],
                    row["sic_3"],
                    row["sic_4"],
                    row["incorporation_date"],
                    row["last_accounts_date"],
                    row["account_category"],
                    row["post_town"],
                    row["post_code"],
                    score,
                    row["score_reasons"],
                ),
            )
            inserted += conn.execute("select changes()").fetchone()[0]
    conn.commit()
    return inserted


def mark_lead(
    conn: sqlite3.Connection,
    company_number: str,
    status: str,
    *,
    xhtml_available: int | None = None,
    filing_date: str | None = None,
    filing_type: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        update leads set
            status          = ?,
            xhtml_available = ?,
            filing_date     = ?,
            filing_type     = ?,
            error_message   = ?,
            processed_at    = ?
        where company_number = ?
        """,
        (
            status,
            xhtml_available,
            filing_date,
            filing_type,
            error_message,
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            company_number,
        ),
    )
    conn.commit()


def pending_leads(
    conn: sqlite3.Connection,
    limit: int | None,
    account_categories: list[str] | None = None,
) -> list[tuple[str, str]]:
    q = "select company_number, company_name from leads where status = 'pending'"
    params: list[Any] = []
    if account_categories:
        placeholders = ",".join("?" for _ in account_categories)
        q += f" and account_category in ({placeholders})"
        params.extend(account_categories)
    q += " order by lead_score desc"
    if limit:
        q += f" limit {limit}"
    return conn.execute(q, params).fetchall()


def print_summary(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "select status, count(*) from leads group by status order by count(*) desc"
    ).fetchall()
    total = sum(r[1] for r in rows)
    xhtml = conn.execute(
        "select count(*) from leads where xhtml_available = 1"
    ).fetchone()[0]
    print("\n--- Database summary ---")
    for status, count in rows:
        print(f"  {status:<15} {count:>7,}")
    print(f"  {'TOTAL':<15} {total:>7,}")
    print(f"  XHTML found:    {xhtml:>7,}")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Leaky-bucket: max `rate` calls per `period` seconds."""

    def __init__(self, rate: int = 10, period: float = 1.0) -> None:
        self.rate = rate
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.period
                while self._calls and self._calls[0] <= cutoff:
                    self._calls.popleft()
                if len(self._calls) < self.rate:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0])
            if sleep_for > 0:
                time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Core enrichment
# ---------------------------------------------------------------------------

def enrich_company(
    extractor: CompaniesHouseExtractor,
    limiter: RateLimiter,
    conn: sqlite3.Connection,
    company_number: str,
    company_name: str,
) -> str:
    """
    Returns the terminal status string for this company.
    """
    try:
        # 1. Filing history (1 API call)
        limiter.wait()
        filings = extractor.get_accounts_filings(company_number)
        latest_filing = pick_latest_accounts_filing(filings)

        if not latest_filing:
            mark_lead(conn, company_number, "no_filing")
            return "no_filing"

        # 2. Document metadata (1 API call)
        limiter.wait()
        document_urls = extractor.get_document_urls(company_number, latest_filing)

        has_xhtml = bool(document_urls.get("xhtml"))

        if not has_xhtml:
            mark_lead(
                conn,
                company_number,
                "no_xhtml",
                xhtml_available=0,
                filing_date=latest_filing.get("date"),
                filing_type=latest_filing.get("type"),
            )
            return "no_xhtml"

        # 3. Fetch and parse XHTML (1 API call)
        limiter.wait()
        xhtml_data = extractor.fetch_document(
            document_urls["xhtml"], content_type="application/xhtml+xml"
        )
        xhtml_text = xhtml_data.decode("utf-8", errors="ignore")
        accounts_extract = extractor.parse_xhtml_accounts(xhtml_text)

        # 4. Get company profile for storage (1 API call)
        limiter.wait()
        profile = extractor.get_company_profile(company_number)

        payload: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "label": company_name,
            "query": None,
            "company_number": company_number,
            "source_mode": "public_api",
            "search_results": [],
            "selected_company": {"company_name": company_name, "company_number": company_number},
            "company_profile": profile,
            "latest_accounts_filing": latest_filing,
            "document_urls": document_urls,
            "downloaded_files": {},
            "accounts_extract": accounts_extract,
        }

        upsert_extractor_payload(conn, payload)
        mark_lead(
            conn,
            company_number,
            "done",
            xhtml_available=1,
            filing_date=latest_filing.get("date"),
            filing_type=latest_filing.get("type"),
        )
        return "done"

    except Exception as exc:
        msg = str(exc)[:500]
        mark_lead(conn, company_number, "error", error_message=msg)
        return f"error: {msg[:80]}"


# ---------------------------------------------------------------------------
# Backfill: extract narrative sections from already-enriched XHTML companies
# ---------------------------------------------------------------------------

def narrative_backfill_queue(
    conn: sqlite3.Connection,
    limit: int | None,
) -> list[tuple[str, str, str]]:
    """Return (company_number, document_id, xhtml_url) for done companies that
    have turnover data but no narrative run yet, ordered by turnover descending."""
    sql = """
        select d.company_number, d.document_id, d.xhtml_url
        from documents d
        join financial_period_summaries fps
          on fps.company_number = d.company_number
         and fps.period_type = 'current'
         and fps.turnover is not null
        join leads l on l.company_number = d.company_number and l.status = 'done'
        where d.xhtml_url is not null
          and not exists (
              select 1 from narrative_runs nr
              where nr.company_number = d.company_number
          )
        order by fps.turnover desc
    """
    if limit:
        sql += f" limit {int(limit)}"
    return conn.execute(sql).fetchall()


def backfill_narrative(
    extractor: CompaniesHouseExtractor,
    limiter: RateLimiter,
    conn: sqlite3.Connection,
    limit: int | None,
) -> dict[str, int]:
    queue = narrative_backfill_queue(conn, limit)
    print(f"Backfilling narrative sections for {len(queue):,} companies...", file=sys.stderr)
    counts: dict[str, int] = {"ok": 0, "no_sections": 0, "error": 0}
    start = time.monotonic()

    for i, (company_number, document_id, xhtml_url) in enumerate(queue, 1):
        try:
            limiter.wait()
            xhtml_data = extractor.fetch_document(xhtml_url, content_type="application/xhtml+xml")
            xhtml_text = xhtml_data.decode("utf-8", errors="ignore")
            narrative = parse_xhtml_narrative(xhtml_text)
            insert_narrative_payload(conn, narrative, company_number, document_id)
            conn.commit()
            section_count = len(narrative.get("sections") or {})
            if section_count:
                counts["ok"] += 1
            else:
                counts["no_sections"] += 1
        except Exception as exc:
            counts["error"] += 1
            print(f"  ERROR {company_number}: {exc}", file=sys.stderr)

        if i % 10 == 0 or i == len(queue):
            elapsed = time.monotonic() - start
            rate = i / elapsed
            remaining = len(queue) - i
            eta_sec = int(remaining / rate) if rate > 0 else 0
            print(
                f"  [{i:>5}/{len(queue):,}]  "
                + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
                + f"  ETA {eta_sec // 60}m {eta_sec % 60}s",
                file=sys.stderr,
            )

    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Batch enrich CH leads with XHTML financial data.")
    parser.add_argument("--leads-csv", default=None, help="Filtered leads CSV from ch_bulk_filter.py.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--min-score", type=int, default=70, help="Minimum lead score to process.")
    parser.add_argument("--limit", type=int, default=None, help="Max companies to process this run.")
    parser.add_argument(
        "--account-categories",
        default=None,
        help="Optional comma-separated lead account categories to process first, e.g. FULL,GROUP.",
    )
    parser.add_argument(
        "--rate", type=int, default=2,
        help="API calls per second (default 2, CH limit is ~2/sec).",
    )
    parser.add_argument(
        "--backfill-narrative",
        action="store_true",
        help=(
            "Re-fetch XHTML for already-enriched companies and extract qualitative "
            "narrative sections (director's report, business review, etc.). "
            "Prioritises companies with the highest turnover. Use --limit to cap the run."
        ),
    )
    args = parser.parse_args(argv)

    if not args.backfill_narrative and not args.leads_csv:
        parser.error("--leads-csv is required unless --backfill-narrative is set.")

    load_dotenv(Path(".env"))
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        print("ERROR: COMPANIES_HOUSE_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    conn = open_sqlite_connection(args.db)
    init_leads_db(conn)

    extractor = CompaniesHouseExtractor(api_key=api_key)
    limiter = RateLimiter(rate=args.rate, period=1.0)

    if args.backfill_narrative:
        counts = backfill_narrative(extractor, limiter, conn, args.limit)
        print(
            f"\nNarrative backfill complete: "
            + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())),
            file=sys.stderr,
        )
        conn.close()
        return 0

    # Standard forward-enrichment path
    new_rows = load_leads_csv(conn, Path(args.leads_csv), args.min_score)
    if new_rows:
        print(f"Imported {new_rows:,} new leads from CSV.", file=sys.stderr)

    account_categories = None
    if args.account_categories:
        account_categories = [part.strip().upper() for part in args.account_categories.split(",") if part.strip()]

    queue = pending_leads(conn, args.limit, account_categories)
    print(f"Processing {len(queue):,} pending leads...", file=sys.stderr)

    if not queue:
        print("Nothing to do — all leads already processed.", file=sys.stderr)
        print_summary(conn)
        return 0

    counts: dict[str, int] = {}
    start = time.monotonic()

    for i, (company_number, company_name) in enumerate(queue, 1):
        status = enrich_company(extractor, limiter, conn, company_number, company_name)
        counts[status] = counts.get(status, 0) + 1

        if i % 10 == 0 or i == len(queue):
            elapsed = time.monotonic() - start
            rate = i / elapsed
            remaining = len(queue) - i
            eta_sec = int(remaining / rate) if rate > 0 else 0
            eta_str = f"{eta_sec // 60}m {eta_sec % 60}s"
            print(
                f"  [{i:>6}/{len(queue):,}]  "
                + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
                + f"  ETA {eta_str}",
                file=sys.stderr,
            )

    print_summary(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
