from __future__ import annotations

import pytest

from scripts.langfuse_eval_helpers import langfuse_tracing as T
from tests.langfuse_fakes import FakeLangfuse


def test_langfuse_from_config_disabled_returns_none() -> None:
    assert T.langfuse_from_config({"langfuse": {"enabled": False}}) is None
    assert T.langfuse_from_config({}) is None


def test_langfuse_from_config_missing_keys_returns_none(monkeypatch, capsys) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY_BUSINESS_PROFILE", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY_BUSINESS_PROFILE", raising=False)
    cfg = {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}
    assert T.langfuse_from_config(cfg) is None
    assert "LANGFUSE_PUBLIC_KEY_BUSINESS_PROFILE" in capsys.readouterr().err


def test_langfuse_from_config_builds_client(monkeypatch) -> None:
    built = {}

    class _Stub:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY_VLM_FINANCIAL", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY_VLM_FINANCIAL", "sk-lf-y")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", _Stub)
    cfg = {"langfuse": {"enabled": True, "key_env": "VLM_FINANCIAL"}}
    client = T.langfuse_from_config(cfg)
    assert client is not None
    assert built["public_key"] == "pk-lf-x"
    assert built["secret_key"] == "sk-lf-y"
    assert built["host"] == "http://localhost:3000"


def test_flush_tolerates_none() -> None:
    T.flush(None)  # no raise


def test_trace_score_delegates_to_create_score() -> None:
    client = FakeLangfuse()
    T.trace_score(client, "tr-1", "field.demand_model", 1.0, data_type="NUMERIC", comment="ok")
    assert client.scores[0].name == "field.demand_model"
    assert client.scores[0].trace_id == "tr-1"
    assert client.scores[0].value == 1.0


def test_case_trace_yields_span_with_trace_id(monkeypatch) -> None:
    import contextlib

    monkeypatch.setattr(T, "case_trace", T.case_trace)  # keep ref
    import scripts.langfuse_eval_helpers.langfuse_tracing as mod

    monkeypatch.setattr(
        "langfuse.propagate_attributes",
        lambda **kwargs: contextlib.nullcontext(),
        raising=False,
    )
    client = FakeLangfuse()
    with mod.case_trace(client, name="c", tags=["t"], input={"x": 1}, output={"y": 2}) as root:
        assert root.trace_id.startswith("tr-")
