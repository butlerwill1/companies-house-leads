#!/usr/bin/env python3
"""
Create, run and score a human-labelled gold set for the business-profile
stage (Gate A2). Mirrors the shape of scripts/pdf_vision_extraction/vlm_financial_eval.py --
same case-file format, same config format -- without the vision-specific
machinery that stage needs and this one does not.

Scoring is deterministic: field values are compared by exact string match
against a human-reviewed "expected" block. An LLM is never used to judge
whether an extraction is correct (a DeepEval faithfulness score on the one
free-text field, business_description, is logged alongside as an advisory
signal only -- it never gates a case).

Eval tracing goes to the self-hosted Langfuse instance (see
docs/LANGFUSE_SETUP.md): a `run` is a Langfuse dataset run, one trace per
case, with per-field correctness as scores. Human gold-label review goes
through a Langfuse annotation queue.

Usage:
    python -m scripts.business_profile_classifier.business_profile_eval initialise --db companies-house.db --count 50
    python -m scripts.business_profile_classifier.business_profile_review --cases-dir evals/business_profile_gold_set/cases
    python -m scripts.business_profile_classifier.business_profile_eval run --config evals/business_profile_gold_set/configs/openrouter-gemini.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from companies_house_core.companies_house_extractor import load_dotenv  # noqa: E402
from scripts.langfuse_eval_helpers import deepeval_judges  # noqa: E402
from scripts.langfuse_eval_helpers.langfuse_annotation import (  # noqa: E402
    completed_trace_ids,
    ensure_queue,
    ensure_score_configs,
    find_queue_id,
    migrate_retired_scores,
    push_case_migrations,
    question_score_configs,
    read_annotations,
    seed_draft_scores,
    sync_queue_items,
)
from scripts.langfuse_eval_helpers.langfuse_runs import (  # noqa: E402
    evaluation,
    experiment_run_name,
    run_experiment,
    sync_dataset,
)
from scripts.langfuse_eval_helpers.langfuse_tracing import (  # noqa: E402
    case_trace,
    flush,
    langfuse_from_config,
    observation,
    restate_trace,
)
from scripts.business_profile_classifier.business_profile_metrics import (  # noqa: E402
    SCORED_FIELDS,
    compute_metrics,
    flatten_metrics,
    score_case,
)
from scripts.business_profile_classifier.business_profile_policy import (  # noqa: E402
    FIELD_VALUES,
    NARRATIVE_SECTION_PRIORITY,
    PROMPT_VERSION,
    RETIRED_VALUES,
    SIC_AGREEMENT_VALUES,
    build_prompt,
)
from scripts.business_profile_classifier.business_profile_prompt_registry import registered_prompt_reference  # noqa: E402
from scripts.business_profile_classifier.companies_house_business_profile import (  # noqa: E402
    BusinessProfileModelClient,
    extract_business_profile,
    fetch_narrative_context,
    load_config,
)

CASE_SCHEMA_VERSION = 1
DATASET_NAME = "business-profile-gold"
ANNOTATION_QUEUE_NAME = "Business profile gold-label review"
LABEL_SOURCE_ID = "claude-opus-5"

# Where sync-annotation-queue records the trace it created per case so
# export-annotations can find it again without a trace search.
ANNOTATION_TRACE_MAP = Path("logs/business-profile-eval/annotation-traces.json")
CHECKPOINT_FIELDS = ("prompt", "raw", "extracted", "errors", "scored")
CHECKPOINT_SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def case_files(cases_dir: Path) -> list[Path]:
    return sorted(path for path in cases_dir.glob("*.json") if path.name != "manifest.json")


def load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_case(path: Path, case: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def empty_expected() -> dict[str, Any]:
    expected: dict[str, Any] = {"business_description": None}
    for field in FIELD_VALUES:
        expected[field] = {"value": None, "quote": None, "section": None}
    expected["sic_agreement"] = {"value": None, "reason": None}
    return expected


def migrate_retired_gold_labels(cases_dir: Path) -> dict[str, int]:
    """Apply value-wide taxonomy renames to reviewed gold labels once.

    The model drafts remain historical evidence of what the drafting model
    answered. Only the reviewed ``expected`` value changes, accompanied by a
    per-case migration record, so the operation is safe to re-run and does
    not turn a taxonomy rename into a new human judgement.
    """
    migrated_cases = 0
    migrated_fields = 0
    for path in case_files(cases_dir):
        case = load_case(path)
        expected = case.get("expected") or {}
        review = case.setdefault("review", {})
        migrations = review.setdefault("taxonomy_migrations", [])
        changed = False
        for field, mapping in RETIRED_VALUES.items():
            entry = expected.get(field)
            if not isinstance(entry, dict):
                continue
            old_value = entry.get("value")
            new_value = mapping.get(old_value)
            if new_value is None:
                continue
            entry["value"] = new_value
            migrations.append(
                {
                    "field": field,
                    "from": old_value,
                    "to": new_value,
                    "migrated_at": utc_now(),
                    "prompt_version": PROMPT_VERSION,
                    "note": "value-wide taxonomy rename; reviewed judgement and evidence kept",
                }
            )
            migrated_fields += 1
            changed = True
        if changed:
            save_case(path, case)
            migrated_cases += 1
    return {"cases": migrated_cases, "fields": migrated_fields}


def build_case(conn: sqlite3.Connection, company_number: str) -> dict[str, Any] | None:
    context = fetch_narrative_context(conn, company_number)
    if context is None or not context["sections"]:
        return None
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "company_number": company_number,
        "company_name": context["company_name"],
        "financial_year": context["financial_year"],
        "sic_code": context["sic_code"],
        "sic_label": context["sic_label"],
        "narrative_run_id": context["narrative_run_id"],
        "sections": context["sections"],
        "expected": empty_expected(),
        "review": {"status": "unreviewed", "reviewed_at": None},
    }


def select_candidate_companies(
    conn: sqlite3.Connection,
    count: int,
    seed: int,
    prefer_sic_prefixes: Sequence[str] | None = None,
) -> list[str]:
    """A diverse sample: spread across Gate A trading_status and across SIC
    groups, not just the highest-turnover companies. A gold set that is all
    obvious trading companies would never exercise the "unclear" path or
    the investment_holding / spv values, which is exactly the ambiguity
    this stage exists to resolve.

    ``prefer_sic_prefixes`` tilts (does not restrict) the sample: within each
    trading_status bucket, companies whose primary SIC code starts with one of
    these prefixes are drawn first. Used to rebalance a gold set that has
    drifted B2B-heavy -- e.g. prefixes for retail / hospitality / consumer
    services pull in more ``consumer_search`` / ``b2c`` cases. The
    trading_status spread and the unseen-SIC preference within that are kept.
    """
    rows = conn.execute(
        """
        select nr.company_number,
               coalesce(max(case when s.signal_key = 'trading_status' then s.signal_text end), 'unknown') as trading_status,
               c.sic_code_primary
        from narrative_runs nr
        join companies c on c.company_number = nr.company_number
        left join company_signals s on s.company_number = nr.company_number
        group by nr.company_number
        """
    ).fetchall()

    prefixes = tuple(prefer_sic_prefixes or ())

    def is_preferred(sic_code: str | None) -> bool:
        return bool(prefixes) and bool(sic_code) and sic_code.startswith(prefixes)

    buckets: dict[str, list[tuple[str, str | None]]] = {}
    for company_number, trading_status, sic_code in rows:
        buckets.setdefault(trading_status, []).append((company_number, sic_code))

    rng = random.Random(seed)
    for bucket in buckets.values():
        rng.shuffle(bucket)
        if prefixes:
            # Stable partition: preferred companies first, original shuffled
            # order preserved within each half.
            bucket.sort(key=lambda entry: not is_preferred(entry[1]))

    selected: list[str] = []
    seen_sic: set[str] = set()
    bucket_names = sorted(buckets)
    index = 0
    while len(selected) < count and any(buckets.values()):
        bucket = buckets[bucket_names[index % len(bucket_names)]]
        index += 1
        if not bucket:
            if all(not b for b in buckets.values()):
                break
            continue
        pick_index = next((i for i, (_, sic) in enumerate(bucket) if sic not in seen_sic), 0)
        company_number, sic_code = bucket.pop(pick_index)
        selected.append(company_number)
        if sic_code:
            seen_sic.add(sic_code)
    return selected


# Consumer-facing SIC divisions: retail (47), land transport incl taxis (49),
# accommodation & food (55/56), publishing & broadcasting (58-60), travel (79),
# education (85), arts & recreation (90-93), membership orgs & repair (94-96).
# A gold set drawn without this bias skews B2B-relationship heavy; these
# prefixes pull in more consumer_search / local_service / b2c cases to check.
CONSUMER_SIC_PREFIXES = (
    "47", "49", "55", "56", "58", "59", "60", "79", "85", "90", "91", "92", "93", "94", "95", "96",
)


def initialise_cases(
    db_path: Path,
    cases_dir: Path,
    count: int,
    seed: int,
    prefer_sic_prefixes: Sequence[str] | None = None,
    company_numbers: Sequence[str] | None = None,
) -> int:
    """Create unreviewed cases. Normally draws a pseudo-random sample, but
    ``company_numbers`` takes exactly the companies named instead -- the gold
    set sometimes needs a specific case (an archetype a taxonomy rule has to
    survive, a company a labelling disagreement turned on), and a sample
    biased by SIC prefix cannot be asked for one."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {path.stem for path in case_files(cases_dir)}
        if company_numbers:
            created = 0
            for company_number in company_numbers:
                if company_number in existing:
                    continue
                case = build_case(conn, company_number)
                if case is None:
                    print(f"  skipped {company_number}: no narrative sections", file=sys.stderr)
                    continue
                save_case(cases_dir / f"{company_number}.json", case)
                created += 1
            return created
        candidates = [
            c
            for c in select_candidate_companies(
                conn, count + len(existing), seed, prefer_sic_prefixes
            )
            if c not in existing
        ]
        created = 0
        for company_number in candidates:
            if created >= count:
                break
            case = build_case(conn, company_number)
            if case is None:
                continue
            save_case(cases_dir / f"{company_number}.json", case)
            created += 1
        return created
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Shared case-level helpers
# ---------------------------------------------------------------------------

_SECTION_ORDER = tuple(NARRATIVE_SECTION_PRIORITY)


def _narrative_preview(case: dict[str, Any]) -> str:
    """The case's filed narrative joined into one readable block, priority
    sections first. Langfuse stores full observation IO (2 MiB field limit),
    so unlike the old MLflow path this is never truncated to fit a tag."""
    sections = case.get("sections") or {}
    ordered = [key for key in _SECTION_ORDER if sections.get(key)]
    ordered += [key for key in sections if key not in ordered and sections.get(key)]
    return "\n\n".join(f"[{key}]\n{sections[key]}" for key in ordered)


def _case_trace_inputs(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_number": case["company_number"],
        "company_name": case.get("company_name"),
        "sic_code": case.get("sic_code"),
        "sic_label": case.get("sic_label"),
        "narrative_sections": _narrative_preview(case),
    }


def _case_trace_outputs(case: dict[str, Any]) -> dict[str, Any]:
    return {"draft_expected": case.get("expected")}


def _draft_answers(case: dict[str, Any]) -> dict[str, Any]:
    """The case's current expected values, one flat dict keyed by question
    name -- what a reviewer's form opens pre-filled with."""
    expected = case.get("expected") or {}
    answers: dict[str, Any] = {"business_description": expected.get("business_description")}
    for field in FIELD_VALUES:
        answers[field] = (expected.get(field) or {}).get("value")
    answers["sic_agreement"] = (expected.get("sic_agreement") or {}).get("value")
    return answers


def _review_question_specs() -> list[dict[str, Any]]:
    """Score-config specs for the annotation queue: business_description is
    free text, every other field is its taxonomy as a category list."""
    specs: list[dict[str, Any]] = [
        {"name": "business_description", "description": "One plain sentence: what the company does."}
    ]
    for field, values in FIELD_VALUES.items():
        specs.append(
            {"name": field, "categories": list(values), "description": f"Confirm or correct {field}."}
        )
    specs.append(
        {"name": "sic_agreement", "categories": list(SIC_AGREEMENT_VALUES), "description": "Confirm or correct sic_agreement."}
    )
    return specs


def _review_field_names() -> list[str]:
    return ["business_description", *FIELD_VALUES.keys(), "sic_agreement"]


def _run_one_case(
    client: BusinessProfileModelClient, model: str, timeout: int, case: dict[str, Any]
) -> dict[str, Any]:
    """One model call + deterministic score for one gold case. Never raises --
    a failed request becomes a rejected outcome with a traceable prompt."""
    context = {
        "company_name": case["company_name"],
        "sections": case["sections"],
        "sic_label": case["sic_label"],
        "sic_code": case["sic_code"],
        "financial_year": case["financial_year"],
    }
    try:
        extracted, errors, prompt, raw = extract_business_profile(client, model, context, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- one bad call must not sink the run, and still needs a trace
        extracted, raw = None, None
        errors = [f"request failed: {exc}"]
        prompt = build_prompt(
            company_name=context["company_name"],
            sections=context["sections"],
            sic_label=context["sic_label"],
            sic_code=context["sic_code"],
        )
    scored = score_case(case, extracted)
    return {
        "case": case,
        "extracted": extracted,
        "errors": errors,
        "prompt": prompt,
        "raw": raw,
        "scored": scored,
    }


def _report(
    args: argparse.Namespace,
    model: str,
    cases: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    elapsed: float,
) -> dict[str, Any]:
    results = [o["scored"] for o in outcomes]
    metrics = compute_metrics(results)
    # Three grades of rejection since 2026-09-14. A response with no JSON is
    # rejected outright; a field whose quote or value fails is dropped on its
    # own and the rest of the response scores. The old whole-response count
    # survives as "responses touched by any rejection" so runs stay
    # comparable at a glance; the field-level rate is the honest one.
    rejected_outright = sum(1 for o in outcomes if o["extracted"] is None)
    responses_with_dropped_fields = sum(1 for o in outcomes if o["extracted"] is not None and o["errors"])
    fields_rejected = sum(
        1 for o in outcomes if o["extracted"] is not None
        for field in (*FIELD_VALUES, "sic_agreement")
        if (o["extracted"].get(field) or {}).get("value") is None
    ) + rejected_outright * (len(FIELD_VALUES) + 1)
    fields_total = len(outcomes) * (len(FIELD_VALUES) + 1)
    # Quotes accepted by the bounded fuzzy match rather than exactly. Not a
    # failure, but the count is the size of the tolerance actually used, and
    # it should stay small: a jump means the model has stopped quoting.
    fields_fuzzy_matched = sum(
        1 for o in outcomes if o["extracted"] is not None
        for field in (*FIELD_VALUES, "sic_agreement")
        if (o["extracted"].get(field) or {}).get("quote_match") == "fuzzy"
    )
    unclear_count = 0
    total_fields = 0
    for o in outcomes:
        if o["extracted"] is None:
            continue
        for field in FIELD_VALUES:
            total_fields += 1
            if o["extracted"].get(field, {}).get("value") == "unclear":
                unclear_count += 1
    return {
        "generated_at": utc_now(),
        "config": str(getattr(args, "config", None)),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "cases": len(cases),
        "responses_rejected_outright": rejected_outright,
        "responses_with_dropped_fields": responses_with_dropped_fields,
        "fields_rejected": fields_rejected,
        "field_pass_rate": round(1 - fields_rejected / fields_total, 4) if fields_total else None,
        "fields_fuzzy_matched": fields_fuzzy_matched,
        "quote_or_validation_rejections": rejected_outright + responses_with_dropped_fields,
        "quote_verification_pass_rate": (
            round(1 - (rejected_outright + responses_with_dropped_fields) / len(cases), 4) if cases else None
        ),
        "unclear_rate": round(unclear_count / total_fields, 4) if total_fields else None,
        "elapsed_seconds": round(elapsed, 1),
        "metrics": metrics,
        "field_accuracy": {f: m.get("accuracy") for f, m in metrics["fields"].items()},
        "field_scored_counts": {f: m.get("scored", 0) for f, m in metrics["fields"].items()},
        "results": results,
    }


def write_responses(outcomes: list[dict[str, Any]], directory: Path, *, model: str) -> Path:
    """One readable file per case with what the model was sent and what it
    said, verbatim, and why the harness accepted or rejected it. The report
    JSON carries only scores, and Langfuse holds the same text behind a UI;
    this is the copy you open in an editor when a number looks wrong."""
    directory.mkdir(parents=True, exist_ok=True)
    for outcome in outcomes:
        case = outcome["case"]
        errors = outcome.get("errors") or []
        if outcome.get("extracted") is None:
            verdict = "REJECTED (no usable JSON): " + "; ".join(errors)
        elif errors:
            verdict = f"accepted with {len(errors)} field(s) DROPPED: " + "; ".join(errors)
        else:
            verdict = "accepted"
        fuzzy = [
            field for field in (*FIELD_VALUES, "sic_agreement")
            if ((outcome.get("extracted") or {}).get(field) or {}).get("quote_match") == "fuzzy"
        ]
        if fuzzy:
            verdict += f"\n\nFuzzy quote match (not the model's exact words) on: {', '.join(fuzzy)}"
        scored = outcome.get("scored") or {}
        rows = "\n".join(
            f"| {field} | {r.get('expected')} | {r.get('actual')} | {'yes' if r.get('correct') else 'no'} |"
            for field, r in (scored.get("fields") or {}).items()
        )
        body = (
            f"# {case.get('company_name')} ({case.get('company_number')}) -- {model} @ {PROMPT_VERSION}\n\n"
            f"## Validation\n\n{verdict}\n\n"
            f"## Score\n\n| field | gold | model | correct |\n| --- | --- | --- | --- |\n{rows}\n\n"
            f"## Model response (verbatim)\n\n```json\n{outcome.get('raw') or '(no response)'}\n```\n\n"
            f"## Prompt sent (verbatim)\n\n```text\n{outcome.get('prompt') or ''}\n```\n"
        )
        (directory / f"{case.get('company_number')}.md").write_text(body, encoding="utf-8")
    return directory


def _print_summary(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    print(f"\n{report['cases']} cases, {report['elapsed_seconds']:.0f}s")
    print(
        f"validation: {report.get('responses_rejected_outright', 0)} responses rejected outright, "
        f"{report.get('responses_with_dropped_fields', 0)} with a dropped field "
        f"({report.get('fields_rejected', 0)} fields dropped; field pass rate {report.get('field_pass_rate')}; "
        f"{report.get('fields_fuzzy_matched', 0)} fields accepted on a fuzzy quote)"
    )
    print(f"unclear rate: {report['unclear_rate']}")
    search = metrics["search_addressable"]
    print(
        f"\nsearch-addressable (the business question): "
        f"precision={search['precision']} recall={search['recall']} F1={search['f1']} "
        f"({search['tp']}/{search['gold_positives']} found, "
        f"{search['missed_by_abstention']} missed by abstention)"
    )
    print("\nper field (only cases with a reviewed expected value count):")
    print(f"  {'field':<26}{'acc':>7}{'base':>7}{'cover':>7}{'macroF1':>9}{'accOnAns':>10}  n")
    for field, m in metrics["fields"].items():
        if not m.get("scored"):
            continue
        acc_on_answerable = m["accuracy_when_committed_on_answerable"]
        print(
            f"  {field:<26}{m['accuracy']:>7.3f}{m['majority_baseline']:>7.3f}"
            f"{m['coverage']:>7.3f}{(m['macro_f1'] if m['macro_f1'] is not None else 0):>9.3f}"
            f"{(acc_on_answerable if acc_on_answerable is not None else 0):>10.3f}"
            f"  {m['scored']}"
        )
        if m["classes_below_min_support"]:
            print(f"    too few examples to measure: {', '.join(m['classes_below_min_support'])}")
        bands = m.get("confidence_bands")
        if bands and any(b["support"] for b in bands["bands"]):
            band_str = "  ".join(
                f"[{b['range'][0]:.2f}-{b['range'][1]:.2f}) n={b['support']} acc={b['accuracy']:.2f}"
                for b in bands["bands"]
                if b["support"]
            )
            print(f"    confidence bands: {band_str}")
            if bands["missing_confidence"]:
                print(f"    ({bands['missing_confidence']} committed answers had no confidence to band)")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _score_plain(
    client: BusinessProfileModelClient, model: str, timeout: int, cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        outcome = _run_one_case(client, model, timeout, case)
        _print_outcome_line(i, len(cases), outcome)
        outcomes.append(outcome)
    return outcomes


def _print_outcome_line(i: int, n: int, outcome: dict[str, Any]) -> None:
    number = outcome["case"]["company_number"]
    if outcome["extracted"] is None:
        print(f"  [{i}/{n}] {number}: REJECTED -- {'; '.join(outcome['errors'])}", file=sys.stderr)
    elif outcome["errors"]:
        print(f"  [{i}/{n}] {number}: {len(outcome['errors'])} field(s) DROPPED -- {'; '.join(outcome['errors'])}",
              file=sys.stderr)


def _dataset_records(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": case["company_number"],
            "input": {
                "company_name": case["company_name"],
                "sic_code": case["sic_code"],
                "sic_label": case["sic_label"],
                "narrative_sections": _narrative_preview(case),
            },
            "expected": case.get("expected"),
            # Both keys are what the Experiments view's "Item Metadata"
            # filter can select a company by; the name is there so a
            # reviewer can filter on "contains" without knowing the number.
            "metadata": {
                "company_number": case["company_number"],
                "company_name": case.get("company_name"),
                "financial_year": case.get("financial_year"),
            },
        }
        for case in cases
    ]


def _checkpoint_identity(case: dict[str, Any], model: str, timeout: int, prompt: str) -> dict[str, Any]:
    """The immutable inputs a saved paid response is allowed to replay for."""
    frozen_case = {
        key: case.get(key)
        for key in ("company_number", "company_name", "financial_year", "sic_code", "sic_label", "sections", "expected")
    }
    return {
        "case_sha256": hashlib.sha256(
            json.dumps(frozen_case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "model_settings": {
            "model": model,
            "timeout_seconds": timeout,
            "temperature": 0,
            "response_format": "json_object",
        },
    }


def _expected_checkpoint_identity(case: dict[str, Any], model: str, timeout: int) -> dict[str, Any]:
    """Render the exact prompt before deciding whether a response may replay."""
    prompt = build_prompt(
        company_name=case["company_name"], sections=case["sections"],
        sic_label=case["sic_label"], sic_code=case["sic_code"],
    )
    return _checkpoint_identity(case, model, timeout, prompt)


def _load_run_checkpoint(
    path: Path, model: str, timeout: int, selected: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Load saved responses for this model and prompt version.

    A resumed run replays these results into new Langfuse traces without
    repeating model calls. A truncated final line from an interrupted process
    is ignored; deleting the file deliberately forces a fresh run.
    """
    if not path.is_file():
        return {}
    expected = {
        case["company_number"]: _expected_checkpoint_identity(case, model, timeout)
        for case in selected
    }
    saved: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("prompt_version") != PROMPT_VERSION or record.get("model") != model:
            continue
        number = record.get("company_number")
        if not isinstance(number, str) or number not in expected:
            continue
        wanted = expected[number]
        compatible = (
            record.get("checkpoint_schema_version") == CHECKPOINT_SCHEMA_VERSION
            and record.get("case_sha256") == wanted["case_sha256"]
            and record.get("prompt_sha256") == wanted["prompt_sha256"]
            and record.get("model_settings") == wanted["model_settings"]
        )
        if not compatible:
            raise ValueError(
                f"Checkpoint {path} cannot be reused for {number}: its frozen case, rendered prompt, "
                "or model settings differ. Use a new checkpoint path rather than replaying a paid response "
                "against changed inputs."
            )
        saved[number] = record
    return saved


def _append_run_checkpoint(path: Path, model: str, timeout: int, outcome: dict[str, Any]) -> None:
    """Durably append one completed response before continuing the batch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "company_number": outcome["case"]["company_number"],
        **_checkpoint_identity(outcome["case"], model, timeout, outcome["prompt"]),
        **{field: outcome.get(field) for field in CHECKPOINT_FIELDS},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _score_langfuse(
    lf: Any,
    config: dict[str, Any],
    client: BusinessProfileModelClient,
    model: str,
    timeout: int,
    verified: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    run_name: str,
    checkpoint_path: Path | None = None,
) -> list[dict[str, Any]]:
    # The dataset always holds the full verified gold set; the run is scoped
    # to `selected` (a --limit / company-number subset, or all of them).
    by_id = {case["company_number"]: case for case in selected}
    sync_dataset(lf, DATASET_NAME, _dataset_records(verified), description="Business-profile gold set")
    item_ids = None if len(selected) == len(verified) else [c["company_number"] for c in selected]

    outcomes: list[dict[str, Any]] = []
    checkpoints = _load_run_checkpoint(checkpoint_path, model, timeout, selected) if checkpoint_path else {}

    judge_metric = None
    if deepeval_judges.judge_enabled(config):
        try:
            judge = deepeval_judges.make_judge(config)
            judge_metric = deepeval_judges.business_description_faithfulness_metric(judge)
        except ImportError:
            print(
                "deepeval not installed; skipping business_description faithfulness.",
                file=sys.stderr,
            )

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        case = by_id[item.id]
        saved = checkpoints.get(case["company_number"])
        if saved is None:
            outcome = _run_one_case(client, model, timeout, case)
            if checkpoint_path:
                _append_run_checkpoint(checkpoint_path, model, timeout, outcome)
        else:
            outcome = {"case": case, **{field: saved.get(field) for field in CHECKPOINT_FIELDS}}
        with observation(
            lf,
            name="business_profile_extraction",
            as_type="generation",
            model=model,
            input=outcome["prompt"],
            output={
                "raw_response": outcome["raw"],
                "payload": outcome["extracted"],
                "errors": outcome["errors"],
            },
        ):
            pass
        # The checkpoint protects the paid response; flush makes the matching
        # trace durable before the next case begins.
        flush(lf)
        outcomes.append(outcome)
        return outcome

    def evaluate(*, output: dict[str, Any], **_: Any) -> list[Any]:
        outcome = output
        evals: list[Any] = []
        for field, result in (outcome["scored"].get("fields") or {}).items():
            if result.get("expected") is None:
                continue
            evals.append(
                evaluation(
                    f"field.{field}",
                    1.0 if result["correct"] else 0.0,
                    data_type="NUMERIC",
                    comment=f"expected={result['expected']} actual={result['actual']}",
                )
            )
        if outcome["extracted"] is None:
            evals.append(
                evaluation(
                    "outcome", "rejected", data_type="CATEGORICAL",
                    comment="; ".join(outcome["errors"])[:500],
                )
            )
        elif outcome["errors"]:
            evals.append(
                evaluation(
                    "outcome", "partial", data_type="CATEGORICAL",
                    comment="; ".join(outcome["errors"])[:500],
                )
            )
        else:
            evals.append(evaluation("outcome", "scored", data_type="CATEGORICAL"))

        description = (outcome["extracted"] or {}).get("business_description")
        if judge_metric is not None and description:
            test_case = deepeval_judges.business_description_test_case(outcome["case"], description)
            score, reason = deepeval_judges.measure_with_timeout(judge_metric, test_case)
            if score is None:
                print(
                    f"  faithfulness judge produced no score for {outcome['case']['company_number']}",
                    file=sys.stderr,
                )
            else:
                evals.append(
                    evaluation("business_description_faithfulness", score,
                               data_type="NUMERIC", comment=(reason or "")[:500])
                )
        return evals

    def aggregate(*, item_results: list[Any], **_: Any) -> list[Any]:
        metrics = compute_metrics([o["scored"] for o in outcomes])
        return [
            evaluation(name, value, data_type="NUMERIC")
            for name, value in flatten_metrics(metrics).items()
        ]

    result = run_experiment(
        lf,
        dataset_name=DATASET_NAME,
        run_name=run_name,
        task=task,
        evaluators=[evaluate],
        run_evaluators=[aggregate],
        description=f"{model} @ {PROMPT_VERSION}",
        metadata={"prompt_version": PROMPT_VERSION, "model": model},
        item_ids=item_ids,
    )
    flush(lf)
    for i, outcome in enumerate(outcomes, 1):
        _print_outcome_line(i, len(outcomes), outcome)
    print(f"\nLangfuse dataset run: {result.dataset_run_url}", file=sys.stderr)
    return outcomes


def _draft_expected_from_extraction(extracted: dict[str, Any] | None) -> dict[str, Any]:
    """Shape a model extraction into the case ``expected`` block. Only values
    that pass the taxonomy check are kept; anything else falls back to the
    empty (null) draft for that field, which is a legitimate "unclear" label
    for a human to confirm or replace."""
    expected = empty_expected()
    if not extracted:
        return expected
    description = extracted.get("business_description")
    if isinstance(description, str) and description.strip():
        expected["business_description"] = description.strip()
    for field, allowed in FIELD_VALUES.items():
        block = extracted.get(field)
        if isinstance(block, dict) and block.get("value") in allowed:
            expected[field] = {
                "value": block.get("value"),
                "quote": block.get("quote"),
                "section": block.get("section"),
                "confidence": block.get("confidence"),
            }
    sic = extracted.get("sic_agreement")
    if isinstance(sic, dict) and sic.get("value") in SIC_AGREEMENT_VALUES:
        expected["sic_agreement"] = {"value": sic.get("value"), "reason": sic.get("reason")}
    return expected


def draft_labels(args: argparse.Namespace) -> int:
    """Run a model over unlabelled gold cases and write its answers into each
    case's ``expected`` block as a *draft* (``review.status = "drafted"``).

    This is label-assist, not ground truth: a drafted case is never scored by
    ``run`` (which stays verified-only) and ``sync-annotation-queue`` puts it
    in the queue as a pending item -- the model's guess pre-filled, for a
    human to check rather than type from scratch. Each case is written to disk
    the moment its model call returns, so an interrupted run keeps every case
    it finished; re-running skips ``drafted`` and ``verified`` cases unless
    ``--redraft`` is given.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(".env"))
    import os

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    config = load_config(Path(args.config))
    model = config["model"]
    timeout = int(config.get("timeout_seconds", 120))
    cases_dir = Path(args.cases_dir)

    if args.include_verified:
        # Overwrites cases marked `verified` -- normally the ground truth this
        # harness scores against, so it is opt-in and never implied by
        # --redraft. Used on 2026-09-07 when the gold set moved to
        # whole-document context: 55 of the 57 cases then marked verified
        # carried the reviewer string "claude-opus-5 (pre-review draft,
        # unconfirmed by a human)" -- model drafts mislabelled as reviewed, so
        # there was no human work to protect. Check the reviewer strings
        # before reaching for this again.
        skip: set[str] = set()
    else:
        skip = {"verified"} if args.redraft else {"verified", "drafted"}
    pending: list[Path] = [
        path
        for path in case_files(cases_dir)
        if load_case(path).get("review", {}).get("status") not in skip
    ]
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print("No cases to draft (all are verified or already drafted).")
        return 0

    client = BusinessProfileModelClient(api_key)
    reviewer = f"{model} (model draft, unconfirmed by a human)"
    drafted = 0
    rejected = 0
    dist: dict[str, dict[str, int]] = {"demand_model": {}, "customer_type": {}}
    for i, path in enumerate(pending, 1):
        case = load_case(path)
        outcome = _run_one_case(client, model, timeout, case)
        extracted = outcome["extracted"]
        case["expected"] = _draft_expected_from_extraction(extracted)
        case["review"] = {"status": "drafted", "reviewed_at": None, "reviewer": reviewer}
        case.pop("draft", None)  # this IS the draft now; an older export's copy would be stale
        save_case(path, case)  # persist per case -- a killed run keeps its progress
        if extracted is None:
            rejected += 1
            print(f"  [{i}/{len(pending)}] {case['company_number']}: REJECTED -- "
                  f"{'; '.join(outcome['errors'])}", file=sys.stderr)
        else:
            drafted += 1
            for field in dist:
                value = (case["expected"].get(field) or {}).get("value") or "null"
                dist[field][value] = dist[field].get(value, 0) + 1
        print(f"  [{i}/{len(pending)}] {case['company_number']}: "
              f"{'drafted' if extracted else 'rejected'}")

    print(json.dumps({
        "drafted": drafted,
        "rejected": rejected,
        "model": model,
        "distribution": dist,
        "next": "python -m scripts.business_profile_classifier.business_profile_eval sync-annotation-queue "
                f"--config {args.config}",
    }, indent=2))
    return 0


_RESPONSE_BLOCK_RE = re.compile(r"## Model response \(verbatim\)\s*```json\n(.*?)\n```", re.S)
_RESPONSE_MODEL_RE = re.compile(r"^# .*? -- (\S+)", re.M)


def load_saved_responses(directory: Path) -> dict[str, tuple[str, str | None]]:
    """company_number -> (raw model JSON, model label) from a responses
    directory written by write_responses (or the same layout pulled from
    Langfuse)."""
    found: dict[str, tuple[str, str | None]] = {}
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        block = _RESPONSE_BLOCK_RE.search(text)
        if not block:
            continue
        label = _RESPONSE_MODEL_RE.search(text)
        found[path.stem] = (block.group(1), label.group(1) if label else None)
    return found


def rescore_responses(args: argparse.Namespace) -> int:
    """Score saved responses again under whatever the harness now does --
    the validation rule, the quote normalisation, the gold texts, the
    labels. Nothing is sent to a model. The report it writes says where the
    responses came from, so it is never mistaken for a fresh run."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from scripts.business_profile_classifier.business_profile_policy import (
        mark_quote_matches, normalise_retired_values, parse_json_response, reject_failed_fields, validate_fields,
    )

    responses_dir = Path(args.responses_dir)
    saved = load_saved_responses(responses_dir)
    if not saved:
        print(f"No saved responses found in {responses_dir}", file=sys.stderr)
        return 1
    cases = {c["company_number"]: c for c in (load_case(p) for p in case_files(Path(args.cases_dir)))}
    model = args.model or next((label for _, label in saved.values() if label), "unknown")

    outcomes: list[dict[str, Any]] = []
    start = time.monotonic()
    for i, (number, (raw, _)) in enumerate(saved.items(), 1):
        case = cases.get(number)
        if case is None:
            print(f"  {number}: no such case, skipped", file=sys.stderr)
            continue
        try:
            payload = parse_json_response(raw)
        except (ValueError, TypeError) as exc:
            extracted, errors = None, [f"response was not valid JSON: {exc}"]
        else:
            normalise_retired_values(payload)  # saved under an older taxonomy
            field_errors = validate_fields(payload, case["sections"])
            mark_quote_matches(payload, case["sections"])
            errors = [e for errs in field_errors.values() for e in errs]
            extracted = reject_failed_fields(payload, field_errors) if field_errors else payload
        outcome = {
            "case": case, "extracted": extracted, "errors": errors, "prompt": "(rescored from saved response)",
            "raw": raw, "scored": score_case(case, extracted),
        }
        _print_outcome_line(i, len(saved), outcome)
        outcomes.append(outcome)

    report = _report(args, model, [o["case"] for o in outcomes], outcomes, time.monotonic() - start)
    report["config"] = f"rescore of {responses_dir}"
    report["rescored_from"] = str(responses_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    report_path = output_dir / f"report-{stamp}-rescore.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print_summary(report)
    print(f"\nReport written to {report_path} (rescored from {responses_dir}, no model calls)")
    return 0


def run_evaluation(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(".env"))
    import os

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    config = load_config(Path(args.config))
    model = config["model"]
    timeout = int(config.get("timeout_seconds", 120))

    cases_dir = Path(args.cases_dir)
    all_cases = [load_case(path) for path in case_files(cases_dir)]
    verified = (
        all_cases
        if args.include_unreviewed
        else [case for case in all_cases if case.get("review", {}).get("status") == "verified"]
    )
    if args.company_numbers:
        wanted = set(args.company_numbers)
        selected = [case for case in verified if case["company_number"] in wanted]
        missing = wanted - {case["company_number"] for case in selected}
        if missing:
            print(f"Unknown or unreviewed company number(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
    else:
        selected = verified[: args.limit] if args.limit else verified
    if not selected:
        print("No verified cases to run (pass --include-unreviewed to run unverified ones too).", file=sys.stderr)
        return 1

    lf = None if args.no_langfuse else langfuse_from_config(config)
    client = BusinessProfileModelClient(api_key)

    start = time.monotonic()
    if lf is None:
        outcomes = _score_plain(client, model, timeout, selected)
    else:
        run_name = experiment_run_name(
            model=model, when=datetime.now(UTC),
            label=(config.get("langfuse") or {}).get("run_name") or config.get("run_name"),
        )
        checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
        outcomes = _score_langfuse(
            lf, config, client, model, timeout, verified, selected, run_name, checkpoint_path,
        )
    cases = selected
    elapsed = time.monotonic() - start

    report = _report(args, model, cases, outcomes, elapsed)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    report_path = output_dir / f"report-{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    responses_dir = write_responses(outcomes, output_dir / f"responses-{stamp}", model=model)

    _print_summary(report)
    print(f"\nReport written to {report_path}")
    print(f"Raw model responses written to {responses_dir}/ (one .md per case)")

    reference = registered_prompt_reference(lf, PROMPT_VERSION) if lf is not None else None
    if reference:
        print(f"prompt: {reference}")
    return 0


# ---------------------------------------------------------------------------
# Langfuse annotation queue (gold-label review)
#
# One self-hosted Langfuse instance holds one project per eval harness
# (business-profile-eval here). Reviewing gold labels does NOT stand up a
# second instance -- it is the same instance's annotation-queue feature,
# exactly as scripts/pdf_vision_extraction/vlm_financial_eval.py's queue lives in the
# vlm-financial-eval project.
#
# Each field's chosen VALUE is a score config (a categorical dropdown, or
# free text for business_description). The supporting quote and section stay
# authoritative in the case JSON and visible in the trace input -- review is
# for the judgement call, not for re-transcribing evidence.
# ---------------------------------------------------------------------------

def _review_trace_name(case: dict[str, Any]) -> str:
    """The company number and name, so the plain search box on the Traces
    page finds a company's review trace. Until 2026-09-14 every review
    trace was named ``business_profile_review`` and the company lived only
    in tags and metadata, which the search box does not read."""
    return f"{case['company_number']} {case.get('company_name') or ''} (gold review)".replace("  ", " ")


def _load_trace_entries() -> dict[str, dict[str, Any]]:
    """``{company_number: {"trace_id", "name", "content"}}`` where
    ``content`` is a digest of the input/output last pushed to the trace.
    Entries written before these were recorded are a bare trace id; they
    are upgraded on read with both None, which is what makes the sync
    restate them."""
    if not ANNOTATION_TRACE_MAP.is_file():
        return {}
    raw = json.loads(ANNOTATION_TRACE_MAP.read_text(encoding="utf-8"))
    return {
        number: {"content": None, **entry} if isinstance(entry, dict) else {"trace_id": entry, "name": None, "content": None}
        for number, entry in raw.items()
    }


def _trace_content_digest(input: Any, output: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([input, output], sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _load_trace_map() -> dict[str, str]:
    return {number: entry["trace_id"] for number, entry in _load_trace_entries().items()}


def _save_trace_entries(entries: dict[str, dict[str, Any]]) -> None:
    ANNOTATION_TRACE_MAP.parent.mkdir(parents=True, exist_ok=True)
    ANNOTATION_TRACE_MAP.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sync_annotation_queue(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(".env"))
    config = load_config(Path(args.config))
    lf = langfuse_from_config(config)
    if lf is None:
        print("Langfuse not configured (see docs/LANGFUSE_SETUP.md).", file=sys.stderr)
        return 1

    config_ids = ensure_score_configs(lf, question_score_configs(_review_question_specs()))
    queue_id = ensure_queue(lf, args.queue_name, list(config_ids.values()))

    cases = [load_case(path) for path in case_files(Path(args.cases_dir))]
    verified = [c for c in cases if c.get("review", {}).get("status") == "verified"]
    if verified:
        sync_dataset(lf, DATASET_NAME, _dataset_records(verified), description="Business-profile gold set")
    entries = _load_trace_entries()
    new_traces = 0
    renamed_traces = 0
    for case in cases:
        company_number = case["company_number"]
        name = _review_trace_name(case)
        tags = ["business-profile-review", f"company:{company_number}"]
        metadata = {
            "company_number": company_number,
            "company_name": case.get("company_name"),
            "sic_code": case.get("sic_code"),
            "sic_label": case.get("sic_label"),
        }
        trace_input = _case_trace_inputs(case)
        trace_output = _case_trace_outputs(case)
        content = _trace_content_digest(trace_input, trace_output)
        entry = entries.get(company_number)
        if entry is not None:
            # The trace shows the case as it was when the trace was made.
            # A case whose text or draft has changed since (the 2026-09-12
            # whole-document refresh, a correction) is restated so the
            # reviewer reads the current filing, not a stale window of it.
            if entry.get("name") != name or entry.get("content") != content:
                restate_trace(
                    lf, entry["trace_id"], name, tags=tags, metadata=metadata,
                    input=trace_input, output=trace_output,
                )
                entry.update({"name": name, "content": content})
                renamed_traces += 1
            continue
        with case_trace(
            lf,
            name=name,
            tags=tags,
            metadata=metadata,
            input=trace_input,
            output=trace_output,
        ) as root:
            entries[company_number] = {"trace_id": root.trace_id, "name": name, "content": content}
        new_traces += 1
    flush(lf)
    _save_trace_entries(entries)
    trace_map = {number: entry["trace_id"] for number, entry in entries.items()}

    trace_ids = [trace_map[case["company_number"]] for case in cases if case["company_number"] in trace_map]
    migrated_scores = 0
    for case in cases:
        trace_id = trace_map.get(case["company_number"])
        if trace_id is None:
            continue
        seed_draft_scores(lf, trace_id, _draft_answers(case), config_ids)
        migrated_scores += len(migrate_retired_scores(lf, trace_id, RETIRED_VALUES, config_ids))
        # Per-case migrations (a rule change that moved this case's label
        # but not the value everywhere) -- the only other way a disk-side
        # correction reaches a trace that already carries a score.
        migrated_scores += len(push_case_migrations(
            lf, trace_id, (case.get("review") or {}).get("taxonomy_migrations") or [], config_ids
        ))
    flush(lf)

    # A human-verified case whose expected block is fully populated opens as
    # COMPLETED -- ready to check, not a backlog. A "drafted" case is also
    # fully populated, but by a model, so it stays PENDING: that IS the
    # backlog the reviewer works through, model guess pre-filled.
    complete = [
        trace_map[case["company_number"]]
        for case in cases
        if case["company_number"] in trace_map
        and case.get("review", {}).get("status") == "verified"
        and all(value is not None for value in _draft_answers(case).values())
    ]
    result = sync_queue_items(lf, queue_id, trace_ids, complete=complete)

    print(json.dumps({
        "queue": args.queue_name,
        "cases": len(cases),
        "new_traces": new_traces,
        "restated_traces": renamed_traces,
        "migrated_scores": migrated_scores,
        "reused_traces": len(cases) - new_traces,
        **result,
    }, indent=2))
    return 0


def export_annotations(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(".env"))
    config = load_config(Path(args.config))
    lf = langfuse_from_config(config)
    if lf is None:
        print("Langfuse not configured (see docs/LANGFUSE_SETUP.md).", file=sys.stderr)
        return 1

    trace_map = _load_trace_map()
    field_names = _review_field_names()
    cases_dir = Path(args.cases_dir)

    queue_id = find_queue_id(lf, ANNOTATION_QUEUE_NAME)
    signed_off = completed_trace_ids(lf, queue_id) if queue_id else set()

    corrections = bool(getattr(args, "corrections", False))
    verified = 0
    corrected = 0
    incomplete = 0
    changed_cases = 0
    for case in [load_case(path) for path in case_files(cases_dir)]:
        already_verified = case.get("review", {}).get("status") == "verified"
        if already_verified and not corrections:
            continue  # already ground truth -- don't re-import over the original reviewer
        trace_id = trace_map.get(case["company_number"])
        if trace_id is None or trace_id not in signed_off:
            continue  # only import cases the reviewer has marked COMPLETED
        answers = read_annotations(lf, trace_id, field_names)
        for name, mapping in RETIRED_VALUES.items():
            if answers.get(name) in mapping:
                answers[name] = mapping[answers[name]]  # a score not yet migrated by sync
        if any(answers.get(name) is None for name in field_names):
            incomplete += 1  # signed off but a field has no score -- skip, don't half-write
            continue

        if already_verified:
            # A verified case is re-read only for differences: a label the
            # reviewer has since changed in Langfuse. An unchanged case is
            # not rewritten, so its reviewed_at and file stay as they were.
            if not _annotations_differ(case, answers, field_names):
                continue
            apply_annotations(case, answers, field_names)
            save_case(cases_dir / f"{case['company_number']}.json", case)
            corrected += 1
            continue

        apply_annotations(case, answers, field_names)
        if case["review"]["changed_fields"]:
            changed_cases += 1
        save_case(cases_dir / f"{case['company_number']}.json", case)
        verified += 1

    print(json.dumps({
        "verified": verified,
        "changed_by_reviewer": changed_cases,
        "corrected": corrected,
        "completed_but_incomplete": incomplete,
    }, indent=2))
    return 0


def _annotations_differ(case: dict[str, Any], answers: dict[str, Any], field_names: list[str]) -> bool:
    expected = case.get("expected") or {}
    for name in field_names:
        current = expected.get(name) if name == "business_description" else (expected.get(name) or {}).get("value")
        if current != answers[name]:
            return True
    return False


def apply_annotations(case: dict[str, Any], answers: dict[str, Any], field_names: list[str]) -> None:
    """Turn a reviewed case into ground truth, keeping the model's draft.

    The case file records two things after this: ``draft`` is what the model
    said, untouched (values, quotes, confidences, and which model said it),
    and ``expected`` is what the reviewer settled on. Where the reviewer kept
    the draft value, ``expected`` keeps the draft's quote and confidence as
    the evidence behind the label. Where the reviewer changed it, the draft's
    evidence is dropped rather than left behind: that quote argued for the
    *old* value, and a quote that contradicts the label it sits under is
    worse than none. ``review.changed_fields`` names every field the human
    overrode, so ``git diff`` on an export shows exactly what the review
    decided. Only the review's decisions live in Langfuse; this file is the
    durable record (a re-sync on 2026-09-09 silently overwrote review edits
    that had nowhere else to live).

    Applied to a case that is already verified (``export-annotations
    --corrections``), the draft block is left as it is, ``changed_fields``
    grows to include the newly corrected fields, ``reviewed_at`` is kept and
    ``corrected_at`` records the correction -- the file then shows both what
    the first review decided and what was later changed.
    """
    original = case.get("expected") or {}
    previous_review = case.get("review") or {}
    if "draft" not in case and case.get("review", {}).get("status") == "drafted":
        case["draft"] = {
            "expected": json.loads(json.dumps(original)),
            "drafted_by": case.get("review", {}).get("reviewer"),
        }

    expected: dict[str, Any] = json.loads(json.dumps(original))
    changed: list[str] = []
    for name in field_names:
        human = answers[name]
        if name == "business_description":
            if original.get(name) != human:
                changed.append(name)
            expected[name] = human
            continue
        block = dict(original.get(name) or {})
        if block.get("value") != human:
            changed.append(name)
            block = (
                {"value": human, "reason": None}
                if name == "sic_agreement"
                else {"value": human, "quote": None, "section": None, "confidence": None}
            )
        expected[name] = block

    case["expected"] = expected
    if previous_review.get("status") == "verified":
        case["review"] = {
            **previous_review,
            "corrected_at": utc_now(),
            "changed_fields": sorted(set(previous_review.get("changed_fields") or []) | set(changed)),
        }
        return
    case["review"] = {
        "status": "verified",
        "reviewed_at": utc_now(),
        "reviewer": "langfuse-annotation-queue",
        "changed_fields": changed,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    initialise = commands.add_parser("initialise", help="Create unreviewed gold-set cases from live narrative data.")
    initialise.add_argument("--db", default="companies-house.db")
    initialise.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")
    initialise.add_argument("--count", type=int, default=50)
    initialise.add_argument("--seed", type=int, default=42)
    initialise.add_argument(
        "--sic-prefix", action="append", metavar="PREFIX", dest="sic_prefixes",
        help="Bias candidate selection toward companies whose primary SIC code starts with "
             "PREFIX (repeatable). Does not restrict -- just draws these first.",
    )
    initialise.add_argument(
        "--company", action="append", metavar="NUMBER", dest="company_numbers",
        help="Create a case for exactly this company number (repeatable). Overrides sampling: "
             "--count, --seed, and --sic-prefix are ignored when given.",
    )
    initialise.add_argument(
        "--bias", choices=["consumer"], help="Shorthand for a curated --sic-prefix set. "
        "'consumer' = retail / hospitality / transport / arts / personal-services divisions, "
        "to rebalance a B2B-heavy gold set toward consumer_search / b2c cases.",
    )

    draft = commands.add_parser(
        "draft-labels",
        help="Run a model over unlabelled cases and pre-fill each expected block as a draft to check.",
    )
    draft.add_argument("--config", required=True)
    draft.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")
    draft.add_argument("--limit", type=int)
    draft.add_argument(
        "--include-verified",
        action="store_true",
        help="Also redraft cases marked verified, overwriting them. Only when those "
             "labels are not actually human work -- check review.reviewer first.",
    )
    draft.add_argument("--redraft", action="store_true",
                       help="Also re-draft cases already marked 'drafted' (never touches 'verified').")

    run = commands.add_parser("run", help="Run verified gold cases through a model and score them.")
    run.add_argument("--config", required=True)
    run.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")
    run.add_argument("--output-dir", default="logs/business-profile-eval")
    run.add_argument("--limit", type=int)
    run.add_argument(
        "--company", action="append", metavar="NUMBER", dest="company_numbers",
        help="Run exactly this verified company number (repeatable); useful for a targeted smoke test.",
    )
    run.add_argument(
        "--checkpoint", default=None,
        help="JSONL file that saves each completed response and replays it on resume without another model call.",
    )
    run.add_argument("--include-unreviewed", action="store_true")
    run.add_argument("--no-langfuse", action="store_true", help="Score locally without logging to Langfuse.")

    rescore = commands.add_parser(
        "rescore",
        help="Re-validate and re-score a run's saved responses (responses-<ts>/*.md) against the current "
             "cases and rules, with no model calls. For measuring a harness change on runs already paid for.",
    )
    rescore.add_argument("--responses-dir", required=True)
    rescore.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")
    rescore.add_argument("--output-dir", default="logs/business-profile-eval")
    rescore.add_argument("--model", default=None, help="Model label for the report (default: read from the files).")

    migrate = commands.add_parser(
        "migrate-retired-labels",
        help="Apply RETIRED_VALUES to reviewed gold labels, recording each mechanical taxonomy migration.",
    )
    migrate.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")

    sync_queue = commands.add_parser(
        "sync-annotation-queue",
        aliases=["sync-review-queue"],
        help="Push case files into the Langfuse annotation queue, seeded with this session's draft labels.",
    )
    sync_queue.add_argument("--config", required=True)
    sync_queue.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")
    sync_queue.add_argument("--queue-name", default=ANNOTATION_QUEUE_NAME)

    export = commands.add_parser(
        "export-annotations",
        aliases=["export-reviews"],
        help="Write human answers from the Langfuse annotation queue back into the case JSON files.",
    )
    export.add_argument("--config", required=True)
    export.add_argument("--cases-dir", default="evals/business_profile_gold_set/cases")
    export.add_argument(
        "--corrections", action="store_true",
        help="Also re-read cases that are already verified and apply any label the reviewer has since "
             "changed in Langfuse. Without this, verified cases are never rewritten.",
    )

    args = parser.parse_args(argv)
    if args.command == "initialise":
        if args.count < 1 and not args.company_numbers:
            parser.error("--count must be positive")
        prefixes = list(args.sic_prefixes or [])
        if args.bias == "consumer":
            prefixes = list(dict.fromkeys(prefixes + list(CONSUMER_SIC_PREFIXES)))
        created = initialise_cases(
            Path(args.db), Path(args.cases_dir), args.count, args.seed, prefixes or None,
            company_numbers=args.company_numbers,
        )
        print(json.dumps({
            "created": created,
            "cases_dir": args.cases_dir,
            "sic_prefixes": [] if args.company_numbers else prefixes,
            "company_numbers": args.company_numbers or [],
        }, indent=2))
        return 0
    if args.command == "draft-labels":
        return draft_labels(args)
    if args.command in ("sync-annotation-queue", "sync-review-queue"):
        return sync_annotation_queue(args)
    if args.command in ("export-annotations", "export-reviews"):
        return export_annotations(args)
    if args.command == "rescore":
        return rescore_responses(args)
    if args.command == "migrate-retired-labels":
        print(json.dumps(migrate_retired_gold_labels(Path(args.cases_dir)), indent=2))
        return 0
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
