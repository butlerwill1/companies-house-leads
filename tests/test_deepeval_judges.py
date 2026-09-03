from __future__ import annotations

import pytest

from scripts.eval_support import deepeval_judges as J


def test_judge_enabled_and_model_name() -> None:
    assert J.judge_enabled({"langfuse": {"deepeval": {"enabled": True}}}) is True
    assert J.judge_enabled({"langfuse": {}}) is False
    assert J.judge_model_name({}) == "google/gemini-2.5-flash"
    assert J.judge_model_name(
        {"langfuse": {"deepeval": {"judge_model": "anthropic/claude-opus-5"}}}
    ) == "anthropic/claude-opus-5"


def test_openrouter_complete_parses_completion(monkeypatch) -> None:
    seen = {}

    class _Resp:
        def raise_for_status(self):  # noqa: D401
            return None

        def json(self):
            return {"choices": [{"message": {"content": "grounded: yes"}}]}

    def _post(url, headers, json, timeout):
        seen["url"] = url
        seen["payload"] = json
        seen["auth"] = headers["Authorization"]
        return _Resp()

    monkeypatch.setattr(J.requests, "post", _post)
    out = J.openrouter_complete("google/gemini-2.5-flash", "judge this", api_key="sk-or-x", json_mode=True)
    assert out == "grounded: yes"
    assert seen["payload"]["temperature"] == 0
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["auth"] == "Bearer sk-or-x"


def test_openrouter_complete_raises_on_api_error(monkeypatch) -> None:
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": {"message": "bad"}}

    monkeypatch.setattr(J.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError):
        J.openrouter_complete("m", "p", api_key="k")


def test_business_description_test_case_uses_sections_as_context() -> None:
    pytest.importorskip("deepeval")
    case = {"company_name": "ACME LTD", "sections": {"principal_activity": "makes widgets", "empty": ""}}
    tc = J.business_description_test_case(case, "ACME makes widgets.")
    assert tc.actual_output == "ACME makes widgets."
    assert any("makes widgets" in c for c in tc.retrieval_context)
    assert all("empty" not in c for c in tc.retrieval_context)
