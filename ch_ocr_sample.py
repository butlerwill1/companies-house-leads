#!/usr/bin/env python3
"""OCR a sample of enriched companies with missing P&L data.

This selects companies already enriched through the XHTML/API path but still
missing turnover data, downloads their PDF accounts, runs the local OCR
extractor, and writes each result into SQLite immediately.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from companies_house_extractor import DOCUMENT_API_BASE, HttpClient, load_dotenv


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="OCR a sample of companies with missing P&L data.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--sample-size", type=int, default=20, help="Number of companies to OCR.")
    parser.add_argument("--min-score", type=int, default=70, help="Minimum lead score.")
    parser.add_argument("--pdf-dir", default="ocr-sample-pdfs", help="Directory to store sampled PDFs.")
    parser.add_argument("--json-dir", default="ocr-sample-json", help="Directory to store OCR JSON outputs.")
    args = parser.parse_args(argv)

    repo_dir = Path.cwd()
    load_dotenv(repo_dir / ".env")
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        print("ERROR: COMPANIES_HOUSE_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    pdf_dir = repo_dir / args.pdf_dir
    json_dir = repo_dir / args.json_dir
    pdf_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        candidates = select_candidates(conn, args.sample_size, args.min_score)
    finally:
        conn.close()

    print(f"Selected {len(candidates)} candidate companies for OCR sample.", file=sys.stderr)
    if not candidates:
        return 0

    client = HttpClient(api_key=api_key)

    for index, (company_number, company_name, document_id) in enumerate(candidates, start=1):
        slug = f"{company_number}-{document_id[:12]}"
        pdf_path = pdf_dir / f"{slug}.pdf"
        narrative_json = json_dir / f"{slug}-narrative.json"

        print(f"[{index}/{len(candidates)}] {company_number} {company_name}", file=sys.stderr)

        if not pdf_path.exists():
            content_url = f"{DOCUMENT_API_BASE}/document/{document_id}/content"
            pdf_bytes = client.get_bytes(content_url, headers={"Accept": "application/pdf"})
            pdf_path.write_bytes(pdf_bytes)

        subprocess.run(
            [
                sys.executable,
                str(repo_dir / "companies_house_pdf_narrative.py"),
                "--pdf",
                str(pdf_path),
                "--output-json",
                str(narrative_json),
                "--ocr-if-needed",
            ],
            check=True,
            cwd=repo_dir,
        )

        subprocess.run(
            [
                sys.executable,
                str(repo_dir / "companies_house_sqlite.py"),
                "--db",
                str(db_path),
                "--narrative-json",
                str(narrative_json),
                "--company-number",
                company_number,
                "--document-id",
                document_id,
            ],
            check=True,
            cwd=repo_dir,
        )

    print("OCR sample completed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
