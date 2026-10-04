#!/usr/bin/env python3
"""Repair turnover, gross profit and operating result stored with the wrong scale.

Until 2026-10-02 the XHTML parser read a printed P&L row without applying the unit
in its column header, and let that row override the tag. A small company's
untagged P&L in £'000 was stored 1,000 times too small, and a KPI table in
£'000 could stand in for the statement. The parser is fixed
(`core.companies_house_extractor`: `display_scale`, `prefer_exact`); this script
re-reads each stored filing with the fixed parser and corrects the rows.

For every XHTML document of the chosen companies it fetches the filing (reusing a
copy on disk where there is one), re-runs `parse_xhtml_accounts`, and compares
turnover, gross profit and operating result with the stored `current` and
`previous` rows. Only a row whose `data_source` is `xhtml` is touched, and only
those three metrics (plus the derived ratios stored beside them). A value the
text-recovery tool filled in is left alone. Every change is appended to
``logs/financial-scale-repair.jsonl`` with its old and new value; a document that
was checked is appended to ``logs/financial-scale-checked.txt`` so a stopped run
resumes without refetching.

Documents of companies with a suspiciously small turnover go first.

Usage:
    python -m scripts.enrichment.reparse_financial_scale --companies-file logs/search-screen/history-cohort.txt --plan
    python -m scripts.enrichment.reparse_financial_scale --companies-file logs/search-screen/history-cohort.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.companies_house_extractor import CompaniesHouseExtractor  # noqa: E402
from scripts.vlm.xhtml_text_financials import fetch_xhtml  # noqa: E402

METRICS = ("turnover", "gross_profit", "operating_result")
CHANGE_LOG = Path("logs/financial-scale-repair.jsonl")
CHECKED_LOG = Path("logs/financial-scale-checked.txt")
SUSPICIOUS_TURNOVER = 100_000


def documents_to_check(conn: sqlite3.Connection, companies: list[str], checked: set[str]) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(companies))
    rows = conn.execute(
        f"""select f.company_number, f.document_id,
                   min(case when f.turnover is not null and f.turnover < {SUSPICIOUS_TURNOVER} then 0 else 1 end) as clean
            from financial_period_summaries f join documents d on d.document_id = f.document_id
            where f.company_number in ({marks}) and f.data_source = 'xhtml'
              and d.xhtml_url is not null and d.xhtml_url != ''
            group by f.company_number, f.document_id""", companies).fetchall()
    suspicious_companies = {r["company_number"] for r in rows if r["clean"] == 0}
    todo = [r for r in rows if r["document_id"] not in checked]
    todo.sort(key=lambda r: (r["company_number"] not in suspicious_companies, r["company_number"], r["document_id"]))
    return todo


def repair_document(conn: sqlite3.Connection, extractor: CompaniesHouseExtractor, company_number: str,
                    document_id: str, xhtml: str) -> list[dict[str, Any]]:
    """Re-parse one filing and fix its stored rows. Returns the changes made."""
    parsed = extractor.parse_xhtml_accounts(xhtml)
    years = parsed["years"]
    changes: list[dict[str, Any]] = []
    for period_type in ("current", "previous"):
        row = conn.execute(
            "select id, turnover, gross_profit, operating_result, derived_payload from financial_period_summaries "
            "where company_number=? and document_id=? and period_type=? and data_source='xhtml'",
            (company_number, document_id, period_type)).fetchone()
        if row is None:
            continue
        derived = json.loads(row["derived_payload"]) if row["derived_payload"] else {}
        recovered = set((derived.get("text_recovery") or {}))
        for metric in METRICS:
            new = (years.get(period_type) or {}).get(metric)
            old = row[metric]
            if new is None or new == old or metric in recovered:
                continue
            conn.execute(f"update financial_period_summaries set {metric}=?, {metric}_reported_value=? where id=?",
                         (new, str(new), row["id"]))
            changes.append({"company_number": company_number, "document_id": document_id, "period_type": period_type,
                            "metric": metric, "old": old, "new": new})
    if changes:
        # The ratios stored beside the figures (margins, change percentages) were
        # computed from the old numbers; refresh them from the corrected parse,
        # keeping the note of anything the text-recovery tool filled in.
        for row in conn.execute("select id, derived_payload from financial_period_summaries where company_number=? "
                                "and document_id=? and data_source='xhtml'", (company_number, document_id)).fetchall():
            old_derived = json.loads(row["derived_payload"]) if row["derived_payload"] else {}
            new_derived = dict(parsed["derived"])
            if "text_recovery" in old_derived:
                new_derived["text_recovery"] = old_derived["text_recovery"]
            conn.execute("update financial_period_summaries set derived_payload=? where id=?",
                         (json.dumps(new_derived), row["id"]))
    conn.commit()
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--companies-file", required=True)
    parser.add_argument("--plan", action="store_true", help="Count the documents to check; fetch nothing.")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path(".env"))

    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    companies = [x.split("#")[0].strip() for x in Path(args.companies_file).read_text(encoding="utf-8").splitlines()
                 if x.split("#")[0].strip()]
    checked = set(CHECKED_LOG.read_text(encoding="utf-8").split()) if CHECKED_LOG.is_file() else set()
    todo = documents_to_check(conn, companies, checked)
    todo = todo[: args.limit] if args.limit else todo
    suspicious = sum(1 for r in todo if r["clean"] == 0)
    print(f"{len(todo)} documents to check ({suspicious} from companies with a suspiciously small turnover); "
          f"{len(checked)} already checked", file=sys.stderr)
    if args.plan:
        return 0
    api_key = os.environ["COMPANIES_HOUSE_API_KEY"]
    extractor = CompaniesHouseExtractor(api_key=None)
    CHANGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    changed_rows = changed_docs = failed = 0
    started = time.monotonic()
    for index, doc in enumerate(todo, 1):
        xhtml = fetch_xhtml(doc["document_id"], api_key, conn, doc["company_number"])
        if xhtml is None:
            failed += 1
            continue
        try:
            changes = repair_document(conn, extractor, doc["company_number"], doc["document_id"], xhtml)
        except Exception as exc:  # noqa: BLE001 -- a filing the parser cannot read must not stop the run
            print(f"  {doc['company_number']} {doc['document_id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if changes:
            changed_docs += 1
            changed_rows += len(changes)
            with CHANGE_LOG.open("a", encoding="utf-8") as handle:
                for change in changes:
                    handle.write(json.dumps(change) + "\n")
        with CHECKED_LOG.open("a", encoding="utf-8") as handle:
            handle.write(doc["document_id"] + "\n")
        if index % 50 == 0 or index == len(todo):
            rate = index / (time.monotonic() - started)
            print(f"  {index}/{len(todo)} changed {changed_rows} values in {changed_docs} documents, "
                  f"{failed} failed, ETA {int((len(todo) - index) / rate / 60)} min", file=sys.stderr)
    print(json.dumps({"checked": len(todo), "documents_changed": changed_docs, "values_changed": changed_rows,
                      "failed": failed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
