#!/usr/bin/env python3
"""
Create, run and score a human-labelled gold set for the business-profile
stage (Gate A2). Mirrors the shape of scripts/vlm/vlm_financial_eval.py --
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
from scripts.eval_support import deepeval_judges  # noqa: E402
from scripts.eval_support.langfuse_annotation import (  # noqa: E402
    ensure_queue,
    ensure_score_configs,
    question_score_configs,
    read_annotations,
    seed_draft_scores,
    sync_queue_items,
)
from scripts.eval_support.langfuse_runs import (  # noqa: E402
    evaluation,
    run_experiment,
    sync_dataset,
)
from scripts.eval_support.langfuse_tracing import (  # noqa: E402
    case_trace,
    flush,
    langfuse_from_config,
    observation,
)
from scripts.profile.business_profile_metrics import (  # noqa: E402
    SCORED_FIELDS,
    compute_metrics,
    flatten_metrics,
    score_case,
)
from scripts.profile.business_profile_policy import (  # noqa: E402
    FIELD_VALUES,
    NARRATIVE_SECTION_PRIORITY,
    PROMPT_VERSION,
    SIC_AGREEMENT_VALUES,
    build_prompt,
)
from scripts.profile.business_profile_prompt_registry import registered_prompt_reference  # noqa: E402
from scripts.profile.companies_house_business_profile import (  # noqa: E402
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
    quote_failures = sum(1 for o in outcomes if o["extracted"] is None)
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
        "config": str(args.config),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "cases": len(cases),
        "quote_or_validation_rejections": quote_failures,
        "quote_verification_pass_rate": round(1 - quote_failures / len(cases), 4) if cases else None,
        "unclear_rate": round(unclear_count / total_fields, 4) if total_fields else None,
        "elapsed_seconds": round(elapsed, 1),
        "metrics": metrics,
        "field_accuracy": {f: m.get("accuracy") for f, m in metrics["fields"].items()},
        "field_scored_counts": {f: m.get("scored", 0) for f, m in metrics["fields"].items()},
        "results": results,
    }


def _print_summary(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    print(f"\n{report['cases']} cases, {report['elapsed_seconds']:.0f}s")
    print(f"quote/validation pass rate: {report['quote_verification_pass_rate']}")
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
        if outcome["extracted"] is None:
            print(
                f"  [{i}/{len(cases)}] {case['company_number']}: REJECTED -- {'; '.join(outcome['errors'])}",
                file=sys.stderr,
            )
        outcomes.append(outcome)
    return outcomes


def _score_langfuse(
    lf: Any,
    config: dict[str, Any],
    client: BusinessProfileModelClient,
    model: str,
    timeout: int,
    cases: list[dict[str, Any]],
    run_name: str,
) -> list[dict[str, Any]]:
    by_id = {case["company_number"]: case for case in cases}
    records = [
        {
            "id": case["company_number"],
            "input": {
                "company_name": case["company_name"],
                "sic_code": case["sic_code"],
                "sic_label": case["sic_label"],
                "narrative_sections": _narrative_preview(case),
            },
            "expected": case.get("expected"),
            "metadata": {
                "company_number": case["company_number"],
                "financial_year": case.get("financial_year"),
            },
        }
        for case in cases
    ]
    sync_dataset(lf, DATASET_NAME, records, description="Business-profile gold set")

    outcomes: list[dict[str, Any]] = []

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
        outcome = _run_one_case(client, model, timeout, case)
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
    )
    flush(lf)
    for i, outcome in enumerate(outcomes, 1):
        if outcome["extracted"] is None:
            print(
                f"  [{i}/{len(outcomes)}] {outcome['case']['company_number']}: REJECTED -- "
                f"{'; '.join(outcome['errors'])}",
                file=sys.stderr,
            )
    print(f"\nLangfuse dataset run: {result.dataset_run_url}", file=sys.stderr)
    return outcomes


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
    cases = [load_case(path) for path in case_files(cases_dir)]
    if not args.include_unreviewed:
        cases = [case for case in cases if case.get("review", {}).get("status") == "verified"]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No verified cases to run (pass --include-unreviewed to run unverified ones too).", file=sys.stderr)
        return 1

    lf = None if args.no_langfuse else langfuse_from_config(config)
    client = BusinessProfileModelClient(api_key)

    start = time.monotonic()
    if lf is None:
        outcomes = _score_plain(client, model, timeout, cases)
    else:
        run_name = (config.get("langfuse") or {}).get("run_name") or config.get("run_name") or "run"
        run_name = f"{run_name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        outcomes = _score_langfuse(lf, config, client, model, timeout, cases, run_name)
    elapsed = time.monotonic() - start

    report = _report(args, model, cases, outcomes, elapsed)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    _print_summary(report)
    print(f"\nReport written to {report_path}")

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
# exactly as scripts/vlm/vlm_financial_eval.py's queue lives in the
# vlm-financial-eval project.
#
# Each field's chosen VALUE is a score config (a categorical dropdown, or
# free text for business_description). The supporting quote and section stay
# authoritative in the case JSON and visible in the trace input -- review is
# for the judgement call, not for re-transcribing evidence.
# ---------------------------------------------------------------------------

def _load_trace_map() -> dict[str, str]:
    if ANNOTATION_TRACE_MAP.is_file():
        return json.loads(ANNOTATION_TRACE_MAP.read_text(encoding="utf-8"))
    return {}


def _save_trace_map(mapping: dict[str, str]) -> None:
    ANNOTATION_TRACE_MAP.parent.mkdir(parents=True, exist_ok=True)
    ANNOTATION_TRACE_MAP.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    trace_map = _load_trace_map()
    new_traces = 0
    for case in cases:
        company_number = case["company_number"]
        if company_number in trace_map:
            continue
        with case_trace(
            lf,
            name="business_profile_review",
            tags=["business-profile-review", f"company:{company_number}"],
            metadata={
                "company_number": company_number,
                "company_name": case.get("company_name"),
                "sic_code": case.get("sic_code"),
                "sic_label": case.get("sic_label"),
            },
            input=_case_trace_inputs(case),
            output=_case_trace_outputs(case),
        ) as root:
            trace_map[company_number] = root.trace_id
        new_traces += 1
    flush(lf)
    _save_trace_map(trace_map)

    trace_ids = [trace_map[case["company_number"]] for case in cases if case["company_number"] in trace_map]
    for case in cases:
        trace_id = trace_map.get(case["company_number"])
        if trace_id is None:
            continue
        seed_draft_scores(lf, trace_id, _draft_answers(case), config_ids)
    flush(lf)

    # Cases whose expected block is already fully populated open as COMPLETED --
    # ready to check, not a backlog to work through.
    complete = [
        trace_map[case["company_number"]]
        for case in cases
        if case["company_number"] in trace_map
        and all(value is not None for value in _draft_answers(case).values())
    ]
    result = sync_queue_items(lf, queue_id, trace_ids, complete=complete)

    print(json.dumps({
        "queue": args.queue_name,
        "cases": len(cases),
        "new_traces": new_traces,
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
    updated = 0
    for case in [load_case(path) for path in case_files(cases_dir)]:
        trace_id = trace_map.get(case["company_number"])
        if trace_id is None:
            continue
        answers = read_annotations(lf, trace_id, field_names)
        human_answers = {name: entry for name, entry in answers.items() if entry.get("human")}
        if not human_answers:
            continue  # only our seeded drafts -- nothing to write back

        expected = case.get("expected") or {}
        for name, entry in human_answers.items():
            if name == "business_description":
                expected["business_description"] = entry["value"]
            elif name == "sic_agreement":
                block = expected.get("sic_agreement") or {}
                block["value"] = entry["value"]
                expected["sic_agreement"] = block
            else:
                block = expected.get(name) or {}
                block["value"] = entry["value"]
                expected[name] = block
        case["expected"] = expected
        if all(name in human_answers for name in field_names):
            case["review"] = {"status": "verified", "reviewed_at": utc_now(), "reviewer": "langfuse-annotation-queue"}
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
    run.add_argument("--no-langfuse", action="store_true", help="Score locally without logging to Langfuse.")

    sync_queue = commands.add_parser(
        "sync-annotation-queue",
        aliases=["sync-review-queue"],
        help="Push case files into the Langfuse annotation queue, seeded with this session's draft labels.",
    )
    sync_queue.add_argument("--config", required=True)
    sync_queue.add_argument("--cases-dir", default="evals/business_profiles/cases")
    sync_queue.add_argument("--queue-name", default=ANNOTATION_QUEUE_NAME)

    export = commands.add_parser(
        "export-annotations",
        aliases=["export-reviews"],
        help="Write human answers from the Langfuse annotation queue back into the case JSON files.",
    )
    export.add_argument("--config", required=True)
    export.add_argument("--cases-dir", default="evals/business_profiles/cases")

    args = parser.parse_args(argv)
    if args.command == "initialise":
        if args.count < 1:
            parser.error("--count must be positive")
        created = initialise_cases(Path(args.db), Path(args.cases_dir), args.count, args.seed)
        print(json.dumps({"created": created, "cases_dir": args.cases_dir}, indent=2))
        return 0
    if args.command in ("sync-annotation-queue", "sync-review-queue"):
        return sync_annotation_queue(args)
    if args.command in ("export-annotations", "export-reviews"):
        return export_annotations(args)
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
