from __future__ import annotations

import sqlite3

import pytest

from companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.ocr import companies_house_pdf_vlm_financials as vlm_financials
from scripts.ocr.companies_house_pdf_vlm_financials import (
    OpenRouterVlmModelClient,
    OllamaVlmModelClient,
    RenderedPage,
    extraction_candidates,
    selected_metrics,
    statement_pages,
    to_pence,
)


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


def test_openrouter_client_includes_configured_request_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(_url: str, **kwargs: object) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse({"choices": [{"message": {"content": '{"pages":[]}'}}]})

    monkeypatch.setattr(vlm_financials.requests, "post", fake_post)
    OpenRouterVlmModelClient("not-a-key", {"reasoning": {"enabled": False}}).generate_json(
        "qwen/qwen3.5-9b", "Find pages", [], 60
    )
    assert captured["json"]["reasoning"] == {"enabled": False}


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
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"turnover": {"candidate_id": "p4-r0", "reason": "Primary statement row", "confidence": 0.95}},
        "previous": {"turnover": {"candidate_id": "p4-r0", "reason": "Primary statement row", "confidence": 0.95}},
    }})
    assert [item["value_pence"] for item in metrics] == [123_400_000, 110_000_000]
    assert all(item["source_page"] == 4 for item in metrics)


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
