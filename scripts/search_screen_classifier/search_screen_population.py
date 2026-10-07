#!/usr/bin/env python3
"""Run the search screen over the whole target population and store the result.

Three steps, each resumable:

  fetch    Save the XHTML filing of every population company that is not on disk
           yet (free Companies House document API, throttled below its limit).
  screen   Run the current prompt over the short extract of each filing. Every
           response is checkpointed as it returns (search_screen_eval's
           checkpoint), so an interrupted run loses nothing and a re-run replays.
  store    Write the results to the `company_search_screen` table in SQLite.
           A company with no XHTML filing (PDF-only, missing) gets a row that
           passes, with the reason in `problem`: the screen fails open.

`log-langfuse` replays the finished run into Langfuse as one dataset run over
the population (no model calls).

Usage:
    python -m scripts.search_screen_classifier.search_screen_population fetch
    python -m scripts.search_screen_classifier.search_screen_population screen --limit 20
    python -m scripts.search_screen_classifier.search_screen_population screen
    python -m scripts.search_screen_classifier.search_screen_population store
    python -m scripts.search_screen_classifier.search_screen_population log-langfuse
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

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from companies_house_core.companies_house_extractor import filed_report_text  # noqa: E402
from scripts.business_profile_classifier.companies_house_business_profile import fetch_narrative_context  # noqa: E402
from scripts.search_screen_classifier import search_screen_eval as ev  # noqa: E402
from scripts.search_screen_classifier.search_screen_cases import RAW_DIR, target_population  # noqa: E402
from scripts.search_screen_classifier.search_screen_policy import PROMPT_VERSION  # noqa: E402

POP_DIR = Path("data/raw/search-screen-filings")
DOCUMENT_URL = "https://document-api.company-information.service.gov.uk/document/{id}/content"
FETCH_LOG = Path("logs/search-screen/population-fetch.jsonl")
THROTTLE_SECONDS = 0.6  # Companies House allows 600 requests per 5 minutes
DB_DEFAULT = "companies-house.db"
POPULATION_DATASET = "search-screen-population"
POPULATION_RUN_ID = 1


def xhtml_path(number: str) -> Path | None:
    for directory in (RAW_DIR, POP_DIR):
        path = directory / f"{number}.xhtml"
        if path.is_file():
            return path
    return None


def _document(conn: sqlite3.Connection, number: str) -> sqlite3.Row | None:
    """The filing the stored narrative came from, else the newest XHTML one."""
    row = conn.execute(
        "select d.document_id, d.xhtml_url from documents d where d.document_id = "
        "(select document_id from narrative_runs where company_number = ? order by id desc limit 1)",
        (number,),
    ).fetchone()
    return row


def fetch(conn: sqlite3.Connection, numbers: list[str], api_key: str) -> dict[str, int]:
    POP_DIR.mkdir(parents=True, exist_ok=True)
    FETCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    if FETCH_LOG.is_file():
        for line in FETCH_LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                seen[rec["company_number"]] = rec["status"]
            except (json.JSONDecodeError, KeyError):
                continue
    counts: dict[str, int] = {}
    todo = [n for n in numbers if xhtml_path(n) is None and seen.get(n) not in ("no_xhtml", "no_document")]
    print(f"{len(numbers) - len(todo)} filings already on disk or settled, {len(todo)} to fetch", file=sys.stderr)
    for index, number in enumerate(todo, 1):
        row = _document(conn, number)
        if row is None:
            status = "no_document"
        elif not row["xhtml_url"]:
            status = "no_xhtml"
        else:
            status = "error"
            for attempt in range(4):
                try:
                    response = requests.get(DOCUMENT_URL.format(id=row["document_id"]), auth=(api_key, ""),
                                            headers={"Accept": "application/xhtml+xml"}, timeout=60)
                except requests.RequestException:
                    time.sleep(10 * (attempt + 1))  # a reset connection must not end a 2,000-filing run
                    continue
                if response.status_code == 429:
                    time.sleep(60)
                    continue
                status = "ok" if response.status_code == 200 else f"http_{response.status_code}"
                if status == "ok":
                    (POP_DIR / f"{number}.xhtml").write_text(response.text, encoding="utf-8")
                break
            time.sleep(THROTTLE_SECONDS)
        counts[status] = counts.get(status, 0) + 1
        with FETCH_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"company_number": number, "status": status}) + "\n")
        if index % 100 == 0 or index == len(todo):
            print(f"  fetched {index}/{len(todo)} {counts}", file=sys.stderr)
    return counts


def build_case(conn: sqlite3.Connection, number: str) -> dict[str, Any] | None:
    """A minimal case: name, SIC and the whole filed report minus the auditor's
    report, from the archived XHTML. None when there is no XHTML filing."""
    path = xhtml_path(number)
    if path is None:
        return None
    context = fetch_narrative_context(conn, number)
    if context is None:
        return None
    return {"company_number": number, "company_name": context["company_name"], "sic_label": context["sic_label"],
            "sections": {"filed_report": filed_report_text(path.read_text(encoding="utf-8", errors="replace"))},
            "raw_dir": str(path.parent)}


def screen(conn: sqlite3.Connection, numbers: list[str], model: str, *, workers: int) -> dict[str, dict[str, Any]]:
    cases = []
    missing = []
    for number in numbers:
        case = build_case(conn, number)
        (cases if case else missing).append(case or number)
    print(f"screening {len(cases)} filings; {len(missing)} have no XHTML filing (they fail open)", file=sys.stderr)
    records: dict[str, dict[str, Any]] = {}
    batch = 200
    for start in range(0, len(cases), batch):
        chunk = cases[start:start + batch]
        for record in ev.run("short", model, chunk, workers=workers, timeout=180, version=PROMPT_VERSION,
                             run_id=POPULATION_RUN_ID):
            records[record["company_number"]] = record
    return records


def document_ids(conn: sqlite3.Connection, numbers: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for number in numbers:
        row = _document(conn, number)
        out[number] = row["document_id"] if row else None
    return out


def store(conn: sqlite3.Connection, numbers: list[str], model: str) -> dict[str, int]:
    done = ev.load_checkpoint()
    docs = document_ids(conn, numbers)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    written = {"screened": 0, "no_filing": 0}
    for number in numbers:
        record = done.get(ev._key(model, "short", number, PROMPT_VERSION, POPULATION_RUN_ID))
        if record is None:
            problem, answer, passes, extra = "no XHTML filing to screen; passed (fails open)", None, 1, {}
            written["no_filing"] += 1
        else:
            answer, passes, problem = record.get("answer"), 1 if record.get("passes") else 0, record.get("problem")
            usage = record.get("usage") or {}
            extra = {"quote": record.get("quote"), "quote_valid": None if record.get("quote_ok") is None
                     else int(bool(record["quote_ok"])), "reason": record.get("reason"),
                     "text_chars": record.get("text_chars"), "prompt_tokens": usage.get("prompt_tokens"),
                     "completion_tokens": usage.get("completion_tokens")}
            written["screened"] += 1
        conn.execute(
            """
            insert into company_search_screen (
                company_number, prompt_version, model, input_kind, answer, passes, quote, quote_valid, reason,
                problem, document_id, text_chars, prompt_tokens, completion_tokens, screened_at
            ) values (?, ?, ?, 'short', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(company_number, prompt_version, model, input_kind) do update set
                answer=excluded.answer, passes=excluded.passes, quote=excluded.quote,
                quote_valid=excluded.quote_valid, reason=excluded.reason, problem=excluded.problem,
                document_id=excluded.document_id, text_chars=excluded.text_chars,
                prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens,
                screened_at=excluded.screened_at
            """,
            (number, PROMPT_VERSION, model, answer, passes, extra.get("quote"), extra.get("quote_valid"),
             extra.get("reason"), problem, docs.get(number), extra.get("text_chars"), extra.get("prompt_tokens"),
             extra.get("completion_tokens"), now),
        )
    conn.commit()
    return written


def summarise(conn: sqlite3.Connection, model: str) -> dict[str, Any]:
    rows = conn.execute(
        "select answer, passes, count(*) from company_search_screen where prompt_version=? and model=? "
        "group by answer, passes", (PROMPT_VERSION, model)).fetchall()
    total = sum(n for *_, n in rows)
    removed = sum(n for _, passes, n in rows if not passes)
    return {"companies": total, "removed": removed, "removed_share": round(removed / total, 3) if total else None,
            "by_answer": {str(a): n for a, _, n in rows}}


def log_to_langfuse(conn: sqlite3.Connection, numbers: list[str], model: str) -> str | None:
    """One Langfuse dataset run over the population, replayed from the saved
    responses. Items have no expected output: there is no gold for the population."""
    from datetime import UTC, datetime

    from scripts.langfuse_eval_helpers.langfuse_runs import (
        dataset_digest, evaluation, experiment_run_name, run_experiment, sync_dataset)
    from scripts.langfuse_eval_helpers.langfuse_tracing import flush, langfuse_from_config, observation
    from scripts.search_screen_classifier.search_screen_prompt_registry import prompt_reference
    from scripts.search_screen_classifier.search_screen_publish import LANGFUSE_CONFIG

    lf = langfuse_from_config(LANGFUSE_CONFIG)
    if lf is None:
        return None
    done = ev.load_checkpoint()
    names = {n: fetch_narrative_context(conn, n) for n in numbers}
    items = [{"id": f"{POPULATION_DATASET}:{n}",
              "input": {"company_name": (names[n] or {}).get("company_name"), "sic_label": (names[n] or {}).get("sic_label")},
              "metadata": {"company_number": n, "company_name": (names[n] or {}).get("company_name")}}
             for n in numbers]
    sync_dataset(lf, POPULATION_DATASET, items, digest=dataset_digest(items),
                 description="Search-screen target population: enriched companies with turnover and profit figures. No expected labels.")
    reference = prompt_reference(lf)

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        number = item.metadata["company_number"]
        record = done.get(ev._key(model, "short", number, PROMPT_VERSION, POPULATION_RUN_ID)) or {
            "answer": None, "passes": True, "problem": "no XHTML filing to screen; passed (fails open)"}
        with observation(lf, name="search_screen", as_type="generation", model=model,
                         input={"company_number": number},
                         output={"raw_response": record.get("raw"), "answer": record.get("answer"),
                                 "passes": record.get("passes"), "problem": record.get("problem")}):
            pass
        flush(lf)
        return record

    def evaluate(*, output: dict[str, Any], **_: Any) -> list[Any]:
        return [evaluation("passes", 1.0 if output.get("passes") else 0.0, data_type="NUMERIC",
                           comment=f"answer={output.get('answer')}")]

    def aggregate(*, item_results: list[Any], **_: Any) -> list[Any]:
        return [evaluation("removal_rate", summarise(conn, model)["removed_share"] or 0.0, data_type="NUMERIC")]

    run_name = experiment_run_name(model=model, when=datetime.now(UTC), label=f"{PROMPT_VERSION} population")
    result = run_experiment(lf, dataset_name=POPULATION_DATASET, run_name=run_name, task=task, evaluators=[evaluate],
                            run_evaluators=[aggregate], description=f"{model} @ {PROMPT_VERSION}, whole population",
                            metadata={"prompt_version": PROMPT_VERSION, "prompt": reference or "", "model": model,
                                      "input": "short", "run_id": str(POPULATION_RUN_ID)})
    flush(lf)
    ev.record_run({"dataset": POPULATION_DATASET, "run_name": run_name, "url": result.dataset_run_url,
                   "prompt_version": PROMPT_VERSION, "run_id": POPULATION_RUN_ID, "input": "short", "model": model,
                   "label_status": "none", "cases": len(numbers)})
    return result.dataset_run_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_DEFAULT)
    parser.add_argument("--model", default=ev.DEFAULT_MODEL)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch")
    screener = commands.add_parser("screen")
    screener.add_argument("--limit", type=int)
    screener.add_argument("--workers", type=int, default=4)
    commands.add_parser("store")
    commands.add_parser("log-langfuse")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path(".env"))

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    numbers = target_population(conn)
    if args.command == "fetch":
        print(json.dumps(fetch(conn, numbers, os.environ["COMPANIES_HOUSE_API_KEY"]), indent=2))
    elif args.command == "screen":
        subset = numbers[: args.limit] if args.limit else numbers
        records = screen(conn, subset, args.model, workers=args.workers)
        print(json.dumps({"screened": len(records), "cost": ev.cost(list(records.values()), ev.model_prices(args.model))}, indent=2))
    elif args.command == "store":
        print(json.dumps({**store(conn, numbers, args.model), **summarise(conn, args.model)}, indent=2))
    else:
        print(log_to_langfuse(conn, numbers, args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
