#!/usr/bin/env python3
"""Model check for the companies W1 could not settle by rule (docs/WEB_STAGE_PLAN.md).

`web_settle` settles a company when its site shows the registered number, the
full legal name as a disclosure, or the name and the registered postcode. That
leaves companies whose site plausibly belongs to them but proves nothing: it
shows a shorter brand ("Heaton Group" for Heaton Group Developments Limited),
or the footer names a sister company with another number. This asks one model
call per company whether the best candidate site is the company's own business
or brand, given the company's filed principal activity, its Maps listing and
what the site says about itself.

The rules of every model stage here apply: one text-only call per company
(about $0.003); a "same_business" verdict must quote the text it was shown,
and the quote must be the website's own words (`web_profile_policy.quote_match`), otherwise the
verdict is treated as `cannot_tell`; each finished case is appended to an
fsync'd checkpoint at once, so a killed run loses nothing and a re-run replays;
`log-langfuse` replays a finished run as one dataset run with a trace per
company. Every call is paid (OpenRouter) and needs the user's go for that run.

Steps:

  plan          Which unsettled companies have a plausible best candidate; no calls.
  run           One model call per planned company (`--limit N` for a trial).
  apply         Companies with a valid `same_business` verdict get a settled
                record (`identity-v4-settled`, rule `model:same_business`), appended
                to the identity checkpoint; `web_population store` then writes it.
  log-langfuse  Replay the run into Langfuse (no calls).
  register      Publish the prompt to Langfuse prompt management.

Checkpoint: `logs/web/settle-model-checkpoint.jsonl`, keyed by (model, prompt
version, company number). Deleting it forces a clean run.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from core.llm_validation import StrictResponseModel, JsonResponseError, parse_json_object, validate_object, validation_message

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.companies_house_sqlite import init_db, utc_now  # noqa: E402
from scripts.screen import search_screen_eval as ev  # noqa: E402
from scripts.web import web_settle as settle  # noqa: E402
from scripts.web.web_crawl import CRAWL_VERSION  # noqa: E402
from scripts.web.web_detect import load_pages  # noqa: E402
from scripts.web.web_fetch import Fetcher  # noqa: E402
from scripts.web.web_profile_policy import _clean_json, quote_match  # noqa: E402

PROMPT_VERSION = "web-settle-check-v2"
PROMPT_NAME = "web-settle-check"
CHECKPOINT = Path("logs/web/settle-model-checkpoint.jsonl")
DATASET = "web-settle-check"
LANGFUSE_CONFIG = {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}
VERDICTS = ("same_business", "different_business", "cannot_tell")


class _IdentityVerdictResponse(StrictResponseModel):
    verdict: str
    reason: str | None = None
    quote: str | None = None

TEMPLATE = """You decide whether a website belongs to a UK company, to confirm the website of a sales lead. Use only the text below.

Company (Companies House): {company_name}, number {company_number}
Registered office: {registered_office}
Principal activity in its latest filed accounts: {principal_activity}
Industry (SIC): {sic}
Google Maps listing: {listing}

Candidate website: {domain}
Facts about the site: {facts}
Page title: {title}
Text from the home page:
{home_excerpt}
Legal and footer text found on the site:
{snippets}

Answer with one JSON object and nothing else:
{{"verdict": "same_business | different_business | cannot_tell", "reason": "<one sentence>", "quote": "<exact words from the website text above (page title, home page text or footer text) that support same_business, otherwise empty>"}}

Rules:
- same_business: this is the website of the company's own business or brand. A sister, parent or group company may operate the site and be named in its footer, as long as the brand and the activity are this company's.
- different_business: a namesake, or another business in another trade.
- cannot_tell: the text does not show enough either way.
- Do not decide from the name alone: what the site does has to fit the company's activity.
- The Maps listing and the filing describe the company; only the website text can show that the site is theirs, so quote the website."""


# ---------------------------------------------------------------- inputs

def _filing_activity(number: str) -> str | None:
    from core.companies_house_extractor import filed_report_text
    from scripts.screen.search_screen_population import xhtml_path
    from scripts.web.web_profile_eval import principal_activity
    path = xhtml_path(number)
    if not path:
        return None
    return principal_activity(filed_report_text(Path(path).read_text(encoding="utf-8", errors="ignore")) or "")


def _facts(evidence: dict[str, Any], company: dict[str, Any]) -> str:
    parts = [f"the registered postcode {company.get('postcode') or '?'} "
             f"{'appears' if evidence['postcode_found'] else 'does not appear'} on the site",
             f"the Maps listing's phone number {'is' if evidence['phone_match'] else 'is not'} shown on the site",
             f"the company's name words {'appear' if evidence['brand_found'] or evidence['name_found'] else 'do not appear'}"
             " as a brand on several pages"]
    if evidence["other_numbers"]:
        parts.append("it prints other company registration number(s): " + ", ".join(evidence["other_numbers"]))
    return "; ".join(parts)


def _plausible(evidence: dict[str, Any]) -> bool:
    return evidence["pages_read"] > 0 and any(
        evidence[k] for k in ("legal_name_found", "name_found", "brand_found", "phone_match", "postcode_found"))


def build_cases(conn: sqlite3.Connection, records: list[dict[str, Any]], companies: dict[str, dict[str, Any]],
                fetcher: Fetcher, *, activity: Callable[[str], str | None] = _filing_activity,
                crawl_version: str = CRAWL_VERSION) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """A case per unsettled company whose best crawled candidate is plausible.
    Returns (cases, counts of the companies left out and why)."""
    cases: list[dict[str, Any]] = []
    left_out = {"no candidate was crawled": 0, "nothing on the site ties it to the company": 0}
    for record in records:
        company = companies.get(record["company_number"])
        best = None
        for candidate in settle.top_candidates(record):
            pages = load_pages(conn, candidate["domain"], crawl_version)
            if not pages or company is None:
                continue
            evidence = settle.site_evidence(candidate["domain"], company, record.get("listing"), pages, fetcher,
                                            digest=True)
            score = tuple(evidence[k] for k in ("legal_name_found", "name_found", "brand_found", "phone_match",
                                                "postcode_found")) + (evidence["pages_read"],)
            if best is None or score > best[0]:
                best = (score, candidate, evidence)
        if best is None:
            left_out["no candidate was crawled"] += 1
            continue
        _, candidate, evidence = best
        if not _plausible(evidence):
            left_out["nothing on the site ties it to the company"] += 1
            continue
        listing = record.get("listing") or {}
        conn_row = conn.execute("select sic_code_primary from companies where company_number = ?",
                                (record["company_number"],)).fetchone()
        sic = conn.execute("select sic_label from sic_groups where sic_code = ?", (conn_row[0],)).fetchone() if conn_row else None
        cases.append({
            "company_number": record["company_number"], "company_name": record["company_name"],
            "domain": candidate["domain"], "tier_before": record["tier"], "evidence": {
                k: v for k, v in evidence.items() if k not in ("home_excerpt", "snippets", "title")},
            "inputs": {
                "company_name": record["company_name"], "company_number": record["company_number"],
                "registered_office": f"{company.get('locality') or ''} {company.get('postcode') or ''}".strip() or "unknown",
                "principal_activity": activity(record["company_number"]) or "not available",
                "sic": (sic[0] if sic else None) or "not available",
                "listing": (f"{listing.get('title')} ({listing.get('category') or 'no category'}), {listing.get('address') or 'no address'}"
                            if listing else "none found"),
                "domain": candidate["domain"], "facts": _facts(evidence, company),
                "title": evidence.get("title") or "none",
                "home_excerpt": evidence.get("home_excerpt") or "(no text)",
                "snippets": "\n".join(f"- {s}" for s in evidence.get("snippets") or []) or "(none found)"}})
    return cases, left_out


REVIEW_FILE = Path("logs/web/settle-model-review.csv")


def write_review(verdicts: list[dict[str, Any]], settled: set[str], path: Path = REVIEW_FILE) -> None:
    """Every verdict, with whether it settled the company, for reading."""
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company number", "company name", "website", "verdict", "settled", "uk sign", "reason", "quote"])
        for v in sorted(verdicts, key=lambda v: (v["verdict"], v["company_name"])):
            evidence = v.get("evidence") or {}
            writer.writerow([v["company_number"], v["company_name"], v["domain"], v["verdict"],
                             "yes" if v["company_number"] in settled else "no",
                             "yes" if evidence.get("uk_site") or evidence.get("postcode_found") else "no",
                             v.get("reason") or v.get("problem") or "", v.get("quote") or ""])


def build_prompt(inputs: dict[str, Any]) -> str:
    return TEMPLATE.format(**inputs)


# ---------------------------------------------------------------- response check

def parse_verdict(raw: str | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """`{verdict, reason, quote, quote_valid, problem}`. A same_business verdict
    whose quote is not in the text shown becomes cannot_tell. Never raises."""
    try:
        parsed = parse_json_object(raw)
    except JsonResponseError as exc:
        return {"verdict": "cannot_tell", "reason": None, "quote": None, "quote_valid": None,
                "problem": "empty response" if not raw else "unparseable response", "validation": exc.validation}
    response, validation = validate_object(parsed, _IdentityVerdictResponse)
    data = parsed.payload
    verdict_value = response.verdict if response else data.get("verdict")
    verdict = verdict_value.strip().lower().replace(" ", "_") if isinstance(verdict_value, str) else "cannot_tell"
    if isinstance(verdict_value, str) and verdict != verdict_value:
        validation["normalisations"].append({"path": "verdict", "code": "normalised_label",
                                               "message": "trimmed and normalised verdict label"})
    if verdict not in VERDICTS:
        verdict = "cannot_tell"
    quote_value = response.quote if response else data.get("quote")
    reason_value = response.reason if response else data.get("reason")
    quote = quote_value.strip() if isinstance(quote_value, str) and quote_value.strip() else None
    reason = reason_value.strip() if isinstance(reason_value, str) and reason_value.strip() else None
    out = {"verdict": verdict, "reason": reason, "quote": quote, "quote_valid": None,
           "problem": validation_message(validation) if validation["errors"] else None, "validation": validation}
    if verdict == "same_business":
        # the website's own words only: the listing and the filing cannot show that the site is the company's
        shown = [inputs["home_excerpt"], inputs["snippets"], inputs["title"]]
        out["quote_valid"] = int(quote_match(quote, shown) is not None)
        if not out["quote_valid"]:
            out.update(verdict="cannot_tell", problem="same_business quote not found in the text shown")
    return out


# ---------------------------------------------------------------- checkpoint and calls

def load_checkpoint(path: Path = CHECKPOINT) -> dict[tuple[str, str, str], dict[str, Any]]:
    done: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        done[(rec["model"], rec["prompt_version"], rec["company_number"])] = rec
    return done


def append_checkpoint(record: dict[str, Any], path: Path = CHECKPOINT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_case(api_key: str | None, model: str, case: dict[str, Any], timeout: int, *,
             call: Callable[..., tuple[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Judge one company. Never raises: a failed call is a recorded problem."""
    prompt = build_prompt(case["inputs"])
    raw: str | None = None
    usage: dict[str, Any] = {}
    error: str | None = None
    try:
        raw, usage = (call or ev.call_model)(api_key, model, prompt, timeout)
    except Exception as exc:  # noqa: BLE001 -- one bad call must not sink the run
        error = f"request failed: {exc}"
    parsed = parse_verdict(raw, case["inputs"])
    if error:
        parsed["problem"] = error
    return {"company_number": case["company_number"], "company_name": case["company_name"], "domain": case["domain"],
            "model": model, "prompt_version": PROMPT_VERSION, "prompt": prompt, "raw": raw, "usage": usage,
            "evidence": case["evidence"], **parsed}


def run(cases: list[dict[str, Any]], model: str, *, workers: int = 4, timeout: int = 120, checkpoint: Path = CHECKPOINT,
        call: Callable[..., tuple[str, dict[str, Any]]] | None = None, api_key: str | None = None,
        log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Run every case not already in the checkpoint; replay the rest. A failed request is retried next run."""
    if api_key is None and call is None:
        load_dotenv(Path(".env"))
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise SystemExit("OPENROUTER_API_KEY not set in .env or the environment.")
    done = load_checkpoint(checkpoint)
    results: dict[str, dict[str, Any]] = {}
    todo = []
    for case in cases:
        record = done.get((model, PROMPT_VERSION, case["company_number"]))
        if record is not None and not (record.get("problem") or "").startswith("request failed"):
            results[case["company_number"]] = record
        else:
            todo.append(case)
    log(f"settle check {PROMPT_VERSION}: {len(results)} replayed, {len(todo)} to run")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_case, api_key, model, case, timeout, call=call) for case in todo]
        for index, future in enumerate(as_completed(futures), 1):
            record = future.result()
            append_checkpoint(record, checkpoint)
            results[record["company_number"]] = record
            if index % 25 == 0 or index == len(todo):
                log(f"  {index}/{len(todo)}")
    return [results[c["company_number"]] for c in cases if c["company_number"] in results]


# ---------------------------------------------------------------- applying verdicts

def promotable(verdict: dict[str, Any]) -> bool:
    """A valid same_business verdict on a site with a sign of a UK presence: a UK domain or phone number, or the
    registered postcode. A global brand's own site for a UK subsidiary (jamf.com for Jamf Ltd) is the right brand
    but not the UK company's marketing property, and a wrong match abroad (a US health system for Lapa Holdings)
    is the costly mistake, so those are held back for review."""
    evidence = verdict.get("evidence") or {}
    return (verdict["verdict"] == "same_business" and verdict.get("quote_valid") == 1
            and bool(evidence.get("uk_site") or evidence.get("postcode_found")))


def settled_records(records: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A settled copy of each unsettled record whose best candidate got a promotable verdict."""
    by_number = {r["company_number"]: r for r in records}
    out = []
    for verdict in verdicts:
        record = by_number.get(verdict["company_number"])
        if record is None or not promotable(verdict):
            continue
        copy = json.loads(json.dumps(record))
        copy.update(resolver_version=settle.SETTLED_VERSION, settled_from=record["resolver_version"], tier="probable",
                    domain=verdict["domain"], resolved_at=utc_now(), settled_rule="model:same_business",
                    settled_model={"model": verdict["model"], "prompt_version": verdict["prompt_version"],
                                   "reason": verdict.get("reason"), "quote": verdict.get("quote")})
        for item in copy["candidates"]:
            if item["domain"] == verdict["domain"]:
                item["tier"] = "probable"
                item["evidence"] = {**item["evidence"], "settled_rule": "model:same_business",
                                    "settled": verdict["evidence"], "model_reason": verdict.get("reason")}
        out.append(copy)
    return out


# ---------------------------------------------------------------- Langfuse

def register(client: Any) -> str | None:
    from scripts.eval_support.langfuse_prompts import register_prompt, registered_prompt_reference
    register_prompt(client, name=PROMPT_NAME, python_format_template=TEMPLATE, version_tag=PROMPT_VERSION,
                    commit_message=f"Synced from scripts/web/web_settle_model.py ({PROMPT_VERSION}).")
    client.flush()
    return registered_prompt_reference(client, name=PROMPT_NAME, expected_version_tag=PROMPT_VERSION)


def log_to_langfuse(records: list[dict[str, Any]], model: str) -> str | None:
    """One dataset run over the judged companies, a trace each, replayed from the saved responses."""
    from datetime import UTC, datetime

    from scripts.eval_support.langfuse_runs import (
        dataset_digest, evaluation, experiment_run_name, run_experiment, sync_dataset)
    from scripts.eval_support.langfuse_tracing import flush, langfuse_from_config, observation

    load_dotenv(Path(".env"))
    lf = langfuse_from_config(LANGFUSE_CONFIG)
    if lf is None:
        return None
    by_number = {r["company_number"]: r for r in records}
    items = [{"id": f"{DATASET}:{r['company_number']}", "input": {"company_name": r["company_name"], "domain": r["domain"]},
              "metadata": {"company_number": r["company_number"], "company_name": r["company_name"]}} for r in records]
    sync_dataset(lf, DATASET, items, digest=dataset_digest(items),
                 description="Is the best candidate site the company's own business? No expected labels.")

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        record = by_number[item.metadata["company_number"]]
        with observation(lf, name="web_settle_check", as_type="generation", model=model, input=record["prompt"],
                         output={"raw_response": record.get("raw"), "verdict": record["verdict"],
                                 "reason": record.get("reason"), "quote": record.get("quote"),
                                 "problem": record.get("problem")}):
            pass
        flush(lf)
        return record

    def evaluate(*, output: dict[str, Any], **_: Any) -> list[Any]:
        return [evaluation("settled", 1.0 if output["verdict"] == "same_business" else 0.0, data_type="NUMERIC",
                           comment=output.get("reason") or output.get("problem") or "")]

    run_name = experiment_run_name(model=model, when=datetime.now(UTC), label=f"{PROMPT_VERSION} settle check")
    result = run_experiment(lf, dataset_name=DATASET, run_name=run_name, task=task, evaluators=[evaluate],
                            description=f"{model} @ {PROMPT_VERSION}",
                            metadata={"prompt_version": PROMPT_VERSION, "model": model})
    flush(lf)
    return result.dataset_run_url


# ---------------------------------------------------------------- CLI

def _setup(args: argparse.Namespace) -> tuple[sqlite3.Connection, list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    from scripts.web.web_population import company_inputs
    records = settle._unsettled(settle.CHECKPOINT, ("ambiguous", "blocked"))
    conn = sqlite3.connect(args.db, timeout=30.0)
    init_db(conn)
    companies = company_inputs(conn, [r["company_number"] for r in records])
    cases, left_out = build_cases(conn, records, companies, Fetcher(cache_only=True))
    return conn, records, cases, left_out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--model", default=ev.DEFAULT_MODEL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    runner = sub.add_parser("run", help="Paid OpenRouter calls: needs a go.")
    runner.add_argument("--limit", type=int)
    runner.add_argument("--workers", type=int, default=4)
    sub.add_parser("apply")
    sub.add_parser("log-langfuse")
    sub.add_parser("register")
    args = parser.parse_args(argv)
    load_dotenv(Path(".env"))

    if args.command == "register":
        from scripts.eval_support.langfuse_tracing import langfuse_from_config
        client = langfuse_from_config(LANGFUSE_CONFIG)
        if client is None:
            raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
        print(register(client))
        return 0

    conn, records, cases, left_out = _setup(args)
    try:
        if args.command == "plan":
            print(json.dumps({"unsettled": len(records), "to_check": len(cases), "left_out": left_out}, indent=2))
            if cases:
                print(build_prompt(cases[0]["inputs"]))
        elif args.command == "run":
            chosen = cases[:args.limit] if args.limit is not None else cases
            verdicts = run(chosen, args.model, workers=args.workers)
            counts: dict[str, int] = {}
            for verdict in verdicts:
                counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
            prices = ev.model_prices(args.model)
            usage = {"prompt": sum((v.get("usage") or {}).get("prompt_tokens", 0) or 0 for v in verdicts),
                     "completion": sum((v.get("usage") or {}).get("completion_tokens", 0) or 0 for v in verdicts)}
            print(json.dumps({"companies": len(verdicts), "verdicts": counts, "tokens": usage,
                              **({"usd": round(usage["prompt"] * prices[0] + usage["completion"] * prices[1], 4)}
                                 if prices else {}),
                              "problems": sum(1 for v in verdicts if v.get("problem"))}, indent=2))
        else:
            verdicts = [r for k, r in load_checkpoint().items() if k[0] == args.model and k[1] == PROMPT_VERSION]
            if args.command == "apply":
                from scripts.web.web_population import append_checkpoint as append_identity
                new = settled_records(records, verdicts)
                for record in new:
                    append_identity(record, settle.CHECKPOINT)
                held = [v for v in verdicts if v["verdict"] == "same_business" and v.get("quote_valid") == 1
                        and not promotable(v)]
                write_review(verdicts, {r["company_number"] for r in new})
                print(json.dumps({"judged": len(verdicts), "settled": len(new), "held_back_no_uk_sign": len(held),
                                  "review_file": str(REVIEW_FILE)}, indent=2))
            elif args.command == "log-langfuse":
                print(log_to_langfuse(verdicts, args.model) or "Langfuse not configured")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
