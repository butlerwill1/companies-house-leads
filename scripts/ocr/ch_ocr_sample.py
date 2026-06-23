#!/usr/bin/env python3
"""OCR a sample of companies — two modes.

Default: selects companies already enriched through the XHTML/API path but still
missing turnover data, downloads their PDF accounts, runs the local OCR
extractor, and writes each result into SQLite immediately.

--no-xhtml: selects companies where no XHTML was available (status=no_xhtml),
looks up their PDF via the CH API, runs full OCR for both qualitative narrative
sections and quantitative financial data, and writes each result into SQLite.

Usage:
    python -m scripts.ocr.ch_ocr_sample --db companies-house.db --sample-size 20
    python -m scripts.ocr.ch_ocr_sample --db companies-house.db --no-xhtml --sample-size 1000
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path

from companies_house_extractor import (
    DOCUMENT_API_BASE,
    CompaniesHouseExtractor,
    HttpClient,
    load_dotenv,
    pick_latest_accounts_filing,
)
from companies_house_pdf_full import process_pdf
from companies_house_sqlite import insert_narrative_payload


class RateLimiter:
    """Leaky-bucket: max `rate` calls per `period` seconds."""

    def __init__(self, rate: int = 10, period: float = 5.0) -> None:
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
                time.sleep(sleep_for + 0.01)


def select_candidates(conn: sqlite3.Connection, sample_size: int, min_score: int) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        """
        select distinct
            l.company_number,
            l.company_name,
            fps.document_id
        from leads l
        join financial_period_summaries fps
            on fps.company_number = l.company_number
           and fps.period_type = 'current'
        left join narrative_runs nr
            on nr.company_number = l.company_number
        where l.status = 'done'
          and l.lead_score >= ?
          and fps.turnover is null
          and fps.document_id is not null
          and nr.company_number is null
        order by l.lead_score desc, l.company_number
        limit ?
        """,
        (min_score, sample_size),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def select_no_xhtml_candidates(conn: sqlite3.Connection, sample_size: int, min_score: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        select l.company_number, l.company_name
        from leads l
        left join narrative_runs nr on nr.company_number = l.company_number
        where l.status = 'no_xhtml'
          and l.lead_score >= ?
          and nr.company_number is null
        order by l.lead_score desc, l.company_number
        limit ?
        """,
        (min_score, sample_size),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="OCR a sample of companies.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--sample-size", type=int, default=20, help="Number of companies to process.")
    parser.add_argument("--min-score", type=int, default=70, help="Minimum lead score.")
    parser.add_argument("--pdf-dir", default="ocr-sample-pdfs", help="Directory to store PDFs.")
    parser.add_argument(
        "--no-xhtml",
        action="store_true",
        help="Process no_xhtml companies: look up PDFs via API and run full OCR.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=30,
        help="Page cap for --no-xhtml OCR (default 30; covers directors' report and financial statements).",
    )
    args = parser.parse_args(argv)

    repo_dir = Path.cwd()
    load_dotenv(repo_dir / ".env")
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        print("ERROR: COMPANIES_HOUSE_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    pdf_dir = repo_dir / args.pdf_dir
    pdf_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        if args.no_xhtml:
            candidates_nox = select_no_xhtml_candidates(conn, args.sample_size, args.min_score)
            print(f"Selected {len(candidates_nox)} no_xhtml candidates for OCR.", file=sys.stderr)
            if not candidates_nox:
                return 0

            extractor = CompaniesHouseExtractor(api_key=api_key)
            client = HttpClient(api_key=api_key)
            rl = RateLimiter(rate=10, period=5.0)
            errors = 0

            for index, (company_number, company_name) in enumerate(candidates_nox, start=1):
                print(f"[{index}/{len(candidates_nox)}] {company_number} {company_name}", file=sys.stderr)
                try:
                    rl.wait()
                    filings = extractor.get_accounts_filings(company_number)
                    filing = pick_latest_accounts_filing(filings)
                    if not filing:
                        print("  No accounts filing found, skipping.", file=sys.stderr)
                        continue

                    rl.wait()
                    links = extractor.get_document_urls(company_number, filing)
                    pdf_url = links.get("pdf")
                    if not pdf_url:
                        print("  No PDF resource available, skipping.", file=sys.stderr)
                        continue

                    document_id = pdf_url.split("/document/")[1].split("/")[0]
                    slug = f"{company_number}-{document_id[:12]}"
                    pdf_path = pdf_dir / f"{slug}.pdf"

                    if not pdf_path.exists():
                        pdf_bytes = client.get_bytes(pdf_url, headers={"Accept": "application/pdf"})
                        pdf_path.write_bytes(pdf_bytes)

                    payload = process_pdf(pdf_path, ocr_if_needed=True, max_pages=args.max_pages)
                    print(f"  text_source={payload['text_source']}", file=sys.stderr)

                    narrative_run_id = insert_narrative_payload(conn, payload, company_number, document_id)
                    conn.commit()
                    print(f"  narrative_run_id={narrative_run_id}", file=sys.stderr)

                except Exception as exc:
                    errors += 1
                    print(f"  ERROR: {exc}", file=sys.stderr)

            print(f"no_xhtml OCR complete: processed={len(candidates_nox) - errors}  errors={errors}", file=sys.stderr)
            return 0 if errors == 0 else 1

        # --- default mode: done companies missing turnover ---
        candidates = select_candidates(conn, args.sample_size, args.min_score)
        print(f"Selected {len(candidates)} candidate companies for OCR sample.", file=sys.stderr)
        if not candidates:
            return 0

        client = HttpClient(api_key=api_key)

        for index, (company_number, company_name, document_id) in enumerate(candidates, start=1):
            slug = f"{company_number}-{document_id[:12]}"
            pdf_path = pdf_dir / f"{slug}.pdf"
            print(f"[{index}/{len(candidates)}] {company_number} {company_name}", file=sys.stderr)

            if not pdf_path.exists():
                content_url = f"{DOCUMENT_API_BASE}/document/{document_id}/content"
                pdf_bytes = client.get_bytes(content_url, headers={"Accept": "application/pdf"})
                pdf_path.write_bytes(pdf_bytes)

            payload = process_pdf(pdf_path, ocr_if_needed=True)
            print(f"  text_source={payload['text_source']}", file=sys.stderr)

            narrative_run_id = insert_narrative_payload(conn, payload, company_number, document_id)
            conn.commit()
            print(f"  narrative_run_id={narrative_run_id}", file=sys.stderr)

        print("OCR sample completed.", file=sys.stderr)
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
