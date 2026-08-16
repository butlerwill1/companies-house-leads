from __future__ import annotations

import json
from types import SimpleNamespace

import mlflow

from scripts.vlm import vlm_financial_eval
from scripts.vlm.vlm_financial_eval import (
    CASE_SCHEMA_VERSION,
    aggregate_scores,
    backfill_page_number_payload,
    cell_comparison_rows,
    canonical_empty_expectations,
    configuration_from_file,
    log_live_result_trace,
    mlflow_dataset_digest,
    mlflow_dataset_records,
    mlflow_review_question_specs,
    needs_page_number_backfill,
    parse_reviewed_metric,
    review_answers_to_case,
    review_seed_payload,
    saved_result_records,
    score_payload,
    validate_case,
)
from scripts.vlm.companies_house_pdf_vlm_financials import ModelCallResult


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


def test_company_context_from_case_preserves_sic_as_advisory_metadata() -> None:
    case = verified_case()
    case["metadata"] = {"sic_1": "65110 - Life insurance", "difficulty": "easy"}

    assert vlm_financial_eval.company_context_from_case(case) == {
        "company_number": "00000001", "sic_codes": ["65110 - Life insurance"],
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


def test_employee_scores_are_stratified_by_gold_evidence_kind() -> None:
    case = verified_case()
    employee = case["expected"]["financial_period_summaries"]["current"]["employees"]
    employee.update({
        "state": "present", "value_count": 0, "displayed_value": None,
        "unit": "COUNT", "source_page": 17, "evidence_kind": "narrative_zero",
    })
    payload = model_payload()
    payload["metrics"].append({
        "period_type": "current", "metric_name": "employees", "value_count": 0,
        "value_pence": None, "source_page": 17, "confidence": 0.9,
        "validation": {"evidence_kind": "narrative_zero"},
    })
    payload["raw_extraction"]["candidates"].append({
        "metric": "employees", "page": 17, "current_value_count": 0,
        "current_evidence_kind": "narrative_zero",
    })

    score = score_payload(case, payload)
    report = aggregate_scores([score])

    assert score["cells"][0]["employee_evidence_kind"] is None
    employee_cell = next(cell for cell in score["cells"] if cell["metric"] == "employees" and cell["period"] == "current")
    assert employee_cell["employee_evidence_kind"] == "narrative_zero"
    assert employee_cell["predicted_employee_evidence_kind"] == "narrative_zero"
    assert report["employee_evidence_kind_groups"]["narrative_zero"]["populated_value_recall"] == 1.0


def test_cell_comparison_rows_expose_the_exact_value_difference() -> None:
    payload = model_payload()
    payload["metrics"][0]["value_pence"] = 999_900
    payload["metrics"][0]["displayed_value"] = "9,999"

    rows = cell_comparison_rows(verified_case(), payload)
    turnover = next(
        row for row in rows
        if row["period"] == "current" and row["metric"] == "turnover"
    )

    assert turnover["outcome"] == "wrong_value"
    assert turnover["expected_displayed_value"] == "1,234"
    assert turnover["predicted_displayed_value"] == "9,999"
    assert turnover["metric_group"] == "core_financial"


def test_verified_case_requires_all_values_to_be_reviewed() -> None:
    case = verified_case()
    case["expected"]["financial_period_summaries"]["current"]["cash"]["state"] = "unreviewed"
    assert "unreviewed expected value for current.cash" in validate_case(case, require_complete=True)


def test_mlflow_dataset_records_are_portable_verified_gold_labels() -> None:
    case = verified_case()
    records = mlflow_dataset_records([case])
    assert records[0]["inputs"] == {
        "case_id": "00000001-doc",
        "company_number": "00000001",
        "document_id": "doc",
        "pdf_sha256": "a" * 64,
        "split": "development",
        "metadata": {"sic_division": "47", "difficulty": "easy"},
    }
    assert "pdf_path" not in records[0]["inputs"]
    assert records[0]["expectations"]["statement_pages"] == [4]
    assert records[0]["tags"]["label_status"] == "verified"
    assert mlflow_dataset_digest(records) == mlflow_dataset_digest(records)


def test_aggregate_calculates_timing_and_20000_document_extrapolation() -> None:
    score = score_payload(verified_case(), model_payload())
    report = aggregate_scores([score], {"compute_cost_gbp_per_hour": 2.0})
    assert report["pdfs_per_hour"] == 300.0
    assert report["estimated_20000_hours"] == 20000 / 300
    assert report["estimated_compute_cost_gbp"] == 2 / 300
    assert report["core_financial_exact_cell_accuracy"] == 1.0
    assert report["employees_exact_cell_accuracy"] == 1.0


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
    assert [question["name"] for question in questions] == [
        "gold_statement_pages",
        "gold_current_turnover", "gold_previous_turnover",
        "gold_current_gross_profit", "gold_previous_gross_profit",
        "gold_current_operating_result", "gold_previous_operating_result",
        "gold_current_profit_after_tax", "gold_previous_profit_after_tax",
        "gold_current_cash", "gold_previous_cash",
        "gold_current_net_assets", "gold_previous_net_assets",
        "gold_current_employees", "gold_previous_employees",
    ]


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
    assert parse_reviewed_metric("- | 8 | count", "employees") == {
        "state": "present",
        "value_pence": None,
        "value_count": 0,
        "displayed_value": "-",
        "unit": "COUNT",
        "source_page": 8,
        "source_label": None,
        "evidence_kind": "dash_zero",
    }
    assert parse_reviewed_metric("NARRATIVE_ZERO | 17 | count", "employees") == {
        "state": "present",
        "value_pence": None,
        "value_count": 0,
        "displayed_value": None,
        "unit": "COUNT",
        "source_page": 17,
        "source_label": None,
        "evidence_kind": "narrative_zero",
    }
    assert parse_reviewed_metric("MISSING", "cash")["state"] == "missing"
    assert parse_reviewed_metric("1,234 | 12 | $", "turnover")["unit"] == "USD"
    assert parse_reviewed_metric("1,234 | 12 | Ł", "turnover")["unit"] == "GBP"


def test_verified_case_accepts_a_non_gbp_reported_value() -> None:
    case = verified_case()
    case["expected"]["financial_period_summaries"]["current"]["turnover"] = (
        parse_reviewed_metric("1,234 | 4 | $", "turnover")
    )

    assert validate_case(case, require_complete=True) == []


def test_scoring_compares_non_gbp_reported_values_and_currency() -> None:
    case = verified_case()
    case["expected"]["financial_period_summaries"]["current"]["turnover"] = (
        parse_reviewed_metric("1,234 | 4 | USD", "turnover")
    )
    payload = model_payload()
    payload["metrics"][0].update({
        "value_pence": None,
        "displayed_value": "1,234",
        "unit": "USD",
        "currency_code": "USD",
        "scale_multiplier": 1,
        "reported_value": "1234",
    })

    score = score_payload(case, payload)
    cell = next(
        item for item in score["cells"]
        if item["period"] == "current" and item["metric"] == "turnover"
    )
    assert cell["correct"] is True

    payload["metrics"][0]["reported_value"] = "9999"
    assert score_payload(case, payload)["counts"]["exact_cells"] == 13

    payload["metrics"][0].update({"reported_value": "1234", "currency_code": "EUR", "unit": "EUR"})
    assert score_payload(case, payload)["counts"]["exact_cells"] == 13


def test_review_parser_preserves_generic_currency_and_decimal_reported_value() -> None:
    parsed = parse_reviewed_metric("1.250 | 4 | KWD", "turnover")

    assert parsed["currency_code"] == "KWD"
    assert parsed["scale_multiplier"] == 1
    assert parsed["reported_value"] == "1.250"
    assert parsed["value_pence"] is None


def test_missing_money_value_rejects_a_stale_reported_amount() -> None:
    case = verified_case()
    missing = case["expected"]["financial_period_summaries"]["current"]["cash"]
    missing.update({"reported_value": "10", "currency_code": "USD", "unit": "USD"})

    assert "missing value must be null for current.cash" in validate_case(
        case, require_complete=True
    )


def test_review_answers_create_a_complete_portable_gold_case() -> None:
    answers = {"gold_statement_pages": SimpleNamespace(value="4, 7")}
    for period in ("current", "previous"):
        for metric in canonical_empty_expectations()[period]:
            value = "3 | 4 | count" if metric == "employees" else "MISSING"
            answers[f"gold_{period}_{metric}"] = SimpleNamespace(value=value)
    answers["gold_current_turnover"] = SimpleNamespace(value="1,234 | 4 | GBP")

    case = review_answers_to_case(
        case_id="00000001-doc",
        company_number="00000001",
        document_id="doc",
        pdf_sha256="a" * 64,
        split="development",
        trace_id="tr-example",
        answers=answers,
        reviewer="reviewer",
        reviewed_at="2026-08-09T10:00:00+00:00",
    )

    assert validate_case(case, require_complete=True) == []
    assert case["pdf_path"] == "mlflow://traces/tr-example"
    assert case["expected"]["statement_pages"] == [4, 7]
    assert case["expected"]["financial_period_summaries"]["current"]["turnover"]["value_pence"] == 123_400


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
