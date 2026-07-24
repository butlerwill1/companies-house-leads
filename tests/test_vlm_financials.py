from __future__ import annotations

import sqlite3

from companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.ocr.companies_house_pdf_vlm_financials import (
    extraction_candidates,
    selected_metrics,
    statement_pages,
    to_pence,
)


def test_statement_pages_includes_statement_neighbours() -> None:
    locator = {"pages": [
        {"page": 2, "statement_type": "other"},
        {"page": 5, "statement_type": "income_statement"},
        {"page": 9, "statement_type": "balance_sheet"},
    ]}
    assert statement_pages(locator, 10) == [4, 5, 6, 8, 9, 10]


def test_money_conversion_preserves_scale_and_sign() -> None:
    assert to_pence("1,234", "GBP", "turnover") == 123_400
    assert to_pence("(1,234)", "GBP_THOUSANDS", "cost_of_sales") == -123_400_000
    assert to_pence("2.5", "GBP_MILLIONS", "turnover") == 250_000_000
    assert to_pence("12", "UNKNOWN", "turnover") is None


def test_rationalisation_selects_only_a_matching_candidate() -> None:
    extraction = {"pages": [{"page": 4, "unit": "GBP_THOUSANDS", "rows": [{
        "metric": "turnover", "source_label": "Turnover", "current_display": "1,234",
        "previous_display": "1,100", "current_column": "2025", "previous_column": "2024",
        "evidence_text": "Turnover 1,234 1,100", "confidence": 0.98,
    }]}]}
    candidates = extraction_candidates(extraction)
    metrics = selected_metrics(candidates, {"choices": [{
        "metric": "turnover", "current_candidate_id": "p4-r0", "previous_candidate_id": "p4-r0",
        "reason": "Primary statement row", "confidence": 0.95,
    }]})
    assert [item["value_pence"] for item in metrics] == [123_400_000, 110_000_000]
    assert all(item["source_page"] == 4 for item in metrics)


def test_rationalisation_deduplicates_the_same_period_and_metric() -> None:
    candidates = [
        {"id": "a", "metric": "turnover", "page": 2, "unit": "UNKNOWN", "current_display": "100", "previous_display": None, "source_label": "Turnover", "evidence_text": "", "confidence": 0.9},
        {"id": "b", "metric": "turnover", "page": 3, "unit": "GBP", "current_display": "100", "previous_display": None, "source_label": "Turnover", "evidence_text": "", "confidence": 0.8},
    ]
    metrics = selected_metrics(candidates, {"choices": [
        {"metric": "turnover", "current_candidate_id": "a", "previous_candidate_id": None, "confidence": 0.9},
        {"metric": "turnover", "current_candidate_id": "b", "previous_candidate_id": None, "confidence": 0.8},
    ]})
    assert len(metrics) == 1
    assert metrics[0]["source_page"] == 3


def test_vlm_results_record_models_and_cost() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    payload = {
        "pdf_path": "example.pdf",
        "models": {"locator": "locator", "vision": "vision", "rationalisation": "text-reviewer"},
        "status": "complete",
        "pages_scanned": [1, 2],
        "candidate_pages": [2],
        "raw_extraction": {},
        "rationalisation": {},
        "usage": {},
        "cost": {"pricing": {"gbp_per_usd": 0.75}, "usd": 0.01, "gbp": 0.0075, "method": "provider_reported"},
        "metrics": [{
            "period_type": "current", "metric_name": "turnover", "value_pence": 123_400,
            "value_count": None, "displayed_value": "1,234", "unit": "GBP", "source_page": 2,
            "source_label": "Turnover", "evidence_text": "Turnover 1,234", "confidence": 0.9,
            "validation": {"unit_known": True},
        }],
    }
    run_id = insert_vlm_financial_payload(conn, payload, "00000001", "document")
    run = conn.execute("select locator_model, vision_model, rationalisation_model, cost_gbp from vlm_financial_extraction_runs where id=?", (run_id,)).fetchone()
    metric = conn.execute("select metric_name, value_pence, vision_model from vlm_financial_metrics where extraction_run_id=?", (run_id,)).fetchone()
    assert run == ("locator", "vision", "text-reviewer", 0.0075)
    assert metric == ("turnover", 123_400, "vision")
