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
from scripts.profile.business_profile_policy import FIELD_VALUES, PROMPT_VERSION, SIC_AGREEMENT_VALUES  # noqa: E402
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
    if queue.schema_ids != schema_ids:
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


def _narrative_preview(case: dict[str, Any]) -> str:
    parts = [f"[{key}]\n{text}" for key, text in case["sections"].items()]
    return "\n\n".join(parts)


def _draft_summary(case: dict[str, Any]) -> str:
    expected = case.get("expected") or {}
    parts = [f"description: {expected.get('business_description') or '(none)'}"]
    for field in FIELD_VALUES:
        parts.append(f"{field}: {(expected.get(field) or {}).get('value')}")
    sic = expected.get("sic_agreement") or {}
    parts.append(f"sic_agreement: {sic.get('value')}")
    return " | ".join(parts)


def _log_case_trace(case: dict[str, Any]) -> str:
    """One trace per company, holding the narrative text a reviewer needs
    and this session's draft labels for context. Traces are immutable once
    created, so re-syncing reuses the existing trace rather than piling up
    a fresh one on every run -- draft-label updates are applied as new
    expectation assessments on top of it, not a new trace."""
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
        root.set_inputs({
            "company_number": case["company_number"],
            "company_name": case.get("company_name"),
            "sic_code": case.get("sic_code"),
            "sic_label": case.get("sic_label"),
            "narrative_sections": _narrative_preview(case),
        })
        root.set_outputs({"draft_expected": case.get("expected")})
    return mlflow.get_last_active_trace_id()


def _seed_draft_expectations(trace_id: str, case: dict[str, Any]) -> None:
    """Pre-fill each field with this session's draft value as an LLM_JUDGE
    expectation, so the reviewer sees a starting point to confirm or
    overwrite rather than a blank form. A human's later answer through the
    MLflow UI is logged as its own (HUMAN-sourced) expectation on top --
    MLflow keeps both, export_reviews prefers the human one."""
    import mlflow
    from mlflow.entities import AssessmentSource

    source = AssessmentSource(source_type="LLM_JUDGE", source_id=LABEL_SOURCE_ID)
    expected = case.get("expected") or {}
    mlflow.log_expectation(
        trace_id=trace_id, name="business_description",
        value=expected.get("business_description"), source=source,
    )
    for field in FIELD_VALUES:
        value = (expected.get(field) or {}).get("value")
        if value is not None:
            mlflow.log_expectation(trace_id=trace_id, name=field, value=value, source=source)
    sic_value = (expected.get("sic_agreement") or {}).get("value")
    if sic_value is not None:
        mlflow.log_expectation(trace_id=trace_id, name="sic_agreement", value=sic_value, source=source)


def sync_review_queue(args: argparse.Namespace) -> int:
    """Make the MLflow review queue contain exactly one trace per case
    file, each seeded with this session's draft labels."""
    try:
        import mlflow
        from mlflow.genai.review_queues import add_items_to_review_queue, list_review_queue_items, remove_items_from_review_queue
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

    for case in cases:
        _seed_draft_expectations(trace_id_by_case[case["company_number"]], case)

    wanted = set(trace_id_by_case.values())
    add_items_to_review_queue(queue.queue_id, item_ids=sorted(wanted))
    existing_items = list(list_review_queue_items(queue.queue_id, max_results=1000))
    stale = [item.item_id for item in existing_items if item.item_id not in wanted]
    if stale:
        remove_items_from_review_queue(queue.queue_id, item_ids=stale)

    print(json.dumps({
        "tracking_uri": mlflow.get_tracking_uri(),
        "experiment": experiment.name,
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
        by_field: dict[str, tuple[Any, str]] = {}
        for assessment in trace.info.assessments or []:
            if assessment.expectation is None:
                continue
            source_type = assessment.source.source_type if assessment.source else "UNKNOWN"
            existing = by_field.get(assessment.name)
            # A human answer always wins; otherwise keep the most recent.
            if existing and existing[1] == "HUMAN" and source_type != "HUMAN":
                continue
            by_field[assessment.name] = (assessment.expectation.value, source_type)

        if not by_field:
            continue
        human_answered = all(field in by_field and by_field[field][1] == "HUMAN" for field in _mlflow_review_fields())
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
