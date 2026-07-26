from __future__ import annotations

from scripts.ocr.vlm_financial_eval import (
    CASE_SCHEMA_VERSION,
    aggregate_scores,
    canonical_empty_expectations,
    configuration_from_file,
    score_payload,
    validate_case,
)


def verified_case() -> dict[str, object]:
    expectations = canonical_empty_expectations()
    expectations["current"]["turnover"] = {
        "state": "present", "value_pence": 123_400, "value_count": None,
        "displayed_value": "1,234", "unit": "GBP", "source_page": 4, "source_label": "Turnover",
    }
    expectations["previous"]["turnover"] = {
        "state": "present", "value_pence": 110_000, "value_count": None,
        "displayed_value": "1,100", "unit": "GBP", "source_page": 4, "source_label": "Turnover",
    }
    for period in ("current", "previous"):
        for metric, value in expectations[period].items():
            if metric != "turnover":
                value["state"] = "missing"
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "id": "00000001-doc", "company_number": "00000001", "document_id": "doc",
        "pdf_path": "example.pdf", "pdf_sha256": "a" * 64, "split": "development",
        "metadata": {"sic_division": "47", "difficulty": "easy"},
        "expected": {"statement_pages": [4], "financial_period_summaries": expectations},
        "review": {"status": "verified"},
    }


def model_payload() -> dict[str, object]:
    return {
        "status": "complete", "candidate_pages": [3, 4, 5], "elapsed_seconds": 12.0,
        "timing": {"thumbnail_render_seconds": 1.0}, "usage": {}, "cost": {},
        "raw_extraction": {"candidates": [{
            "metric": "turnover", "page": 4, "current_display": "1,234", "previous_display": "1,100",
        }]},
        "metrics": [
            {"period_type": "current", "metric_name": "turnover", "value_pence": 123_400, "value_count": None, "confidence": 0.9},
            {"period_type": "previous", "metric_name": "turnover", "value_pence": 110_000, "value_count": None, "confidence": 0.9},
        ],
    }


def test_scoring_distinguishes_exact_values_and_missing_values() -> None:
    score = score_payload(verified_case(), model_payload())
    assert score["page"]["recall"] == 1.0
    assert score["counts"]["exact_cells"] == 14
    assert score["counts"]["false_positive"] == 0
    assert score["whole_document_exact"] is True


def test_scoring_flags_a_false_positive_for_an_expected_missing_metric() -> None:
    payload = model_payload()
    payload["metrics"].append({
        "period_type": "current", "metric_name": "cash", "value_pence": 50_000, "value_count": None,
    })
    score = score_payload(verified_case(), payload)
    assert score["counts"]["false_positive"] == 1
    assert score["whole_document_exact"] is False


def test_verified_case_requires_all_values_to_be_reviewed() -> None:
    case = verified_case()
    case["expected"]["financial_period_summaries"]["current"]["cash"]["state"] = "unreviewed"
    assert "unreviewed expected value for current.cash" in validate_case(case, require_complete=True)


def test_aggregate_calculates_timing_and_20000_document_extrapolation() -> None:
    score = score_payload(verified_case(), model_payload())
    report = aggregate_scores([score], {"compute_cost_gbp_per_hour": 2.0})
    assert report["pdfs_per_hour"] == 300.0
    assert report["estimated_20000_hours"] == 20000 / 300
    assert report["estimated_compute_cost_gbp"] == 2 / 300


def test_aggregate_keeps_unreviewed_smoke_results_out_of_quality_scores() -> None:
    report = aggregate_scores([{
        "status": "complete", "unscored": True, "elapsed_seconds": 10,
        "counts": {}, "timing": {}, "cost": {},
    }])
    assert report["scored_documents"] == 0
    assert report["exact_cell_accuracy"] is None
    assert report["pdfs_per_hour"] == 360


def test_configuration_rejects_secrets(tmp_path: object) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("provider: ollama\nlocator_model: a\nvision_model: a\nrationalisation_model: a\napi_key: no\n")
    try:
        configuration_from_file(path)
    except ValueError as error:
        assert "secret key" in str(error)
    else:
        raise AssertionError("configuration with an API key must fail")
