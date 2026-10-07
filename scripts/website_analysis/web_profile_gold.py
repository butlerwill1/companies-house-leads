#!/usr/bin/env python3
"""W3 gold sets in Langfuse: drafted labels, human review, export (docs/WEB_STAGE_PLAN.md).

Two review queues, one trace per company in each:

  Web profile labels review    customer type, conversion, area, urgency, ticket
                               band, channel fit, Google category, main town
  Web profile phrases review   the reference search phrases, and a verdict on
                               the profile model's own phrases

Each trace shows what the model is shown (site text, principal activity, Maps
category) and the draft with its reasons. The draft is pre-filled into the
annotate form (score source ANNOTATION, as `langfuse_annotation` requires), so
reviewing is: read, change anything wrong, press Complete. Only completed
items are exported, into the case files under `evals/website_profile_gold_set/cases/`,
which are the durable record (Langfuse holds the review, not the truth).

The drafts are not ground truth until reviewed: `draft.label_source` says who
wrote them. Score names carry a `web_` prefix because score configs are shared
across the Langfuse project and the business-profile stage already owns
`customer_type` with different categories.

Steps (no model calls, no paid calls):

  cases     Build or refresh case files from a drafts JSON and the crawl cache.
  sync      Create or refresh the traces, queues and pre-filled drafts.
  export    Write completed reviews into the case files (`expected`).
  status    Counts per queue.

Usage:
    python -m scripts.website_analysis.web_profile_gold cases --drafts evals/website_profile_gold_set/drafts-2026-10-02.json
    python -m scripts.website_analysis.web_profile_gold sync
    python -m scripts.website_analysis.web_profile_gold export --reviewer will
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from companies_house_core.companies_house_sqlite import utc_now  # noqa: E402
from scripts.business_profile_classifier.business_profile_eval import case_files, load_case, save_case  # noqa: E402
from scripts.website_analysis import web_profile_policy as policy  # noqa: E402

GOLD_DIR = Path("evals/website_profile_gold_set/cases")
TRACE_MAP = Path("evals/website_profile_gold_set/langfuse-traces.json")
PHRASE_VOLUME = Path("logs/web/w3-phrase-volume.json")
# The API cannot change a queue's score configs after it is made, so a new field
# means a new queue: v3 added wins_by_tender (the first queue is emptied, not deleted).
LABEL_QUEUE = "Web profile labels review v3"
RETIRED_QUEUES = {"Web profile labels review": "labels"}
PHRASE_QUEUE = "Web profile phrases review"
LANGFUSE_CONFIG = {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}
CATEGORICAL = {
    "customer_type": policy.CUSTOMER_TYPES, "conversion_action": policy.CONVERSIONS,
    "geography": policy.GEOGRAPHIES, "wins_by_tender": policy.TENDER_VALUES, "urgency": policy.URGENCIES,
    "ticket_band": policy.TICKET_BANDS, "channel_fit": policy.CHANNEL_FITS,
}
TEXT_LABELS = ("google_category", "main_town")
LABEL_FIELDS = (*CATEGORICAL, *TEXT_LABELS)
PHRASE_VERDICTS = ("good", "partly_good", "poor")
PHRASE_SEPARATOR = ", "
# Score values a later taxonomy renamed, rewritten in Langfuse wherever a score still
# holds them (prompt v3 renamed quote_form; tender became the wins_by_tender flag).
RETIRED_SCORES = {"web_conversion_action": {"quote_form": "enquiry_form"}}
DESCRIPTIONS = {
    "customer_type": "Who pays: consumer, business, or mixed when both are a substantial share.",
    "conversion_action": "The main route the website offers a new customer: buy_online, book, call (phone and no "
                         "form), enquiry_form (any form, callback requests included; wins a tie with phone), visit.",
    "wins_by_tender": "yes when much of the work comes through public tenders, frameworks or contracted public "
                      "programmes, which ads do not reach; no when customers come to it directly.",
    "geography": "How far it serves: local (one town or city), regional, national, international.",
    "urgency": "emergency (needed today), planned (booked ahead), considered (compared over time).",
    "ticket_band": "Typical value of one sale or one customer's contract.",
    "channel_fit": "search: people know what they need; social: visual, impulse, lifestyle; both.",
    "google_category": "The best category from Google's list (snake_case, e.g. law_firm). Fix the Maps one if wrong.",
    "main_town": "The main town served or based in; blank if none.",
    "search_phrases": "Reference phrases a customer types, comma-separated. No place names for where the customer "
                      "is (W4 adds location); a destination business keeps the place it sells. No brands, no 'near me'.",
    "model_phrases": "Verdict on the profile model's own phrases: good, partly_good or poor.",
}


def score_name(field: str) -> str:
    return f"web_{field}"


# ---------------------------------------------------------------- case files

def build_cases(drafts: dict[str, Any], inputs: dict[str, dict[str, Any]], model_phrases: dict[str, list[str]],
                *, cases_dir: Path = GOLD_DIR) -> list[str]:
    """Write one case file per drafted company. `inputs[number]` is the case
    the model sees (web_profile_eval.build_cases). An existing case keeps its
    `expected` and `review`: only the draft and inputs are refreshed."""
    written = []
    source = drafts.get("label_source") or "draft"
    for number, draft in sorted(drafts["cases"].items()):
        case_in = inputs.get(number)
        if case_in is None or not (case_in.get("text") or "").strip():
            continue
        path = cases_dir / f"{number}.json"
        old = load_case(path) if path.exists() else {}
        labels = {f: {"value": draft[f][0], "reason": draft[f][1]} for f in (*CATEGORICAL, "google_category")}
        labels["main_town"] = {"value": draft.get("main_town"), "reason": None}
        save_case(path, {
            "schema_version": 1, "company_number": number, "company_name": case_in["company_name"],
            "domain": case_in.get("domain"), "principal_activity": case_in.get("principal_activity"),
            "listing_category": case_in.get("listing_category"), "text": case_in["text"],
            "set": old.get("set") or drafts.get("set") or "test-companies", "blind": old.get("blind", False),
            "blind_review": old.get("blind_review"),
            "draft": {"label_source": source, "labels": labels, "search_phrases": list(draft["search_phrases"]),
                      "phrase_note": draft.get("phrase_note") or None,
                      "migrations": draft.get("migrations") or [],
                      "model_phrases": model_phrases.get(number) or [],
                      "model_phrases_verdict": draft.get("model_phrases_verdict"),
                      "model_phrases_note": draft.get("model_phrases_note")},
            "expected": old.get("expected"), "expected_phrases": old.get("expected_phrases"),
            "review": old.get("review"), "phrase_review": old.get("phrase_review"),
            "excluded": {"reason": f"same website as {draft['duplicate_of']}", "duplicate_of": draft["duplicate_of"]}
            if draft.get("duplicate_of") else None})
        written.append(number)
    return written


# ---------------------------------------------------------------- trace payloads

def _volume_note(phrases: Iterable[str], volumes: dict[str, int | None]) -> list[str]:
    return [f"{p} ({volumes[p] if volumes.get(p) is not None else 'not looked up'})" if p in volumes
            else f"{p} (not looked up)" for p in phrases]


def label_trace(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(input, output) for a company's labels trace: what the model sees, then the draft."""
    labels = case["draft"]["labels"]
    trace_input = {"company": f"{case['company_number']} {case['company_name']}",
                   "website": f"https://{case['domain']}" if case.get("domain") else None,
                   "google_maps_category": case.get("listing_category"),
                   "principal_activity": case.get("principal_activity"),
                   "site_text": case["text"]}
    trace_output = {"draft_by": case["draft"]["label_source"],
                    **{f: {"value": labels[f]["value"], "reason": labels[f]["reason"]} for f in LABEL_FIELDS},
                    "what_each_field_means": {f: DESCRIPTIONS[f] for f in LABEL_FIELDS}}
    return trace_input, trace_output


def phrase_trace(case: dict[str, Any], volumes: dict[str, int | None]) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = case["draft"]["labels"]
    trace_input = {"company": f"{case['company_number']} {case['company_name']}",
                   "website": f"https://{case['domain']}" if case.get("domain") else None,
                   "sells": case.get("principal_activity"),
                   "google_category": labels["google_category"]["value"],
                   "customer_type": labels["customer_type"]["value"], "geography": labels["geography"]["value"],
                   "site_text_start": " ".join(case["text"].split())[:2500]}
    trace_output = {"draft_by": case["draft"]["label_source"],
                    "reference_phrases (monthly UK searches)": _volume_note(case["draft"]["search_phrases"], volumes),
                    "phrase_note": case["draft"].get("phrase_note"),
                    "model_phrases gpt-5.4-mini (monthly UK searches)": _volume_note(case["draft"]["model_phrases"], volumes),
                    "draft verdict on model phrases": case["draft"].get("model_phrases_verdict"),
                    "why": case["draft"].get("model_phrases_note"),
                    "rule": DESCRIPTIONS["search_phrases"]}
    return trace_input, trace_output


def label_answers(case: dict[str, Any]) -> dict[str, Any]:
    """The pre-filled form: the reviewed answer if exported, else the draft."""
    source = case.get("expected") or {f: v["value"] for f, v in case["draft"]["labels"].items()}
    return {score_name(f): (source.get(f) or None) for f in LABEL_FIELDS}


def phrase_answers(case: dict[str, Any]) -> dict[str, Any]:
    phrases = case.get("expected_phrases") or case["draft"]["search_phrases"]
    verdict = (case.get("phrase_review") or {}).get("model_phrases") or case["draft"].get("model_phrases_verdict")
    return {score_name("search_phrases"): PHRASE_SEPARATOR.join(phrases), score_name("model_phrases"): verdict}


def score_specs(queue: str) -> list[dict[str, Any]]:
    if queue == LABEL_QUEUE:
        return ([{"name": score_name(f), "categories": list(v), "description": DESCRIPTIONS[f]} for f, v in CATEGORICAL.items()]
                + [{"name": score_name(f), "description": DESCRIPTIONS[f]} for f in TEXT_LABELS])
    return [{"name": score_name("search_phrases"), "description": DESCRIPTIONS["search_phrases"]},
            {"name": score_name("model_phrases"), "categories": list(PHRASE_VERDICTS),
             "description": DESCRIPTIONS["model_phrases"]}]


# ---------------------------------------------------------------- export

def apply_label_review(case: dict[str, Any], answers: dict[str, Any], reviewer: str) -> list[str]:
    """Write reviewed labels into `expected`; return the fields the reviewer changed."""
    problems = [f for f in CATEGORICAL if answers.get(score_name(f)) not in CATEGORICAL[f]]
    if problems:
        raise ValueError(f"{case['company_number']}: no valid answer for {', '.join(problems)}")
    expected = {f: (str(answers.get(score_name(f))).strip() or None) if answers.get(score_name(f)) is not None else None
                for f in LABEL_FIELDS}
    draft = {f: v["value"] for f, v in case["draft"]["labels"].items()}
    changed = [f for f in LABEL_FIELDS if expected.get(f) != draft.get(f)]
    case["expected"] = expected
    case["review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": utc_now(), "changed_fields": changed,
                      "label_source": case["draft"]["label_source"]}
    return changed


def apply_phrase_review(case: dict[str, Any], answers: dict[str, Any], reviewer: str) -> bool:
    text = answers.get(score_name("search_phrases")) or ""
    phrases = [" ".join(p.lower().split()) for p in str(text).replace("\n", ",").split(",")]
    phrases = list(dict.fromkeys(p for p in phrases if p))
    if not phrases:
        raise ValueError(f"{case['company_number']}: no reference phrases")
    verdict = answers.get(score_name("model_phrases"))
    case["expected_phrases"] = phrases
    case["phrase_review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": utc_now(),
                             "model_phrases": verdict if verdict in PHRASE_VERDICTS else None,
                             "phrases_changed": phrases != case["draft"]["search_phrases"]}
    return case["phrase_review"]["phrases_changed"]


# ---------------------------------------------------------------- Langfuse I/O

def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _load_map(path: Path = TRACE_MAP) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_map(entries: dict[str, dict[str, Any]], path: Path = TRACE_MAP) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _client():
    from scripts.langfuse_eval_helpers.langfuse_tracing import langfuse_from_config
    load_dotenv(Path(".env"))
    lf = langfuse_from_config(LANGFUSE_CONFIG)
    if lf is None:
        raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
    return lf


def sync(cases_dir: Path = GOLD_DIR, volumes: dict[str, int | None] | None = None) -> dict[str, Any]:
    from scripts.langfuse_eval_helpers.langfuse_annotation import (
        ensure_queue, ensure_score_configs, migrate_retired_scores, push_case_migrations, question_score_configs,
        seed_draft_scores, sync_queue_items)
    from scripts.langfuse_eval_helpers.langfuse_tracing import case_trace, flush, restate_trace

    lf = _client()
    volumes = volumes or {}
    # An excluded case (a second company on the same website) leaves both queues:
    # sync_queue_items drops any item not in the list. Its trace and scores stay.
    cases = [c for c in (load_case(p) for p in case_files(cases_dir)) if not c.get("excluded")]
    entries = _load_map()
    report: dict[str, Any] = {}
    for queue, kind, build, answers, done in (
            (LABEL_QUEUE, "labels", label_trace, label_answers, lambda c: (c.get("review") or {}).get("status") == "verified"),
            (PHRASE_QUEUE, "phrases", lambda c: phrase_trace(c, volumes), phrase_answers,
             lambda c: (c.get("phrase_review") or {}).get("status") == "verified")):
        config_ids = ensure_score_configs(lf, question_score_configs(score_specs(queue)))
        queue_id = ensure_queue(lf, queue, list(config_ids.values()))
        missing = set(config_ids.values()) - set(lf.api.annotation_queues.get_queue(queue_id).score_config_ids or [])
        if missing:
            raise SystemExit(f"queue {queue!r} lacks {len(missing)} of its score fields; the API cannot add them. "
                             "Add them in the Langfuse page (edit queue) or give the queue a new name.")
        created = restated = 0
        for case in cases:
            number = case["company_number"]
            name = f"{number} {case['company_name']} (web {kind})"
            trace_input, trace_output = build(case)
            content = _digest(trace_input, trace_output)
            key = f"{kind}:{number}"
            entry = entries.get(key)
            tags = ["web-profile-gold", f"web-{kind}", f"company:{number}"]
            metadata = {"company_number": number, "company_name": case["company_name"]}
            if entry is None:
                with case_trace(lf, name=name, tags=tags, metadata=metadata, input=trace_input, output=trace_output) as root:
                    entries[key] = {"trace_id": root.trace_id, "content": content}
                created += 1
            elif entry.get("content") != content:
                restate_trace(lf, entry["trace_id"], name, tags=tags, metadata=metadata,
                              input=trace_input, output=trace_output)
                entry["content"] = content
                restated += 1
        flush(lf)
        _save_map(entries)
        trace_ids = [entries[f"{kind}:{c['company_number']}"]["trace_id"] for c in cases]
        for case in cases:
            seed_draft_scores(lf, entries[f"{kind}:{case['company_number']}"]["trace_id"], answers(case), config_ids)
        flush(lf)
        migrated = 0
        if kind == "labels":
            # A draft value changed by a rule change is rewritten only where the
            # score still holds the old value, so a reviewer's own answer stays.
            for case in cases:
                trace_id = entries[f"labels:{case['company_number']}"]["trace_id"]
                migrated += len(migrate_retired_scores(lf, trace_id, RETIRED_SCORES, config_ids))
                migrations = [{**m, "field": score_name(m["field"])} for m in case["draft"].get("migrations") or []]
                migrated += len(push_case_migrations(lf, trace_id, migrations, config_ids))
            flush(lf)
        complete = [entries[f"{kind}:{c['company_number']}"]["trace_id"] for c in cases if done(c)]
        report[queue] = {"queue_id": queue_id, "cases": len(cases), "created": created, "restated": restated,
                         "migrated_scores": migrated,
                         **sync_queue_items(lf, queue_id, trace_ids, complete=complete)}
    report["retired queues emptied"] = _empty_retired_queues(lf)
    return report


def _empty_retired_queues(lf: Any) -> int:
    from scripts.langfuse_eval_helpers.langfuse_annotation import _all_queue_items, find_queue_id
    removed = 0
    for name in RETIRED_QUEUES:
        queue_id = find_queue_id(lf, name)
        for item in _all_queue_items(lf, queue_id) if queue_id else []:
            lf.api.annotation_queues.delete_queue_item(queue_id, item.id)
            removed += 1
    return removed


def export(reviewer: str, cases_dir: Path = GOLD_DIR) -> dict[str, Any]:
    from scripts.langfuse_eval_helpers.langfuse_annotation import completed_trace_ids, find_queue_id, read_annotations

    lf = _client()
    entries = _load_map()
    result: dict[str, Any] = {}
    for queue, kind, fields, apply in (
            (LABEL_QUEUE, "labels", [score_name(f) for f in LABEL_FIELDS], apply_label_review),
            (PHRASE_QUEUE, "phrases", [score_name("search_phrases"), score_name("model_phrases")], apply_phrase_review)):
        queue_id = find_queue_id(lf, queue)
        signed_off = completed_trace_ids(lf, queue_id) if queue_id else set()
        written = changed = 0
        problems: list[str] = []
        for path in case_files(cases_dir):
            case = load_case(path)
            if case.get("excluded"):
                continue
            entry = entries.get(f"{kind}:{case['company_number']}")
            if entry is None or entry["trace_id"] not in signed_off:
                continue
            answers = read_annotations(lf, entry["trace_id"], fields)
            try:
                changed += int(bool(apply(case, answers, reviewer)))
            except ValueError as exc:
                problems.append(str(exc))
                continue
            save_case(path, case)
            written += 1
        result[queue] = {"completed": len(signed_off), "written": written, "changed_by_reviewer": changed,
                         "problems": problems}
    return result


def reopen(numbers: Iterable[str], kind: str = "labels") -> int:
    """Set completed queue items back to PENDING, for cases whose draft changed
    after the reviewer signed them off (a new field, a migrated value)."""
    from scripts.langfuse_eval_helpers.langfuse_annotation import _all_queue_items, find_queue_id

    lf = _client()
    queue_id = find_queue_id(lf, LABEL_QUEUE if kind == "labels" else PHRASE_QUEUE)
    wanted = {_load_map()[f"{kind}:{n}"]["trace_id"] for n in numbers}
    count = 0
    for item in _all_queue_items(lf, queue_id):
        if item.object_id in wanted and str(getattr(item, "status", "")).upper().endswith("COMPLETED"):
            lf.api.annotation_queues.update_queue_item(queue_id, item.id, status="PENDING")
            count += 1
    return count


def queue_urls(report: dict[str, Any]) -> dict[str, str]:
    """Links to the annotation queues on the local Langfuse."""
    import os
    host = (os.getenv("LANGFUSE_HOST") or "http://localhost:3000").rstrip("/")
    lf = _client()
    project_id = None
    try:
        project_id = lf.api.projects.get().data[0].id
    except Exception:  # noqa: BLE001 -- the link is a convenience
        pass
    return {queue: (f"{host}/project/{project_id}/annotation-queues/{info['queue_id']}" if project_id
                    else f"{host} (Annotation Queues: {queue})") for queue, info in report.items()
            if isinstance(info, dict)}


# ---------------------------------------------------------------- CLI

def _model_phrases(version: str = policy.PROMPT_VERSION) -> dict[str, list[str]]:
    from scripts.website_analysis.web_profile_eval import load_checkpoint
    return {k[3]: r.get("seed_keywords") or [] for k, r in load_checkpoint().items() if k[1] == version and k[2] == 1}


def _volumes() -> dict[str, int | None]:
    """Search volumes already looked up (cache only, no calls)."""
    if not PHRASE_VOLUME.exists():
        return {}
    return {r["keyword"]: r.get("search_volume") for r in json.loads(PHRASE_VOLUME.read_text(encoding="utf-8"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    sub = parser.add_subparsers(dest="command", required=True)
    cases_cmd = sub.add_parser("cases")
    cases_cmd.add_argument("--drafts", type=Path, required=True)
    sub.add_parser("sync")
    exp = sub.add_parser("export")
    exp.add_argument("--reviewer", required=True)
    sub.add_parser("status")
    reopen_cmd = sub.add_parser("reopen")
    reopen_cmd.add_argument("--numbers", nargs="+", required=True)
    reopen_cmd.add_argument("--kind", choices=("labels", "phrases"), default="labels")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.command == "cases":
        from scripts.website_analysis import web_profile_eval as ev
        from scripts.website_analysis.web_fetch import Fetcher
        drafts = json.loads(args.drafts.read_text(encoding="utf-8"))
        conn = sqlite3.connect(args.db, timeout=30.0)
        try:
            from companies_house_core.companies_house_extractor import filed_report_text
            from scripts.search_screen_classifier.search_screen_population import xhtml_path

            def filing_text(number: str) -> str | None:
                path = xhtml_path(number)
                return filed_report_text(Path(path).read_text(encoding="utf-8", errors="ignore")) if path else None

            inputs = {c["company_number"]: c for c in ev.build_cases(
                conn, sorted(drafts["cases"]), Fetcher(cache_only=True), [], filing_text=filing_text)}
        finally:
            conn.close()
        print(f"{len(build_cases(drafts, inputs, _model_phrases()))} case files in {GOLD_DIR}")
    elif args.command == "sync":
        report = sync(volumes=_volumes())
        print(json.dumps(report, indent=2))
        for queue, url in queue_urls(report).items():
            print(f"{queue}: {url}")
    elif args.command == "reopen":
        print(f"{reopen(args.numbers, args.kind)} items set back to pending")
    elif args.command == "export":
        print(json.dumps(export(args.reviewer), indent=2))
    elif args.command == "status":
        cases = [load_case(p) for p in case_files(GOLD_DIR)]
        excluded = [c["company_number"] for c in cases if c.get("excluded")]
        cases = [c for c in cases if not c.get("excluded")]
        print(json.dumps({"cases": len(cases), "excluded": excluded,
                          "labels_reviewed": sum((c.get("review") or {}).get("status") == "verified" for c in cases),
                          "phrases_reviewed": sum((c.get("phrase_review") or {}).get("status") == "verified" for c in cases)},
                         indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
