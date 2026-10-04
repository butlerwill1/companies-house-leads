#!/usr/bin/env python3
"""Run the VLM financial pipeline over the PDF-only filings of a company list.

`ch_backfill_history.py` cannot read an accounts filing that exists only as a
PDF or scan, so it appends each one to ``logs/history-pdf-only-filings.jsonl``.
This script turns that list into financial history:

1. Pick the filings worth reading. Every filing carries two years (current and
   comparative), so in a run of consecutive scanned filings only every other one
   is needed. A filing is read only if the year its period ends in is not already
   covered by a row that has both turnover and profit after tax, from the XHTML
   tags, the text fallback, or an earlier VLM run; each one read covers its own
   year and the year before. ``--cover all`` reads every filing instead, which
   adds a cross-check between adjacent filings at about twice the cost.
2. Download the PDF from the Companies House document API.
3. Run ``process_pdf_vlm_financials`` (locate the statement pages, read them with
   a vision model, rationalise the rows to canonical metrics) and store the result
   with ``insert_vlm_financial_payload``: audit rows in
   ``vlm_financial_extraction_runs`` and ``vlm_financial_metrics``, and a
   ``financial_period_summaries`` row per period with ``data_source = 'vlm'``.

Model calls run in a few threads; database writes happen on the main thread. A
filing already read (a VLM run exists for its document id) is skipped, so a
stopped run resumes.

Usage:
    python -m scripts.vlm.history_vlm_batch --plan                       # count what would be read, no calls
    python -m scripts.vlm.history_vlm_batch --limit 3
    python -m scripts.vlm.history_vlm_batch --workers 6
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.companies_house_sqlite import insert_vlm_financial_payload  # noqa: E402
from scripts.vlm.companies_house_pdf_vlm_financials import (  # noqa: E402
    OpenRouterVlmModelClient,
    company_context_from_sqlite,
    process_pdf_vlm_financials,
)

PDF_LOG = Path("logs/history-pdf-only-filings.jsonl")
PDF_DIR = Path("data/raw/history-pdfs")
# The statement locator (a small, cheap model) sees page thumbnails. Shown a whole
# 30-page filing in one request it returned nothing, and with 4 to 12 pages it
# often returned one entry too many, which the pipeline rejects; with 2 or 3 pages
# it is reliable (checked on a 36-page filing). A filing is tried at each size in
# turn until one run does not end in `error`.
LOCATOR_BATCH_SIZES = (2, 3, 4)


def document_id(pdf_url: str) -> str:
    return pdf_url.rstrip("/").split("/")[-2]


def read_pdf_log(path: Path = PDF_LOG) -> list[dict[str, Any]]:
    """Every distinct PDF-only filing (the log may repeat one across restarts)."""
    seen: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("pdf_url") and entry.get("period_end") and entry.get("transaction_id"):
                seen[entry["transaction_id"]] = entry
    return list(seen.values())


def _year(period_end: str) -> int:
    return int(period_end[:4])


def covered_years(conn: sqlite3.Connection, company_number: str) -> set[int]:
    """Financial years already held with both turnover and profit after tax."""
    rows = conn.execute(
        "select distinct financial_year from financial_period_summaries "
        "where company_number=? and financial_year is not null and turnover is not null "
        "and profit_after_tax is not null", (company_number,)).fetchall()
    return {int(r[0]) for r in rows}


def plan_filings(entries: list[dict[str, Any]], covered: dict[str, set[int]], *, cover: str = "minimal",
                 done_documents: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Which filings to read. ``minimal``: newest first, read a filing only if its
    own year is not yet covered, then count it as covering that year and the one
    before. ``all``: every filing not already read."""
    chosen: list[dict[str, Any]] = []
    by_company: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_company.setdefault(entry["company_number"], []).append(entry)
    for company, filings in sorted(by_company.items()):
        years = set(covered.get(company, set()))
        for filing in sorted(filings, key=lambda f: f["period_end"], reverse=True):
            if document_id(filing["pdf_url"]) in done_documents:
                years.update({_year(filing["period_end"]), _year(filing["period_end"]) - 1})
                continue
            if cover == "minimal" and _year(filing["period_end"]) in years:
                continue
            chosen.append(filing)
            years.update({_year(filing["period_end"]), _year(filing["period_end"]) - 1})
    return chosen


def download_pdf(entry: dict[str, Any], api_key: str) -> Path | None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = PDF_DIR / f"{entry['company_number']}-{document_id(entry['pdf_url'])}.pdf"
    if path.is_file() and path.stat().st_size > 0:
        return path
    for attempt in range(4):
        try:
            response = requests.get(entry["pdf_url"], auth=(api_key, ""), headers={"Accept": "application/pdf"}, timeout=120)
        except requests.RequestException:
            time.sleep(10 * (attempt + 1))
            continue
        if response.status_code == 429:
            time.sleep(60)
            continue
        if response.status_code != 200:
            return None
        path.write_bytes(response.content)
        return path
    return None


def read_one(entry: dict[str, Any], ch_key: str, client: Any, db_path: Path, max_pages: int) -> dict[str, Any]:
    """Download and run the pipeline. Never raises: a failure is returned as an error record."""
    try:
        pdf = download_pdf(entry, ch_key)
        if pdf is None:
            return {"entry": entry, "error": "download failed"}
        context = company_context_from_sqlite(db_path, entry["company_number"], None)
        payload: dict[str, Any] = {}
        for batch_size in LOCATOR_BATCH_SIZES:
            payload = process_pdf_vlm_financials(pdf, client, max_pages=max_pages, company_context=context,
                                                 locator_batch_size=batch_size)
            if payload.get("status") != "error":
                break
        return {"entry": entry, "payload": payload}
    except Exception as exc:  # noqa: BLE001 -- one bad scan must not stop the batch
        return {"entry": entry, "error": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--cover", choices=("minimal", "all"), default="minimal")
    parser.add_argument("--plan", action="store_true", help="Count the filings that would be read; make no calls.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=60)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path(".env"))

    conn = sqlite3.connect(args.db, timeout=120)  # the backfill writes to the same file
    entries = read_pdf_log()
    covered = {c: covered_years(conn, c) for c in {e["company_number"] for e in entries}}
    # A filing is read once a run of it did not end in `error`, or after two runs
    # that did (the first pass used one locator batch size, the second tries 2, 3
    # and 4): a filing that fails like that is treated as unreadable, not retried
    # for ever.
    done = frozenset(r[0] for r in conn.execute(
        "select document_id from vlm_financial_extraction_runs where vision_model != 'xhtml_text' "
        "and document_id is not null group by document_id "
        "having sum(status != 'error') > 0 or count(*) >= 2"))
    plan = plan_filings(entries, covered, cover=args.cover, done_documents=done)
    print(f"{len(entries)} PDF-only filings logged; {len(plan)} to read ({args.cover} cover); "
          f"{len(done)} already read", file=sys.stderr)
    if args.plan:
        print(json.dumps({"logged": len(entries), "to_read": len(plan), "companies": len({p['company_number'] for p in plan})}))
        return 0
    plan = plan[: args.limit] if args.limit else plan
    client = OpenRouterVlmModelClient(os.environ["OPENROUTER_API_KEY"])
    ch_key = os.environ["COMPANIES_HOUSE_API_KEY"]
    totals = {"stored": 0, "error": 0, "metrics": 0, "usd": 0.0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(read_one, e, ch_key, client, Path(args.db), args.max_pages) for e in plan]
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            entry = result["entry"]
            if "payload" not in result:
                totals["error"] += 1
                print(f"  {entry['company_number']} {entry['period_end']}: {result['error']}", file=sys.stderr)
                continue
            payload = result["payload"]
            insert_vlm_financial_payload(conn, payload, entry["company_number"], document_id(entry["pdf_url"]))
            if payload.get("status") == "error":
                totals["error"] += 1  # audited, but the filing will be retried next pass
                print(f"  {entry['company_number']} {entry['period_end']}: pipeline status error", file=sys.stderr)
                continue
            totals["stored"] += 1
            totals["metrics"] += len(payload.get("metrics") or [])
            totals["usd"] += float((payload.get("cost") or {}).get("usd") or 0)
            if index % 10 == 0 or index == len(plan):
                print(f"  {index}/{len(plan)} {totals}", file=sys.stderr)
    print(json.dumps(totals, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
