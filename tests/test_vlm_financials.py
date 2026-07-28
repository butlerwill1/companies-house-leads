from __future__ import annotations

import sqlite3

import pytest

from companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.ocr import companies_house_pdf_vlm_financials as vlm_financials
from scripts.ocr.companies_house_pdf_vlm_financials import (
    OpenRouterVlmModelClient,
    OllamaVlmModelClient,
    RenderedPage,
    ModelCallResult,
    combine_model_calls,
    extraction_candidates,
    selected_metrics,
    statement_pages,
    to_pence,
)
from scripts.ocr.financial_metric_policy import add_canonical_equivalents


class FakeResponse:
    """Small requests response double for offline provider contract tests."""

    def __init__(self, body: dict[str, object]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._body


def test_statement_pages_includes_statement_neighbours() -> None:
    locator = {"pages": [
        {"page": 2, "statement_type": "other"},
        {"page": 5, "statement_type": "income_statement"},
        {"page": 9, "statement_type": "balance_sheet"},
    ]}
    assert statement_pages(locator, 10) == [4, 5, 6, 8, 9, 10]


def test_statement_pages_accepts_numeric_page_strings() -> None:
    locator = {"pages": [
        {"page": "5", "statement_type": "income_statement"},
        {"page": "not-a-page", "statement_type": "balance_sheet"},
    ]}

    assert statement_pages(locator, 10) == [4, 5, 6]


def test_ollama_client_uses_native_vision_payload_and_returns_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse({
            "message": {"content": '{"pages":[]}'},
            "prompt_eval_count": 31,
            "eval_count": 7,
            "total_duration": 2_000_000,
        })

    monkeypatch.setattr(vlm_financials.requests, "post", fake_post)
    result = OllamaVlmModelClient().generate_json(
        "qwen3-vl:test",
        "Find statement pages.",
        [RenderedPage(page=4, image_b64="image-one"), RenderedPage(page=5, image_b64="image-two")],
        60,
    )

    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["format"] == "json"
    assert payload["messages"][0]["images"] == ["image-one", "image-two"]
    assert "Image 1 is document page 4." in payload["messages"][0]["content"]
    assert result.payload == {"pages": []}
    assert result.usage["prompt_tokens"] == 31
    assert result.elapsed_seconds >= 0


def test_ollama_client_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="local SSH or SSM tunnel"):
        OllamaVlmModelClient("http://10.0.0.12:11434")


def test_ollama_health_check_uses_tags_without_starting_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse({"models": [{"name": "private-vision:latest"}]})

    monkeypatch.setattr(vlm_financials.requests, "get", fake_get)
    assert OllamaVlmModelClient().health_check({"private-vision"}) == ["private-vision:latest"]
    assert captured["url"] == "http://127.0.0.1:11434/api/tags"
    assert captured["timeout"] == 10


def test_ollama_health_check_reports_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        vlm_financials.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse({"models": [{"name": "other-model"}]}),
    )
    with pytest.raises(RuntimeError, match="required model"):
        OllamaVlmModelClient().health_check({"private-vision"})


def test_batched_locator_calls_combine_usage_and_page_results() -> None:
    combined = combine_model_calls(
        [
            ModelCallResult({"pages": [{"page": 1}]}, {"prompt_tokens": 5}, 1.2, 10, 0.9),
            ModelCallResult({"pages": [{"page": 2}]}, {"prompt_tokens": 7}, 2.3, 20, 1.8),
        ],
        pages=[{"page": 1}, {"page": 2}],
    )
    assert combined.payload == {"pages": [{"page": 1}, {"page": 2}]}
    assert combined.usage == {"prompt_tokens": 12}
    assert combined.elapsed_seconds == 3.5
    assert combined.image_payload_bytes == 30
    assert combined.model_reported_seconds == 2.7


def test_pdf_pipeline_batches_locator_and_extraction_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = [RenderedPage(page, "aGVsbG8=") for page in range(1, 6)]
    monkeypatch.setattr(vlm_financials, "render_pages", lambda *_args, **_kwargs: rendered)

    class RecordingClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.calls: list[tuple[str, list[int]]] = []

        def generate_json(
            self, _model: str, prompt: str, pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            self.calls.append((prompt, [page.page for page in pages]))
            if prompt == vlm_financials.LOCATOR_PROMPT:
                payload = {
                    "pages": [
                        {"page": page.page, "statement_type": "income_statement"}
                        for page in pages
                    ]
                }
            else:
                payload = {
                    "pages": [
                        {"page": page.page, "statement_type": "income_statement", "rows": []}
                        for page in pages
                    ]
                }
            return ModelCallResult(payload, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = RecordingClient()
    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"),
        client,
        locator_batch_size=2,
        extraction_batch_size=2,
    )
    locator_calls = [pages for prompt, pages in client.calls if prompt == vlm_financials.LOCATOR_PROMPT]
    extraction_calls = [
        pages for prompt, pages in client.calls if prompt == vlm_financials.EXTRACTION_PROMPT
    ]
    assert locator_calls == [[1, 2], [3, 4], [5]]
    assert extraction_calls == [[1, 2], [3, 4], [5]]
    assert payload["timing"]["locator_batches"] == 3
    assert payload["timing"]["extraction_batches"] == 3


def test_openrouter_client_includes_configured_request_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(_url: str, **kwargs: object) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse({
            "id": "gen-test",
            "model": "qwen/qwen3.5-9b",
            "provider": "DeepInfra",
            "openrouter_metadata": {"provider_name": "DeepInfra"},
            "choices": [{"message": {"content": '{"pages":[]}'}}],
        })

    monkeypatch.setattr(vlm_financials.requests, "post", fake_post)
    result = OpenRouterVlmModelClient("not-a-key", {"reasoning": {"enabled": False}}).generate_json(
        "qwen/qwen3.5-9b", "Find pages", [], 60
    )
    assert captured["json"]["reasoning"] == {"enabled": False}
    assert captured["headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert result.provider_metadata == {
        "generation_id": "gen-test",
        "model": "qwen/qwen3.5-9b",
        "provider": "DeepInfra",
        "openrouter_metadata": {"provider_name": "DeepInfra"},
    }


def test_openrouter_client_reports_provider_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class ErrorResponse(FakeResponse):
        text = "provider error"

        def raise_for_status(self) -> None:
            raise vlm_financials.requests.HTTPError("400")

    monkeypatch.setattr(
        vlm_financials.requests,
        "post",
        lambda *_args, **_kwargs: ErrorResponse({"error": {"message": "image limit exceeded"}}),
    )
    with pytest.raises(RuntimeError, match="image limit exceeded"):
        OpenRouterVlmModelClient("not-a-key").generate_json(
            "qwen/qwen3.5-9b", "Find pages", [], 60
        )


def test_money_conversion_preserves_scale_and_sign() -> None:
    assert to_pence("1,234", "GBP", "turnover") == 123_400
    assert to_pence("(1,234)", "GBP_THOUSANDS", "cost_of_sales") == -123_400_000
    assert to_pence("2.5", "GBP_MILLIONS", "turnover") == 250_000_000
    assert to_pence("12", "UNKNOWN", "turnover") is None
    assert to_pence("-", "GBP", "turnover") == 0


def test_insurance_rows_gain_traceable_canonical_equivalents() -> None:
    extraction = {"pages": [{"page": 12, "unit": "GBP", "rows": [
        {
            "metric": "gross_premiums_written",
            "source_label": "Gross premiums written",
            "current_display": "1,027,336",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Gross premiums written 1,027,336 -",
            "confidence": 0.99,
        },
        {
            "metric": "net_earned_premiums",
            "source_label": "Earned premiums, net of reinsurance",
            "current_display": "426,652",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Earned premiums, net of reinsurance 426,652 -",
            "confidence": 0.98,
        },
        {
            "metric": "claims_incurred_net_reinsurance",
            "source_label": "Claims incurred, net of reinsurance",
            "current_display": "(275,956)",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Claims incurred, net of reinsurance (275,956) -",
            "confidence": 0.98,
        },
        {
            "metric": "technical_account_result",
            "source_label": "Balance on the technical account for general business",
            "current_display": "(12,552)",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Balance on the technical account (12,552) -",
            "confidence": 0.99,
        },
    ]}]}

    candidates = add_canonical_equivalents(extraction_candidates(extraction))
    canonical = {
        candidate["metric"]: candidate
        for candidate in candidates
        if candidate["metric"] in {"turnover", "gross_profit", "operating_result"}
    }

    assert canonical["turnover"]["current_display"] == "1,027,336"
    assert canonical["gross_profit"]["current_display"] == "150,696"
    assert canonical["operating_result"]["current_display"] == "(12,552)"
    assert canonical["gross_profit"]["derivation"] == {
        "policy": "general_insurance",
        "kind": "derived_equivalent",
        "formula": "net_earned_premiums + claims_incurred_net_reinsurance",
        "source_candidate_ids": ["p12-r1", "p12-r2"],
    }


def test_selected_insurance_equivalent_keeps_derivation_provenance() -> None:
    candidates = add_canonical_equivalents([
        {
            "id": "p12-r0",
            "metric": "technical_account_result",
            "page": 12,
            "unit": "GBP",
            "source_label": "Balance on the technical account for general business",
            "current_display": "(12,552)",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Balance on the technical account (12,552) -",
            "confidence": 0.99,
        },
    ])
    candidate = next(item for item in candidates if item["metric"] == "operating_result")
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"operating_result": {
            "candidate_id": candidate["id"],
            "reason": "Insurance technical-account equivalent",
            "confidence": 0.99,
        }},
    }})

    assert metrics[0]["value_pence"] == -1_255_200
    assert metrics[0]["validation"]["derivation"] == {
        "policy": "general_insurance",
        "kind": "reported_equivalent",
        "formula": "technical_account_result",
        "source_candidate_ids": ["p12-r0"],
    }


def test_pdf_pipeline_maps_insurance_rows_before_rationalisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_financials,
        "render_pages",
        lambda *_args, **_kwargs: [RenderedPage(1, "aGVsbG8=")],
    )

    class InsuranceClient:
        provider_name = "test"

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                payload = {"pages": [{"page": 1, "statement_type": "income_statement"}]}
            elif prompt == vlm_financials.EXTRACTION_PROMPT:
                payload = {"pages": [{"page": 1, "unit": "GBP", "rows": [{
                    "metric": "gross_premiums_written",
                    "source_label": "Gross premiums written",
                    "current_display": "1,027,336",
                    "previous_display": "-",
                    "current_column": "2024",
                    "previous_column": "2023",
                    "evidence_text": "Gross premiums written 1,027,336 -",
                    "confidence": 0.99,
                }]}]}
            else:
                assert "insurance-turnover-p1-r0" in prompt
                payload = {"financial_period_summaries": {
                    "current": {"turnover": {
                        "candidate_id": "insurance-turnover-p1-r0",
                        "reason": "Reported gross premiums written",
                        "confidence": 0.99,
                    }},
                    "previous": {"turnover": {
                        "candidate_id": "insurance-turnover-p1-r0",
                        "reason": "Reported gross premiums written",
                        "confidence": 0.99,
                    }},
                }}
            return ModelCallResult(payload, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("insurance.pdf"),
        InsuranceClient(),
    )

    assert [
        (metric["period_type"], metric["value_pence"])
        for metric in payload["metrics"]
    ] == [("current", 102_733_600), ("previous", 0)]
    assert all(
        metric["validation"]["derivation"]["policy"] == "general_insurance"
        for metric in payload["metrics"]
    )


def test_rationalisation_selects_only_a_matching_candidate() -> None:
    extraction = {"pages": [{"page": 4, "unit": "GBP_THOUSANDS", "rows": [{
        "metric": "turnover", "source_label": "Turnover", "current_display": "1,234",
        "previous_display": "1,100", "current_column": "2025", "previous_column": "2024",
        "evidence_text": "Turnover 1,234 1,100", "confidence": 0.98,
    }]}]}
    candidates = extraction_candidates(extraction)
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"turnover": {"candidate_id": "p4-r0", "reason": "Primary statement row", "confidence": 0.95}},
        "previous": {"turnover": {"candidate_id": "p4-r0", "reason": "Primary statement row", "confidence": 0.95}},
    }})
    assert [item["value_pence"] for item in metrics] == [123_400_000, 110_000_000]
    assert all(item["source_page"] == 4 for item in metrics)


def test_extraction_candidates_accept_numeric_page_strings() -> None:
    extraction = {"pages": [{"page": "12", "unit": "GBP", "rows": [{
        "metric": "net_assets", "source_label": "Net assets",
        "current_display": "99,538,865", "previous_display": "86,490,628",
        "current_column": "2025", "previous_column": "2024",
        "evidence_text": "Net assets 99,538,865 86,490,628", "confidence": 0.95,
    }]}]}

    candidates = extraction_candidates(extraction)

    assert candidates[0]["id"] == "p12-r0"
    assert candidates[0]["page"] == 12


def test_rationalisation_rejects_a_candidate_for_the_wrong_column() -> None:
    candidates = [
        {"id": "a", "metric": "cash", "page": 2, "unit": "GBP", "current_display": "100", "previous_display": None, "source_label": "Cash", "evidence_text": "", "confidence": 0.9},
    ]
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"turnover": {"candidate_id": "a", "reason": "wrong", "confidence": 0.9}},
    }})
    assert metrics == []


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
    metric = conn.execute("select company_number, metric_name, value_pence, vision_model from vlm_financial_metrics where extraction_run_id=?", (run_id,)).fetchone()
    canonical = conn.execute("select turnover, data_source from financial_period_summaries where company_number=? and document_id=? and period_type='current'", ("00000001", "document")).fetchone()
    assert run == ("locator", "vision", "text-reviewer", 0.0075)
    assert metric == ("00000001", "turnover", 123_400, "vision")
    assert canonical == (1_234, "vlm")


def test_vlm_canonical_summary_does_not_overwrite_xhtml_data() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute(
        """
        insert into financial_period_summaries (
            company_number, document_id, period_type, turnover, raw_payload, data_source
        ) values ('00000002', 'document', 'current', 999, '{}', 'xhtml')
        """
    )
    payload = {
        "pdf_path": "example.pdf",
        "models": {"locator": "locator", "vision": "vision", "rationalisation": "text-reviewer"},
        "status": "complete", "pages_scanned": [], "candidate_pages": [],
        "raw_extraction": {}, "rationalisation": {}, "usage": {},
        "cost": {"pricing": {}, "usd": 0, "gbp": 0, "method": "provider_reported"},
        "metrics": [{
            "period_type": "current", "metric_name": "turnover", "value_pence": 123_400,
            "value_count": None, "displayed_value": "1,234", "unit": "GBP", "source_page": 1,
            "source_label": "Turnover", "evidence_text": "", "confidence": 1,
            "validation": {"unit_known": True},
        }],
    }
    insert_vlm_financial_payload(conn, payload, "00000002", "document")
    assert conn.execute(
        "select turnover, data_source from financial_period_summaries where company_number='00000002'"
    ).fetchone() == (999, "xhtml")
