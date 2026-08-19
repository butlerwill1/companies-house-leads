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
from scripts.profile.business_profile_policy import FIELD_VALUES, PROMPT_VERSION  # noqa: E402
from scripts.profile.companies_house_business_profile import (  # noqa: E402
    BusinessProfileModelClient,
    extract_business_profile,
    fetch_narrative_context,
    load_config,
)

CASE_SCHEMA_VERSION = 1
SCORED_FIELDS = (*FIELD_VALUES.keys(), "sic_agreement")


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

    args = parser.parse_args(argv)
    if args.command == "initialise":
        if args.count < 1:
            parser.error("--count must be positive")
        created = initialise_cases(Path(args.db), Path(args.cases_dir), args.count, args.seed)
        print(json.dumps({"created": created, "cases_dir": args.cases_dir}, indent=2))
        return 0
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
