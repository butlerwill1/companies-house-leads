#!/usr/bin/env python3
"""Create, run and score a human-labelled financial-PDF VLM evaluation set.

The gold labels live in JSON files in ``evals/vlm_financials/cases``.  This
module intentionally scores numbers deterministically; an LLM is never used to
decide whether a financial value is correct.
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# Allow the documented ``python .\\scripts\\ocr\\...`` invocation as well as
# module execution from the repository root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from companies_house_extractor import load_dotenv  # noqa: E402
from scripts.ocr.companies_house_pdf_vlm_financials import (
    CANONICAL_METRICS,
    DEFAULT_OLLAMA_BASE_URL,
    OllamaVlmModelClient,
    OpenRouterVlmModelClient,
    VlmModelClient,
    process_pdf_vlm_financials,
)  # noqa: E402

PERIODS = ("current", "previous")
CASE_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_pdf_path(case: dict[str, Any]) -> Path:
    """Resolve a portable case path relative to the repository when needed."""
    path = Path(case["pdf_path"])
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def canonical_empty_expectations() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        period: {
            metric: {
                "state": "unreviewed",
                "value_pence": None,
                "value_count": None,
                "displayed_value": None,
                "unit": None,
                "source_page": None,
                "source_label": None,
            }
            for metric in CANONICAL_METRICS
        }
        for period in PERIODS
    }


def case_path(cases_dir: Path, case_id: str) -> Path:
    return cases_dir / f"{case_id}.json"


def load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_case(path: Path, case: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def case_files(cases_dir: Path) -> list[Path]:
    return sorted(path for path in cases_dir.glob("*.json") if path.name != "manifest.json")


def load_verified_cases(cases_dir: Path, include_unreviewed: bool) -> list[dict[str, Any]]:
    cases = [load_case(path) for path in case_files(cases_dir)]
    if include_unreviewed:
        return cases
    return [case for case in cases if case.get("review", {}).get("status") == "verified"]


def validate_case(case: dict[str, Any], *, require_complete: bool = False) -> list[str]:
    errors: list[str] = []
    if case.get("schema_version") != CASE_SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    for key in ("id", "company_number", "pdf_path", "pdf_sha256", "split", "metadata", "expected"):
        if not case.get(key):
            errors.append(f"missing {key}")
    if case.get("split") not in {"development", "holdout"}:
        errors.append("split must be development or holdout")
    pages = case.get("expected", {}).get("statement_pages")
    if not isinstance(pages, list) or any(not isinstance(page, int) or page < 1 for page in pages):
        errors.append("statement_pages must be positive integers")
    summaries = case.get("expected", {}).get("financial_period_summaries", {})
    for period in PERIODS:
        for metric in CANONICAL_METRICS:
            value = summaries.get(period, {}).get(metric)
            if not isinstance(value, dict):
                errors.append(f"missing expected {period}.{metric}")
                continue
            state = value.get("state")
            if state not in {"unreviewed", "present", "missing"}:
                errors.append(f"invalid state for {period}.{metric}")
            if metric == "employees" and value.get("value_pence") is not None:
                errors.append(f"employees must use value_count for {period}.{metric}")
            if metric != "employees" and value.get("value_count") is not None:
                errors.append(f"money metric must use value_pence for {period}.{metric}")
            if state == "present":
                amount = value.get("value_count") if metric == "employees" else value.get("value_pence")
                if not isinstance(amount, int):
                    errors.append(f"present value missing for {period}.{metric}")
                if not isinstance(value.get("source_page"), int):
                    errors.append(f"present value needs source_page for {period}.{metric}")
            if state == "missing" and any(
                value.get(key) is not None for key in ("value_pence", "value_count")
            ):
                errors.append(f"missing value must be null for {period}.{metric}")
            if require_complete and state == "unreviewed":
                errors.append(f"unreviewed expected value for {period}.{metric}")
    if require_complete and not pages:
        errors.append("verified case needs at least one statement page")
    return errors


def configuration_from_file(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("evaluation configuration must be a mapping")
    forbidden = {"api_key", "token", "secret", "password"}

    def check(value: Any, location: str = "") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in forbidden:
                    raise ValueError(f"secret key '{location}{key}' is not allowed in evaluation config")
                check(nested, f"{location}{key}.")
        elif isinstance(value, list):
            for nested in value:
                check(nested, location)

    check(config)
    provider = config.get("provider")
    if provider not in {"openrouter", "ollama"}:
        raise ValueError("provider must be openrouter or ollama")
    for key in ("locator_model", "vision_model", "rationalisation_model"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"{key} is required")
    return config


def build_client(config: dict[str, Any]) -> VlmModelClient:
    if config["provider"] == "ollama":
        return OllamaVlmModelClient(config.get("ollama_base_url", DEFAULT_OLLAMA_BASE_URL))
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY must be set for an OpenRouter evaluation")
    request_options = config.get("openrouter_request_options") or {}
    if not isinstance(request_options, dict):
        raise ValueError("openrouter_request_options must be a mapping")
    return OpenRouterVlmModelClient(api_key, request_options)


def select_cases(db_path: Path, cases_dir: Path, count: int) -> list[dict[str, Any]]:
    """Create a balanced, unreviewed no-XHTML case set from locally available PDFs."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """
            select nr.company_number, nr.document_id, nr.pdf_path, nr.text_source,
                   l.sic_1, l.account_category, c.company_name
            from narrative_runs nr
            left join documents d on d.document_id = nr.document_id
            left join leads l on l.company_number = nr.company_number
            left join companies c on c.company_number = nr.company_number
            where nr.pdf_path is not null and (d.xhtml_url is null or d.xhtml_url = '')
            order by nr.id desc
            """
        ).fetchall()
    finally:
        connection.close()
    strata: dict[tuple[str, str], list[tuple[Any, ...]]] = defaultdict(list)
    seen_paths: set[Path] = set()
    for row in rows:
        pdf_path = Path(str(row[2]))
        if not pdf_path.exists() or pdf_path in seen_paths:
            continue
        seen_paths.add(pdf_path)
        sic_group = str(row[4] or "unknown")[:2] or "unknown"
        strata[(sic_group, str(row[5] or "unknown"))].append(row)
    selected: list[tuple[Any, ...]] = []
    keys = sorted(strata, key=lambda key: (-len(strata[key]), key))
    while keys and len(selected) < count:
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            if len(selected) == count:
                break
            selected.append(strata[key].pop(0))
            if strata[key]:
                next_keys.append(key)
        keys = next_keys
    cases: list[dict[str, Any]] = []
    for index, row in enumerate(selected, start=1):
        company_number, document_id, raw_path, text_source, sic_1, account_category, company_name = row
        pdf_path = Path(str(raw_path)).resolve()
        case_id = f"{company_number or 'unknown'}-{document_id or pdf_path.stem}".replace("/", "-")
        split = "holdout" if index > count - 15 else "development"
        try:
            stored_pdf_path = str(pdf_path.relative_to(Path.cwd().resolve()))
        except ValueError:
            stored_pdf_path = str(pdf_path)
        case = {
            "schema_version": CASE_SCHEMA_VERSION,
            "id": case_id,
            "company_number": company_number,
            "document_id": document_id,
            "pdf_path": stored_pdf_path,
            "pdf_sha256": sha256_file(pdf_path),
            "split": split,
            "metadata": {
                "company_name": company_name,
                "sic_1": sic_1,
                "sic_division": str(sic_1 or "unknown")[:2] or "unknown",
                "account_category": account_category or "unknown",
                "text_source": text_source or "unknown",
                "scan_quality": "unreviewed",
                "layout_type": "unreviewed",
                "difficulty": "unreviewed",
            },
            "expected": {
                "statement_pages": [],
                "financial_period_summaries": canonical_empty_expectations(),
            },
            "review": {"status": "unreviewed", "reviewer": None, "reviewed_at": None, "notes": None},
        }
        save_case(case_path(cases_dir, case_id), case)
        cases.append(case)
    manifest = {
        "schema_version": CASE_SCHEMA_VERSION,
        "created_at": utc_now(),
        "count": len(cases),
        "development": sum(case["split"] == "development" for case in cases),
        "holdout": sum(case["split"] == "holdout" for case in cases),
        "selection": "round-robin SIC division and account category; no XHTML URL",
    }
    save_case(cases_dir / "manifest.json", manifest)
    return cases


def metrics_by_key(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(item["period_type"], item["metric_name"]): item for item in payload.get("metrics", [])}


def candidate_matches_expected(candidate: dict[str, Any], expected: dict[str, Any], period: str, metric: str) -> bool:
    if candidate.get("metric") != metric or candidate.get("page") != expected.get("source_page"):
        return False
    display = candidate.get(f"{period}_display")
    return display is not None and str(display) == str(expected.get("displayed_value"))


def score_payload(case: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Score one model result against one fully verified gold case."""
    errors = validate_case(case, require_complete=True)
    if errors:
        raise ValueError(f"invalid verified case {case.get('id')}: {', '.join(errors)}")
    expected = case["expected"]
    gold_pages = set(expected["statement_pages"])
    predicted_pages = set(payload.get("candidate_pages") or [])
    page_tp = len(gold_pages & predicted_pages)
    page_precision = page_tp / len(predicted_pages) if predicted_pages else 0.0
    page_recall = page_tp / len(gold_pages) if gold_pages else 1.0
    page_f1 = 2 * page_precision * page_recall / (page_precision + page_recall) if page_precision + page_recall else 0.0

    predicted_metrics = metrics_by_key(payload)
    predicted_candidates = payload.get("raw_extraction", {}).get("candidates") or []
    cells: list[dict[str, Any]] = []
    candidate_present = 0
    rationalisation_correct = 0
    for period in PERIODS:
        for metric in CANONICAL_METRICS:
            gold = expected["financial_period_summaries"][period][metric]
            predicted = predicted_metrics.get((period, metric))
            expected_present = gold["state"] == "present"
            predicted_present = predicted is not None
            correct = False
            if not expected_present:
                correct = not predicted_present
            elif predicted is not None:
                expected_value = gold["value_count"] if metric == "employees" else gold["value_pence"]
                actual_value = predicted.get("value_count") if metric == "employees" else predicted.get("value_pence")
                correct = expected_value == actual_value
            source_candidate = any(
                candidate_matches_expected(candidate, gold, period, metric)
                for candidate in predicted_candidates
            ) if expected_present else False
            candidate_present += int(source_candidate)
            rationalisation_correct += int(source_candidate and correct)
            cells.append({
                "period": period,
                "metric": metric,
                "expected_present": expected_present,
                "predicted_present": predicted_present,
                "correct": correct,
                "candidate_present": source_candidate,
                "confidence": predicted.get("confidence") if predicted else None,
            })
    true_positive = sum(cell["expected_present"] and cell["predicted_present"] and cell["correct"] for cell in cells)
    false_positive = sum(not cell["expected_present"] and cell["predicted_present"] for cell in cells)
    false_negative = sum(cell["expected_present"] and not cell["correct"] for cell in cells)
    expected_populated = sum(cell["expected_present"] for cell in cells)
    expected_missing = len(cells) - expected_populated
    return {
        "case_id": case["id"],
        "split": case["split"],
        "metadata": case["metadata"],
        "status": payload.get("status"),
        "page": {"precision": page_precision, "recall": page_recall, "f1": page_f1, "gold": len(gold_pages), "predicted": len(predicted_pages), "true_positive": page_tp},
        "cells": cells,
        "counts": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "expected_populated": expected_populated,
            "expected_missing": expected_missing,
            "exact_cells": sum(cell["correct"] for cell in cells),
            "cells": len(cells),
            "candidate_present": candidate_present,
            "rationalisation_correct": rationalisation_correct,
        },
        "whole_document_exact": all(cell["correct"] for cell in cells) and page_recall == 1.0,
        "timing": payload.get("timing", {}),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "usage": payload.get("usage", {}),
        "cost": payload.get("cost", {}),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def aggregate_scores(scores: list[dict[str, Any]], hardware: dict[str, Any] | None = None) -> dict[str, Any]:
    complete = [score for score in scores if score.get("status") == "complete"]
    scored = [score for score in complete if not score.get("unscored", False)]
    counts = defaultdict(int)
    elapsed = [float(score["elapsed_seconds"]) for score in scores if isinstance(score.get("elapsed_seconds"), (int, float))]
    for score in scored:
        for key, value in score["counts"].items():
            counts[key] += int(value)
    precision = counts["true_positive"] / (counts["true_positive"] + counts["false_positive"]) if counts["true_positive"] + counts["false_positive"] else 1.0
    recall = counts["true_positive"] / counts["expected_populated"] if counts["expected_populated"] else 1.0
    page_precision = statistics.fmean(score["page"]["precision"] for score in scored) if scored else None
    page_recall = statistics.fmean(score["page"]["recall"] for score in scored) if scored else None
    throughput = len(scores) / (sum(elapsed) / 3600) if elapsed and sum(elapsed) else None
    report: dict[str, Any] = {
        "documents": len(scores),
        "scored_documents": len(scored),
        "complete": len(complete),
        "errors": len(scores) - len(complete),
        "page_precision_mean": page_precision,
        "page_recall_mean": page_recall,
        "exact_cell_accuracy": counts["exact_cells"] / counts["cells"] if counts["cells"] else None,
        "populated_value_precision": precision,
        "populated_value_recall": recall,
        "false_positive_rate_for_missing": counts["false_positive"] / counts["expected_missing"] if counts["expected_missing"] else None,
        "candidate_recall": counts["candidate_present"] / counts["expected_populated"] if counts["expected_populated"] else None,
        "rationalisation_accuracy_when_candidate_present": counts["rationalisation_correct"] / counts["candidate_present"] if counts["candidate_present"] else None,
        "whole_document_exact_rate": sum(score["whole_document_exact"] for score in scored) / len(scored) if scored else None,
        "latency_seconds": {"p50": percentile(elapsed, 0.5), "p90": percentile(elapsed, 0.9), "p95": percentile(elapsed, 0.95), "mean": statistics.fmean(elapsed) if elapsed else None},
        "pdfs_per_hour": throughput,
        "estimated_20000_hours": 20_000 / throughput if throughput else None,
    }
    if hardware and elapsed:
        hours = sum(elapsed) / 3600
        compute_rate = hardware.get("compute_cost_gbp_per_hour")
        watts = hardware.get("wall_power_watts")
        electricity = hardware.get("electricity_gbp_per_kwh")
        report["estimated_compute_cost_gbp"] = hours * float(compute_rate) if compute_rate is not None else None
        report["estimated_energy_cost_gbp"] = (
            hours * float(watts) / 1000 * float(electricity)
            if watts is not None and electricity is not None else None
        )
    return report


def git_revision() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_case(case: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    client = build_client(config)
    pdf_path = resolve_pdf_path(case)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF is missing: {pdf_path}")
    if sha256_file(pdf_path) != case["pdf_sha256"]:
        raise RuntimeError(f"PDF hash changed for {case['id']}")
    payload = process_pdf_vlm_financials(
        pdf_path,
        client,
        locator_model=config["locator_model"],
        vision_model=config["vision_model"],
        rationalisation_model=config["rationalisation_model"],
        max_pages=int(config.get("max_pages", 60)),
        gbp_per_usd=float(config.get("gbp_per_usd", 0.75)),
        timeout=int(config.get("timeout_seconds", 180)),
    )
    fallback = config.get("fallback")
    if fallback and payload["status"] == "no_statement_pages_found":
        fallback_config = {**fallback, "fallback": None}
        fallback_payload = run_case_payload(pdf_path, fallback_config)
        fallback_payload["fallback"] = {"reason": "no_statement_pages_found", "primary": payload}
        payload = fallback_payload
    if case.get("review", {}).get("status") != "verified":
        return payload, {
            "case_id": case["id"],
            "split": case["split"],
            "metadata": case["metadata"],
            "status": payload.get("status"),
            "unscored": True,
            "elapsed_seconds": payload.get("elapsed_seconds"),
            "timing": payload.get("timing", {}),
            "usage": payload.get("usage", {}),
            "cost": payload.get("cost", {}),
            "counts": {},
        }
    return payload, score_payload(case, payload)


def run_case_payload(pdf_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    return process_pdf_vlm_financials(
        pdf_path,
        build_client(config),
        locator_model=config["locator_model"],
        vision_model=config["vision_model"],
        rationalisation_model=config["rationalisation_model"],
        max_pages=int(config.get("max_pages", 60)),
        gbp_per_usd=float(config.get("gbp_per_usd", 0.75)),
        timeout=int(config.get("timeout_seconds", 180)),
    )


def log_mlflow(config: dict[str, Any], report: dict[str, Any], output_dir: Path) -> str | None:
    settings = config.get("mlflow") or {}
    if not settings.get("enabled", False):
        return None
    try:
        import mlflow
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to enable MLflow logging") from error
    mlflow.set_tracking_uri(settings.get("tracking_uri", "http://127.0.0.1:5000"))
    mlflow.set_experiment(settings.get("experiment", "companies-house-vlm-financial-eval"))
    with mlflow.start_run(run_name=settings.get("run_name")) as run:
        safe_params = {key: value for key, value in config.items() if key not in {"fallback", "mlflow", "hardware"}}
        mlflow.log_params({key: str(value) for key, value in safe_params.items()})
        mlflow.log_param("git_revision", git_revision() or "unknown")
        for key, value in report["aggregate"].items():
            if isinstance(value, (int, float)):
                mlflow.log_metric(key, float(value))
        for key, value in report["aggregate"].get("latency_seconds", {}).items():
            if value is not None:
                mlflow.log_metric(f"latency_{key}", float(value))
        mlflow.log_dict(report, "report.json")
        mlflow.log_artifacts(str(output_dir), artifact_path="evaluation")
        return run.info.run_id


def run_evaluation(args: argparse.Namespace) -> int:
    load_dotenv(Path.cwd() / ".env")
    config = configuration_from_file(Path(args.config))
    if args.no_mlflow:
        config["mlflow"] = {"enabled": False}
    cases = load_verified_cases(Path(args.cases_dir), args.include_unreviewed)
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    if args.limit is not None:
        cases = cases[:args.limit]
    if not cases:
        raise RuntimeError("No cases matched; verify cases first or use --include-unreviewed for a smoke run")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    outcomes: list[dict[str, Any]] = []

    def execute(case: dict[str, Any], attempt: int) -> dict[str, Any]:
        try:
            payload, score = run_case(case, config)
            name = f"{case['id']}-attempt-{attempt}.json"
            (output_dir / name).write_text(json.dumps({"payload": payload, "score": score}, indent=2), encoding="utf-8")
            return score
        except Exception as error:
            return {"case_id": case["id"], "split": case["split"], "metadata": case["metadata"], "status": "error", "error": str(error), "counts": defaultdict(int), "page": {"precision": 0.0, "recall": 0.0, "f1": 0.0}, "whole_document_exact": False}

    jobs = [(case, attempt) for attempt in range(1, args.repeats + 1) for case in cases]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency or int(config.get("concurrency", 1))) as executor:
        for outcome in executor.map(lambda job: execute(*job), jobs):
            outcomes.append(outcome)
            print(json.dumps({"case_id": outcome["case_id"], "status": outcome["status"]}), file=sys.stderr)
    report = {
        "created_at": utc_now(),
        "config": {key: value for key, value in config.items() if key not in {"fallback", "mlflow", "hardware"}},
        "git_revision": git_revision(),
        "dataset_cases": len(cases),
        "repeats": args.repeats,
        "batch_elapsed_seconds": round(time.perf_counter() - started, 4),
        "aggregate": aggregate_scores(outcomes, config.get("hardware")),
        "outcomes": outcomes,
    }
    report["mlflow_run_id"] = log_mlflow(config, report, output_dir)
    (output_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "aggregate": report["aggregate"], "mlflow_run_id": report["mlflow_run_id"]}, indent=2))
    return 0 if not report["aggregate"]["errors"] else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialise = commands.add_parser("initialise", help="Create unreviewed, balanced PDF cases.")
    initialise.add_argument("--db", default="companies-house.db")
    initialise.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    initialise.add_argument("--count", type=int, default=50)
    run = commands.add_parser("run", help="Run verified gold cases and log an experiment.")
    run.add_argument("--config", required=True)
    run.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    run.add_argument("--output-dir", default="logs/vlm-financial-eval")
    run.add_argument("--split", choices=("all", "development", "holdout"), default="all")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--concurrency", type=int)
    run.add_argument("--limit", type=int, help="Limit cases for a low-cost smoke benchmark.")
    run.add_argument("--include-unreviewed", action="store_true")
    run.add_argument("--no-mlflow", action="store_true", help="Save JSON artifacts without starting MLflow.")
    args = parser.parse_args(argv)
    if args.command == "initialise":
        if args.count < 1:
            parser.error("--count must be positive")
        cases = select_cases(Path(args.db), Path(args.cases_dir), args.count)
        print(json.dumps({"created": len(cases), "cases_dir": args.cases_dir}, indent=2))
        return 0
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
