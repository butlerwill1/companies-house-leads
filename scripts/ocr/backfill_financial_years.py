#!/usr/bin/env python3
"""Backfill explicit financial years from retained VLM evidence and iXBRL contexts."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.companies_house_extractor import (
    CompaniesHouseExtractor,
    load_dotenv,
    parse_financial_year,
)
from core.companies_house_sqlite import init_db


class RequestStartLimiter:
    """Reserve request start times across workers at a fixed maximum rate."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be greater than zero")
        self.interval = 1.0 / rate
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            scheduled = max(time.monotonic(), self.next_start)
            self.next_start = scheduled + self.interval
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def unambiguous_candidate_years(raw_extraction: dict[str, Any]) -> dict[str, int]:
    years: dict[str, set[int]] = {"current": set(), "previous": set()}
    for candidate in raw_extraction.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for period_type in years:
            financial_year = parse_financial_year(candidate.get(f"{period_type}_column"))
            if financial_year is not None:
                years[period_type].add(financial_year)
    return {
        period_type: next(iter(values))
        for period_type, values in years.items()
        if len(values) == 1
    }


def backfill_vlm_years(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, int]:
    runs = conn.execute(
        """
        select id, company_number, document_id, raw_extraction_payload
        from vlm_financial_extraction_runs
        order by id
        """
    ).fetchall()
    updated_metrics = 0
    updated_summaries = 0
    for run_id, company_number, document_id, raw_payload in runs:
        years = unambiguous_candidate_years(json.loads(raw_payload))
        for period_type, financial_year in years.items():
            metric_count = conn.execute(
                """
                select count(*) from vlm_financial_metrics
                where extraction_run_id = ? and period_type = ? and financial_year is null
                """,
                (run_id, period_type),
            ).fetchone()[0]
            summary_count = conn.execute(
                """
                select count(*) from financial_period_summaries
                where company_number = ? and document_id = ? and period_type = ?
                  and data_source = 'vlm' and financial_year is null
                """,
                (company_number, document_id, period_type),
            ).fetchone()[0]
            updated_metrics += int(metric_count)
            updated_summaries += int(summary_count)
            if not dry_run:
                conn.execute(
                    """
                    update vlm_financial_metrics set financial_year = ?
                    where extraction_run_id = ? and period_type = ? and financial_year is null
                    """,
                    (financial_year, run_id, period_type),
                )
                conn.execute(
                    """
                    update financial_period_summaries set financial_year = ?
                    where company_number = ? and document_id = ? and period_type = ?
                      and data_source = 'vlm' and financial_year is null
                    """,
                    (financial_year, company_number, document_id, period_type),
                )
    if not dry_run:
        conn.commit()
    return {
        "runs_checked": len(runs),
        "metrics_updated": updated_metrics,
        "summaries_updated": updated_summaries,
    }


def backfill_xhtml_years(
    conn: sqlite3.Connection,
    extractor: CompaniesHouseExtractor,
    *,
    rate: float,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    limiter = RequestStartLimiter(rate)
    sql = """
        select distinct fps.company_number, fps.document_id, d.xhtml_url
        from financial_period_summaries fps
        join documents d on d.document_id = fps.document_id
        where fps.data_source = 'xhtml'
          and fps.financial_year is null
          and d.xhtml_url is not null
        order by fps.company_number, fps.document_id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " limit ?"
        params = (limit,)
    documents = conn.execute(sql, params).fetchall()
    processed = updated = skipped = failed = 0

    def fetch_document_years(
        document: tuple[str, str, str],
    ) -> tuple[str, str, dict[str, Any] | None, str | None]:
        company_number, document_id, xhtml_url = document
        limiter.wait()
        try:
            xhtml = extractor.fetch_document(
                xhtml_url,
                content_type="application/xhtml+xml",
            ).decode("utf-8", errors="ignore")
            return company_number, document_id, extractor.parse_xhtml_accounts(xhtml).get("years") or {}, None
        except Exception as error:
            return company_number, document_id, None, str(error)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = executor.map(fetch_document_years, documents)
        for company_number, document_id, years, error in results:
            if error is not None or years is None:
                failed += 1
                print(
                    json.dumps({"company_number": company_number, "document_id": document_id, "error": error}),
                    file=sys.stderr,
                    flush=True,
                )
                processed += 1
                continue
            document_updates = 0
            for period_type in ("current", "previous"):
                financial_year = (years.get(period_type) or {}).get("financial_year")
                if not isinstance(financial_year, int) or isinstance(financial_year, bool):
                    continue
                row_count = conn.execute(
                    """
                    select count(*) from financial_period_summaries
                    where company_number = ? and document_id = ? and period_type = ?
                      and data_source = 'xhtml' and financial_year is null
                    """,
                    (company_number, document_id, period_type),
                ).fetchone()[0]
                document_updates += int(row_count)
                if not dry_run:
                    conn.execute(
                        """
                        update financial_period_summaries set financial_year = ?
                        where company_number = ? and document_id = ? and period_type = ?
                          and data_source = 'xhtml' and financial_year is null
                        """,
                        (financial_year, company_number, document_id, period_type),
                    )
            if document_updates:
                updated += document_updates
                if not dry_run:
                    conn.commit()
            else:
                skipped += 1
            processed += 1
            if processed % 100 == 0 or processed == len(documents):
                print(
                    json.dumps(
                        {
                            "processed": processed,
                            "total": len(documents),
                            "rows_updated": updated,
                            "skipped": skipped,
                            "failed": failed,
                        }
                    ),
                    flush=True,
                )
    return {
        "documents_selected": len(documents),
        "documents_processed": processed,
        "rows_updated": updated,
        "documents_skipped": skipped,
        "documents_failed": failed,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite database to update.")
    parser.add_argument("--source", choices=("all", "xhtml", "vlm"), default="all")
    parser.add_argument("--rate", type=float, default=2.0, help="Maximum XHTML requests per second.")
    parser.add_argument("--limit", type=int, help="Optional XHTML document limit.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        result: dict[str, Any] = {"db": str(Path(args.db).resolve()), "dry_run": args.dry_run}
        if args.source in {"all", "vlm"}:
            result["vlm"] = backfill_vlm_years(conn, dry_run=args.dry_run)
        if args.source in {"all", "xhtml"}:
            extractor = CompaniesHouseExtractor(api_key=os.getenv("COMPANIES_HOUSE_API_KEY"))
            result["xhtml"] = backfill_xhtml_years(
                conn,
                extractor,
                rate=args.rate,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        print(json.dumps(result, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
