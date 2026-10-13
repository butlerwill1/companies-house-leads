#!/usr/bin/env python3
"""Langfuse annotation queue for reviewing the search-screen drafts.

One trace per case. The trace input is the whole filed report with only the
independent auditor's report removed (``sections.filed_report``), the output is the draft label with its quote and reason, and the
``search_screen`` score is pre-filled with that draft. The reviewer opens the
queue, reads, corrects the dropdown if they disagree, optionally adds a note,
and presses Complete. ``export`` then writes every COMPLETED item back into its
case file as a verified label.

Blind cases that have no ``blind_review`` yet are kept out of the queue (their
drafts would be visible there); once blind labels are imported they join it as
already-verified items. Nothing here calls a model.

Usage:
    python -m scripts.search_screen_classifier.search_screen_queue sync
    python -m scripts.search_screen_classifier.search_screen_queue export --reviewer will
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from scripts.langfuse_eval_helpers.langfuse_annotation import (  # noqa: E402
    completed_trace_ids,
    ensure_queue,
    ensure_score_configs,
    find_queue_id,
    question_score_configs,
    read_annotations,
    seed_draft_scores,
    sync_queue_items,
)
from scripts.langfuse_eval_helpers.langfuse_tracing import case_trace, flush, langfuse_from_config  # noqa: E402
from scripts.business_profile_classifier.business_profile_eval import case_files, load_case, save_case, utc_now  # noqa: E402
from scripts.search_screen_classifier.search_screen_cases import CASES_DIR, SCREEN_LABELS  # noqa: E402
from scripts.search_screen_classifier.search_screen_publish import FULL_TEXT_NOTE, LANGFUSE_CONFIG, best_label, full_filing_text  # noqa: E402

QUEUE_NAME = "Search screen gold-label review"
TRACE_MAP = Path("logs/search-screen/annotation-traces.json")
# Digest of the input/output last written to each trace, so a changed case is
# restated in place (same trace id, so queue items and scores are kept).
CONTENT_MAP = Path("logs/search-screen/annotation-trace-content.json")
LABEL_FIELD = "search_screen"
NOTES_FIELD = "review_notes"
TAGS = ["search-screen-review"]

QUESTION = (
    "Would a potential customer, consumer or business, look for a business like this online and then "
    "buy, book or enquire directly?  likely = customers choose suppliers themselves (shops, hospitality, "
    "clinics, pharmacies, dealers, trades, searchable B2B).  possible = trading, but the text does not show "
    "how customers choose.  unlikely = holding, SPV, financing or concession vehicle, captive supply, "
    "tender- or framework-only, a handful of large contracted buyers, investment property.  "
    "A group parent follows the group's trade."
)


def question_specs() -> list[dict[str, Any]]:
    return [
        {"name": LABEL_FIELD, "categories": list(SCREEN_LABELS),
         "description": "Search-addressable? likely / possible / unlikely"},
        {"name": NOTES_FIELD, "description": "Optional: why you changed the label"},
    ]


def in_queue(case: dict[str, Any]) -> bool:
    """A blind case waits outside the queue until its blind label is imported."""
    if case.get("blind") and not case.get("blind_review"):
        return False
    return best_label(case)[0] is not None


def trace_name(case: dict[str, Any]) -> str:
    return f"{case['company_number']} {case.get('company_name') or ''} (search-screen review)".replace("  ", " ")


def trace_input(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": QUESTION,
        "company": f"{case['company_number']} {case.get('company_name')}",
        "sic": case.get("sic_label"),
        "financial_year": case.get("financial_year"),
        "financials": case.get("financials"),
        "text_source": FULL_TEXT_NOTE,
        "filing_text": full_filing_text(case),
    }


def trace_output(case: dict[str, Any]) -> dict[str, Any]:
    label, status = best_label(case)
    return {"draft_label": label["value"], "quote": label.get("quote"), "reason": label.get("reason"), "label_status": status}


def draft_answers(case: dict[str, Any]) -> dict[str, Any]:
    label, _ = best_label(case)
    return {LABEL_FIELD: label["value"] if label else None}


def apply_review(case: dict[str, Any], value: str, notes: str | None, *, reviewer: str, now: str) -> None:
    """Turn a completed queue item into a verified label. Agreement keeps the
    draft's quote and reason as the evidence; a change drops them, as a quote
    arguing for the old label would contradict the new one."""
    if value not in SCREEN_LABELS:
        raise ValueError(f"{case['company_number']}: {value!r} is not one of {SCREEN_LABELS}")
    draft, _ = best_label(case)
    draft = draft or {}
    agreed = value == draft.get("value")
    case["expected"] = {"search_screen": dict(draft) if agreed else
                        {"value": value, "quote": None, "section": None, "reason": notes or None}}
    case["review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": now,
                      "draft_source": "draft_full", "changed_from": None if agreed else draft.get("value"),
                      "notes": notes or None}


def _load_map() -> dict[str, str]:
    return json.loads(TRACE_MAP.read_text(encoding="utf-8")) if TRACE_MAP.is_file() else {}


def _save_map(mapping: dict[str, str], path: Path = TRACE_MAP) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def content_digest(trace_input: Any, trace_output: Any) -> str:
    payload = json.dumps([trace_input, trace_output], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _client() -> Any:
    load_dotenv(Path(".env"))
    client = langfuse_from_config(LANGFUSE_CONFIG)
    if client is None:
        raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
    return client


def sync(cases_dir: Path) -> dict[str, Any]:
    lf = _client()
    config_ids = ensure_score_configs(lf, question_score_configs(question_specs()))
    queue_id = ensure_queue(lf, QUEUE_NAME, list(config_ids.values()))
    cases = [c for c in (load_case(p) for p in case_files(cases_dir)) if in_queue(c)]
    mapping = _load_map()
    contents = json.loads(CONTENT_MAP.read_text(encoding="utf-8")) if CONTENT_MAP.is_file() else {}
    signed_off = completed_trace_ids(lf, queue_id)
    carried: dict[str, dict[str, Any]] = {}  # company -> the reviewer's answers on its old trace
    keep_complete: set[str] = set()
    created = rebuilt = 0
    for case in cases:
        number = case["company_number"]
        kwargs = dict(
            name=trace_name(case), tags=TAGS + [f"company:{number}", f"cohort:{case['cohort']}"],
            metadata={"company_number": number, "company_name": case.get("company_name"),
                      "cohort": case["cohort"], "hard_category": case.get("hard_category")},
            input=trace_input(case), output=trace_output(case),
        )
        digest = content_digest(kwargs["input"], kwargs["output"])
        old = mapping.get(number)
        if old is not None and contents.get(number) == digest:
            continue
        if old is not None:
            # The input or draft changed. Restating a trace in place (an extra
            # root span) did not reliably change what the annotation view
            # shows (2026-09-30: the queue kept showing the shortened filing),
            # so the case gets a fresh trace. Whatever the reviewer entered on
            # the old one, and its COMPLETED status, is carried over first.
            carried[number] = read_annotations(lf, old, [LABEL_FIELD, NOTES_FIELD])
            if old in signed_off:
                keep_complete.add(number)
            rebuilt += 1
        else:
            created += 1
        with case_trace(lf, **kwargs) as root:
            mapping[number] = root.trace_id
        contents[number] = digest
    flush(lf)
    _save_map(mapping)
    _save_map(contents, CONTENT_MAP)
    for case in cases:
        number = case["company_number"]
        answers = {**draft_answers(case), **{k: v for k, v in carried.get(number, {}).items() if v is not None}}
        seed_draft_scores(lf, mapping[number], answers, config_ids)
    flush(lf)
    complete = {mapping[n] for n in keep_complete} | {
        mapping[c["company_number"]] for c in cases if (c.get("review") or {}).get("status") == "verified"}
    complete |= {mapping[c["company_number"]] for c in cases if mapping[c["company_number"]] in signed_off}
    result = sync_queue_items(lf, queue_id, [mapping[c["company_number"]] for c in cases], complete=complete)
    return {"queue": QUEUE_NAME, "cases": len(cases), "new_traces": created, "rebuilt_traces": rebuilt,
            "carried_complete": sorted(keep_complete), **result}


def export(cases_dir: Path, reviewer: str) -> dict[str, int]:
    lf = _client()
    queue_id = find_queue_id(lf, QUEUE_NAME)
    signed_off = completed_trace_ids(lf, queue_id) if queue_id else set()
    mapping = _load_map()
    now = utc_now()
    verified = changed = incomplete = 0
    for path in case_files(cases_dir):
        case = load_case(path)
        if (case.get("review") or {}).get("status") == "verified":
            continue
        trace_id = mapping.get(case["company_number"])
        if trace_id is None or trace_id not in signed_off:
            continue
        answers = read_annotations(lf, trace_id, [LABEL_FIELD, NOTES_FIELD])
        if answers.get(LABEL_FIELD) is None:
            incomplete += 1
            continue
        apply_review(case, answers[LABEL_FIELD], answers.get(NOTES_FIELD) or None, reviewer=reviewer, now=now)
        changed += bool(case["review"]["changed_from"])
        save_case(path, case)
        verified += 1
    return {"verified": verified, "changed_by_reviewer": changed, "completed_but_incomplete": incomplete}


# --- Blind queue -------------------------------------------------------------
# The blind cases get their own queue: the full filing and the question, but no
# draft in the trace and no pre-filled score, so the reviewer's label is
# independent of the drafter's. Export writes it as `blind_review` (and as the
# verified gold), which is what `blind_agreement` compares with the drafts.
BLIND_QUEUE_NAME = "Search screen BLIND labelling (no drafts shown)"
BLIND_TRACE_MAP = Path("logs/search-screen/blind-annotation-traces.json")
BLIND_TAGS = ["search-screen-blind"]


def blind_cases(cases_dir: Path) -> list[dict[str, Any]]:
    return [c for c in (load_case(p) for p in case_files(cases_dir)) if c.get("blind")]


def blind_trace_output() -> dict[str, Any]:
    return {"note": "Blind case: no draft is shown. Pick a label in the Annotate panel, then Complete."}


def sync_blind(cases_dir: Path) -> dict[str, Any]:
    lf = _client()
    config_ids = ensure_score_configs(lf, question_score_configs(question_specs()))
    queue_id = ensure_queue(lf, BLIND_QUEUE_NAME, list(config_ids.values()))
    mapping = json.loads(BLIND_TRACE_MAP.read_text(encoding="utf-8")) if BLIND_TRACE_MAP.is_file() else {}
    created = 0
    for case in blind_cases(cases_dir):
        number = case["company_number"]
        if number in mapping:
            continue
        with case_trace(lf, name=f"{number} {case.get('company_name') or ''} (blind label)",
                        tags=BLIND_TAGS + [f"company:{number}"],
                        metadata={"company_number": number, "company_name": case.get("company_name")},
                        input=trace_input(case), output=blind_trace_output()) as root:
            mapping[number] = root.trace_id
        created += 1
    flush(lf)
    _save_map(mapping, BLIND_TRACE_MAP)
    # No seed_draft_scores here: the answer box must start empty.
    result = sync_queue_items(lf, queue_id, list(mapping.values()))
    return {"queue": BLIND_QUEUE_NAME, "cases": len(mapping), "new_traces": created, **result}


def apply_blind(case: dict[str, Any], value: str, notes: str | None, *, reviewer: str, now: str) -> None:
    if value not in SCREEN_LABELS:
        raise ValueError(f"{case['company_number']}: {value!r} is not one of {SCREEN_LABELS}")
    case["blind_review"] = {"value": value, "reviewer": reviewer, "reviewed_at": now, "notes": notes or None}
    case["expected"] = {"search_screen": {"value": value, "quote": None, "section": None, "reason": notes or None}}
    case["review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": now, "draft_source": "blind",
                      "changed_from": None, "notes": notes or None}


def export_blind(cases_dir: Path, reviewer: str) -> dict[str, int]:
    lf = _client()
    queue_id = find_queue_id(lf, BLIND_QUEUE_NAME)
    signed_off = completed_trace_ids(lf, queue_id) if queue_id else set()
    mapping = json.loads(BLIND_TRACE_MAP.read_text(encoding="utf-8")) if BLIND_TRACE_MAP.is_file() else {}
    now = utc_now()
    written = incomplete = 0
    for path in case_files(cases_dir):
        case = load_case(path)
        trace_id = mapping.get(case["company_number"])
        if not case.get("blind") or case.get("blind_review") or trace_id not in signed_off:
            continue
        answers = read_annotations(lf, trace_id, [LABEL_FIELD, NOTES_FIELD])
        if answers.get(LABEL_FIELD) is None:
            incomplete += 1
            continue
        apply_blind(case, answers[LABEL_FIELD], answers.get(NOTES_FIELD) or None, reviewer=reviewer, now=now)
        save_case(path, case)
        written += 1
    return {"blind_labels_written": written, "completed_but_no_label": incomplete}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-dir", default=str(CASES_DIR))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="Create the queue, traces and pre-filled draft scores.")
    exported = commands.add_parser("export", help="Write COMPLETED queue items back as verified labels.")
    exported.add_argument("--reviewer", required=True)
    commands.add_parser("sync-blind", help="Create the blind queue: filings only, nothing pre-filled.")
    blind_export = commands.add_parser("export-blind", help="Write COMPLETED blind items back as blind labels.")
    blind_export.add_argument("--reviewer", required=True)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cases_dir = Path(args.cases_dir)
    result = {"sync": lambda: sync(cases_dir), "export": lambda: export(cases_dir, args.reviewer),
              "sync-blind": lambda: sync_blind(cases_dir),
              "export-blind": lambda: export_blind(cases_dir, args.reviewer)}[args.command]()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
