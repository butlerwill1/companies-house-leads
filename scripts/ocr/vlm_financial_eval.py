#!/usr/bin/env python3
"""Create, run and score a human-labelled financial-PDF VLM evaluation set.

MLflow Review is the mutable source of truth for gold labels.  Published MLflow
datasets are immutable snapshots of those completed reviews.  This module
intentionally scores numbers deterministically; an LLM is never used to decide
whether a financial value is correct.
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

# Allow the documented ``python .\\scripts\\ocr\\...`` invocation as well as
# module execution from the repository root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_extractor import load_dotenv  # noqa: E402
from scripts.ocr.companies_house_pdf_vlm_financials import (
    CANONICAL_METRICS,
    DEFAULT_OLLAMA_BASE_URL,
    RATIONALISATION_PROMPT,
    ModelCallResult,
    OllamaVlmModelClient,
    OpenRouterVlmModelClient,
    VlmModelClient,
    extraction_candidates,
    normalise_company_context,
    normalise_unit,
    process_pdf_vlm_financials,
    selected_metrics,
    currency_and_scale,
    reported_value,
    to_count,
    to_pence,
    usage_cost_usd,
)  # noqa: E402
from scripts.ocr.financial_metric_policy import add_canonical_equivalents  # noqa: E402

PERIODS = ("current", "previous")
CASE_SCHEMA_VERSION = 1
MLFLOW_REVIEW_QUEUE_NAME = "Financial PDF gold-label review"
DEFAULT_MLFLOW_DATASET_NAME = "companies-house-financial-gold-v1"

METRIC_TITLES = {
    "turnover": "Turnover",
    "gross_profit": "Gross profit",
    "operating_result": "Operating result",
    "profit_after_tax": "Profit after tax",
    "cash": "Cash",
    "net_assets": "Net assets",
    "employees": "Employees",
}
CORE_FINANCIAL_METRICS = tuple(metric for metric in CANONICAL_METRICS if metric != "employees")


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
    result = {
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
    return result


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


def mlflow_dataset_records(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the portable, human-labelled records to publish to MLflow.

    The PDF files deliberately stay outside MLflow.  The Companies House
    identifiers and content hash are enough to connect a dataset record to the
    exact local source document without copying financial documents into the
    tracking service.
    """
    records: list[dict[str, Any]] = []
    for case in sorted(cases, key=lambda item: str(item["id"])):
        errors = validate_case(case, require_complete=True)
        if errors:
            raise ValueError(f"case {case.get('id', '<unknown>')} is not publishable: {', '.join(errors)}")
        records.append({
            "inputs": {
                "case_id": case["id"],
                "company_number": case["company_number"],
                "document_id": case["document_id"],
                "pdf_sha256": case["pdf_sha256"],
                "split": case["split"],
                "metadata": copy.deepcopy(case["metadata"]),
            },
            "expectations": {
                "statement_pages": copy.deepcopy(case["expected"]["statement_pages"]),
                "employee_evidence_pages": copy.deepcopy(
                    case["expected"].get("employee_evidence_pages")
                ),
                "financial_period_summaries": copy.deepcopy(
                    case["expected"]["financial_period_summaries"]
                ),
            },
            "tags": {
                "label_status": "verified",
                "case_schema_version": str(case["schema_version"]),
            },
        })
    return records


def mlflow_dataset_digest(records: list[dict[str, Any]]) -> str:
    """Produce a stable content identity for a published gold-label snapshot."""
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
                errors.append(f"money metric must not use value_count for {period}.{metric}")
            if state == "present":
                currency, amount = metric_comparison_value(value, metric)
                if amount is None:
                    errors.append(f"present value missing for {period}.{metric}")
                if metric != "employees" and currency is None:
                    errors.append(f"present money value needs currency for {period}.{metric}")
                if not isinstance(value.get("source_page"), int):
                    errors.append(f"present value needs source_page for {period}.{metric}")
                if metric == "employees" and value.get("evidence_kind") is not None and value.get("evidence_kind") not in {
                    "numeric", "dash_zero", "narrative_zero",
                }:
                    errors.append(f"invalid employee evidence_kind for {period}.{metric}")
            if state == "missing" and any(
                value.get(key) is not None
                for key in ("value_pence", "value_count", "reported_value")
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
    if config.get("recovery_vision_model") is not None and not isinstance(
        config["recovery_vision_model"], str
    ):
        raise ValueError("recovery_vision_model must be a string when supplied")
    return config


def build_client(config: dict[str, Any]) -> VlmModelClient:
    if config["provider"] == "ollama":
        base_url = (
            os.getenv("PRIVATE_OLLAMA_BASE_URL")
            or os.getenv("OLLAMA_BASE_URL")
            or config.get("ollama_base_url", DEFAULT_OLLAMA_BASE_URL)
        )
        client = OllamaVlmModelClient(base_url)
        client.health_check(
            {
                config["locator_model"],
                config["vision_model"],
                config["rationalisation_model"],
            } | ({config["recovery_vision_model"]} if config.get("recovery_vision_model") else set())
        )
        return client
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


def metric_comparison_value(
    value: dict[str, Any] | None, metric: str
) -> tuple[str | None, Decimal | int | None]:
    """Return an exact source-currency value without applying FX conversion."""
    if value is None:
        return None, None
    if metric == "employees":
        return "COUNT", value.get("value_count")

    unit = normalise_unit(value.get("unit"))
    unit_currency, _scale = currency_and_scale(unit)
    declared_currency = str(value.get("currency_code") or "").upper() or None
    if declared_currency and unit_currency and declared_currency != unit_currency:
        return f"{declared_currency}!={unit_currency}", None
    currency = declared_currency or unit_currency

    raw_reported = value.get("reported_value")
    if raw_reported is not None:
        try:
            return currency, Decimal(str(raw_reported))
        except InvalidOperation:
            return currency, None

    value_pence = value.get("value_pence")
    if currency in {None, "GBP"} and isinstance(value_pence, int):
        return "GBP", Decimal(value_pence) / Decimal(100)
    return currency, None


def candidate_matches_expected(candidate: dict[str, Any], expected: dict[str, Any], period: str, metric: str) -> bool:
    if candidate.get("metric") != metric or candidate.get("page") != expected.get("source_page"):
        return False
    if metric == "employees" and expected.get("evidence_kind") is not None:
        return (
            candidate.get(f"{period}_value_count") == expected.get("value_count")
            and candidate.get(f"{period}_evidence_kind") == expected.get("evidence_kind")
        )
    display = candidate.get(f"{period}_display")
    return display is not None and str(display) == str(expected.get("displayed_value"))


def cell_counts(cells: list[dict[str, Any]]) -> dict[str, int]:
    """Count deterministic cell outcomes for a metric group or whole case."""
    true_positive = sum(
        cell["expected_present"] and cell["predicted_present"] and cell["correct"]
        for cell in cells
    )
    false_positive = sum(
        not cell["expected_present"] and cell["predicted_present"] for cell in cells
    )
    false_negative = sum(cell["expected_present"] and not cell["correct"] for cell in cells)
    expected_populated = sum(cell["expected_present"] for cell in cells)
    result = {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "expected_populated": expected_populated,
        "expected_missing": len(cells) - expected_populated,
        "exact_cells": sum(cell["correct"] for cell in cells),
        "cells": len(cells),
        "candidate_present": sum(cell["candidate_present"] for cell in cells),
        "rationalisation_correct": sum(
            cell["candidate_present"] and cell["correct"] for cell in cells
        ),
    }
    return result


def metric_group_summary(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose a compact, directly comparable score for a named metric group."""
    counts = cell_counts(cells)
    return {
        "counts": counts,
        "exact_cell_accuracy": counts["exact_cells"] / counts["cells"] if counts["cells"] else None,
        "populated_value_recall": (
            counts["true_positive"] / counts["expected_populated"]
            if counts["expected_populated"]
            else None
        ),
        "populated_value_precision": (
            counts["true_positive"] / (counts["true_positive"] + counts["false_positive"])
            if counts["true_positive"] + counts["false_positive"]
            else None
        ),
    }


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
    raw_extraction = payload.get("raw_extraction", {})
    predicted_candidates = (
        raw_extraction.get("accepted_candidates") or raw_extraction.get("candidates") or []
    )
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
                expected_value = metric_comparison_value(gold, metric)
                actual_value = metric_comparison_value(predicted, metric)
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
                "employee_evidence_kind": gold.get("evidence_kind") if metric == "employees" else None,
                "predicted_employee_evidence_kind": (
                    (predicted.get("validation") or {}).get("evidence_kind")
                    if metric == "employees" and predicted else None
                ),
            })
    counts = cell_counts(cells)
    employee_gold_pages = expected.get("employee_evidence_pages")
    employee_page_score = None
    if isinstance(employee_gold_pages, list):
        gold_employee_pages = set(employee_gold_pages)
        predicted_employee_pages = set(payload.get("employee_evidence_pages") or [])
        employee_true_positive = len(gold_employee_pages & predicted_employee_pages)
        employee_page_score = {
            "precision": (
                employee_true_positive / len(predicted_employee_pages)
                if predicted_employee_pages else 0.0
            ),
            "recall": (
                employee_true_positive / len(gold_employee_pages) if gold_employee_pages else 1.0
            ),
            "gold": len(gold_employee_pages),
            "predicted": len(predicted_employee_pages),
            "true_positive": employee_true_positive,
        }
    return {
        "case_id": case["id"],
        "split": case["split"],
        "metadata": case["metadata"],
        "status": payload.get("status"),
        "error": payload.get("error"),
        "error_stage": payload.get("error_stage"),
        "warnings": payload.get("warnings") or [],
        "page": {"precision": page_precision, "recall": page_recall, "f1": page_f1, "gold": len(gold_pages), "predicted": len(predicted_pages), "true_positive": page_tp},
        "employee_evidence_page": employee_page_score,
        "cells": cells,
        "counts": counts,
        "metric_groups": {
            "core_financial": metric_group_summary(
                [cell for cell in cells if cell["metric"] in CORE_FINANCIAL_METRICS]
            ),
            "employees": metric_group_summary(
                [cell for cell in cells if cell["metric"] == "employees"]
            ),
        },
        "employee_evidence_kind_groups": {
            kind: metric_group_summary([
                cell for cell in cells
                if cell["metric"] == "employees" and cell["employee_evidence_kind"] == kind
            ])
            for kind in sorted({
                cell["employee_evidence_kind"]
                for cell in cells
                if cell["metric"] == "employees" and cell["employee_evidence_kind"] is not None
            })
        },
        "whole_document_exact": all(cell["correct"] for cell in cells) and page_recall == 1.0,
        "timing": payload.get("timing", {}),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "usage": payload.get("usage", {}),
        "cost": payload.get("cost", {}),
    }


def cell_comparison_rows(case: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one human-readable deterministic comparison row per expected cell.

    MLflow's trace viewer shows the pipeline inputs and outputs, but it does not
    automatically render a field-by-field financial comparison.  Keeping this
    projection separate from ``score_payload`` means both the benchmark and a
    later re-score against corrected labels use precisely the same comparison
    rules.
    """
    score = score_payload(case, payload)
    expected_summaries = case["expected"]["financial_period_summaries"]
    predicted = metrics_by_key(payload)
    rows: list[dict[str, Any]] = []
    for cell in score["cells"]:
        period = cell["period"]
        metric = cell["metric"]
        expected = expected_summaries[period][metric]
        actual = predicted.get((period, metric))
        if cell["correct"]:
            outcome = "correct"
        elif not cell["expected_present"]:
            outcome = "unexpected_prediction"
        elif not cell["predicted_present"]:
            outcome = "missing_prediction"
        else:
            outcome = "wrong_value"
        rows.append(
            {
                "company_number": case["company_number"],
                "case_id": case["id"],
                "split": case["split"],
                "period": period,
                "metric": metric,
                "metric_title": METRIC_TITLES[metric],
                "metric_group": "employees" if metric == "employees" else "core_financial",
                "outcome": outcome,
                "correct": cell["correct"],
                "candidate_present": cell["candidate_present"],
                "expected_state": expected["state"],
                "expected_displayed_value": expected.get("displayed_value"),
                "expected_unit": expected.get("unit"),
                "expected_source_page": expected.get("source_page"),
                "expected_employee_evidence_kind": expected.get("evidence_kind") if metric == "employees" else None,
                "predicted_displayed_value": actual.get("displayed_value") if actual else None,
                "predicted_unit": actual.get("unit") if actual else None,
                "predicted_source_page": actual.get("source_page") if actual else None,
                "predicted_employee_evidence_kind": (
                    (actual.get("validation") or {}).get("evidence_kind")
                    if metric == "employees" and actual else None
                ),
                "confidence": cell["confidence"],
            }
        )
    return rows


def write_cell_comparison_reports(
    rows: list[dict[str, Any]], output_dir: Path, *, prefix: str = "cell"
) -> dict[str, Any]:
    """Write complete and error-only CSV reports suitable for MLflow artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0]) if rows else [
        "company_number", "case_id", "split", "period", "metric", "metric_title",
        "metric_group", "outcome", "correct", "candidate_present", "expected_state",
        "expected_displayed_value", "expected_unit", "expected_source_page",
        "expected_employee_evidence_kind", "predicted_displayed_value", "predicted_unit",
        "predicted_source_page", "predicted_employee_evidence_kind", "confidence",
    ]
    all_path = output_dir / f"{prefix}-comparison.csv"
    error_path = output_dir / f"{prefix}-errors.csv"
    for path, selected_rows in ((all_path, rows), (error_path, [row for row in rows if not row["correct"]])):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(selected_rows)

    def group_summary(selected_rows: list[dict[str, Any]]) -> dict[str, int | float | None]:
        expected = [row for row in selected_rows if row["expected_state"] == "present"]
        correct_expected = [row for row in expected if row["correct"]]
        return {
            "cells": len(selected_rows),
            "expected_values": len(expected),
            "correct_expected_values": len(correct_expected),
            "expected_value_accuracy": len(correct_expected) / len(expected) if expected else None,
            "errors": sum(not row["correct"] for row in selected_rows),
        }

    summary = {
        "generated_at": utc_now(),
        "overall": group_summary(rows),
        "core_financial": group_summary([row for row in rows if row["metric_group"] == "core_financial"]),
        "employees": group_summary([row for row in rows if row["metric_group"] == "employees"]),
        "comparison_csv": all_path.name,
        "errors_csv": error_path.name,
    }
    summary_path = output_dir / f"{prefix}-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {**summary, "summary_path": str(summary_path)}


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
    grouped_counts: dict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    employee_evidence_kind_counts: dict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    elapsed = [float(score["elapsed_seconds"]) for score in scores if isinstance(score.get("elapsed_seconds"), (int, float))]
    for score in scored:
        for key, value in score["counts"].items():
            counts[key] += int(value)
        for group_name, group in (score.get("metric_groups") or {}).items():
            for key, value in (group.get("counts") or {}).items():
                grouped_counts[group_name][key] += int(value)
        for kind, group in (score.get("employee_evidence_kind_groups") or {}).items():
            for key, value in (group.get("counts") or {}).items():
                employee_evidence_kind_counts[kind][key] += int(value)
    precision = counts["true_positive"] / (counts["true_positive"] + counts["false_positive"]) if counts["true_positive"] + counts["false_positive"] else 1.0
    recall = counts["true_positive"] / counts["expected_populated"] if counts["expected_populated"] else 1.0
    page_precision = statistics.fmean(score["page"]["precision"] for score in scored) if scored else None
    page_recall = statistics.fmean(score["page"]["recall"] for score in scored) if scored else None
    employee_page_scores = [
        score["employee_evidence_page"]
        for score in scored
        if score.get("employee_evidence_page") is not None
    ]
    throughput = len(scores) / (sum(elapsed) / 3600) if elapsed and sum(elapsed) else None
    report: dict[str, Any] = {
        "documents": len(scores),
        "scored_documents": len(scored),
        "complete": len(complete),
        "errors": len(scores) - len(complete),
        "warning_documents": sum(bool(score.get("warnings")) for score in scores),
        "warnings": sum(len(score.get("warnings") or []) for score in scores),
        "page_precision_mean": page_precision,
        "page_recall_mean": page_recall,
        "employee_evidence_page_precision_mean": (
            statistics.fmean(score["precision"] for score in employee_page_scores)
            if employee_page_scores else None
        ),
        "employee_evidence_page_recall_mean": (
            statistics.fmean(score["recall"] for score in employee_page_scores)
            if employee_page_scores else None
        ),
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
        "employee_evidence_kind_groups": {
            kind: {
                "counts": dict(group),
                "exact_cell_accuracy": group["exact_cells"] / group["cells"] if group["cells"] else None,
                "populated_value_recall": (
                    group["true_positive"] / group["expected_populated"]
                    if group["expected_populated"] else None
                ),
                "populated_value_precision": (
                    group["true_positive"] / (group["true_positive"] + group["false_positive"])
                    if group["true_positive"] + group["false_positive"] else None
                ),
            }
            for kind, group in sorted(employee_evidence_kind_counts.items())
        },
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
    for group_name, group in grouped_counts.items():
        prefix = f"{group_name}_"
        report[f"{prefix}exact_cell_accuracy"] = (
            group["exact_cells"] / group["cells"] if group["cells"] else None
        )
        report[f"{prefix}populated_value_recall"] = (
            group["true_positive"] / group["expected_populated"]
            if group["expected_populated"]
            else None
        )
        report[f"{prefix}populated_value_precision"] = (
            group["true_positive"] / (group["true_positive"] + group["false_positive"])
            if group["true_positive"] + group["false_positive"]
            else None
        )
    return report


def git_revision() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def company_context_from_case(case: dict[str, Any]) -> dict[str, Any]:
    """Build advisory registration context without using it as a gold label."""
    metadata = case.get("metadata") or {}
    sic_codes = [metadata[key] for key in ("sic_1", "sic_2", "sic_3", "sic_4") if metadata.get(key)]
    return normalise_company_context({
        "company_number": case.get("company_number"),
        "sic_codes": sic_codes,
    })


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
        recovery_vision_model=config.get("recovery_vision_model"),
        rationalisation_model=config["rationalisation_model"],
        max_pages=int(config.get("max_pages", 60)),
        locator_batch_size=config.get("locator_batch_size"),
        extraction_batch_size=config.get("extraction_batch_size"),
        recovery_render_long_edge=int(config.get("recovery_render_long_edge", 2048)),
        json_max_attempts=int(config.get("json_max_attempts", 2)),
        gbp_per_usd=float(config.get("gbp_per_usd", 0.75)),
        timeout=int(config.get("timeout_seconds", 180)),
        company_context=company_context_from_case(case),
    )
    fallback = config.get("fallback")
    if fallback and payload["status"] == "no_statement_pages_found":
        fallback_config = {**fallback, "fallback": None}
        fallback_payload = run_case_payload(
            pdf_path, fallback_config, company_context=company_context_from_case(case)
        )
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


def run_case_payload(
    pdf_path: Path, config: dict[str, Any], *, company_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return process_pdf_vlm_financials(
        pdf_path,
        build_client(config),
        locator_model=config["locator_model"],
        vision_model=config["vision_model"],
        recovery_vision_model=config.get("recovery_vision_model"),
        rationalisation_model=config["rationalisation_model"],
        max_pages=int(config.get("max_pages", 60)),
        locator_batch_size=config.get("locator_batch_size"),
        extraction_batch_size=config.get("extraction_batch_size"),
        recovery_render_long_edge=int(config.get("recovery_render_long_edge", 2048)),
        json_max_attempts=int(config.get("json_max_attempts", 2)),
        gbp_per_usd=float(config.get("gbp_per_usd", 0.75)),
        timeout=int(config.get("timeout_seconds", 180)),
        company_context=company_context,
    )


def needs_page_number_backfill(payload: dict[str, Any]) -> bool:
    """Return whether numeric string pages caused saved rows to be discarded."""
    raw = payload.get("raw_extraction") or {}
    if raw.get("candidates"):
        return False
    detail = raw.get("detail") or {}
    return any(
        isinstance(page_item.get("page"), str)
        and page_item["page"].strip().isdigit()
        and bool(page_item.get("rows"))
        for page_item in detail.get("pages") or []
    )


def backfill_page_number_payload(
    payload: dict[str, Any],
    model_client: VlmModelClient,
    *,
    rationalisation_model: str,
    timeout: int,
    gbp_per_usd: float,
    original_trace_id: str,
) -> dict[str, Any]:
    """Rebuild candidates and rerun only rationalisation for one saved extraction."""
    if not needs_page_number_backfill(payload):
        raise ValueError("payload does not contain discarded numeric-string page rows")
    corrected = copy.deepcopy(payload)
    raw = corrected.setdefault("raw_extraction", {})
    candidates = add_canonical_equivalents(
        extraction_candidates(raw.get("detail") or {})
    )
    if not candidates:
        raise ValueError("page normalisation produced no financial candidates")
    call: ModelCallResult = model_client.generate_json(
        rationalisation_model,
        (
            f"{RATIONALISATION_PROMPT}\n\nCANDIDATES:\n"
            f"{json.dumps({'candidates': candidates}, separators=(',', ':'))}"
        ),
        [],
        timeout,
    )
    rationalisation = call.payload
    metrics = selected_metrics(candidates, rationalisation)
    pricing = model_client.pricing_snapshot().get(rationalisation_model, {})
    cost_usd, cost_method = usage_cost_usd(call.usage, pricing)
    raw["candidates"] = candidates
    corrected["rationalisation"] = rationalisation
    corrected["metrics"] = metrics
    corrected.setdefault("usage", {})["backfill_rationalisation"] = {
        "model": rationalisation_model,
        "usage": call.usage,
        "elapsed_seconds": round(call.elapsed_seconds, 4),
        "image_payload_bytes": 0,
        "model_reported_seconds": call.model_reported_seconds,
        "provider_metadata": call.provider_metadata,
        "cost_usd": cost_usd,
        "cost_method": cost_method,
    }
    corrected["backfill"] = {
        "kind": "numeric_string_page_number",
        "original_trace_id": original_trace_id,
        "created_at": utc_now(),
        "rationalisation_model": rationalisation_model,
        "candidate_count": len(candidates),
        "metric_count": len(metrics),
        "cost": {
            "usd": cost_usd,
            "gbp": round(cost_usd * gbp_per_usd, 8)
            if cost_usd is not None
            else None,
            "method": cost_method,
        },
    }
    return corrected


def start_mlflow_run(config: dict[str, Any]) -> str | None:
    """Start the evaluation run before document work so live traces survive interruptions."""
    settings = config.get("mlflow") or {}
    if not settings.get("enabled", False):
        return None
    try:
        import mlflow
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to enable MLflow logging") from error
    mlflow.set_tracking_uri(settings.get("tracking_uri", "http://127.0.0.1:5000"))
    mlflow.set_experiment(settings.get("experiment", "companies-house-vlm-financial-eval"))
    run = mlflow.start_run(run_name=settings.get("run_name"))
    safe_params = {
        key: value
        for key, value in config.items()
        if key not in {"fallback", "mlflow", "hardware"}
    }
    mlflow.log_params({key: str(value) for key, value in safe_params.items()})
    mlflow.log_param("git_revision", git_revision() or "unknown")
    return run.info.run_id


def finish_mlflow_run(
    config: dict[str, Any],
    report: dict[str, Any],
    output_dir: Path,
    cases_dir: Path,
    run_id: str | None,
) -> None:
    """Log aggregate artifacts and finish a run whose case traces were logged live."""
    if run_id is None:
        return
    import mlflow

    for key, value in report["aggregate"].items():
        if isinstance(value, (int, float)):
            mlflow.log_metric(key, float(value))
    for key, value in report["aggregate"].get("latency_seconds", {}).items():
        if value is not None:
            mlflow.log_metric(f"latency_{key}", float(value))
    mlflow.log_dict(report, "report.json")
    mlflow.log_artifacts(str(output_dir), artifact_path="evaluation")
    manifest, _created = log_saved_result_traces(
        output_dir,
        cases_dir,
        config,
        run_id=run_id,
        outcomes=report.get("outcomes") or [],
    )
    mlflow.log_metric("traces", float(len(manifest["traces"])))
    manifest_path = output_dir / "trace_manifest.json"
    if manifest_path.is_file():
        mlflow.log_artifact(str(manifest_path), artifact_path="evaluation")
    mlflow.end_run(status="FINISHED")


def mlflow_review_question_specs() -> list[dict[str, Any]]:
    """Return stable, human-readable questions used to create the gold labels."""
    questions = [
        {
            "name": "gold_statement_pages",
            "title": "Statement pages",
            "type": "expectation",
            "input": "text",
            "instruction": (
                "Enter every financial-statement page number, separated by commas "
                "(for example: 12, 13, 15)."
            ),
        },
    ]
    for metric in CANONICAL_METRICS:
        for period in PERIODS:
            period_title = "Current period" if period == "current" else "Previous period"
            questions.append(
                {
                    "name": f"gold_{period}_{metric}",
                    "title": f"{period_title}: {METRIC_TITLES[metric]}",
                    "type": "expectation",
                    "input": "text",
                    "instruction": (
                        "Enter: exact displayed value | source page | displayed unit. "
                        "Example: (1,234) | 12 | £000. For an explicit employee narrative zero, "
                        "enter NARRATIVE_ZERO | source page | count. Enter MISSING when the metric "
                        "is not disclosed for this period."
                    ),
                }
            )
    return questions


def _mlflow_review_schemas(experiment_id: str) -> list[Any]:
    from mlflow.genai.label_schemas import (
        InputText,
        create_label_schema,
        list_label_schemas,
    )

    existing = {schema.name: schema for schema in list_label_schemas(experiment_id=experiment_id)}
    schemas: list[Any] = []
    for question in mlflow_review_question_specs():
        schema_name = question["title"]
        schema = existing.get(schema_name)
        if schema is None:
            input_type = InputText(max_length=500)
            schema = create_label_schema(
                name=schema_name,
                type=question["type"],
                input=input_type,
                instruction=question["instruction"],
                enable_comment=False,
                experiment_id=experiment_id,
            )
        schemas.append(schema)
    return schemas


def _mlflow_review_queue(experiment_id: str, schemas: list[Any], queue_name: str) -> Any:
    from mlflow.genai.review_queues import (
        create_review_queue,
        list_review_queues,
        update_review_queue,
    )

    schema_ids = [schema.schema_id for schema in schemas]
    queue = next(
        (candidate for candidate in list_review_queues(experiment_id=experiment_id) if candidate.name == queue_name),
        None,
    )
    if queue is None:
        return create_review_queue(
            queue_name,
            queue_type="custom",
            schema_ids=schema_ids,
            experiment_id=experiment_id,
        )
    if queue.schema_ids != schema_ids:
        return update_review_queue(queue.queue_id, schema_ids=schema_ids)
    return queue


def _gold_label_preview(case: dict[str, Any]) -> str | None:
    """Summarise the reviewed gold labels, or None when the case is not reviewed yet."""
    if case.get("review", {}).get("status") != "verified":
        return None
    summaries = (case.get("expected") or {}).get("financial_period_summaries") or {}
    values = []
    for period in PERIODS:
        cells = summaries.get(period) or {}
        for metric in CANONICAL_METRICS:
            cell = cells.get(metric) or {}
            if cell.get("state") != "present":
                continue
            values.append(f"{period} {metric}={cell.get('displayed_value')}")
    if not values:
        return "gold: every metric missing"
    return "gold: " + "; ".join(values)


def _trace_response_preview(payload: dict[str, Any], case: dict[str, Any]) -> str:
    """Prefer the reviewed gold labels; fall back to what the model extracted.

    Trace previews are immutable once logged, so a trace created before its review
    keeps the model-extraction summary. The "model:" prefix keeps that readable
    rather than looking like the review itself came back empty.
    """
    gold = _gold_label_preview(case)
    if gold is not None:
        return gold
    metrics = payload.get("metrics") or []
    if not metrics:
        return f"model ({payload.get('status', 'unknown')}): no canonical metrics"
    values = []
    for metric in metrics:
        value = metric.get("value_count")
        if value is None:
            value = metric.get("value_pence")
        values.append(f"{metric.get('period_type')} {metric.get('metric_name')}={value}")
    return "model: " + "; ".join(values)


def log_saved_case_trace(
    case: dict[str, Any],
    payload: dict[str, Any],
    *,
    run_id: str | None,
) -> str:
    """Create one post-run MLflow trace with the source PDF attached."""
    try:
        import mlflow
        from mlflow.entities import SpanType
        from mlflow.tracing.attachments import Attachment
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to enable MLflow tracing") from error

    pdf_path = Path(payload.get("pdf_path") or resolve_pdf_path(case)).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF for trace does not exist: {pdf_path}")
    raw = payload.get("raw_extraction") or {}
    tags = {
        "eval.case_id": case["id"],
        "eval.company_number": case["company_number"],
        "eval.document_id": case["document_id"],
        "eval.split": case["split"],
        "eval.provider": str(payload.get("provider") or "unknown"),
        "eval.status": str(payload.get("status") or "unknown"),
    }
    company_context = payload.get("company_context") or company_context_from_case(case)
    sic_codes = company_context.get("sic_codes") or []
    if sic_codes:
        tags["eval.sic_codes"] = "; ".join(str(code) for code in sic_codes)
    if payload.get("review_seed"):
        tags["eval.review_seed"] = "true"
    backfill = payload.get("backfill") or {}
    if backfill:
        tags.update(
            {
                "eval.correction": str(backfill.get("kind") or "unknown"),
                "backfill.original_trace_id": str(
                    backfill.get("original_trace_id") or "unknown"
                ),
            }
        )
    with mlflow.start_span(
        name="financial_pdf_evaluation",
        span_type=SpanType.WORKFLOW,
        run_id=run_id,
    ) as root:
        mlflow.update_current_trace(
            tags=tags,
            request_preview=f"Review financial PDF for company {case['company_number']}",
            response_preview=_trace_response_preview(payload, case),
            model_id=(payload.get("models") or {}).get("vision"),
        )
        root.set_inputs(
            {
                "company_number": case["company_number"],
                "document_id": case["document_id"],
                "pdf_sha256": case["pdf_sha256"],
                "company_context": company_context,
                "source_pdf": Attachment.from_file(pdf_path, content_type="application/pdf"),
            }
        )
        with mlflow.start_span(name="statement_page_locator", span_type=SpanType.LLM) as span:
            span.set_inputs({"pages_scanned": payload.get("pages_scanned") or []})
            span.set_outputs(
                {
                    "candidate_pages": payload.get("candidate_pages") or [],
                    "employee_evidence_pages": payload.get("employee_evidence_pages") or [],
                    "locator_output": raw.get("locator") or {},
                    "response_reliability": (
                        (payload.get("usage") or {}).get("locator", {}).get("reliability") or {}
                    ),
                }
            )
            span.set_attribute(
                "measured_elapsed_seconds",
                (payload.get("usage") or {}).get("locator", {}).get("elapsed_seconds"),
            )
        with mlflow.start_span(name="financial_row_extraction", span_type=SpanType.LLM) as span:
            span.set_inputs({"candidate_pages": payload.get("candidate_pages") or []})
            span.set_outputs({
                "detail_output": raw.get("detail") or {},
                "employee_detail_output": raw.get("employee_detail") or {},
                "statement_page_coverage": raw.get("coverage") or {},
                "row_validation": raw.get("row_validation") or {},
                "response_reliability": (
                    (payload.get("usage") or {}).get("vision", {}).get("reliability") or {}
                ),
            })
            span.set_attribute(
                "measured_elapsed_seconds",
                (payload.get("usage") or {}).get("vision", {}).get("elapsed_seconds"),
            )
        with mlflow.start_span(name="canonical_rationalisation", span_type=SpanType.LLM) as span:
            span.set_inputs({
                "candidate_rows": raw.get("candidates") or [],
                "company_context_advisory_only": company_context,
            })
            span.set_outputs({
                "rationalisation": payload.get("rationalisation") or {},
                "resolved_rationalisation": payload.get("resolved_rationalisation") or {},
                "deterministic_diagnostics": payload.get("rationalisation_diagnostics") or {},
                "insurance_policy_diagnostics": payload.get("insurance_policy_diagnostics") or {},
                "response_reliability": (
                    (payload.get("usage") or {}).get("rationalisation", {}).get("reliability") or {}
                ),
            })
            span.set_attribute(
                "measured_elapsed_seconds",
                (payload.get("usage") or {}).get("rationalisation", {}).get("elapsed_seconds"),
            )
        root.set_outputs(
            {
                "status": payload.get("status"),
                "candidate_pages": payload.get("candidate_pages") or [],
                "employee_evidence_pages": payload.get("employee_evidence_pages") or [],
                "canonical_metrics": payload.get("metrics") or [],
                "financial_period_summaries": (
                    (payload.get("rationalisation") or {}).get("financial_period_summaries") or {}
                ),
                "elapsed_seconds": payload.get("elapsed_seconds"),
                "cost": payload.get("cost") or {},
                "error": payload.get("error"),
                "warnings": payload.get("warnings") or [],
                "company_context": company_context,
                "insurance_policy_diagnostics": payload.get("insurance_policy_diagnostics") or {},
            }
        )
        return root.trace_id


def _error_trace_payload(
    case: dict[str, Any],
    outcome: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build a traceable payload for a failed document from its saved outcome."""
    return {
        "pdf_path": str(resolve_pdf_path(case)),
        "provider": config.get("provider"),
        "models": {
            "locator": config.get("locator_model"),
            "vision": config.get("vision_model"),
            "rationalisation": config.get("rationalisation_model"),
        },
        "status": "error",
        "error": outcome.get("error") or "Benchmark failed before producing a result payload",
        "pages_scanned": [],
        "candidate_pages": [],
        "raw_extraction": {},
        "rationalisation": {},
        "metrics": [],
        "timing": outcome.get("timing") or {},
        "usage": outcome.get("usage") or {},
        "cost": outcome.get("cost") or {},
        "elapsed_seconds": outcome.get("elapsed_seconds"),
    }


def saved_result_records(
    results_dir: Path,
    cases_dir: Path,
    config: dict[str, Any],
    outcomes: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Load saved payloads and synthesise records for pre-payload failures."""
    records: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for result_path in sorted(results_dir.glob("*-attempt-1.json")):
        record = json.loads(result_path.read_text(encoding="utf-8"))
        case_id = record["score"]["case_id"]
        records[case_id] = (load_case(case_path(cases_dir, case_id)), record["payload"])
    for outcome in outcomes:
        case_id = outcome["case_id"]
        if case_id not in records:
            case = load_case(case_path(cases_dir, case_id))
            records[case_id] = (case, _error_trace_payload(case, outcome, config))
    return records


def saved_cell_comparison_rows(results_dir: Path, cases_dir: Path) -> list[dict[str, Any]]:
    """Re-score saved model outputs using the labels currently in the repository.

    This deliberately does not change the historical benchmark result.  It is a
    transparent, zero-model-cost comparison for when a reviewer corrects a gold
    label after a benchmark has completed.
    """
    rows: list[dict[str, Any]] = []
    for result_path in sorted(results_dir.glob("*-attempt-*.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        payload = result.get("payload")
        case_id = (result.get("score") or {}).get("case_id")
        if not isinstance(payload, dict) or not isinstance(case_id, str):
            continue
        case = load_case(case_path(cases_dir, case_id))
        for row in cell_comparison_rows(case, payload):
            rows.append({**row, "result_file": result_path.name})
    return rows


def report_saved_cell_errors(args: argparse.Namespace) -> int:
    """Create a field-level report for a completed benchmark and optionally upload it."""
    results_dir = Path(args.results_dir)
    cases_dir = Path(args.cases_dir)
    rows = saved_cell_comparison_rows(results_dir, cases_dir)
    report_dir = results_dir / "current-labels-cell-report"
    report = write_cell_comparison_reports(rows, report_dir, prefix="current-labels-cell")

    summary_path = results_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    run_id = args.run_id or summary.get("mlflow_run_id")
    if args.log_mlflow and run_id:
        import mlflow

        mlflow.set_tracking_uri(args.tracking_uri or "http://127.0.0.1:5000")
        with mlflow.start_run(run_id=run_id):
            mlflow.log_artifacts(str(report_dir), artifact_path="evaluation/current-labels-cell-report")
            core = report["core_financial"]
            employees = report["employees"]
            for name, value in {
                "current_labels_core_financial_expected_value_accuracy": core["expected_value_accuracy"],
                "current_labels_core_financial_errors": core["errors"],
                "current_labels_employees_expected_value_accuracy": employees["expected_value_accuracy"],
                "current_labels_employees_errors": employees["errors"],
            }.items():
                if value is not None:
                    mlflow.log_metric(name, float(value))
            mlflow.set_tag("current_labels_cell_report", "evaluation/current-labels-cell-report")
            mlflow.set_tag("current_labels_cell_report_generated_at", report["generated_at"])
    print(json.dumps({"report_dir": str(report_dir), "mlflow_run_id": run_id, **report}, indent=2))
    return 0


def log_saved_result_traces(
    results_dir: Path,
    cases_dir: Path,
    config: dict[str, Any],
    *,
    run_id: str | None,
    outcomes: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Create any missing per-document traces and persist an idempotency manifest."""
    import mlflow

    manifest_path = results_dir / "trace_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"run_id": run_id, "traces": {}}
    )
    if manifest.get("run_id") not in {None, run_id}:
        raise ValueError(
            f"{manifest_path} belongs to MLflow run {manifest['run_id']}, not {run_id}"
        )
    manifest["run_id"] = run_id
    created = 0
    for case_id, (case, payload) in saved_result_records(
        results_dir, cases_dir, config, outcomes
    ).items():
        if case_id in manifest["traces"]:
            continue
        trace_id = log_saved_case_trace(case, payload, run_id=run_id)
        manifest["traces"][case_id] = trace_id
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        created += 1
        print(json.dumps({"case_id": case_id, "trace_id": trace_id}), file=sys.stderr)
    mlflow.flush_trace_async_logging()
    return manifest, created


def log_live_result_trace(
    results_dir: Path,
    case: dict[str, Any],
    payload: dict[str, Any],
    *,
    run_id: str,
) -> str:
    """Persist one completed case trace immediately and exactly once."""
    import mlflow

    manifest_path = results_dir / "trace_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"run_id": run_id, "traces": {}}
    )
    if manifest.get("run_id") not in {None, run_id}:
        raise ValueError(
            f"{manifest_path} belongs to MLflow run {manifest['run_id']}, not {run_id}"
        )
    manifest["run_id"] = run_id
    existing_trace_id = manifest["traces"].get(case["id"])
    if existing_trace_id:
        return existing_trace_id
    trace_id = log_saved_case_trace(case, payload, run_id=run_id)
    manifest["traces"][case["id"]] = trace_id
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    mlflow.flush_trace_async_logging()
    print(json.dumps({"case_id": case["id"], "trace_id": trace_id}), file=sys.stderr)
    return trace_id


def import_saved_results_as_traces(args: argparse.Namespace) -> int:
    """Attach existing benchmark results and PDFs to an MLflow review queue."""
    try:
        import mlflow
        from mlflow.genai.review_queues import add_items_to_review_queue
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to enable MLflow tracing") from error

    config = configuration_from_file(Path(args.config))
    settings = config.get("mlflow") or {}
    mlflow.set_tracking_uri(args.tracking_uri or settings.get("tracking_uri", "http://127.0.0.1:5000"))
    experiment = mlflow.set_experiment(
        args.experiment or settings.get("experiment", "companies-house-vlm-financial-eval")
    )
    results_dir = Path(args.results_dir)
    cases_dir = Path(args.cases_dir)
    summary_path = results_dir / "summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    run_id = args.run_id or summary.get("mlflow_run_id")
    if run_id is None:
        raise ValueError(
            "No MLflow run ID was found in summary.json; pass --run-id for an interrupted run"
        )
    outcomes = list(summary.get("outcomes") or [])
    if args.include_missing_cases:
        saved_case_ids = {
            json.loads(result_path.read_text(encoding="utf-8"))["score"]["case_id"]
            for result_path in results_dir.glob("*-attempt-1.json")
        }
        outcomes.extend(
            {
                "case_id": case["id"],
                "status": "error",
                "error": "Interrupted run did not save a per-document result",
            }
            for case in load_verified_cases(cases_dir, include_unreviewed=True)
            if case["id"] not in saved_case_ids
        )
    manifest, created = log_saved_result_traces(
        results_dir,
        cases_dir,
        config,
        run_id=run_id,
        outcomes=outcomes,
    )

    schemas = _mlflow_review_schemas(experiment.experiment_id)
    queue = _mlflow_review_queue(experiment.experiment_id, schemas, args.queue_name)
    trace_ids = list(manifest["traces"].values())
    add_items_to_review_queue(queue.queue_id, item_ids=trace_ids)
    print(
        json.dumps(
            {
                "created_traces": created,
                "total_traces": len(trace_ids),
                "queue_name": queue.name,
                "queue_id": queue.queue_id,
                "experiment_id": experiment.experiment_id,
            },
            indent=2,
        )
    )
    return 0


def review_seed_payload(case: dict[str, Any]) -> dict[str, Any]:
    """Create a PDF-only trace payload for manual gold-label review."""
    return {
        "pdf_path": str(resolve_pdf_path(case)),
        "provider": "manual-review",
        "models": {},
        "status": "review_seed",
        "pages_scanned": [],
        "candidate_pages": [],
        "raw_extraction": {},
        "rationalisation": {},
        "metrics": [],
        "timing": {},
        "usage": {},
        "cost": {},
        "elapsed_seconds": None,
        "review_seed": True,
    }


def _latest_case_traces(experiment_id: str, case_ids: set[str]) -> dict[str, tuple[str, bool]]:
    """Return the preferred trace for each case, preserving human-reviewed traces."""
    from mlflow import MlflowClient

    client = MlflowClient()
    latest: dict[str, tuple[bool, int, str]] = {}
    page_token: str | None = None
    while True:
        traces = client.search_traces(
            experiment_ids=[experiment_id],
            max_results=100,
            page_token=page_token,
            include_spans=False,
        )
        for trace in traces:
            case_id = trace.info.tags.get("eval.case_id")
            if case_id not in case_ids:
                continue
            has_human_answers = bool(trace.info.assessments)
            candidate = (has_human_answers, trace.info.timestamp_ms, trace.info.trace_id)
            if case_id not in latest or candidate > latest[case_id]:
                latest[case_id] = candidate
        page_token = traces.token
        if not page_token:
            break
    return {
        case_id: (trace_id, has_human_answers)
        for case_id, (has_human_answers, _, trace_id) in latest.items()
    }


def sync_mlflow_review_queue(args: argparse.Namespace) -> int:
    """Make a review queue contain exactly one current trace for every case file."""
    try:
        import mlflow
        from mlflow.genai.review_queues import (
            add_items_to_review_queue,
            list_review_queue_items,
            list_review_queues,
            remove_items_from_review_queue,
            set_review_queue_item_status,
        )
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to synchronise MLflow reviews") from error

    config = configuration_from_file(Path(args.config))
    settings = config.get("mlflow") or {}
    mlflow.set_tracking_uri(args.tracking_uri or settings.get("tracking_uri", "http://127.0.0.1:5000"))
    experiment = mlflow.set_experiment(
        args.experiment or settings.get("experiment", "companies-house-vlm-financial-eval")
    )
    cases = load_verified_cases(Path(args.cases_dir), include_unreviewed=True)
    case_by_id = {case["id"]: case for case in cases}
    trace_by_case = _latest_case_traces(experiment.experiment_id, set(case_by_id))
    seeded_case_ids: list[str] = []
    for case_id, case in case_by_id.items():
        if case_id not in trace_by_case:
            trace_by_case[case_id] = (
                log_saved_case_trace(
                    case,
                    review_seed_payload(case),
                    run_id=None,
                ),
                False,
            )
            seeded_case_ids.append(case_id)
    mlflow.flush_trace_async_logging()

    schemas = _mlflow_review_schemas(experiment.experiment_id)
    existing_queue = next(
        (
            candidate
            for candidate in list_review_queues(experiment_id=experiment.experiment_id)
            if candidate.name == args.queue_name
        ),
        None,
    )
    schema_ids = [schema.schema_id for schema in schemas]
    if existing_queue is not None and existing_queue.schema_ids != schema_ids:
        assigned_items = list(list_review_queue_items(existing_queue.queue_id, max_results=1000))
        if assigned_items:
            remove_items_from_review_queue(
                existing_queue.queue_id,
                item_ids=[item.item_id for item in assigned_items],
            )
    queue = _mlflow_review_queue(experiment.experiment_id, schemas, args.queue_name)
    wanted_trace_ids = {trace_id for trace_id, _ in trace_by_case.values()}
    add_items_to_review_queue(queue.queue_id, item_ids=sorted(wanted_trace_ids))
    existing_items = list(list_review_queue_items(queue.queue_id, max_results=1000))
    stale_item_ids = [item.item_id for item in existing_items if item.item_id not in wanted_trace_ids]
    if stale_item_ids:
        remove_items_from_review_queue(queue.queue_id, item_ids=stale_item_ids)
    for trace_id, has_human_answers in trace_by_case.values():
        if has_human_answers:
            set_review_queue_item_status(
                queue.queue_id,
                item_id=trace_id,
                status="complete",
                completed_by="default",
            )
    final_items = list(list_review_queue_items(queue.queue_id, max_results=1000))
    if {item.item_id for item in final_items} != wanted_trace_ids:
        raise RuntimeError("MLflow review queue did not match the current evaluation cases")
    print(
        json.dumps(
            {
                "queue_name": queue.name,
                "queue_id": queue.queue_id,
                "cases": len(case_by_id),
                "reused_traces": len(case_by_id) - len(seeded_case_ids),
                "seeded_traces": len(seeded_case_ids),
                "removed_queue_items": len(stale_item_ids),
            },
            indent=2,
        )
    )
    return 0


def backfill_page_number_traces(args: argparse.Namespace) -> int:
    """Create replacement traces for rows discarded by string page numbers."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path.cwd() / ".env")
    try:
        import mlflow
        from mlflow import MlflowClient
        from mlflow.genai.review_queues import (
            add_items_to_review_queue,
            get_review_queue,
        )
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to run the backfill") from error

    config = configuration_from_file(Path(args.config))
    settings = config.get("mlflow") or {}
    if not settings.get("enabled", False):
        raise ValueError("the backfill config must enable MLflow")
    settings["run_name"] = args.run_name
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "backfill_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"run_id": None, "corrections": {}, "errors": {}}
    )
    manifest.setdefault("errors", {})
    mlflow.set_tracking_uri(
        args.tracking_uri
        or settings.get("tracking_uri", "http://127.0.0.1:5000")
    )
    experiment = mlflow.set_experiment(
        args.experiment
        or settings.get("experiment", "companies-house-vlm-financial-eval")
    )
    if manifest.get("run_id"):
        run = mlflow.start_run(run_id=manifest["run_id"])
        run_id = run.info.run_id
    else:
        run_id = start_mlflow_run(config)
        if run_id is None:
            raise RuntimeError("MLflow did not start a correction run")
        manifest["run_id"] = run_id
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    cases_dir = Path(args.cases_dir)
    records: list[tuple[Path, str, dict[str, Any], dict[str, Any]]] = []
    for source_dir_text in args.source_results_dir:
        source_dir = Path(source_dir_text)
        source_manifest_path = source_dir / "trace_manifest.json"
        if not source_manifest_path.is_file():
            raise FileNotFoundError(f"Missing source trace manifest: {source_manifest_path}")
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        for case_id, original_trace_id in source_manifest.get("traces", {}).items():
            result_path = source_dir / f"{case_id}-attempt-1.json"
            if not result_path.is_file():
                continue
            record = json.loads(result_path.read_text(encoding="utf-8"))
            payload = record.get("payload") or {}
            if needs_page_number_backfill(payload):
                records.append((result_path, original_trace_id, record, payload))

    model_client = build_client(config)
    tracking_client = MlflowClient()
    replacements: list[str] = []
    created = 0
    try:
        for result_path, original_trace_id, record, payload in records:
            existing = manifest["corrections"].get(original_trace_id)
            if existing:
                replacements.append(existing["replacement_trace_id"])
                continue
            case_id = record["score"]["case_id"]
            case = load_case(case_path(cases_dir, case_id))
            corrected: dict[str, Any] | None = None
            last_error: Exception | None = None
            for attempt in range(1, args.max_attempts + 1):
                try:
                    corrected = backfill_page_number_payload(
                        payload,
                        model_client,
                        rationalisation_model=config["rationalisation_model"],
                        timeout=int(config.get("timeout_seconds", 180)),
                        gbp_per_usd=float(config.get("gbp_per_usd", 0.75)),
                        original_trace_id=original_trace_id,
                    )
                    break
                except Exception as error:
                    last_error = error
                    print(
                        json.dumps(
                            {
                                "original_trace_id": original_trace_id,
                                "attempt": attempt,
                                "error": str(error),
                            }
                        ),
                        file=sys.stderr,
                    )
            if corrected is None:
                manifest["errors"][original_trace_id] = {
                    "case_id": case_id,
                    "source_result": str(result_path),
                    "error": str(last_error),
                    "attempts": args.max_attempts,
                }
                manifest_path.write_text(
                    json.dumps(manifest, indent=2),
                    encoding="utf-8",
                )
                continue
            manifest["errors"].pop(original_trace_id, None)
            if case.get("review", {}).get("status") == "verified":
                score = score_payload(case, corrected)
            else:
                score = copy.deepcopy(record.get("score") or {})
                score.update(
                    {
                        "status": corrected.get("status"),
                        "unscored": True,
                        "elapsed_seconds": corrected.get("elapsed_seconds"),
                    }
                )
            corrected_path = output_dir / (
                f"{case_id}-replacement-for-{original_trace_id}.json"
            )
            corrected_path.write_text(
                json.dumps({"payload": corrected, "score": score}, indent=2),
                encoding="utf-8",
            )
            replacement_trace_id = log_saved_case_trace(
                case,
                corrected,
                run_id=run_id,
            )
            tracking_client.set_trace_tag(
                original_trace_id,
                "eval.correction_status",
                "superseded",
            )
            tracking_client.set_trace_tag(
                original_trace_id,
                "backfill.replacement_trace_id",
                replacement_trace_id,
            )
            tracking_client.set_trace_tag(
                replacement_trace_id,
                "eval.correction_status",
                "replacement",
            )
            manifest["corrections"][original_trace_id] = {
                "replacement_trace_id": replacement_trace_id,
                "case_id": case_id,
                "source_result": str(result_path),
                "corrected_result": str(corrected_path),
                "candidate_count": corrected["backfill"]["candidate_count"],
                "metric_count": corrected["backfill"]["metric_count"],
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2),
                encoding="utf-8",
            )
            replacements.append(replacement_trace_id)
            created += 1
            print(
                json.dumps(
                    {
                        "original_trace_id": original_trace_id,
                        "replacement_trace_id": replacement_trace_id,
                        "metrics": len(corrected.get("metrics") or []),
                    }
                ),
                file=sys.stderr,
            )
        mlflow.flush_trace_async_logging()
        mlflow.log_metric("corrections", float(len(manifest["corrections"])))
        mlflow.log_artifacts(str(output_dir), artifact_path="backfill")
        if replacements:
            queue = get_review_queue(
                name=args.queue_name,
                experiment_id=experiment.experiment_id,
            )
            add_items_to_review_queue(queue.queue_id, item_ids=replacements)
        mlflow.end_run(status="FINISHED")
    except BaseException:
        mlflow.end_run(status="KILLED")
        raise
    print(
        json.dumps(
            {
                "run_id": run_id,
                "affected": len(records),
                "created": created,
                "replacements": len(manifest["corrections"]),
                "errors": len(manifest["errors"]),
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )
    return 1 if manifest["errors"] else 0


def parse_reviewed_metric(value: str, metric: str) -> dict[str, Any]:
    """Parse the documented MLflow review answer into one gold-label cell."""
    text = value.strip()
    if text.upper() == "MISSING":
        return {
            "state": "missing",
            "value_pence": None,
            "value_count": None,
            "displayed_value": None,
            "unit": None,
            "source_page": None,
            "source_label": None,
        }
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 3:
        raise ValueError("expected: displayed value | source page | displayed unit")
    displayed_value, page_text, unit_text = parts
    try:
        source_page = int(page_text)
    except ValueError as error:
        raise ValueError("source page must be an integer") from error
    if source_page < 1:
        raise ValueError("source page must be positive")
    if metric == "employees" and displayed_value.upper() == "NARRATIVE_ZERO":
        if unit_text.lower() not in {"count", "counts"}:
            raise ValueError("employee narrative zero must use count as its displayed unit")
        return {
            "state": "present",
            "value_pence": None,
            "value_count": 0,
            "displayed_value": None,
            "unit": "COUNT",
            "source_page": source_page,
            "source_label": None,
            "evidence_kind": "narrative_zero",
        }
    if metric == "employees":
        unit = "COUNT"
    else:
            review_unit = (
                unit_text.upper()
            .replace("£'000", "GBP_THOUSANDS")
            .replace("£000", "GBP_THOUSANDS")
            .replace("£M", "GBP_MILLIONS")
            .replace("£", "GBP")
            .replace("$'000", "USD_THOUSANDS")
            .replace("$000", "USD_THOUSANDS")
            .replace("$M", "USD_MILLIONS")
                .replace("$", "USD")
            )
            review_unit = review_unit.replace("\u00a3", "GBP").replace("\u0141", "GBP")
            unit = normalise_unit(review_unit)
    if metric != "employees" and unit == "UNKNOWN":
        raise ValueError(f"unsupported displayed unit: {unit_text}")
    value_count = to_count(displayed_value) if metric == "employees" else None
    value_pence = None if metric == "employees" else to_pence(displayed_value, unit, metric)
    reported = None if metric == "employees" else reported_value(displayed_value, unit, metric)
    if value_count is None and reported is None:
        raise ValueError("displayed value is not numeric")
    result = {
        "state": "present",
        "value_pence": value_pence,
        "value_count": value_count,
        "displayed_value": displayed_value,
        "unit": unit,
        "source_page": source_page,
        "source_label": None,
    }
    if metric == "employees" and re.fullmatch(r"[-\u2013\u2014]+", displayed_value):
        result["evidence_kind"] = "dash_zero"
    # Keep historical GBP gold-label JSON byte-for-byte compatible while
    # retaining the new authoritative value for non-sterling reviews.
    if metric != "employees" and currency_and_scale(unit)[0] != "GBP":
        result.update({"currency_code": currency_and_scale(unit)[0], "scale_multiplier": currency_and_scale(unit)[1], "reported_value": str(reported)})
    return result


def parse_reviewed_statement_pages(value: str) -> list[int]:
    """Parse the single statement-page Review answer and validate it early."""
    try:
        pages = sorted({int(page.strip()) for page in value.split(",") if page.strip()})
    except ValueError as error:
        raise ValueError("statement pages must be comma-separated integers") from error
    if not pages or pages[0] < 1:
        raise ValueError("statement pages must contain positive integers")
    return pages


def latest_assessments_by_name(assessments: list[Any]) -> dict[str, Any]:
    """Keep the newest answer when MLflow retains an assessment edit history."""
    latest: dict[str, Any] = {}
    for assessment in assessments:
        name = getattr(assessment, "name", None)
        if not isinstance(name, str):
            continue
        timestamp = getattr(assessment, "last_update_time_ms", 0) or 0
        previous = latest.get(name)
        if previous is None or timestamp >= (getattr(previous, "last_update_time_ms", 0) or 0):
            latest[name] = assessment
    return latest


def review_answers_to_case(
    *,
    case_id: str,
    company_number: str,
    document_id: str,
    pdf_sha256: str,
    split: str,
    trace_id: str,
    answers: dict[str, Any],
    reviewer: str | None,
    reviewed_at: str | None,
) -> dict[str, Any]:
    """Build a portable dataset case from one completed MLflow Review trace."""
    expected_names = {
        question["name"] for question in mlflow_review_question_specs()
        if question["type"] == "expectation"
    }
    missing = sorted(expected_names - answers.keys())
    if missing:
        raise ValueError(f"missing answers {', '.join(missing)}")
    statement_pages = parse_reviewed_statement_pages(str(answers["gold_statement_pages"].value))
    summaries = canonical_empty_expectations()
    for period in PERIODS:
        for metric in CANONICAL_METRICS:
            summaries[period][metric] = parse_reviewed_metric(
                str(answers[f"gold_{period}_{metric}"].value), metric
            )
    case = {
        "schema_version": CASE_SCHEMA_VERSION,
        "id": case_id,
        "company_number": company_number,
        "document_id": document_id,
        # This is deliberately a trace reference, not a path to a PDF copied into MLflow.
        "pdf_path": f"mlflow://traces/{trace_id}",
        "pdf_sha256": pdf_sha256,
        "split": split,
        "metadata": {"label_source": "mlflow_review", "review_trace_id": trace_id},
        "expected": {
            "statement_pages": statement_pages,
            "financial_period_summaries": summaries,
        },
        "review": {
            "status": "verified",
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "notes": None,
        },
    }
    errors = validate_case(case, require_complete=True)
    if errors:
        raise ValueError("; ".join(errors))
    return case


def completed_review_cases(mlflow: Any, queue: Any) -> list[dict[str, Any]]:
    """Read one latest, complete, valid label set per case from an MLflow queue."""
    from mlflow.genai.review_queues import list_review_queue_items

    question_by_title = {
        question["title"]: question["name"] for question in mlflow_review_question_specs()
    }
    latest_item_by_case: dict[str, Any] = {}
    for item in list_review_queue_items(queue.queue_id, status="complete", max_results=1000):
        trace = mlflow.get_trace(item.item_id)
        case_id = trace.info.tags.get("eval.case_id")
        if not case_id:
            continue
        previous = latest_item_by_case.get(case_id)
        item_time = getattr(item, "completed_time_ms", None) or getattr(item, "last_update_time_ms", 0) or 0
        previous_time = (
            (getattr(previous[0], "completed_time_ms", None) or getattr(previous[0], "last_update_time_ms", 0) or 0)
            if previous else -1
        )
        if item_time >= previous_time:
            latest_item_by_case[case_id] = (item, trace)

    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    for case_id, (item, trace) in sorted(latest_item_by_case.items()):
        tags = trace.info.tags
        # MLflow may abbreviate the JSON trace-input metadata once the PDF attachment
        # is present.  The root span retains the typed inputs in full, so prefer it.
        inputs = next(
            (
                span.inputs for span in trace.data.spans
                if isinstance(getattr(span, "inputs", None), dict)
                and "pdf_sha256" in span.inputs
            ),
            None,
        )
        if inputs is None:
            inputs = json.loads(trace.info.trace_metadata.get("mlflow.traceInputs", "{}"))
        assessments = latest_assessments_by_name(list(trace.info.assessments))
        answers = {
            question_by_title[title]: assessment
            for title, assessment in assessments.items()
            if title in question_by_title
        }
        try:
            cases.append(review_answers_to_case(
                case_id=case_id,
                company_number=str(tags["eval.company_number"]),
                document_id=str(tags["eval.document_id"]),
                pdf_sha256=str(inputs["pdf_sha256"]),
                split=str(tags.get("eval.split", "development")),
                trace_id=trace.info.trace_id,
                answers=answers,
                reviewer=getattr(item, "completed_by", None),
                reviewed_at=datetime.fromtimestamp(
                    (getattr(item, "completed_time_ms", 0) or 0) / 1000, UTC
                ).replace(microsecond=0).isoformat() if getattr(item, "completed_time_ms", None) else None,
            ))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{case_id}: {error}")
    if errors:
        raise ValueError("Completed MLflow Review labels are not publishable:\n" + "\n".join(errors))
    if not cases:
        raise ValueError("No completed MLflow Review items with gold-label answers were found")
    return cases


def export_mlflow_reviews(args: argparse.Namespace) -> int:
    """Write completed MLflow gold-label answers back to repository case JSON."""
    try:
        import mlflow
        from mlflow.genai.review_queues import get_review_queue, list_review_queue_items
    except ImportError as error:
        raise RuntimeError("Install requirements-eval.txt to export MLflow reviews") from error

    config = configuration_from_file(Path(args.config))
    settings = config.get("mlflow") or {}
    mlflow.set_tracking_uri(args.tracking_uri or settings.get("tracking_uri", "http://127.0.0.1:5000"))
    experiment = mlflow.set_experiment(
        args.experiment or settings.get("experiment", "companies-house-vlm-financial-eval")
    )
    queue = get_review_queue(name=args.queue_name, experiment_id=experiment.experiment_id)
    completed = list_review_queue_items(queue.queue_id, status="complete", max_results=1000)
    manifest_path = Path(args.results_dir) / "trace_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"traces": {}}
    )
    case_by_trace = {
        trace_id: case_id for case_id, trace_id in manifest.get("traces", {}).items()
    }
    question_by_title = {
        question["title"]: question["name"] for question in mlflow_review_question_specs()
    }
    exported = 0
    errors: list[str] = []
    for item in completed:
        trace = mlflow.get_trace(item.item_id)
        case_id = case_by_trace.get(item.item_id) or trace.info.tags.get("eval.case_id")
        if case_id is None:
            continue
        if not case_path(Path(args.cases_dir), case_id).is_file():
            continue
        answers = {
            question_by_title[assessment.name]: assessment
            for assessment in trace.info.assessments
            if assessment.name in question_by_title
        }
        expected_names = {
            question["name"]
            for question in mlflow_review_question_specs()
            if question["type"] == "expectation"
        }
        if not expected_names.issubset(answers):
            missing = sorted(expected_names - answers.keys())
            errors.append(f"{case_id}: missing answers {', '.join(missing)}")
            continue
        try:
            statement_pages = sorted(
                {
                    int(page.strip())
                    for page in str(answers["gold_statement_pages"].value).split(",")
                    if page.strip()
                }
            )
            if not statement_pages or statement_pages[0] < 1:
                raise ValueError("statement pages must contain positive integers")
            summaries = canonical_empty_expectations()
            for period in PERIODS:
                for metric in CANONICAL_METRICS:
                    answer = answers[f"gold_{period}_{metric}"]
                    summaries[period][metric] = parse_reviewed_metric(str(answer.value), metric)
        except ValueError as error:
            errors.append(f"{case_id}: {error}")
            continue
        case_file = case_path(Path(args.cases_dir), case_id)
        case = load_case(case_file)
        case["expected"] = {
            "statement_pages": statement_pages,
            "financial_period_summaries": summaries,
        }
        source = next(iter(answers.values())).source
        case["review"] = {
            "status": "verified",
            "reviewer": getattr(source, "source_id", None),
            "reviewed_at": utc_now(),
            "notes": None,
        }
        validation_errors = validate_case(case, require_complete=True)
        if validation_errors:
            errors.append(f"{case_id}: {'; '.join(validation_errors)}")
            continue
        save_case(case_file, case)
        exported += 1
    print(json.dumps({"completed": len(completed), "exported": exported, "errors": errors}, indent=2))
    return 1 if errors else 0


def create_mlflow_dataset(args: argparse.Namespace) -> int:
    """Freeze completed MLflow Review labels as one immutable MLflow dataset."""
    try:
        import mlflow
        from mlflow.genai.datasets import create_dataset, search_datasets
        from mlflow.genai.review_queues import get_review_queue
    except ImportError as error:
        raise SystemExit("MLflow GenAI dataset support is required; install requirements-eval.txt") from error

    config = configuration_from_file(Path(args.config))
    settings = config.get("mlflow") or {}
    mlflow.set_tracking_uri(args.tracking_uri or settings.get("tracking_uri", "http://127.0.0.1:5000"))
    experiment = mlflow.set_experiment(
        args.experiment or settings.get("experiment", "companies-house-vlm-financial-eval")
    )
    queue = get_review_queue(name=args.queue_name, experiment_id=experiment.experiment_id)
    cases = completed_review_cases(mlflow, queue)
    records = mlflow_dataset_records(cases)
    digest = mlflow_dataset_digest(records)
    name = args.dataset_name
    escaped_name = name.replace("'", "''")
    matches = search_datasets(
        experiment_ids=experiment.experiment_id,
        filter_string=f"name = '{escaped_name}'",
        max_results=2,
    )
    if len(matches) > 1:
        raise SystemExit(f"More than one MLflow dataset is named {name!r}; choose a new --dataset-name.")
    if matches:
        dataset = matches[0]
        existing_digest = (dataset.tags or {}).get("gold_label_sha256")
        if existing_digest != digest:
            raise SystemExit(
                f"MLflow dataset {name!r} already exists with different labels. "
                "Keep it as an immutable snapshot and choose a new --dataset-name."
            )
        created = False
    else:
        dataset = create_dataset(
            name=name,
            experiment_id=experiment.experiment_id,
            tags={
                "gold_label_sha256": digest,
                "record_count": str(len(records)),
                "source": "mlflow-review-queue",
                "review_queue": args.queue_name,
                "pdf_storage": "local; referenced by sha256 only",
            },
        )
        dataset.merge_records(records)
        created = True
    print(json.dumps({
        "created": created,
        "dataset_id": dataset.dataset_id,
        "dataset_name": dataset.name,
        "records": len(records),
        "gold_label_sha256": digest,
        "experiment_id": experiment.experiment_id,
        "review_queue": args.queue_name,
    }, indent=2))
    return 0


def run_evaluation(args: argparse.Namespace) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path.cwd() / ".env")
    config = configuration_from_file(Path(args.config))
    if args.no_mlflow:
        config["mlflow"] = {"enabled": False}
    elif args.run_name:
        config.setdefault("mlflow", {})["run_name"] = args.run_name
    cases = load_verified_cases(Path(args.cases_dir), args.include_unreviewed)
    if args.company_numbers:
        requested_companies = {
            company_number.strip().upper()
            for company_number in args.company_numbers.split(",")
            if company_number.strip()
        }
        cases = [
            case
            for case in cases
            if str(case["company_number"]).strip().upper() in requested_companies
        ]
        found_companies = {str(case["company_number"]).strip().upper() for case in cases}
        missing_companies = requested_companies - found_companies
        if missing_companies:
            raise RuntimeError(
                "No verified case found for company number(s): "
                + ", ".join(sorted(missing_companies))
            )
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
    mlflow_run_id = start_mlflow_run(config)

    def execute(
        case: dict[str, Any], attempt: int
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        case_started = time.perf_counter()
        try:
            payload, score = run_case(case, config)
            name = f"{case['id']}-attempt-{attempt}.json"
            (output_dir / name).write_text(json.dumps({"payload": payload, "score": score}, indent=2), encoding="utf-8")
            return case, payload, score
        except Exception as error:
            elapsed_seconds = round(time.perf_counter() - case_started, 4)
            score = {
                "case_id": case["id"],
                "split": case["split"],
                "metadata": case["metadata"],
                "status": "error",
                "error": str(error),
                "elapsed_seconds": elapsed_seconds,
                "timing": {},
                "usage": {},
                "cost": {},
                "counts": defaultdict(int),
                "page": {"precision": 0.0, "recall": 0.0, "f1": 0.0},
                "whole_document_exact": False,
            }
            payload = _error_trace_payload(case, score, config)
            name = f"{case['id']}-attempt-{attempt}.json"
            (output_dir / name).write_text(
                json.dumps({"payload": payload, "score": score}, indent=2),
                encoding="utf-8",
            )
            return case, payload, score

    try:
        jobs = [(case, attempt) for attempt in range(1, args.repeats + 1) for case in cases]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.concurrency or int(config.get("concurrency", 1))
        ) as executor:
            for case, payload, outcome in executor.map(lambda job: execute(*job), jobs):
                outcomes.append(outcome)
                if mlflow_run_id is not None:
                    log_live_result_trace(
                        output_dir,
                        case,
                        payload,
                        run_id=mlflow_run_id,
                    )
                print(
                    json.dumps(
                        {"case_id": outcome["case_id"], "status": outcome["status"]}
                    ),
                    file=sys.stderr,
                )
        report = {
            "created_at": utc_now(),
            "config": {
                key: value
                for key, value in config.items()
                if key not in {"fallback", "mlflow", "hardware"}
            },
            "git_revision": git_revision(),
            "dataset_cases": len(cases),
            "repeats": args.repeats,
            "batch_elapsed_seconds": round(time.perf_counter() - started, 4),
            "aggregate": aggregate_scores(outcomes, config.get("hardware")),
            "outcomes": outcomes,
            "mlflow_run_id": mlflow_run_id,
        }
        finish_mlflow_run(
            config,
            report,
            output_dir,
            Path(args.cases_dir),
            mlflow_run_id,
        )
        (output_dir / "summary.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "output_dir": str(output_dir),
                    "aggregate": report["aggregate"],
                    "mlflow_run_id": report["mlflow_run_id"],
                },
                indent=2,
            )
        )
        return 0 if not report["aggregate"]["errors"] else 1
    except BaseException:
        if mlflow_run_id is not None:
            import mlflow

            mlflow.end_run(status="KILLED")
        raise


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
    run.add_argument(
        "--company-numbers",
        help="Comma-separated verified Companies House numbers to evaluate.",
    )
    run.add_argument("--include-unreviewed", action="store_true")
    run.add_argument("--no-mlflow", action="store_true", help="Save JSON artifacts without starting MLflow.")
    run.add_argument(
        "--run-name",
        help="Override the MLflow run name without changing the provider configuration.",
    )
    traces = commands.add_parser(
        "import-traces",
        help="Import saved benchmark results and source PDFs into an MLflow review queue.",
    )
    traces.add_argument("--config", required=True)
    traces.add_argument("--results-dir", required=True)
    traces.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    traces.add_argument("--tracking-uri")
    traces.add_argument("--experiment")
    traces.add_argument("--run-id", help="Attach traces to this run when summary.json is absent.")
    traces.add_argument(
        "--include-missing-cases",
        action="store_true",
        help="Create error traces for case files absent from an interrupted full-dataset run.",
    )
    traces.add_argument("--queue-name", default=MLFLOW_REVIEW_QUEUE_NAME)
    sync_review = commands.add_parser(
        "sync-review-queue",
        help="Make an MLflow review queue contain exactly the current evaluation cases.",
    )
    sync_review.add_argument("--config", required=True)
    sync_review.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    sync_review.add_argument("--tracking-uri")
    sync_review.add_argument("--experiment")
    sync_review.add_argument("--queue-name", default=MLFLOW_REVIEW_QUEUE_NAME)
    backfill = commands.add_parser(
        "backfill-page-numbers",
        help="Create corrected traces from saved rows whose pages were numeric strings.",
    )
    backfill.add_argument("--config", required=True)
    backfill.add_argument(
        "--source-results-dir",
        action="append",
        required=True,
        help="Results directory to audit; repeat for multiple source runs.",
    )
    backfill.add_argument("--output-dir", required=True)
    backfill.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    backfill.add_argument("--tracking-uri")
    backfill.add_argument("--experiment")
    backfill.add_argument("--run-name", default="numeric-string-page-number-backfill")
    backfill.add_argument("--queue-name", default=MLFLOW_REVIEW_QUEUE_NAME)
    backfill.add_argument("--max-attempts", type=int, default=3)
    export = commands.add_parser(
        "export-reviews",
        help="Write completed MLflow review answers back to the gold-label case JSON.",
    )
    export.add_argument("--config", required=True)
    export.add_argument("--results-dir", required=True)
    export.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    export.add_argument("--tracking-uri")
    export.add_argument("--experiment")
    export.add_argument("--queue-name", default=MLFLOW_REVIEW_QUEUE_NAME)
    dataset = commands.add_parser(
        "create-mlflow-dataset",
        help="Freeze completed MLflow Review labels as an immutable MLflow evaluation dataset.",
    )
    dataset.add_argument("--config", required=True)
    dataset.add_argument("--tracking-uri")
    dataset.add_argument("--experiment")
    dataset.add_argument("--queue-name", default=MLFLOW_REVIEW_QUEUE_NAME)
    dataset.add_argument("--dataset-name", default=DEFAULT_MLFLOW_DATASET_NAME)
    cell_report = commands.add_parser(
        "report-cell-errors",
        help="Create a downloadable per-cell comparison report for saved benchmark results.",
    )
    cell_report.add_argument("--results-dir", required=True)
    cell_report.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    cell_report.add_argument("--run-id", help="MLflow run to receive the report artifact.")
    cell_report.add_argument("--tracking-uri")
    cell_report.add_argument(
        "--log-mlflow",
        action="store_true",
        help="Upload the report to the specified or saved MLflow run.",
    )
    args = parser.parse_args(argv)
    if args.command == "initialise":
        if args.count < 1:
            parser.error("--count must be positive")
        cases = select_cases(Path(args.db), Path(args.cases_dir), args.count)
        print(json.dumps({"created": len(cases), "cases_dir": args.cases_dir}, indent=2))
        return 0
    if args.command == "import-traces":
        return import_saved_results_as_traces(args)
    if args.command == "sync-review-queue":
        return sync_mlflow_review_queue(args)
    if args.command == "backfill-page-numbers":
        if args.max_attempts < 1:
            parser.error("--max-attempts must be positive")
        return backfill_page_number_traces(args)
    if args.command == "export-reviews":
        return export_mlflow_reviews(args)
    if args.command == "create-mlflow-dataset":
        return create_mlflow_dataset(args)
    if args.command == "report-cell-errors":
        return report_saved_cell_errors(args)
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
