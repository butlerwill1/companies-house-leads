from __future__ import annotations

import json

import mlflow

from scripts.ocr import vlm_financial_eval
from scripts.ocr.vlm_financial_eval import (
    CASE_SCHEMA_VERSION,
    aggregate_scores,
    backfill_page_number_payload,
    canonical_empty_expectations,
    configuration_from_file,
    log_live_result_trace,
    mlflow_review_question_specs,
    needs_page_number_backfill,
    parse_reviewed_metric,
    review_seed_payload,
    saved_result_records,
    score_payload,
    validate_case,
)
from scripts.ocr.companies_house_pdf_vlm_financials import ModelCallResult


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


def test_mlflow_review_questions_cover_all_gold_values() -> None:
    questions = mlflow_review_question_specs()
    names = {question["name"] for question in questions}
    assert "gold_statement_pages" in names
    assert "financial_extraction_correct" not in names
    for period in ("current", "previous"):
        for metric in canonical_empty_expectations()[period]:
            assert f"gold_{period}_{metric}" in names
    assert len(names) == 15


def test_mlflow_review_metric_parser_handles_values_and_missing() -> None:
    assert parse_reviewed_metric("(1,234) | 12 | £000", "turnover") == {
        "state": "present",
        "value_pence": -123_400_000,
        "value_count": None,
        "displayed_value": "(1,234)",
        "unit": "GBP_THOUSANDS",
        "source_page": 12,
        "source_label": None,
    }
    assert parse_reviewed_metric("27 | 8 | count", "employees")["value_count"] == 27
    assert parse_reviewed_metric("MISSING", "cash")["state"] == "missing"


def test_review_seed_payload_has_no_model_output() -> None:
    payload = review_seed_payload(verified_case())
    assert payload["provider"] == "manual-review"
    assert payload["status"] == "review_seed"
    assert payload["review_seed"] is True
    assert payload["metrics"] == []


def test_saved_trace_records_include_payloads_and_pre_payload_failures(tmp_path: object) -> None:
    cases_dir = tmp_path / "cases"
    results_dir = tmp_path / "results"
    cases_dir.mkdir()
    results_dir.mkdir()
    complete_case = verified_case()
    failed_case = {
        **verified_case(),
        "id": "00000002-doc",
        "company_number": "00000002",
    }
    for case in (complete_case, failed_case):
        (cases_dir / f"{case['id']}.json").write_text(
            json.dumps(case),
            encoding="utf-8",
        )
    (results_dir / "00000001-doc-attempt-1.json").write_text(
        json.dumps(
            {
                "payload": model_payload(),
                "score": {"case_id": complete_case["id"]},
            }
        ),
        encoding="utf-8",
    )

    records = saved_result_records(
        results_dir,
        cases_dir,
        {
            "provider": "ollama",
            "locator_model": "private-vision",
            "vision_model": "private-vision",
            "rationalisation_model": "private-vision",
        },
        [
            {"case_id": complete_case["id"], "status": "complete"},
            {
                "case_id": failed_case["id"],
                "status": "error",
                "error": "request timed out",
                "elapsed_seconds": 180.0,
            },
        ],
    )

    assert records[complete_case["id"]][1]["status"] == "complete"
    failed_payload = records[failed_case["id"]][1]
    assert failed_payload["status"] == "error"
    assert failed_payload["error"] == "request timed out"
    assert failed_payload["models"]["vision"] == "private-vision"


def test_live_result_trace_is_persisted_immediately_and_idempotently(
    tmp_path: object,
    monkeypatch: object,
) -> None:
    logged: list[tuple[str, str | None]] = []
    flushed: list[bool] = []

    def fake_log_saved_case_trace(
        case: dict[str, object],
        _payload: dict[str, object],
        *,
        run_id: str | None,
    ) -> str:
        logged.append((str(case["id"]), run_id))
        return "tr-live"

    monkeypatch.setattr(
        vlm_financial_eval,
        "log_saved_case_trace",
        fake_log_saved_case_trace,
    )
    monkeypatch.setattr(
        mlflow,
        "flush_trace_async_logging",
        lambda: flushed.append(True),
    )

    first = log_live_result_trace(
        tmp_path,
        verified_case(),
        model_payload(),
        run_id="run-live",
    )
    second = log_live_result_trace(
        tmp_path,
        verified_case(),
        model_payload(),
        run_id="run-live",
    )

    manifest = json.loads((tmp_path / "trace_manifest.json").read_text(encoding="utf-8"))
    assert first == second == "tr-live"
    assert manifest == {
        "run_id": "run-live",
        "traces": {"00000001-doc": "tr-live"},
    }
    assert logged == [("00000001-doc", "run-live")]
    assert flushed == [True]


def test_page_number_backfill_reruns_only_rationalisation() -> None:
    payload = {
        **model_payload(),
        "provider": "openrouter",
        "models": {
            "locator": "model",
            "vision": "model",
            "rationalisation": "model",
        },
        "raw_extraction": {
            "locator": {},
            "detail": {
                "pages": [
                    {
                        "page": "12",
                        "unit": "GBP",
                        "rows": [
                            {
                                "metric": "net_assets",
                                "source_label": "Net assets",
                                "current_display": "99,538,865",
                                "previous_display": "86,490,628",
                                "current_column": "2025",
                                "previous_column": "2024",
                                "evidence_text": "Net assets 99,538,865 86,490,628",
                                "confidence": 0.95,
                            }
                        ],
                    }
                ]
            },
            "candidates": [],
        },
        "rationalisation": {"financial_period_summaries": {}},
        "metrics": [],
    }

    class RationalisationClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        def generate_json(
            self,
            model: str,
            prompt: str,
            pages: list[object],
            _timeout: int,
        ) -> ModelCallResult:
            self.calls.append((prompt, pages))
            assert model == "model"
            assert '"page":12' in prompt
            return ModelCallResult(
                {
                    "financial_period_summaries": {
                        "current": {
                            "net_assets": {
                                "candidate_id": "p12-r0",
                                "reason": "exact_match",
                                "confidence": 0.95,
                            }
                        },
                        "previous": {
                            "net_assets": {
                                "candidate_id": "p12-r0",
                                "reason": "exact_match",
                                "confidence": 0.95,
                            }
                        },
                    }
                },
                {"prompt_tokens": 10, "completion_tokens": 5},
                0.2,
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {
                "model": {
                    "prompt": "0.000001",
                    "completion": "0.000002",
                }
            }

    client = RationalisationClient()
    assert needs_page_number_backfill(payload) is True

    corrected = backfill_page_number_payload(
        payload,
        client,
        rationalisation_model="model",
        timeout=30,
        gbp_per_usd=0.75,
        original_trace_id="tr-original",
    )

    assert len(client.calls) == 1
    assert client.calls[0][1] == []
    assert corrected["raw_extraction"]["candidates"][0]["page"] == 12
    assert [metric["value_pence"] for metric in corrected["metrics"]] == [
        9_953_886_500,
        8_649_062_800,
    ]
    assert corrected["backfill"]["original_trace_id"] == "tr-original"
    assert payload["raw_extraction"]["candidates"] == []
