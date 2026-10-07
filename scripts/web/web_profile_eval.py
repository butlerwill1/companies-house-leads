#!/usr/bin/env python3
"""W3 site profile: run it, store it, and measure it (docs/WEB_STAGE_PLAN.md).

Every model call is a paid OpenRouter call (about $0.003 a company) and needs
the user's go for that run. The rules of `.claude/skills/langfuse-eval-discipline`
apply: every finished case is appended to an fsync'd checkpoint at once (a
killed run loses nothing and a re-run replays), and `log-langfuse` replays a
finished run into Langfuse as one dataset run with a trace per company (no
model calls).

Steps:

  run          Profile companies (`--limit N`, `--numbers ...`, or the next
               N in queue order that have crawled site text). A company with no
               usable site text is recorded with a problem and is not sent
               to the model.
  store        Write the checkpoint to `company_web_profile`.
  log-langfuse Replay a finished run into Langfuse (no model calls).

Gold set (`evals/web_profile/`, about 60 companies, 20 labelled blind first):

  gold-draw           Seeded draw of companies that have site text.
  gold-export         `--sheet blind|review` CSV for the reviewer.
  gold-import         Write the reviewer's verdicts into the case files.
  gold-score          Per-field accuracy, quote validity and, with two runs, rerun noise.

Usage:
    python -m scripts.web.web_profile_eval run --limit 5 --dry-run
    python -m scripts.web.web_profile_eval run --limit 20
    python -m scripts.web.web_profile_eval store
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.companies_house_sqlite import init_db, json_text, utc_now  # noqa: E402
from scripts.business_profile_classifier.business_profile_eval import case_files, load_case, save_case  # noqa: E402
from scripts.screen import search_screen_eval as ev  # noqa: E402
from scripts.web import web_profile_policy as policy  # noqa: E402
from scripts.web.web_crawl import CRAWL_VERSION  # noqa: E402
from scripts.web.web_fetch import Fetcher, parse_html  # noqa: E402
from scripts.web.web_trading_names import trading_names  # noqa: E402

DB_DEFAULT = "companies-house.db"
CHECKPOINT = Path("logs/web/profile-checkpoint.jsonl")
RUN_INDEX = Path("logs/web/profile-langfuse-runs.jsonl")
GOLD_DIR = Path("evals/web_profile/cases")
SELECTION = GOLD_DIR.parent / "selection.json"
POPULATION_DATASET = "web-profile-population"
LANGFUSE_CONFIG = {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}
DEFAULT_MODEL = ev.DEFAULT_MODEL
PAGE_KIND_ORDER = ("home", "about", "service", "contact", "pricing", "location", "landing", "blog", "product",
                   "other", "privacy", "terms")
PER_PAGE_CHARS = 3000
GOLD_FIELDS = ("customer_type", "conversion_action", "geography", "wins_by_tender", "urgency", "channel_fit")
GOLD_ENUMS = {"customer_type": policy.CUSTOMER_TYPES, "conversion_action": policy.CONVERSIONS,
              "wins_by_tender": policy.TENDER_VALUES,
              "geography": policy.GEOGRAPHIES, "urgency": policy.URGENCIES, "channel_fit": policy.CHANNEL_FITS}
GOLD_COUNT = 60
GOLD_BLIND = 20
GOLD_SEED = 20261002


# ---------------------------------------------------------------- inputs

def site_text(conn: sqlite3.Connection, domain: str, fetcher: Fetcher, *, crawl_version: str = CRAWL_VERSION,
              max_chars: int = policy.MAX_TEXT_CHARS) -> str:
    """The visible text of a site's crawled pages, key pages first, each capped,
    read from the page cache (no requests)."""
    rows = conn.execute("select url, page_kind from web_pages where domain = ? and crawl_version = ? "
                        "and status_code = 200 and fetch_error is null order by id", (domain, crawl_version)).fetchall()
    order = {kind: i for i, kind in enumerate(PAGE_KIND_ORDER)}
    rows = sorted((r for r in rows if r[1] in order), key=lambda r: order[r[1]])
    parts: list[str] = []
    used = 0
    for url, kind in rows:
        page = fetcher.get(url, accept=("html", "pdf"))
        if not page.html:
            continue
        text = parse_html(page.html, page.final_url or url).text[:PER_PAGE_CHARS]
        if not text.strip():
            continue
        chunk = f"[{kind}: {url}] {text}"
        separator = 2 if parts else 0                      # the "\n\n" joining pages counts against the cap
        room = max_chars - used - separator
        if room <= 0:
            break
        parts.append(chunk[:room])
        used += separator + len(parts[-1])
        if used >= max_chars:
            break
    return "\n\n".join(parts)


def principal_activity(filing_text: str) -> str | None:
    """The line of a filed report that states the principal activity."""
    lines = [" ".join(line.split()) for line in filing_text.splitlines()]
    for index, line in enumerate(lines):
        if "principal activit" not in line.lower():
            continue
        if len(line) < 40:   # a bare heading ("Principal activities"): the statement is the next line
            line = next((nxt for nxt in lines[index + 1:index + 4] if len(nxt) >= 20), "")
        if 20 <= len(line) <= 900:
            return line[:600]
    return None


def build_cases(conn: sqlite3.Connection, numbers: list[str], fetcher: Fetcher, categories: list[dict[str, Any]], *,
                filing_text: Callable[[str], str | None] | None = None,
                crawl_version: str = CRAWL_VERSION, resolver_version: str | None = None) -> list[dict[str, Any]]:
    """One case per company that has a chosen website: its text, filing activity,
    Maps category, and the category shortlist. Companies without a website or
    without site text get a case with empty `text`, which `run` records but
    does not send."""
    cases = []
    for number in numbers:
        row = conn.execute("select company_name from companies where company_number = ?", (number,)).fetchone()
        if row is None:
            continue
        ident = conn.execute(
            "select domain from company_web_identity where company_number = ? and role = 'main' "
            + ("and resolver_version = ? " if resolver_version else "") + "order by id desc limit 1",
            (number, resolver_version) if resolver_version else (number,)).fetchone()
        domain = ident[0] if ident else None
        listing = conn.execute("select category from company_google_listing where company_number = ? "
                               "order by id desc limit 1", (number,)).fetchone()
        text = site_text(conn, domain, fetcher, crawl_version=crawl_version) if domain else ""
        activity = principal_activity(filing_text(number) or "") if filing_text else None
        listing_category = listing[0] if listing and listing[0] else None
        names = trading_names(conn, number)
        cases.append({
            "company_number": number, "company_name": row[0], "domain": domain, "text": text,
            "principal_activity": activity, "listing_category": listing_category,
            "categories": [] if listing_category else policy.shortlist_categories(categories, f"{activity or ''} {text[:3000]}"),
            "brand_terms": [policy_brand(row[0]), *names]})
    return cases


def policy_brand(company_name: str) -> str:
    from scripts.web.web_identity import clean_name
    return clean_name(company_name)


# ---------------------------------------------------------------- checkpoint and model calls

Key = tuple[str, str, int, str]


def _key(model: str, version: str, run_id: int, number: str) -> Key:
    return (model, version, run_id, number)


def load_checkpoint(path: Path = CHECKPOINT) -> dict[Key, dict[str, Any]]:
    done: dict[Key, dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        done[_key(rec["model"], rec["prompt_version"], rec.get("run_id", 1), rec["company_number"])] = rec
    return done


def append_checkpoint(record: dict[str, Any], path: Path = CHECKPOINT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_case(api_key: str | None, model: str, case: dict[str, Any], timeout: int, *, run_id: int = 1,
             call: Callable[..., tuple[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Profile one company. Never raises: a failed call is a recorded problem."""
    base = {"company_number": case["company_number"], "model": model, "prompt_version": policy.PROMPT_VERSION,
            "run_id": run_id, "domain": case.get("domain"), "text_chars": len(case.get("text") or "")}
    if not (case.get("text") or "").strip():
        return {**base, "raw": None, "usage": {}, **policy.parse_profile(
            None, "", listing_category=case.get("listing_category")),
            "problem": "no website text to profile" if case.get("domain") else "no website found"}
    prompt = policy.build_prompt(
        company_name=case["company_name"], principal_activity=case.get("principal_activity"),
        listing_category=case.get("listing_category"), text=case["text"], categories=case.get("categories") or ())
    def parse(raw: str | None) -> dict[str, Any]:
        return policy.parse_profile(raw, case["text"], listing_category=case.get("listing_category"),
                                    allowed_categories=case.get("categories") or (),
                                    brand_terms=case.get("brand_terms") or (),
                                    principal_activity=case.get("principal_activity"))

    raw: str | None = None
    usage: dict[str, Any] = {}
    error: str | None = None
    try:
        raw, usage = (call or ev.call_model)(api_key, model, prompt, timeout)
    except Exception as exc:  # noqa: BLE001 -- one bad call must not sink the run
        error = f"request failed: {exc}"
    parsed = parse(raw)
    if error:
        parsed["problem"] = error
        return {**base, "raw": raw, "usage": usage, "attempts": 1, **parsed}
    record = {**base, "raw": raw, "usage": usage, "attempts": 1, **parsed}
    failures = policy.quote_failures(parsed)
    if not failures or raw is None:
        return record
    # One retry, telling the model which quotes were not found. The second
    # answer replaces the first only if it parses; both are kept, so the rate
    # at which first answers fail stays measurable.
    try:
        raw2, usage2 = (call or ev.call_model)(api_key, model, policy.build_retry_prompt(prompt, raw, failures), timeout)
    except Exception as exc:  # noqa: BLE001
        return {**record, "attempts": 2, "retry_problem": f"retry failed: {exc}"}
    usage_total = {k: (usage.get(k) or 0) + (usage2.get(k) or 0)
                   for k in ("prompt_tokens", "completion_tokens") if k in usage or k in usage2}
    first = {"raw": raw, "problem": parsed.get("problem"),
             **{k: parsed.get(k) for f in policy.QUOTED_FIELDS for k in (f, f"{f}_quote", f"{f}_quote_valid")}}
    parsed2 = parse(raw2)
    if raw2 is None or (parsed2.get("problem") or "").startswith(("empty response", "unparseable")):
        return {**record, "usage": usage_total, "attempts": 2, "first_attempt": first,
                "retry_problem": parsed2.get("problem")}
    return {**base, "raw": raw2, "usage": usage_total, "attempts": 2, "first_attempt": first, **parsed2}


def run(cases: list[dict[str, Any]], model: str, *, workers: int = 4, timeout: int = 120, run_id: int = 1,
        checkpoint: Path = CHECKPOINT, call: Callable[..., tuple[str, dict[str, Any]]] | None = None,
        api_key: str | None = None, log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Run every case not already in the checkpoint; replay the rest. A failed
    request is retried next run (it is not treated as done)."""
    if api_key is None and call is None:
        load_dotenv(Path(".env"))
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise SystemExit("OPENROUTER_API_KEY not set in .env or the environment.")
    done = load_checkpoint(checkpoint)
    results: dict[str, dict[str, Any]] = {}
    todo = []
    for case in cases:
        record = done.get(_key(model, policy.PROMPT_VERSION, run_id, case["company_number"]))
        if record is not None and not (record.get("problem") or "").startswith("request failed"):
            results[case["company_number"]] = record
        else:
            todo.append(case)
    log(f"profile {policy.PROMPT_VERSION} run {run_id}: {len(results)} replayed, {len(todo)} to run")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_case, api_key, model, case, timeout, run_id=run_id, call=call): case for case in todo}
        for index, future in enumerate(as_completed(futures), 1):
            record = future.result()
            append_checkpoint(record, checkpoint)
            results[record["company_number"]] = record
            if index % 20 == 0 or index == len(todo):
                log(f"  {index}/{len(todo)}")
    return [results[c["company_number"]] for c in cases if c["company_number"] in results]


def cost(records: list[dict[str, Any]], prices: tuple[float, float] | None) -> dict[str, Any]:
    prompt = sum((r.get("usage") or {}).get("prompt_tokens", 0) or 0 for r in records)
    completion = sum((r.get("usage") or {}).get("completion_tokens", 0) or 0 for r in records)
    out: dict[str, Any] = {"calls": sum(1 for r in records if r.get("raw") is not None),
                           "prompt_tokens": prompt, "completion_tokens": completion}
    if prices:
        out["usd"] = round(prompt * prices[0] + completion * prices[1], 4)
    return out


# ---------------------------------------------------------------- storage

PROFILE_COLUMNS = (
    "company_number", "profile_version", "model", "domain", "summary", "products_services", "customer_type",
    "customer_type_quote", "customer_type_quote_valid", "customer_type_quote_match", "conversion_action",
    "conversion_action_quote", "conversion_action_quote_valid", "conversion_action_quote_match", "geography",
    "main_town", "geography_quote", "geography_quote_valid", "geography_quote_match", "wins_by_tender",
    "wins_by_tender_quote", "wins_by_tender_quote_valid", "wins_by_tender_quote_match", "urgency", "ticket_band",
    "channel_fit", "google_category", "category_source", "seed_keywords", "problem", "attempts", "first_attempt",
    "prompt_tokens", "completion_tokens", "profiled_at")


def store_profiles(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> int:
    """One row per company, version and model; a re-store replaces it."""
    written = 0
    sql = (f"insert or replace into company_web_profile ({', '.join(PROFILE_COLUMNS)}) "
           f"values ({', '.join('?' for _ in PROFILE_COLUMNS)})")
    for rec in records:
        row = {**rec, "profile_version": rec["prompt_version"],
               "products_services": json_text(rec.get("products_services") or []),
               "seed_keywords": json_text(rec.get("seed_keywords") or []),
               "first_attempt": json_text(rec["first_attempt"]) if rec.get("first_attempt") else None,
               "prompt_tokens": (rec.get("usage") or {}).get("prompt_tokens"),
               "completion_tokens": (rec.get("usage") or {}).get("completion_tokens"), "profiled_at": utc_now()}
        conn.execute(sql, [row.get(c) for c in PROFILE_COLUMNS])
        written += 1
    conn.commit()
    return written


# ---------------------------------------------------------------- Langfuse replay

def log_to_langfuse(cases: list[dict[str, Any]], records: list[dict[str, Any]], model: str, *, run_id: int = 1) -> str | None:
    """One Langfuse dataset run over the profiled companies, replayed from the
    saved responses (no model calls). A trace per company, rejections and
    problems included. Returns the run url, or None if Langfuse is not configured."""
    from datetime import UTC, datetime

    from scripts.eval_support.langfuse_runs import (
        dataset_digest, evaluation, experiment_run_name, run_experiment, sync_dataset)
    from scripts.eval_support.langfuse_tracing import flush, langfuse_from_config, observation
    from scripts.web.web_profile_prompt_registry import prompt_reference

    load_dotenv(Path(".env"))
    lf = langfuse_from_config(LANGFUSE_CONFIG)
    if lf is None:
        return None
    by_number = {r["company_number"]: r for r in records}
    items = [{"id": f"{POPULATION_DATASET}:{c['company_number']}",
              "input": {"company_name": c["company_name"], "domain": c.get("domain")},
              "metadata": {"company_number": c["company_number"], "company_name": c["company_name"]}}
             for c in cases if c["company_number"] in by_number]
    sync_dataset(lf, POPULATION_DATASET, items, digest=dataset_digest(items),
                 description="Web-stage site profiles for the screen-passing population. No expected labels.")

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        record = by_number[item.metadata["company_number"]]
        with observation(lf, name="web_profile", as_type="generation", model=model,
                         input={"company_number": record["company_number"], "domain": record.get("domain")},
                         output={"raw_response": record.get("raw"), "customer_type": record.get("customer_type"),
                                 "conversion_action": record.get("conversion_action"),
                                 "google_category": record.get("google_category"), "problem": record.get("problem"),
                                 "attempts": record.get("attempts"),
                                 "first_attempt_problem": (record.get("first_attempt") or {}).get("problem")}):
            pass
        flush(lf)
        return record

    def evaluate(*, output: dict[str, Any], **_: Any) -> list[Any]:
        usable = output.get("raw") is not None and not output.get("problem")
        return [evaluation("usable", 1.0 if usable else 0.0, data_type="NUMERIC", comment=output.get("problem") or "")]

    def aggregate(*, item_results: list[Any], **_: Any) -> list[Any]:
        usable = sum(1 for r in records if r.get("raw") is not None and not r.get("problem"))
        return [evaluation("usable_rate", usable / len(records) if records else 0.0, data_type="NUMERIC")]

    run_name = experiment_run_name(model=model, when=datetime.now(UTC), label=f"{policy.PROMPT_VERSION} population")
    result = run_experiment(lf, dataset_name=POPULATION_DATASET, run_name=run_name, task=task, evaluators=[evaluate],
                            run_evaluators=[aggregate], description=f"{model} @ {policy.PROMPT_VERSION}",
                            metadata={"prompt_version": policy.PROMPT_VERSION, "model": model, "run_id": str(run_id),
                                      "prompt": prompt_reference(lf) or "not registered for this version"})
    flush(lf)
    RUN_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with RUN_INDEX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"dataset": POPULATION_DATASET, "run_name": run_name, "url": result.dataset_run_url,
                                 "prompt_version": policy.PROMPT_VERSION, "model": model, "run_id": run_id,
                                 "cases": len(items)}) + "\n")
    return result.dataset_run_url


# ---------------------------------------------------------------- gold set

def gold_draw(cases: list[dict[str, Any]], *, count: int = GOLD_COUNT, blind: int = GOLD_BLIND, seed: int = GOLD_SEED,
              cases_dir: Path = GOLD_DIR, selection: Path = SELECTION) -> list[str]:
    """A seeded draw from the cases that have site text. Made once."""
    if selection.exists():
        raise SystemExit(f"{selection} exists; the draw is made once (delete it deliberately to redraw)")
    eligible = sorted((c for c in cases if (c.get("text") or "").strip()), key=lambda c: c["company_number"])
    chosen = random.Random(seed).sample(eligible, min(count, len(eligible)))
    for position, case in enumerate(chosen):
        save_case(cases_dir / f"{case['company_number']}.json", {
            "schema_version": 1, "company_number": case["company_number"], "company_name": case["company_name"],
            "domain": case.get("domain"), "principal_activity": case.get("principal_activity"),
            "listing_category": case.get("listing_category"), "text": case["text"], "blind": position < blind,
            "blind_review": None, "expected": None, "review": None})
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text(json.dumps({"seed": seed, "count": count, "blind": blind,
                                     "drawn": [c["company_number"] for c in chosen], "drawn_at": utc_now()}, indent=1)
                         + "\n", encoding="utf-8")
    return [c["company_number"] for c in chosen]


def _excerpt(case: dict[str, Any], chars: int = 1500) -> str:
    return " ".join((case.get("text") or "").split())[:chars]


def gold_rows(sheet: str, records: dict[str, dict[str, Any]], cases_dir: Path = GOLD_DIR) -> list[list[Any]]:
    """`blind`: the blind cases with no model answer. `review`: every case with
    the model's answer (blind cases once their blind verdict is in)."""
    if sheet not in ("blind", "review"):
        raise ValueError("sheet must be 'blind' or 'review'")
    header = ["company number", "company name", "website", "listing category", "principal activity", "site text (start)"]
    if sheet == "review":
        header += [f"model: {f}" for f in GOLD_FIELDS]
    header += [f"verdict: {f}" + (" (agree or true value)" if sheet == "review" else "") for f in GOLD_FIELDS] + ["notes"]
    rows: list[list[Any]] = [header]
    for path in case_files(cases_dir):
        case = load_case(path)
        if case.get("excluded"):
            continue
        if sheet == "blind" and not (case.get("blind") and not case.get("blind_review")):
            continue
        if sheet == "review" and case.get("blind") and not case.get("blind_review"):
            continue
        record = records.get(case["company_number"])
        if sheet == "review" and record is None:
            continue
        base = [case["company_number"], case["company_name"], case.get("domain"), case.get("listing_category"),
                case.get("principal_activity"), _excerpt(case)]
        model_cells = [record.get(f) for f in GOLD_FIELDS] if sheet == "review" else []
        rows.append(base + model_cells + [""] * len(GOLD_FIELDS) + [""])
    return rows


def gold_import(csv_path: Path, *, kind: str, reviewer: str, cases_dir: Path = GOLD_DIR,
                records: dict[str, dict[str, Any]] | None = None) -> int:
    """Validate every row, then write. `agree` (review sheet only) takes the model's value."""
    if kind not in ("blind", "review"):
        raise ValueError("kind must be 'blind' or 'review'")
    records = records or {}
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        number_col = next((f for f in fields if f.lower().startswith("company number")), None)
        if number_col is None:
            raise ValueError("the CSV needs a 'company number' column")
        columns = {f: next((c for c in fields if c.lower().startswith(f"verdict: {f}")), None) for f in GOLD_FIELDS}
        rows = list(reader)
    updates: list[tuple[Path, dict[str, str], str]] = []
    for row in rows:
        number = row[number_col].strip().zfill(8) if row[number_col].strip().isdigit() else row[number_col].strip()
        verdicts = {f: (row.get(col) or "").strip().lower().replace(" ", "_") for f, col in columns.items() if col}
        verdicts = {f: v for f, v in verdicts.items() if v}
        if not verdicts:
            continue
        path = cases_dir / f"{number}.json"
        if not path.exists():
            raise ValueError(f"no case file for company number {number!r}")
        resolved: dict[str, str] = {}
        for field, value in verdicts.items():
            if value == "agree":
                if kind != "review" or not (records.get(number) or {}).get(field):
                    raise ValueError(f"{number}: cannot agree with {field}: no model answer")
                value = records[number][field]
            if value not in GOLD_ENUMS[field]:
                raise ValueError(f"{number}: {field} {value!r} is not one of {GOLD_ENUMS[field]}")
            resolved[field] = value
        updates.append((path, resolved, (row.get("notes") or "").strip()))
    now = utc_now()
    for path, resolved, notes in updates:
        case = load_case(path)
        if kind == "blind":
            case["blind_review"] = {**resolved, "reviewer": reviewer, "reviewed_at": now}
        else:
            case["expected"] = {**(case.get("expected") or {}), **resolved}
            case["review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": now, "notes": notes or None}
        save_case(path, case)
    return len(updates)


def gold_score(records_by_run: dict[int, dict[str, dict[str, Any]]], cases_dir: Path = GOLD_DIR) -> dict[str, Any]:
    """Per-field accuracy on verified cases, how often the model said `unclear`,
    quote validity, blind agreement, and (with two runs) how often the two runs
    agree: the noise a prompt change has to beat."""
    first = records_by_run[min(records_by_run)] if records_by_run else {}
    fields: dict[str, dict[str, int]] = {f: {"n": 0, "correct": 0, "unclear": 0} for f in GOLD_FIELDS}
    quotes = {"checked": 0, "valid": 0}
    blind = {"n": 0, "agree": 0}
    for path in case_files(cases_dir):
        case = load_case(path)
        expected, record = case.get("expected"), first.get(case["company_number"])
        if record is None or case.get("excluded"):
            continue  # an excluded case (a second company on the same website) would count twice
        for field in GOLD_FIELDS:
            if field in QUOTE_FIELDS_SET and record.get(f"{field}_quote_valid") is not None:
                quotes["checked"] += 1
                quotes["valid"] += int(record[f"{field}_quote_valid"] == 1)
        if not expected:
            continue
        for field in GOLD_FIELDS:
            if field not in expected:
                continue
            fields[field]["n"] += 1
            fields[field]["correct"] += int(record.get(field) == expected[field])
            fields[field]["unclear"] += int(record.get(field) == "unclear")
            blind_review = case.get("blind_review")
            if blind_review and field in blind_review:
                blind["n"] += 1
                blind["agree"] += int(blind_review[field] == expected[field])
    result: dict[str, Any] = {"fields": {f: {**v, "accuracy": round(v["correct"] / v["n"], 3) if v["n"] else None}
                                         for f, v in fields.items()}, "quotes": quotes, "blind_agreement": blind}
    if len(records_by_run) >= 2:
        a, b = (records_by_run[k] for k in sorted(records_by_run)[:2])
        shared = [n for n in a if n in b]
        result["rerun"] = {"companies": len(shared), "agreement": {
            f: round(sum(a[n].get(f) == b[n].get(f) for n in shared) / len(shared), 3) if shared else None
            for f in GOLD_FIELDS}}
    return result


QUOTE_FIELDS_SET = frozenset(policy.QUOTED_FIELDS)


# ---------------------------------------------------------------- CLI

def _numbers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.numbers:
        return [n.strip().zfill(8) if n.strip().isdigit() else n.strip().upper() for n in args.numbers]
    from scripts.web.web_rank_order import load_queue
    done = {k[3] for k in load_checkpoint() if k[0] == args.model and k[1] == policy.PROMPT_VERSION and k[2] == args.run_id}
    have_text = {n for (n,) in conn.execute(
        "select distinct i.company_number from company_web_identity i join web_pages p on p.domain = i.domain "
        "where i.role = 'main' and p.crawl_version = ? and p.status_code = 200", (CRAWL_VERSION,))}
    queue = [row["company_number"] for row in load_queue(conn)]
    gold = {p.stem for p in GOLD_DIR.glob("*.json")} if GOLD_DIR.exists() else set()
    pick = [n for n in queue if n in have_text and n not in done and n not in gold]
    return pick[:args.limit] if args.limit is not None else pick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_DEFAULT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--run-id", type=int, default=1)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="Profile companies (paid OpenRouter calls: needs a go).")
    runner.add_argument("--limit", type=int)
    runner.add_argument("--numbers", nargs="+")
    runner.add_argument("--workers", type=int, default=4)
    runner.add_argument("--dry-run", action="store_true", help="Build the inputs and show the first prompt; no model call.")
    runner.add_argument("--gold", action="store_true", help="Run the gold-set companies.")
    sub.add_parser("store", help="Write the checkpoint to company_web_profile.")
    sub.add_parser("log-langfuse", help="Replay the finished run into Langfuse (no model calls).")
    sub.add_parser("gold-draw")
    export = sub.add_parser("gold-export")
    export.add_argument("--sheet", choices=("blind", "review"), required=True)
    export.add_argument("--out", type=Path, required=True)
    imp = sub.add_parser("gold-import")
    imp.add_argument("--csv", type=Path, required=True)
    imp.add_argument("--kind", choices=("blind", "review"), required=True)
    imp.add_argument("--reviewer", required=True)
    sub.add_parser("gold-score")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        init_db(conn)
        fetcher = Fetcher(cache_only=True)
        categories: list[dict[str, Any]] = []
        try:
            from scripts.web.search_providers import SearchClient
            categories = SearchClient(cache_only=True).business_categories()
        except Exception:  # noqa: BLE001 -- the category list is optional: without it only listing categories are used
            categories = []

        def filing_text(number: str) -> str | None:
            from core.companies_house_extractor import filed_report_text
            from scripts.screen.search_screen_population import xhtml_path
            path = xhtml_path(number)
            return filed_report_text(Path(path).read_text(encoding="utf-8", errors="ignore")) if path else None

        if args.command == "run":
            numbers = [p.stem for p in sorted(GOLD_DIR.glob("*.json"))] if args.gold else _numbers(conn, args)
            cases = build_cases(conn, numbers, fetcher, categories, filing_text=filing_text)
            if args.dry_run:
                sendable = [c for c in cases if (c.get("text") or "").strip()]
                print(json.dumps({"companies": len(cases), "with_site_text": len(sendable),
                                  "model": args.model, "run_id": args.run_id}, indent=2))
                if sendable:
                    first = sendable[0]
                    print(policy.build_prompt(company_name=first["company_name"],
                                              principal_activity=first.get("principal_activity"),
                                              listing_category=first.get("listing_category"), text=first["text"],
                                              categories=first.get("categories") or ())[:3000])
                return 0
            records = run(cases, args.model, workers=args.workers, run_id=args.run_id)
            print(json.dumps({"companies": len(records), "usable": sum(1 for r in records if not r.get("problem")),
                              **cost(records, ev.model_prices(args.model))}, indent=2))
        elif args.command == "store":
            records = [r for k, r in load_checkpoint().items() if k[0] == args.model and k[2] == args.run_id
                       and k[1] == policy.PROMPT_VERSION]
            print(f"{store_profiles(conn, records)} profiles stored")
        elif args.command == "log-langfuse":
            records = [r for k, r in load_checkpoint().items() if k[0] == args.model and k[2] == args.run_id
                       and k[1] == policy.PROMPT_VERSION]
            cases = [{"company_number": r["company_number"], "company_name": (conn.execute(
                "select company_name from companies where company_number = ?", (r["company_number"],)).fetchone() or [""])[0],
                "domain": r.get("domain")} for r in records]
            print(log_to_langfuse(cases, records, args.model, run_id=args.run_id) or "Langfuse not configured")
        elif args.command == "gold-draw":
            numbers = [n for (n,) in conn.execute("select distinct company_number from company_web_identity "
                                                  "where role = 'main'")]
            drawn = gold_draw(build_cases(conn, numbers, fetcher, categories, filing_text=filing_text))
            print(f"drew {len(drawn)} cases into {GOLD_DIR}")
        elif args.command == "gold-export":
            records = {k[3]: r for k, r in load_checkpoint().items() if k[0] == args.model and k[2] == args.run_id}
            rows = gold_rows(args.sheet, records)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            print(f"{len(rows) - 1} rows -> {args.out}")
        elif args.command == "gold-import":
            records = {k[3]: r for k, r in load_checkpoint().items() if k[0] == args.model and k[2] == args.run_id}
            print(f"{gold_import(args.csv, kind=args.kind, reviewer=args.reviewer, records=records)} verdicts written")
        elif args.command == "gold-score":
            by_run: dict[int, dict[str, dict[str, Any]]] = {}
            for (model, version, run_id, number), rec in load_checkpoint().items():
                if model == args.model and version == policy.PROMPT_VERSION:
                    by_run.setdefault(run_id, {})[number] = rec
            print(json.dumps(gold_score(by_run), indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
