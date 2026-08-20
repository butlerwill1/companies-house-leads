#!/usr/bin/env python3
"""
Create, run and score a human-labelled gold set for the business-profile
stage (Gate A2). Mirrors the shape of scripts/vlm/vlm_financial_eval.py --
same case-file format, same config format -- without the vision-specific
machinery that stage needs and this one does not.

Scoring is deterministic: field values are compared by exact string match
against a human-reviewed "expected" block. An LLM is never used to judge
whether an extraction is correct.

Usage:
    python -m scripts.profile.business_profile_eval initialise --db companies-house.db --count 50
    python -m scripts.profile.business_profile_review --cases-dir evals/business_profiles/cases
    python -m scripts.profile.business_profile_eval run --config evals/business_profiles/configs/openrouter-gemini.yaml
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_extractor import load_dotenv  # noqa: E402
from scripts.profile.business_profile_policy import (  # noqa: E402
    FIELD_VALUES,
    NARRATIVE_SECTION_PRIORITY,
    PROMPT_VERSION,
    SIC_AGREEMENT_VALUES,
)
from scripts.profile.companies_house_business_profile import (  # noqa: E402
    BusinessProfileModelClient,
    extract_business_profile,
    fetch_narrative_context,
    load_config,
)

CASE_SCHEMA_VERSION = 1
SCORED_FIELDS = (*FIELD_VALUES.keys(), "sic_agreement")
MLFLOW_REVIEW_QUEUE_NAME = "Business profile gold-label review"
LABEL_SOURCE_ID = "claude-opus-5"

# MLflow's Review UI only pre-fills a question when the trace carries an
# expectation whose source is source_type=HUMAN *and* whose source_id equals
# the viewer's own identity -- an LLM_JUDGE-sourced expectation renders as an
# empty "Select an option" no matter what it contains. So a draft answer has
# to be written under the reviewer's identity to be visible as a starting
# point, and DRAFT_METADATA_KEY is what keeps it honestly labelled as a draft
# rather than a human judgement (see _seed_draft_expectations/export_reviews).
DRAFT_METADATA_KEY = "draft_source"
FALLBACK_REVIEWER_ID = "default"


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


def select_candidate_companies(conn: sqlite3.Connection, count: int, seed: int) -> list[str]:
    """A diverse sample: spread across Gate A trading_status and across SIC
    groups, not just the highest-turnover companies. A gold set that is all
    obvious trading companies would never exercise the "unclear" path or
    the investment_holding / spv values, which is exactly the ambiguity
    this stage exists to resolve."""
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

    buckets: dict[str, list[tuple[str, str | None]]] = {}
    for company_number, trading_status, sic_code in rows:
        buckets.setdefault(trading_status, []).append((company_number, sic_code))

    rng = random.Random(seed)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    # Round-robin across trading_status buckets so a 50-case set does not
    # end up 90% "trading" just because that bucket is largest.
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
        # Within a bucket, prefer a SIC group not already represented.
        pick_index = next((i for i, (_, sic) in enumerate(bucket) if sic not in seen_sic), 0)
        company_number, sic_code = bucket.pop(pick_index)
        selected.append(company_number)
        if sic_code:
            seen_sic.add(sic_code)
    return selected


def initialise_cases(db_path: Path, cases_dir: Path, count: int, seed: int) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {path.stem for path in case_files(cases_dir)}
        candidates = [c for c in select_candidate_companies(conn, count + len(existing), seed) if c not in existing]
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


def score_case(case: dict[str, Any], extracted: dict[str, Any] | None) -> dict[str, Any]:
    expected = case["expected"]
    result: dict[str, Any] = {"company_number": case["company_number"], "fields": {}}
    if extracted is None:
        for field in SCORED_FIELDS:
            result["fields"][field] = {"expected": (expected.get(field) or {}).get("value"), "actual": None, "correct": False}
        return result
    for field in SCORED_FIELDS:
        expected_value = (expected.get(field) or {}).get("value")
        actual_value = (extracted.get(field) or {}).get("value")
        result["fields"][field] = {
            "expected": expected_value,
            "actual": actual_value,
            "correct": expected_value is not None and expected_value == actual_value,
        }
    return result


def run_evaluation(args: argparse.Namespace) -> int:
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
    cases = [load_case(path) for path in case_files(cases_dir)]
    if not args.include_unreviewed:
        cases = [case for case in cases if case.get("review", {}).get("status") == "verified"]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No verified cases to run (pass --include-unreviewed to run unverified ones too).", file=sys.stderr)
        return 1

    client = BusinessProfileModelClient(api_key)
    results = []
    quote_failures = 0
    unclear_count = 0
    total_fields = 0
    start = time.monotonic()

    for i, case in enumerate(cases, 1):
        context = {
            "company_name": case["company_name"],
            "sections": case["sections"],
            "sic_label": case["sic_label"],
            "sic_code": case["sic_code"],
            "financial_year": case["financial_year"],
        }
        extracted, errors, _ = extract_business_profile(client, model, context, timeout=timeout)
        if extracted is None:
            quote_failures += 1
            print(f"  [{i}/{len(cases)}] {case['company_number']}: REJECTED -- {'; '.join(errors)}", file=sys.stderr)
        else:
            for field in FIELD_VALUES:
                total_fields += 1
                if extracted.get(field, {}).get("value") == "unclear":
                    unclear_count += 1
        results.append(score_case(case, extracted))

    elapsed = time.monotonic() - start

    field_accuracy: dict[str, dict[str, int]] = {field: {"correct": 0, "scored": 0} for field in SCORED_FIELDS}
    for result in results:
        for field, outcome in result["fields"].items():
            if outcome["expected"] is not None:
                field_accuracy[field]["scored"] += 1
                if outcome["correct"]:
                    field_accuracy[field]["correct"] += 1

    report = {
        "generated_at": utc_now(),
        "config": str(args.config),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "cases": len(cases),
        "quote_or_validation_rejections": quote_failures,
        "quote_verification_pass_rate": round(1 - quote_failures / len(cases), 4) if cases else None,
        "unclear_rate": round(unclear_count / total_fields, 4) if total_fields else None,
        "elapsed_seconds": round(elapsed, 1),
        "field_accuracy": {
            field: round(v["correct"] / v["scored"], 4) if v["scored"] else None
            for field, v in field_accuracy.items()
        },
        "field_scored_counts": {field: v["scored"] for field, v in field_accuracy.items()},
        "results": results,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{len(cases)} cases, {elapsed:.0f}s")
    print(f"quote/validation pass rate: {report['quote_verification_pass_rate']}")
    print(f"unclear rate: {report['unclear_rate']}")
    print("field accuracy (only cases with a reviewed expected value count):")
    for field, acc in report["field_accuracy"].items():
        scored = report["field_scored_counts"][field]
        print(f"  {field:<26} {acc if acc is not None else 'n/a':<8} (n={scored})")
    print(f"\nReport written to {report_path}")

    if not config.get("mlflow", {}).get("enabled"):
        return 0
    try:
        import mlflow
    except ImportError:
        print("mlflow not installed (pip install -r requirements-eval.txt); skipping MLflow logging.", file=sys.stderr)
        return 0
    settings = config["mlflow"]
    mlflow.set_tracking_uri(settings.get("tracking_uri", "http://127.0.0.1:5000"))
    mlflow.set_experiment(settings.get("experiment", "companies-house-business-profile-eval"))
    with mlflow.start_run(run_name=settings.get("run_name")):
        mlflow.log_params({"model": model, "prompt_version": PROMPT_VERSION, "cases": len(cases)})
        for key in ("quote_verification_pass_rate", "unclear_rate"):
            if report[key] is not None:
                mlflow.log_metric(key, report[key])
        for field, acc in report["field_accuracy"].items():
            if acc is not None:
                mlflow.log_metric(f"accuracy_{field}", acc)
        mlflow.log_dict(report, "report.json")
    return 0


# ---------------------------------------------------------------------------
# MLflow review queue
#
# One shared MLflow tracking server (the same http://127.0.0.1:5000 the VLM
# pipeline uses) holds several experiments. Reviewing business profiles here
# does NOT start a second MLflow instance -- it is a second *experiment*
# (companies-house-business-profile-eval) inside the same server, exactly
# how scripts/vlm/vlm_financial_eval.py's "Financial PDF gold-label review"
# queue lives inside companies-house-vlm-financial-eval. Both always read
# tracking_uri from the config file, never hardcode a different host, so
# there is one place that decides which server every stage talks to.
#
# Scope: the label schemas below capture each field's chosen VALUE as a
# dropdown (mlflow.genai.label_schemas.InputCategorical), which is what a
# human reviewer actually needs to confirm or correct quickly. The
# supporting quote and section stay authoritative in the case JSON, seeded
# from this session's draft labels and visible in the trace's request
# preview for context -- MLflow review is for the judgement call, not for
# re-transcribing evidence.
# ---------------------------------------------------------------------------


def _mlflow_review_fields() -> dict[str, tuple[str, ...] | None]:
    """Field name -> allowed values (None means free text)."""
    return {"business_description": None, **FIELD_VALUES, "sic_agreement": SIC_AGREEMENT_VALUES}


def _label_schemas(experiment_id: str) -> list[Any]:
    from mlflow.genai.label_schemas import InputCategorical, InputText, create_label_schema, list_label_schemas

    existing = {schema.name: schema for schema in list_label_schemas(experiment_id=experiment_id)}
    schemas = []
    for field, values in _mlflow_review_fields().items():
        schema = existing.get(field)
        if schema is None:
            input_type = InputText(max_length=300) if values is None else InputCategorical(list(values))
            schema = create_label_schema(
                name=field,
                type="expectation",
                input=input_type,
                instruction=f"Confirm or correct {field} for this company, from the narrative shown.",
                experiment_id=experiment_id,
            )
        schemas.append(schema)
    return schemas


def _get_or_create_queue(experiment_id: str, schema_ids: list[str], queue_name: str) -> Any:
    from mlflow.genai.review_queues import create_review_queue, list_review_queues, update_review_queue

    queue = next(
        (q for q in list_review_queues(experiment_id=experiment_id) if q.name == queue_name),
        None,
    )
    if queue is None:
        return create_review_queue(queue_name, queue_type="custom", schema_ids=schema_ids, experiment_id=experiment_id)
    # The server does not promise to echo schema_ids back in the order they
    # were sent (it returned them sorted, ours is field-declaration order),
    # so an order-sensitive != here is a false positive on every run --
    # and update_review_queue rejects changing a queue's schemas once items
    # are assigned to it, so a spurious update call breaks a working queue.
    if set(queue.schema_ids) != set(schema_ids):
        return update_review_queue(queue.queue_id, schema_ids=schema_ids)
    return queue


def _existing_trace_id_for_company(experiment_id: str, company_number: str) -> str | None:
    from mlflow import MlflowClient

    client = MlflowClient()
    traces = client.search_traces(
        locations=[experiment_id],
        filter_string=f"tags.`eval.company_number` = '{company_number}'",
        max_results=1,
        include_spans=False,
    )
    return traces[0].info.trace_id if traces else None


# turnover_note/employee_note come first: they are the newest, most
# decision-relevant evidence (see the gold-set re-label against the
# filings' turnover notes) and must survive ahead of narrative_report
# boilerplate if the preview has to be cut short.
_NARRATIVE_PREVIEW_PRIORITY = ("turnover_note", "employee_note") + NARRATIVE_SECTION_PRIORITY


def _cited_sections(case: dict[str, Any]) -> list[str]:
    """Sections the current expected values actually cite as evidence
    (field["section"]), in field order, deduplicated. These must survive a
    length cut ahead of any generic priority -- a reviewer checking a label
    against its quote needs the section that quote came from, not just
    whichever sections happen to rank highest by type."""
    expected = case.get("expected") or {}
    seen: list[str] = []
    for value in expected.values():
        section = (value or {}).get("section") if isinstance(value, dict) else None
        if section and section not in seen:
            seen.append(section)
    return seen


# Below this many spare characters, a truncated excerpt would be too short
# to carry any real sentence -- drop the section entirely rather than keep
# a near-empty, useless fragment.
_MIN_USEFUL_EXCERPT_CHARS = 40

# Fixed budget reserved for the "[omitted for length: ...]" suffix -- see
# _narrative_preview for why this is a flat reservation, not a computed one.
_OMITTED_SUFFIX_RESERVE = 200


def _narrative_preview(case: dict[str, Any], max_chars: int | None = None) -> str:
    """Join the case's sections into one preview string. Without max_chars
    this is the full text (used for scoring/review context that has no size
    constraint). With max_chars, this is built in two passes so a length cut
    always costs generic content before evidence:

    1. Every section a current label actually cites (see _cited_sections)
       is included, in citation order -- as a truncated excerpt with a
       "chars total" marker if it doesn't fit whole, but never dropped
       outright unless there is no room for even a useful excerpt. A
       reviewer checking a label against its quote needs the section that
       quote came from; the alternative (drop it because it happened to be
       long) left 12 of the 47 gold cases unable to show the very evidence
       their labels were re-derived from.
    2. Only the budget left over goes to generic-priority filler
       (_NARRATIVE_PREVIEW_PRIORITY, then anything else), which is dropped
       whole rather than truncated -- it's helpful context, not evidence a
       label depends on, so a partial paragraph isn't worth the confusion.

    This exists because the trace's mlflow.traceInputs tag has a ~4096-char
    value limit that MLflow silently truncates past into invalid JSON if
    written unbounded -- this broke for real once turnover_note/employee_note
    pushed section text past it."""
    sections = case.get("sections") or {}
    cited = [key for key in _cited_sections(case) if sections.get(key)]
    filler_priority = [key for key in _NARRATIVE_PREVIEW_PRIORITY if key not in cited]
    filler = filler_priority + [key for key in sections if key not in cited and key not in filler_priority]

    def build(effective_max_chars: int | None) -> tuple[list[str], list[str]]:
        included: list[str] = []
        omitted: list[str] = []

        def remaining_budget() -> int | None:
            if effective_max_chars is None:
                return None
            used = len("\n\n".join(included)) + (2 if included else 0)
            return effective_max_chars - used

        # An equal share of the budget per cited section, so one unusually
        # long section (a 5999-character principal_activity, in one real
        # case) cannot claim the whole remaining budget and leave nothing
        # for the next cited section in line -- every cited section gets a
        # fair shot at an excerpt rather than first-come-first-served.
        cited_share = (
            None if effective_max_chars is None else max(_MIN_USEFUL_EXCERPT_CHARS, effective_max_chars // max(1, len(cited)))
        )

        for key in cited:
            text = sections[key]
            block = f"[{key}]\n{text}"
            budget = remaining_budget()
            if budget is not None:
                budget = min(budget, cited_share)
            if budget is not None and len(block) > budget:
                marker = f"...[truncated, {len(text)} chars total]"
                header = f"[{key}]\n"
                keep = budget - len(header) - len(marker)
                if keep < _MIN_USEFUL_EXCERPT_CHARS:
                    omitted.append(key)
                    continue
                block = f"{header}{text[:keep]}{marker}"
            included.append(block)

        for key in filler:
            text = sections.get(key)
            if not text:
                continue
            block = f"[{key}]\n{text}"
            budget = remaining_budget()
            if budget is not None and len(block) > budget:
                omitted.append(key)
                continue
            included.append(block)

        return included, omitted

    # The "[omitted for length: ...]" suffix is itself appended after every
    # inclusion decision, so its length has to come out of the budget before
    # those decisions are made -- otherwise the returned string can run over
    # max_chars by however long that list of names turns out to be. Its
    # exact length depends on what ends up omitted, which depends on how
    # much budget was reserved for it: chasing that exactly (rebuild,
    # measure the real suffix, rebuild again) is a feedback loop that can
    # spiral -- dropping one more section to make room lengthens the
    # "omitted" list, which shrinks the budget further, which drops more
    # sections. A fixed reservation sidesteps that entirely: this dataset
    # has at most ~10 distinct section keys, and even naming every one of
    # them is under 200 characters, so reserving that much up front costs
    # a small, constant slice of a budget that is always in the thousands
    # in production and never causes the spiral.
    effective_max_chars = None if max_chars is None else max(0, max_chars - _OMITTED_SUFFIX_RESERVE)
    included, omitted = build(effective_max_chars)

    preview = "\n\n".join(included)
    if omitted:
        preview += f"\n\n[omitted for length: {', '.join(omitted)}]"
    return preview


def _draft_summary(case: dict[str, Any]) -> str:
    expected = case.get("expected") or {}
    parts = [f"description: {expected.get('business_description') or '(none)'}"]
    for field in FIELD_VALUES:
        parts.append(f"{field}: {(expected.get(field) or {}).get('value')}")
    sic = expected.get("sic_agreement") or {}
    parts.append(f"sic_agreement: {sic.get('value')}")
    return " | ".join(parts)


def _case_trace_inputs(case: dict[str, Any], max_narrative_chars: int | None = None) -> dict[str, Any]:
    return {
        "company_number": case["company_number"],
        "company_name": case.get("company_name"),
        "sic_code": case.get("sic_code"),
        "sic_label": case.get("sic_label"),
        "narrative_sections": _narrative_preview(case, max_narrative_chars),
    }


# MLflow silently truncates a trace tag value past ~4096 characters instead
# of rejecting it -- writing this dict unbounded through root.set_inputs()
# (a trace tag under the hood, same as set_trace_tag) truncates mid-JSON and
# leaves the Inputs panel unparseable. This happened for real once
# turnover_note/employee_note were added to every case's sections.
_TRACE_TAG_VALUE_LIMIT = 4096


def _size_bounded_case_trace_inputs(case: dict[str, Any]) -> dict[str, Any]:
    """_case_trace_inputs(case), shrinking narrative_sections (whole
    sections dropped, highest-priority first survives -- see
    _NARRATIVE_PREVIEW_PRIORITY) until the JSON-encoded tag value fits
    MLflow's limit."""
    max_chars: int | None = None
    for _ in range(20):
        inputs = _case_trace_inputs(case, max_chars)
        encoded_len = len(json.dumps(inputs, ensure_ascii=False))
        if encoded_len <= _TRACE_TAG_VALUE_LIMIT or not inputs["narrative_sections"]:
            return inputs
        overshoot = encoded_len - _TRACE_TAG_VALUE_LIMIT
        current_len = len(inputs["narrative_sections"])
        # Shrink by at least the overshoot (plus margin for the
        # "[omitted for length: ...]" note and JSON escaping) so each retry
        # strictly decreases -- otherwise a small overshoot from escaping
        # alone, not section count, could loop without ever dropping a
        # section.
        max_chars = max(0, current_len - overshoot - 100)
    return _case_trace_inputs(case, 0)


def _case_trace_outputs(case: dict[str, Any]) -> dict[str, Any]:
    return {"draft_expected": case.get("expected")}


def _log_case_trace(case: dict[str, Any]) -> str:
    """One trace per company, holding the narrative text a reviewer needs
    and this session's draft labels for context. Traces are immutable once
    created, so re-syncing reuses the existing trace rather than piling up
    a fresh one on every run -- draft-label updates are applied as new
    expectation assessments on top of it, not a new trace. The inputs/outputs
    set here are only the trace's *starting* snapshot; _refresh_trace_snapshot
    keeps them current on every subsequent sync."""
    import mlflow
    from mlflow.entities import SpanType

    with mlflow.start_span(name="business_profile_review", span_type=SpanType.WORKFLOW) as root:
        mlflow.update_current_trace(
            tags={
                "eval.case_id": case["company_number"],
                "eval.company_number": case["company_number"],
                "eval.company_name": str(case.get("company_name") or ""),
                "eval.sic_code": str(case.get("sic_code") or ""),
                "eval.sic_label": str(case.get("sic_label") or ""),
            },
            request_preview=f"{case.get('company_name')} (SIC {case.get('sic_code')}: {case.get('sic_label')})",
            response_preview=_draft_summary(case),
        )
        root.set_inputs(_size_bounded_case_trace_inputs(case))
        root.set_outputs(_case_trace_outputs(case))
    return mlflow.get_last_active_trace_id()


def _refresh_trace_snapshot(trace_id: str, case: dict[str, Any]) -> None:
    """Keep an existing trace's Inputs/Outputs panel current with this
    session's case data.

    A case's sections and expected values can change after its trace already
    exists (this happened for real: the turnover/employee notes were added
    to every case's sections, and 26 cases were re-labelled against them,
    after the first sync had already created all 47 traces) -- and
    _log_case_trace's root.set_inputs()/set_outputs() only run once, at
    creation, so a reused trace would keep showing whatever sections and
    draft existed on that first sync forever. mlflow.traceInputs and
    mlflow.traceOutputs are, concretely, just trace tags carrying the JSON
    set_inputs()/set_outputs() recorded, and MlflowClient.set_trace_tag is
    documented to work on an already-ended trace, so overwriting them here
    is a supported, non-destructive way to bring an old trace's evidence
    panel up to date without disturbing its assessments or queue membership.

    request_preview/response_preview (the short summary shown in the review
    queue's list view) have no equivalent public update path -- MLflow only
    computes them once, when the column is still NULL -- so a trace's list
    row can keep showing its original summary sentence after a relabel even
    though the detailed panel and every field's draft value are correct."""
    from mlflow import MlflowClient

    client = MlflowClient()
    inputs_json = json.dumps(_size_bounded_case_trace_inputs(case), ensure_ascii=False)
    outputs_json = json.dumps(_case_trace_outputs(case), ensure_ascii=False)
    client.set_trace_tag(trace_id, "mlflow.traceInputs", inputs_json)
    client.set_trace_tag(trace_id, "mlflow.traceOutputs", outputs_json)


def _reviewer_identity(experiment_id: str) -> str:
    """The identity the Review UI treats as "me". MLflow seeds a USER-type
    review queue per reviewer whose name is that identity (on a no-auth local
    server there is exactly one, named "default"), which is the only place the
    value is exposed to a client."""
    try:
        from mlflow.genai.review_queues import list_review_queues

        for queue in list_review_queues(experiment_id=experiment_id):
            if str(queue.queue_type) == "user" and queue.name:
                return queue.name
    except Exception:
        pass
    return FALLBACK_REVIEWER_ID


def _seed_draft_expectations(trace_id: str, case: dict[str, Any], reviewer_id: str) -> None:
    """Pre-fill each field with this session's draft value so the reviewer
    opens a form that is already answered and only has to check it.

    The draft is written under the reviewer's own identity because that is
    the only thing the Review UI will display (see DRAFT_METADATA_KEY), and
    is tagged with that metadata key so it stays distinguishable from an
    answer the reviewer actually made -- export_reviews relies on the tag,
    not on the source type, to decide whether a field was really confirmed.

    Re-syncing updates an existing draft in place rather than logging a
    second same-named expectation: duplicates make the UI's per-field lookup
    ambiguous and the dropdown falls back to rendering empty."""
    import mlflow
    from mlflow.entities import AssessmentSource, Expectation

    source = AssessmentSource(source_type="HUMAN", source_id=reviewer_id)
    existing_by_name: dict[str, str] = {}
    for assessment in mlflow.get_trace(trace_id).info.assessments:
        if type(assessment).__name__ != "Expectation":
            continue
        # Only ever overwrite our own drafts; a real reviewer answer for the
        # same field must survive a re-sync untouched.
        if (assessment.metadata or {}).get(DRAFT_METADATA_KEY) != LABEL_SOURCE_ID:
            continue
        existing_by_name[assessment.name] = assessment.assessment_id

    metadata = {DRAFT_METADATA_KEY: LABEL_SOURCE_ID}

    def _set(name: str, value: Any) -> None:
        if value is None:
            return
        assessment_id = existing_by_name.get(name)
        if assessment_id is None:
            mlflow.log_expectation(
                trace_id=trace_id, name=name, value=value, source=source, metadata=metadata
            )
            return
        mlflow.update_assessment(
            trace_id=trace_id,
            assessment_id=assessment_id,
            assessment=Expectation(name=name, value=value, source=source, metadata=metadata),
        )

    expected = case.get("expected") or {}
    _set("business_description", expected.get("business_description"))
    for field in FIELD_VALUES:
        _set(field, (expected.get(field) or {}).get("value"))
    _set("sic_agreement", (expected.get("sic_agreement") or {}).get("value"))


def sync_review_queue(args: argparse.Namespace) -> int:
    """Make the MLflow review queue contain exactly one trace per case
    file, each seeded with this session's draft labels and marked complete
    -- the queue opens already fully answered, ready to check rather than
    to work through as a backlog."""
    try:
        import mlflow
        from mlflow.genai.review_queues import (
            add_items_to_review_queue,
            list_review_queue_items,
            remove_items_from_review_queue,
            set_review_queue_item_status,
        )
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to synchronise MLflow reviews") from error

    config = load_config(Path(args.config))
    settings = config.get("mlflow") or {}
    mlflow.set_tracking_uri(args.tracking_uri or settings.get("tracking_uri", "http://127.0.0.1:5000"))
    experiment = mlflow.set_experiment(
        args.experiment or settings.get("experiment", "companies-house-business-profile-eval")
    )

    cases = [load_case(path) for path in case_files(Path(args.cases_dir))]
    schemas = _label_schemas(experiment.experiment_id)
    queue = _get_or_create_queue(experiment.experiment_id, [s.schema_id for s in schemas], args.queue_name)

    trace_id_by_case: dict[str, str] = {}
    seeded = 0
    for case in cases:
        trace_id = _existing_trace_id_for_company(experiment.experiment_id, case["company_number"])
        if trace_id is None:
            trace_id = _log_case_trace(case)
            seeded += 1
        trace_id_by_case[case["company_number"]] = trace_id
    # Traces are exported asynchronously; a newly created trace_id is not
    # yet a real server-side trace until this flushes, and logging an
    # expectation against it too early fails with RESOURCE_DOES_NOT_EXIST.
    mlflow.flush_trace_async_logging()

    reviewer_id = _reviewer_identity(experiment.experiment_id)
    for case in cases:
        trace_id = trace_id_by_case[case["company_number"]]
        _seed_draft_expectations(trace_id, case, reviewer_id)
        # Always refresh, even for a trace created moments ago in this same
        # run -- cheap, idempotent, and the only thing that keeps a reused
        # trace's evidence panel from drifting away from the case file after
        # a later sections/relabel change.
        _refresh_trace_snapshot(trace_id, case)

    wanted = set(trace_id_by_case.values())
    add_items_to_review_queue(queue.queue_id, item_ids=sorted(wanted))
    existing_items = list(list_review_queue_items(queue.queue_id, max_results=1000))
    stale = [item.item_id for item in existing_items if item.item_id not in wanted]
    if stale:
        remove_items_from_review_queue(queue.queue_id, item_ids=stale)

    # Every item already has a full set of draft answers -- mark it complete
    # so the queue opens showing 47/47 done rather than a pending backlog.
    # A completed item is still fully editable; this only changes how it is
    # presented, not whether it can be corrected.
    marked_complete = 0
    for item_id in wanted:
        set_review_queue_item_status(queue.queue_id, item_id=item_id, status="complete", completed_by=reviewer_id)
        marked_complete += 1

    print(json.dumps({
        "tracking_uri": mlflow.get_tracking_uri(),
        "experiment": experiment.name,
        "marked_complete": marked_complete,
        "experiment_id": experiment.experiment_id,
        "queue_name": queue.name,
        "cases": len(cases),
        "new_traces": seeded,
        "reused_traces": len(cases) - seeded,
        "removed_stale_items": len(stale),
    }, indent=2))
    return 0


def export_reviews(args: argparse.Namespace) -> int:
    """Pull human-entered expectations back from the MLflow review queue
    into each case's "expected" block. A case is marked verified once a
    human has answered every field (unclear counts as answered)."""
    try:
        import mlflow
        from mlflow import MlflowClient
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to export MLflow reviews") from error

    config = load_config(Path(args.config))
    settings = config.get("mlflow") or {}
    mlflow.set_tracking_uri(args.tracking_uri or settings.get("tracking_uri", "http://127.0.0.1:5000"))
    experiment = mlflow.set_experiment(
        args.experiment or settings.get("experiment", "companies-house-business-profile-eval")
    )
    client = MlflowClient()

    cases_dir = Path(args.cases_dir)
    updated = 0
    for case in [load_case(path) for path in case_files(cases_dir)]:
        trace_id = _existing_trace_id_for_company(experiment.experiment_id, case["company_number"])
        if trace_id is None:
            continue
        trace = client.get_trace(trace_id)
        by_field: dict[str, tuple[Any, bool]] = {}
        for assessment in trace.info.assessments or []:
            if type(assessment).__name__ != "Expectation":
                continue
            # Drafts we seeded carry our metadata marker; anything without it
            # was entered by the reviewer. Source type cannot make this call:
            # a draft has to be HUMAN-sourced for the UI to show it at all.
            is_draft = (assessment.metadata or {}).get(DRAFT_METADATA_KEY) == LABEL_SOURCE_ID
            existing = by_field.get(assessment.name)
            # A reviewer's own answer always wins over our draft.
            if existing and not existing[1] and is_draft:
                continue
            by_field[assessment.name] = (assessment.value, is_draft)

        if not by_field:
            continue
        human_answered = all(
            field in by_field and not by_field[field][1] for field in _mlflow_review_fields()
        )
        expected = case.get("expected") or {}
        if "business_description" in by_field:
            expected["business_description"] = by_field["business_description"][0]
        for field in FIELD_VALUES:
            if field in by_field:
                entry = expected.get(field) or {}
                entry["value"] = by_field[field][0]
                expected[field] = entry
        if "sic_agreement" in by_field:
            entry = expected.get("sic_agreement") or {}
            entry["value"] = by_field["sic_agreement"][0]
            expected["sic_agreement"] = entry
        case["expected"] = expected
        if human_answered:
            case["review"] = {"status": "verified", "reviewed_at": utc_now(), "reviewer": "mlflow-review-queue"}
        save_case(cases_dir / f"{case['company_number']}.json", case)
        updated += 1

    print(json.dumps({"updated_cases": updated}, indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    initialise = commands.add_parser("initialise", help="Create unreviewed gold-set cases from live narrative data.")
    initialise.add_argument("--db", default="companies-house.db")
    initialise.add_argument("--cases-dir", default="evals/business_profiles/cases")
    initialise.add_argument("--count", type=int, default=50)
    initialise.add_argument("--seed", type=int, default=42)

    run = commands.add_parser("run", help="Run verified gold cases through a model and score them.")
    run.add_argument("--config", required=True)
    run.add_argument("--cases-dir", default="evals/business_profiles/cases")
    run.add_argument("--output-dir", default="logs/business-profile-eval")
    run.add_argument("--limit", type=int)
    run.add_argument("--include-unreviewed", action="store_true")

    sync_queue = commands.add_parser(
        "sync-review-queue",
        help="Push case files into the MLflow review queue, seeded with this session's draft labels.",
    )
    sync_queue.add_argument("--config", required=True)
    sync_queue.add_argument("--cases-dir", default="evals/business_profiles/cases")
    sync_queue.add_argument("--tracking-uri")
    sync_queue.add_argument("--experiment")
    sync_queue.add_argument("--queue-name", default=MLFLOW_REVIEW_QUEUE_NAME)

    export = commands.add_parser(
        "export-reviews",
        help="Write human answers from the MLflow review queue back into the case JSON files.",
    )
    export.add_argument("--config", required=True)
    export.add_argument("--cases-dir", default="evals/business_profiles/cases")
    export.add_argument("--tracking-uri")
    export.add_argument("--experiment")

    args = parser.parse_args(argv)
    if args.command == "initialise":
        if args.count < 1:
            parser.error("--count must be positive")
        created = initialise_cases(Path(args.db), Path(args.cases_dir), args.count, args.seed)
        print(json.dumps({"created": created, "cases_dir": args.cases_dir}, indent=2))
        return 0
    if args.command == "sync-review-queue":
        return sync_review_queue(args)
    if args.command == "export-reviews":
        return export_reviews(args)
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
