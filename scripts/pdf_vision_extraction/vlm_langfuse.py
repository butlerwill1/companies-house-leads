"""VLM-financial-specific Langfuse helpers.

The generic Langfuse plumbing lives in scripts.langfuse_eval_helpers; this module holds
the pieces particular to the VLM harness: the three-stage trace shape (locator
-> extraction -> rationalisation) with the source PDF attached, and the
15-question gold-label review spec.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.langfuse_eval_helpers.langfuse_tracing import observation, pdf_media
from scripts.pdf_vision_extraction.companies_house_pdf_vlm_financials import CANONICAL_METRICS

if TYPE_CHECKING:  # pragma: no cover
    from langfuse import Langfuse

PERIODS = ("current", "previous")
DATASET_NAME = "vlm-financial-gold"
ANNOTATION_QUEUE_NAME = "Financial PDF gold-label review"
DEFAULT_DATASET_SNAPSHOT_NAME = "companies-house-financial-gold-v1"

METRIC_TITLES = {
    "turnover": "Turnover",
    "gross_profit": "Gross profit",
    "operating_result": "Operating result",
    "profit_after_tax": "Profit after tax",
    "cash": "Cash",
    "net_assets": "Net assets",
    "employees": "Employees",
}


def review_question_specs() -> list[dict[str, Any]]:
    """The 15 gold-label questions, unchanged from the MLflow harness -- pure
    data, fed to score-config creation and used to parse answers back."""
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
        }
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
                        "Example: (1,234) | 12 | GBP_THOUSANDS. For an explicit employee narrative "
                        "zero, enter NARRATIVE_ZERO | source page | count. Enter MISSING when the "
                        "metric is not disclosed for this period."
                    ),
                }
            )
    return questions


def review_score_config_specs() -> list[dict[str, Any]]:
    """review_question_specs shaped for scripts.langfuse_eval_helpers.langfuse_annotation
    (all free-text, keyed by the question `name`)."""
    return [
        {"name": q["name"], "description": q["instruction"]}
        for q in review_question_specs()
    ]


def dataset_records(cases: list[dict[str, Any]], validate) -> list[dict[str, Any]]:
    """Portable, human-labelled records to publish as a snapshot dataset. PDFs
    stay outside Langfuse -- the Companies House ids + content hash connect a
    record to its exact local document."""
    records: list[dict[str, Any]] = []
    for case in sorted(cases, key=lambda item: str(item["id"])):
        errors = validate(case, require_complete=True)
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
                "employee_evidence_pages": copy.deepcopy(case["expected"].get("employee_evidence_pages")),
                "financial_period_summaries": copy.deepcopy(case["expected"]["financial_period_summaries"]),
            },
            "tags": {"label_status": "verified", "case_schema_version": str(case["schema_version"])},
        })
    return records


def dataset_digest(records: list[dict[str, Any]]) -> str:
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def emit_stage_spans(
    client: "Langfuse", payload: dict[str, Any], *, pdf_path: Path | None, models: dict[str, Any]
) -> None:
    """Add the locator / extraction / rationalisation GENERATION spans (and the
    source PDF as media) under whatever observation is currently active -- the
    experiment runner's item trace, or a standalone review trace."""
    raw = payload.get("raw_extraction") or {}
    usage = payload.get("usage") or {}

    if pdf_path is not None and Path(pdf_path).is_file():
        with observation(client, name="source_pdf", as_type="span",
                         input={"source_pdf": pdf_media(pdf_path)}):
            pass

    with observation(
        client, name="statement_page_locator", as_type="generation", model=models.get("locator"),
        input={"pages_scanned": payload.get("pages_scanned") or []},
        output={
            "candidate_pages": payload.get("candidate_pages") or [],
            "employee_evidence_pages": payload.get("employee_evidence_pages") or [],
            "locator_output": raw.get("locator") or {},
        },
        metadata={"measured_elapsed_seconds": (usage.get("locator") or {}).get("elapsed_seconds")},
    ):
        pass

    with observation(
        client, name="financial_row_extraction", as_type="generation", model=models.get("vision"),
        input={"candidate_pages": payload.get("candidate_pages") or []},
        output={
            "detail_output": raw.get("detail") or {},
            "employee_detail_output": raw.get("employee_detail") or {},
            "row_validation": raw.get("row_validation") or {},
        },
        metadata={"measured_elapsed_seconds": (usage.get("vision") or {}).get("elapsed_seconds")},
    ):
        pass

    with observation(
        client, name="canonical_rationalisation", as_type="generation", model=models.get("rationalisation"),
        input={"candidate_rows": raw.get("candidates") or []},
        output={
            "rationalisation": payload.get("rationalisation") or {},
            "resolved_rationalisation": payload.get("resolved_rationalisation") or {},
            "canonical_metrics": payload.get("metrics") or [],
        },
        metadata={"measured_elapsed_seconds": (usage.get("rationalisation") or {}).get("elapsed_seconds")},
    ):
        pass


def score_evaluations(score: dict[str, Any], evaluation) -> list[Any]:
    """Turn a deterministic score_payload() result into Langfuse Evaluations."""
    evals: list[Any] = [evaluation("status", str(score.get("status") or "unknown"), data_type="CATEGORICAL")]
    page = score.get("page") or {}
    for key in ("precision", "recall", "f1"):
        if page.get(key) is not None:
            evals.append(evaluation(f"page_{key}", float(page[key]), data_type="NUMERIC"))
    counts = score.get("counts") or {}
    cells = counts.get("cells") or 0
    if cells:
        evals.append(evaluation("exact_cell_accuracy", (counts.get("exact_cells") or 0) / cells, data_type="NUMERIC"))
    for group in ("core_financial", "employees"):
        summary = (score.get(group) or {})
        acc = summary.get("exact_cell_accuracy")
        if acc is not None:
            evals.append(evaluation(f"{group}_exact_cell_accuracy", float(acc), data_type="NUMERIC"))
    cost = (score.get("cost") or {}).get("gbp")
    if cost is not None:
        evals.append(evaluation("cost_gbp", float(cost), data_type="NUMERIC"))
    return evals
