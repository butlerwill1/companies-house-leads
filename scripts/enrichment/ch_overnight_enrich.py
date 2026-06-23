#!/usr/bin/env python3
"""Long-running Companies House enrichment runner.

This is the unattended version of the batch pipeline. It is designed for long
overnight runs where the interactive session may time out but the local Python
process should keep going.

Key behavior:
- resume-safe: each company is written to SQLite immediately
- retries transient failures with exponential backoff
- handles HTTP 429 / 5xx / network errors more gently than the short-run script
- re-checks the pending queue in batches until it hits a stop condition

Use this for larger batches after the leads table has been loaded and you want
the process to keep working until the queue, time budget, or limit is exhausted.

Usage:
    python -m scripts.enrichment.ch_overnight_enrich --leads-csv data/ch-leads-full.csv --db companies-house.db
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.enrichment.ch_batch_enrich import (
    RateLimiter,
    init_leads_db,
    load_dotenv,
    load_leads_csv,
    mark_lead,
    open_sqlite_connection,
    pending_leads,
    print_summary,
)
from companies_house_extractor import CompaniesHouseExtractor, pick_latest_accounts_filing
from companies_house_sqlite import upsert_extractor_payload


TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
THREAD_STATE = threading.local()
LOG_WRITE_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_transient_error(exc: Exception) -> tuple[bool, int | None, float | None]:
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        if "database is locked" in message or "database is busy" in message:
            return True, None, None
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After")
        retry_seconds = float(retry_after) if retry_after and retry_after.isdigit() else None
        return exc.code in TRANSIENT_HTTP_CODES, exc.code, retry_seconds
    if isinstance(exc, urllib.error.URLError):
        return True, None, None
    if isinstance(exc, TimeoutError):
        return True, None, None
    message = str(exc).lower()
    if "timed out" in message or "timeout" in message or "temporarily unavailable" in message:
        return True, None, None
    return False, None, None


def enrich_company_once(
    extractor: CompaniesHouseExtractor,
    limiter: RateLimiter,
    conn: sqlite3.Connection,
    company_number: str,
    company_name: str,
) -> str:
    limiter.wait()
    filings = extractor.get_accounts_filings(company_number)
    latest_filing = pick_latest_accounts_filing(filings)

    if not latest_filing:
        mark_lead(conn, company_number, "no_filing")
        return "no_filing"

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

    limiter.wait()
    xhtml_data = extractor.fetch_document(
        document_urls["xhtml"], content_type="application/xhtml+xml"
    )
    xhtml_text = xhtml_data.decode("utf-8", errors="ignore")
    accounts_extract = extractor.parse_xhtml_accounts(xhtml_text)

    limiter.wait()
    profile = extractor.get_company_profile(company_number)

    payload: dict[str, Any] = {
        "generated_at": utc_now(),
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


def process_company_with_retries(
    limiter: RateLimiter,
    db_path: str,
    api_key: str,
    company_number: str,
    company_name: str,
    *,
    max_attempts: int,
    backoff_base: float,
    backoff_max: float,
    log_file: Path | None,
) -> tuple[str, int]:
    extractor = get_worker_extractor(api_key)
    conn = open_sqlite_connection(db_path)
    last_error = ""
    retry_count = 0
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                return enrich_company_once(extractor, limiter, conn, company_number, company_name), retry_count
            except Exception as exc:
                transient, http_code, retry_after = is_transient_error(exc)
                last_error = str(exc)[:500]
                log_event(
                    log_file,
                    {
                        "ts": utc_now(),
                        "event": "company_error",
                        "company_number": company_number,
                        "company_name": company_name,
                        "attempt": attempt,
                        "transient": transient,
                        "http_code": http_code,
                        "message": last_error,
                    },
                )
                if transient and attempt < max_attempts:
                    retry_count += 1
                    sleep_for = retry_after if retry_after is not None else min(backoff_base * (2 ** (attempt - 1)), backoff_max)
                    print(
                        f"Transient error for {company_number} on attempt {attempt}/{max_attempts}"
                        f" ({http_code or 'network'}). Sleeping {sleep_for:.0f}s.",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_for)
                    continue

                mark_lead(conn, company_number, "error", error_message=last_error)
                return "error", retry_count

        mark_lead(conn, company_number, "error", error_message=last_error)
        return "error", retry_count
    finally:
        conn.close()


def log_event(log_file: Path | None, payload: dict[str, Any]) -> None:
    if not log_file:
        return
    with LOG_WRITE_LOCK:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def get_worker_extractor(api_key: str) -> CompaniesHouseExtractor:
    if getattr(THREAD_STATE, "api_key", None) != api_key or not hasattr(THREAD_STATE, "extractor"):
        THREAD_STATE.api_key = api_key
        THREAD_STATE.extractor = CompaniesHouseExtractor(api_key=api_key)
    return THREAD_STATE.extractor


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return "  ".join(f"{key}:{value}" for key, value in sorted(counts.items()))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run a long-lived CH enrichment pass with retries.")
    parser.add_argument("--leads-csv", required=True, help="Filtered leads CSV from ch_bulk_filter.py.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--min-score", type=int, default=70, help="Minimum lead score to load from CSV.")
    parser.add_argument("--batch-size", type=int, default=200, help="Companies to pull from the pending queue per cycle.")
    parser.add_argument("--max-companies", type=int, help="Optional total companies to process before exiting.")
    parser.add_argument("--max-hours", type=float, help="Optional wall-clock time limit in hours.")
    parser.add_argument(
        "--account-categories",
        default=None,
        help="Optional comma-separated account categories, e.g. FULL,GROUP.",
    )
    parser.add_argument("--rate", type=int, default=2, help="API calls per second.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent company workers sharing one global rate limiter.")
    parser.add_argument("--retry-attempts", type=int, default=6, help="Retries per company for transient failures.")
    parser.add_argument("--backoff-base", type=float, default=30.0, help="Initial retry backoff in seconds.")
    parser.add_argument("--backoff-max", type=float, default=900.0, help="Maximum retry backoff in seconds.")
    parser.add_argument(
        "--idle-sleep",
        type=float,
        default=300.0,
        help="How long to sleep when no matching pending leads remain.",
    )
    parser.add_argument("--log-file", help="Optional JSONL log file path.")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        print("ERROR: COMPANIES_HOUSE_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    log_file = Path(args.log_file) if args.log_file else None
    account_categories = None
    if args.account_categories:
        account_categories = [part.strip().upper() for part in args.account_categories.split(",") if part.strip()]

    conn = open_sqlite_connection(args.db)
    init_leads_db(conn)
    new_rows = load_leads_csv(conn, Path(args.leads_csv), args.min_score)
    if new_rows:
        print(f"Imported {new_rows:,} new leads from CSV.", file=sys.stderr)

    limiter = RateLimiter(rate=args.rate, period=1.0)

    started = time.monotonic()
    processed_total = 0
    overall_counts: dict[str, int] = {}
    transient_retry_total = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(args.workers, 1)) as executor:
        while True:
            if args.max_companies is not None and processed_total >= args.max_companies:
                print(f"Reached max-companies limit: {processed_total:,}", file=sys.stderr)
                break

            if args.max_hours is not None and (time.monotonic() - started) >= args.max_hours * 3600:
                print(f"Reached max-hours limit: {args.max_hours}", file=sys.stderr)
                break

            remaining_limit = None
            if args.max_companies is not None:
                remaining_limit = max(args.max_companies - processed_total, 0)
                if remaining_limit == 0:
                    break

            batch_limit = args.batch_size
            if remaining_limit is not None:
                batch_limit = min(batch_limit, remaining_limit)

            queue = pending_leads(conn, batch_limit, account_categories)
            if not queue:
                print(f"No pending leads matched. Sleeping {args.idle_sleep:.0f}s.", file=sys.stderr)
                log_event(log_file, {"ts": utc_now(), "event": "idle_sleep", "sleep_seconds": args.idle_sleep})
                time.sleep(args.idle_sleep)
                continue

            batch_start = time.monotonic()
            batch_counts: dict[str, int] = {}
            batch_retry_total = 0
            print(
                f"Starting batch of {len(queue):,} companies with {max(args.workers, 1)} workers.",
                file=sys.stderr,
            )

            future_map = {
                executor.submit(
                    process_company_with_retries,
                    limiter,
                    args.db,
                    api_key,
                    company_number,
                    company_name,
                    max_attempts=args.retry_attempts,
                    backoff_base=args.backoff_base,
                    backoff_max=args.backoff_max,
                    log_file=log_file,
                ): (company_number, company_name)
                for company_number, company_name in queue
            }

            for index, future in enumerate(concurrent.futures.as_completed(future_map), start=1):
                company_number, company_name = future_map[future]
                try:
                    status, retry_count = future.result()
                except Exception as exc:
                    status = "error"
                    retry_count = 0
                    log_event(
                        log_file,
                        {
                            "ts": utc_now(),
                            "event": "worker_crash",
                            "company_number": company_number,
                            "company_name": company_name,
                            "message": str(exc)[:500],
                        },
                    )

                batch_counts[status] = batch_counts.get(status, 0) + 1
                overall_counts[status] = overall_counts.get(status, 0) + 1
                batch_retry_total += retry_count
                transient_retry_total += retry_count
                processed_total += 1

                if processed_total % 10 == 0 or index == len(queue):
                    elapsed = time.monotonic() - started
                    throughput = processed_total / elapsed if elapsed > 0 else 0.0
                    print(
                        f"[total {processed_total:,} | batch {index:,}/{len(queue):,}] "
                        + f"batch {format_counts(batch_counts)}  "
                        + f"overall {format_counts(overall_counts)}  "
                        + f"retries(batch/total):{batch_retry_total}/{transient_retry_total}  "
                        + f"avg {throughput:.2f} companies/sec",
                        file=sys.stderr,
                    )

                log_event(
                    log_file,
                    {
                        "ts": utc_now(),
                        "event": "company_done",
                        "company_number": company_number,
                        "company_name": company_name,
                        "status": status,
                        "processed_total": processed_total,
                    },
                )

            batch_elapsed = time.monotonic() - batch_start
            print(f"Completed batch in {batch_elapsed / 60:.1f} minutes.", file=sys.stderr)
            print_summary(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
