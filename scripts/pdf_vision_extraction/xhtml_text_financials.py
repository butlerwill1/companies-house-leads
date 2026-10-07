#!/usr/bin/env python3
"""Recover turnover and profit after tax from the visible text of an XHTML filing.

An XHTML (iXBRL) filing sometimes shows its figures on the page without tagging
them in machine-readable form, so the tag parser (`parse_xhtml_accounts`) leaves
`turnover` or `profit_after_tax` empty. The numbers are still in the text. This
module is the standard way to get them back: no image rendering, one cheap text
model call per filing.

The process, one document at a time:

1. ``statement_windows`` cuts the filing's text down to the profit and loss
   statement(s), the turnover note, and the units line, with a flag for whether
   any profit and loss statement exists at all.
2. ``build_prompt`` asks a text model for the two periods' figures, each with the
   row's label and a verbatim quote, and for the unit (pounds, thousands, millions).
3. ``validate_recovery`` accepts a figure only if the quote is found word for
   word in the text sent, the number appears in the quote, the row label is a
   plausible turnover or after-tax-profit row (the same label rules the PDF
   pipeline uses), and the unit is known. Anything else is `rejected`, with the
   reason. A model that says there is no profit and loss statement, over text with
   none, yields `not_filed`: some small-company accounts omit it, and no method
   can recover a figure that was never filed.
4. ``store_recovery`` writes an audit row to ``vlm_financial_extraction_runs`` and one
   row per recovered figure to ``vlm_financial_metrics`` (quote, label, unit), and
   fills **only empty** turnover or profit cells of the matching
   ``financial_period_summaries`` rows. A value the XHTML tags already gave is
   never overwritten. Where each cell came from is recorded in the row's
   ``derived_payload.text_recovery``.

Never trusts the model's arithmetic: it only copies; the scaling to pounds is done
here with the PDF pipeline's own unit helpers.

Usage:
    python -m scripts.pdf_vision_extraction.xhtml_text_financials run --companies-file logs/search-screen/history-cohort.txt --limit 20
    python -m scripts.pdf_vision_extraction.xhtml_text_financials run --companies-file logs/search-screen/history-cohort.txt
    python -m scripts.pdf_vision_extraction.xhtml_text_financials eval --limit 40     # accuracy on filings whose tags DID give the figures
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from companies_house_core.companies_house_extractor import filed_report_text  # noqa: E402
from scripts.business_profile_classifier.business_profile_policy import parse_json_response, quote_match_kind  # noqa: E402
from scripts.pdf_vision_extraction.companies_house_pdf_vlm_financials import (  # noqa: E402
    currency_and_scale,
    normalise_unit,
    reported_value,
)
from scripts.pdf_vision_extraction.financial_metric_policy import canonical_metric_label_is_compatible  # noqa: E402

PROMPT_VERSION = "xhtml-text-financials-v1"
STAGE_MODEL = "xhtml_text"
DEFAULT_MODEL = "openai/gpt-5.4-mini"
METRICS = ("turnover", "profit_after_tax")
PERIODS = ("current", "previous")
MAX_TEXT_CHARS = 9000
XHTML_DIR = Path("data/raw/xhtml-accounts-for-text-recovery")
DOCUMENT_URL = "https://document-api.company-information.service.gov.uk/document/{id}/content"

_STATEMENT_HEADING = re.compile(
    r"^(?:group |consolidated |company )?(?:statement of comprehensive income|income statement|"
    r"(?:consolidated )?profit and loss account|statement of income|statement of profit or loss)\b", re.I)
_PL_FIRST_ROW = re.compile(r"^(?:turnover|revenue|sales|total income|income|gross profit|fees)\b", re.I)
_TURNOVER_NOTE = re.compile(r"^(?:turnover|revenue)\b.{0,40}\b(?:analys|class|activit|represent|geograph)", re.I)
_UNIT_LINE = re.compile(r"(?:£\s?'?000|£000|£\s?m\b|£'m|in thousands|in millions|\bthousands\b|\bmillions\b|\$|€|\bUSD\b|\bEUR\b)", re.I)
_TURNOVER_LABEL = re.compile(r"\b(?:turnover|revenue|sales|income|fees|takings)\b", re.I)
_NOT_TURNOVER_LABEL = re.compile(r"\b(?:gross profit|cost of|other (?:operating )?income|interest|investment income|"
                                 r"comprehensive income|total comprehensive|income tax|deferred)\b", re.I)
_PROFIT_LABEL = re.compile(r"\b(?:profit|loss|surplus|deficit|result|comprehensive income|earnings)\b", re.I)


# --- 1. the text sent to the model ------------------------------------------

def statement_windows(filed_text: str, *, max_chars: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    """(text for the model, whether a profit and loss statement was found).

    A window is the lines from a statement heading that is followed within
    thirty lines by a typical first row, so a contents-page mention does not
    count. A company's filing can hold a group and a company statement; each
    gets a window. A turnover-analysis note and the lines naming the unit are
    added, in document order, with a marker on each block.
    """
    lines = [line.strip() for line in filed_text.splitlines()]
    picked: dict[int, str] = {}
    found_statement = False

    def take(start: int, stop: int, label: str) -> None:
        picked.setdefault(start, f"[{label}]")
        for index in range(max(start, 0), min(stop, len(lines))):
            if lines[index]:
                picked.setdefault(index, lines[index])

    for index, line in enumerate(lines):
        if _STATEMENT_HEADING.match(line) and any(_PL_FIRST_ROW.match(x) for x in lines[index + 1:index + 31]):
            found_statement = True
            take(index - 6, index + 75, "profit and loss statement")
    for index, line in enumerate(lines):
        if _TURNOVER_NOTE.match(line):
            take(index, index + 22, "turnover note")
    unit_lines: list[str] = []
    for index, line in enumerate(lines):
        if _UNIT_LINE.search(line) and len(line) < 80 and line not in unit_lines and len(unit_lines) < 6:
            unit_lines.append(line)
            take(index, index + 1, "unit")
    ordered = [picked[i] for i in sorted(picked)]
    text = "\n".join(ordered)
    return (text[:max_chars], found_statement)


PROMPT_TEMPLATE = """You are reading text taken from a UK company's filed annual accounts. Find the \
profit and loss figures for the two financial periods shown and copy them exactly.

Return only JSON:
{{"unit": "GBP" | "GBP_THOUSANDS" | "GBP_MILLIONS" | "USD" | "EUR" | "UNKNOWN",
  "statement_scope": "company" | "group" | "unknown",
  "no_profit_and_loss": true | false,
  "periods": {{
    "current":  {{"period_end": "YYYY-MM-DD or null", "turnover": ROW, "profit_after_tax": ROW}},
    "previous": {{"period_end": "YYYY-MM-DD or null", "turnover": ROW, "profit_after_tax": ROW}}}}}}
where ROW is null or {{"label": "the row's label", "displayed": "the number exactly as printed, with any bracket or minus sign", "quote": "the label and its numbers exactly as they appear in the text"}}.

Rules:
- turnover is the top-line sales or revenue row (not gross profit, not other income).
- profit_after_tax is the profit or loss for the financial year after tax (the bottom line of the \
statement), not profit before tax and not an operating subtotal. A loss is negative.
- "current" is the most recent period (usually the first column), "previous" the one before.
- Copy numbers as printed. Do not scale or convert them; give the unit separately from the heading \
(for example "£'000" is GBP_THOUSANDS).
- If a figure is not in the text, use null for it. If the text has no profit and loss statement at \
all, set no_profit_and_loss to true. Never estimate or calculate a figure.
- When there is both a group and a company statement, use the group (consolidated) one.

Company: {company_name}

Text:
<<<
{text}
>>>"""


def build_prompt(company_name: str, text: str) -> str:
    return PROMPT_TEMPLATE.format(company_name=company_name or "(unknown)", text=text)


# --- 2. checking the answer --------------------------------------------------

def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def validate_row(metric: str, row: Any, text: str, unit: str) -> dict[str, Any]:
    """One figure's verdict: ``{"status": "recovered", ...value fields}`` or
    ``{"status": "rejected", "reason": ...}`` or ``{"status": "absent"}``."""
    if row is None:
        return {"status": "absent"}
    if not isinstance(row, dict):
        return {"status": "rejected", "reason": "row is not an object"}
    label, displayed, quote = (str(row.get(k) or "").strip() for k in ("label", "displayed", "quote"))
    if not (label and displayed and quote):
        return {"status": "rejected", "reason": "label, displayed value or quote missing"}
    if quote_match_kind(quote, text) is None:
        return {"status": "rejected", "reason": "quote not found in the text sent"}
    if not _digits(displayed) or _digits(displayed) not in _digits(quote):
        return {"status": "rejected", "reason": "displayed number not in the quote"}
    if metric == "turnover" and (not _TURNOVER_LABEL.search(label) or _NOT_TURNOVER_LABEL.search(label)):
        return {"status": "rejected", "reason": f"label is not a turnover row: {label!r}"}
    if metric == "profit_after_tax" and (not _PROFIT_LABEL.search(label)
                                         or not canonical_metric_label_is_compatible(metric, label)):
        return {"status": "rejected", "reason": f"label is not an after-tax profit row: {label!r}"}
    currency, scale = currency_and_scale(unit)
    if scale is None:
        return {"status": "rejected", "reason": "unit unknown"}
    value = reported_value(displayed, unit, metric)
    if value is None:
        return {"status": "rejected", "reason": "number could not be read"}
    return {"status": "recovered", "label": label, "displayed": displayed, "quote": quote, "unit": unit,
            "currency_code": currency, "scale_multiplier": scale, "reported_value": str(value)}


def validate_recovery(payload: dict[str, Any], text: str, found_statement: bool) -> dict[str, Any]:
    """The whole document's verdict: per period and metric, plus the overall
    ``status`` (``recovered`` if anything was, ``not_filed`` if the text has no
    profit and loss statement and the model agrees, else ``unresolved``)."""
    unit = normalise_unit(payload.get("unit"))
    periods: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        raw = (payload.get("periods") or {}).get(period) or {}
        periods[period] = {"period_end": raw.get("period_end"),
                           **{metric: validate_row(metric, raw.get(metric), text, unit) for metric in METRICS}}
    recovered = sum(1 for p in periods.values() for m in METRICS if p[m]["status"] == "recovered")
    if recovered:
        status = "recovered"
    elif not found_statement and bool(payload.get("no_profit_and_loss")):
        status = "not_filed"
    else:
        status = "unresolved"
    return {"status": status, "unit": unit, "statement_scope": payload.get("statement_scope"), "periods": periods}


# --- 3. one document, end to end ---------------------------------------------

ModelCall = Callable[[str], tuple[str, dict[str, Any]]]


def recover_from_text(call_model: ModelCall, company_name: str, xhtml_text: str) -> dict[str, Any]:
    """Run steps 1 to 3 on one filing's XHTML. Never raises on a bad model
    answer: that becomes status ``unresolved`` with the problem recorded."""
    filed = filed_report_text(xhtml_text)
    text, found_statement = statement_windows(filed)
    result: dict[str, Any] = {"prompt_version": PROMPT_VERSION, "found_statement": found_statement,
                              "text_chars": len(text), "usage": {}, "raw": None, "problem": None}
    if not text.strip():
        return {**result, "status": "unresolved", "problem": "no statement text found", "periods": {}, "unit": "UNKNOWN"}
    try:
        raw, usage = call_model(build_prompt(company_name, text))
        result.update(raw=raw, usage=usage)
        payload = parse_json_response(raw)
    except Exception as exc:  # noqa: BLE001 -- one bad filing must not stop a batch
        return {**result, "status": "unresolved", "problem": f"{type(exc).__name__}: {exc}", "periods": {}, "unit": "UNKNOWN"}
    return {**result, **validate_recovery(payload, text, found_statement)}


# --- 4. storing --------------------------------------------------------------

def _whole_pounds(value: str, currency: str | None) -> int | None:
    """The integer the summary's turnover/profit columns hold (pounds), or None
    when the figure is not GBP or has pence that the integer column cannot keep."""
    if currency != "GBP":
        return None
    amount = Decimal(value)
    return int(amount) if amount == amount.to_integral_value() else None


def store_recovery(conn: sqlite3.Connection, company_number: str, document_id: str, result: dict[str, Any],
                   model: str, cost_usd: float | None) -> dict[str, int]:
    """Audit rows plus fill-only updates. Returns counts of cells filled."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cursor = conn.execute(
        """insert into vlm_financial_extraction_runs (company_number, document_id, pdf_path, locator_model, vision_model,
               rationalisation_model, status, pages_scanned_payload, candidate_pages_payload, raw_extraction_payload,
               rationalisation_payload, usage_payload, pricing_payload, cost_usd, cost_method, created_at)
           values (?, ?, '', ?, ?, ?, ?, '[]', '[]', ?, ?, ?, '{}', ?, 'estimated', ?)""",
        (company_number, document_id, PROMPT_VERSION, STAGE_MODEL, model, result["status"],
         json.dumps({"raw": result.get("raw"), "problem": result.get("problem")}),
         json.dumps({k: result.get(k) for k in ("status", "unit", "statement_scope", "periods", "found_statement")},
                    default=str),
         json.dumps(result.get("usage") or {}), cost_usd, now))
    run_id = int(cursor.lastrowid)
    filled = {"turnover": 0, "profit_after_tax": 0}
    for period, entry in (result.get("periods") or {}).items():
        row = conn.execute(
            "select id, turnover, profit_after_tax, derived_payload, currency_code, financial_year from "
            "financial_period_summaries where company_number=? and document_id=? and period_type=?",
            (company_number, document_id, period)).fetchone()
        for metric in METRICS:
            verdict = entry.get(metric) or {}
            if verdict.get("status") != "recovered":
                continue
            pence = int(Decimal(verdict["reported_value"]) * 100) if verdict["currency_code"] == "GBP" else None
            conn.execute(
                """insert into vlm_financial_metrics (extraction_run_id, company_number, period_type, financial_year,
                       metric_name, value_pence, displayed_value, unit, currency_code, scale_multiplier, reported_value,
                       source_label, evidence_text, vision_model, rationalisation_model, validation_payload)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, company_number, period, row["financial_year"] if row else None, metric, pence,
                 verdict["displayed"], verdict["unit"], verdict["currency_code"], verdict["scale_multiplier"],
                 verdict["reported_value"], verdict["label"], verdict["quote"], STAGE_MODEL, model,
                 json.dumps({"stage": "xhtml_text", "prompt_version": PROMPT_VERSION})))
            if row is None or row[metric] is not None:
                continue  # a value the tags already gave is never overwritten
            amount = _whole_pounds(verdict["reported_value"], verdict["currency_code"])
            if amount is None:
                continue
            derived = json.loads(row["derived_payload"] or "{}")
            derived.setdefault("text_recovery", {})[metric] = {"run_id": run_id, "prompt_version": PROMPT_VERSION}
            conn.execute(
                f"update financial_period_summaries set {metric}=?, {metric}_reported_value=?, derived_payload=?, "
                "currency_code=coalesce(currency_code, ?) where id=?",
                (amount, verdict["reported_value"], json.dumps(derived), verdict["currency_code"], row["id"]))
            filled[metric] += 1
    conn.commit()
    return filled


# --- 5. choosing documents, fetching text, the CLI ----------------------------

def documents_needing_recovery(conn: sqlite3.Connection, companies: list[str]) -> list[sqlite3.Row]:
    """XHTML documents of these companies with an empty turnover or profit cell
    that this prompt version has not already tried."""
    marks = ",".join("?" * len(companies))
    return conn.execute(
        f"""select f.company_number, f.document_id, co.company_name, d.xhtml_url
            from financial_period_summaries f
            join documents d on d.document_id = f.document_id
            join companies co on co.company_number = f.company_number
            where f.company_number in ({marks}) and d.xhtml_url is not null and d.xhtml_url != ''
              and (f.turnover is null or f.profit_after_tax is null)
              and not exists (select 1 from vlm_financial_extraction_runs r where r.document_id = f.document_id
                              and r.vision_model = ? and r.locator_model = ?)
            group by f.company_number, f.document_id order by f.company_number, f.document_id""",
        (*companies, STAGE_MODEL, PROMPT_VERSION)).fetchall()


LOCAL_FILING_DIRS = (Path("data/raw/search-screen-filings"), Path("data/raw/business-profile-filed-reports"))


def local_xhtml(conn: sqlite3.Connection, company_number: str, document_id: str) -> str | None:
    """A filing already on disk. The screen and gold-set downloads are named by
    company number and hold the filing the stored narrative came from, so they
    count only for that document."""
    path = XHTML_DIR / f"{document_id}.xhtml"
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    row = conn.execute("select document_id from narrative_runs where company_number=? order by id desc limit 1",
                       (company_number,)).fetchone()
    if row and row[0] == document_id:
        for directory in LOCAL_FILING_DIRS:
            candidate = directory / f"{company_number}.xhtml"
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
    return None


def fetch_xhtml(document_id: str, api_key: str, conn: sqlite3.Connection | None = None,
                company_number: str | None = None) -> str | None:
    if conn is not None and company_number:
        local = local_xhtml(conn, company_number, document_id)
        if local is not None:
            return local
    path = XHTML_DIR / f"{document_id}.xhtml"
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    for attempt in range(4):
        try:
            response = requests.get(DOCUMENT_URL.format(id=document_id), auth=(api_key, ""),
                                    headers={"Accept": "application/xhtml+xml"}, timeout=60)
        except requests.RequestException:
            time.sleep(10 * (attempt + 1))
            continue
        if response.status_code == 429:
            time.sleep(60)
            continue
        if response.status_code != 200:
            return None
        XHTML_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(response.text, encoding="utf-8")
        time.sleep(0.6)
        return response.text
    return None


def _model_call(api_key: str, model: str) -> ModelCall:
    from scripts.search_screen_classifier.search_screen_eval import call_model

    return lambda prompt: call_model(api_key, model, prompt, 180)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--companies-file", required=True)
    runner.add_argument("--limit", type=int)
    evaluator = commands.add_parser("eval", help="Accuracy on filings whose tags did give both figures.")
    evaluator.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path(".env"))
    conn = sqlite3.connect(args.db, timeout=120)  # other pipeline stages write to the same file
    conn.row_factory = sqlite3.Row
    ch_key, router_key = os.environ["COMPANIES_HOUSE_API_KEY"], os.environ["OPENROUTER_API_KEY"]
    call = _model_call(router_key, args.model)
    from scripts.search_screen_classifier.search_screen_eval import model_prices
    prices = model_prices(args.model)

    def cost_of(usage: dict[str, Any]) -> float | None:
        return (int(usage.get("prompt_tokens") or 0) * prices[0] + int(usage.get("completion_tokens") or 0) * prices[1]) \
            if prices else None

    if args.command == "run":
        companies = [x.split("#")[0].strip() for x in Path(args.companies_file).read_text(encoding="utf-8").splitlines()
                     if x.split("#")[0].strip()]
        todo = documents_needing_recovery(conn, companies)
        todo = todo[: args.limit] if args.limit else todo
        print(f"{len(todo)} documents to read", file=sys.stderr)
        tally: dict[str, int] = {}
        filled = {"turnover": 0, "profit_after_tax": 0}
        spent = 0.0
        for index, doc in enumerate(todo, 1):
            xhtml = fetch_xhtml(doc["document_id"], ch_key, conn, doc["company_number"])
            if xhtml is None:
                tally["fetch_failed"] = tally.get("fetch_failed", 0) + 1
                continue
            result = recover_from_text(call, doc["company_name"], xhtml)
            cost = cost_of(result.get("usage") or {})
            spent += cost or 0
            counts = store_recovery(conn, doc["company_number"], doc["document_id"], result, args.model, cost)
            tally[result["status"]] = tally.get(result["status"], 0) + 1
            for key, value in counts.items():
                filled[key] += value
            if index % 25 == 0 or index == len(todo):
                print(f"  {index}/{len(todo)} {tally} filled {filled} ${spent:.2f}", file=sys.stderr)
        print(json.dumps({"documents": len(todo), "status": tally, "cells_filled": filled, "cost_usd": round(spent, 3)}, indent=2))
    else:
        rows = conn.execute(
            """select f.company_number, f.document_id, co.company_name,
                      max(case when f.period_type='current' then f.turnover end) ct,
                      max(case when f.period_type='previous' then f.turnover end) pt,
                      max(case when f.period_type='current' then f.profit_after_tax end) cp,
                      max(case when f.period_type='previous' then f.profit_after_tax end) pp
               from financial_period_summaries f join companies co on co.company_number=f.company_number
               join documents d on d.document_id=f.document_id
               where f.data_source='xhtml' and d.xhtml_url is not null and d.xhtml_url!=''
               group by f.company_number, f.document_id
               having ct is not null and pt is not null and cp is not null and pp is not null
               order by random() limit ?""", (args.limit,)).fetchall()
        exact = total = unresolved = 0
        categories = {"exact": 0, "tag_scale_error": 0, "model_absent": 0, "rejected": 0, "different_value": 0}
        misses: list[str] = []
        for doc in rows:
            xhtml = local_xhtml(conn, doc["company_number"], doc["document_id"])  # offline: no extra API load
            if xhtml is None:
                continue
            result = recover_from_text(call, doc["company_name"], xhtml)
            if result["status"] != "recovered":
                unresolved += 1
            truth = {("current", "turnover"): doc["ct"], ("previous", "turnover"): doc["pt"],
                     ("current", "profit_after_tax"): doc["cp"], ("previous", "profit_after_tax"): doc["pp"]}
            for (period, metric), expected in truth.items():
                verdict = ((result.get("periods") or {}).get(period) or {}).get(metric) or {}
                total += 1
                got = _whole_pounds(verdict["reported_value"], verdict["currency_code"]) if verdict.get("status") == "recovered" else None
                if got == expected:
                    exact += 1
                    categories["exact"] += 1
                else:
                    if got is not None and expected and got in (expected * 1000, expected // 1000 if expected % 1000 == 0 else -1):
                        # A figure exactly 1,000 times the tag: the tag took a thousands-scale row (a KPI table or a
                        # £'000 statement) without scaling. The filing text is right and the stored tag is wrong.
                        categories["tag_scale_error"] += 1
                    elif verdict.get("status") == "absent":
                        categories["model_absent"] += 1
                    elif verdict.get("status") == "rejected":
                        categories["rejected"] += 1
                    else:
                        categories["different_value"] += 1
                    misses.append(f"{doc['company_number']} {period} {metric}: expected {expected}, got {got} ({verdict.get('status')}: {verdict.get('reason', '')})")
        print(json.dumps({"filings": len(rows), "figures": total, "exact": exact,
                          "exact_rate": round(exact / total, 3) if total else None, "filings_unresolved": unresolved,
                          "categories": categories}, indent=2))
        for miss in misses[:25]:
            print(" ", miss)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
