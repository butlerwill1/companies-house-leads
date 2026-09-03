from __future__ import annotations

from scripts.eval_support import migrate_mlflow_to_langfuse as M


def test_eval_tags_keeps_only_eval_and_backfill_namespaces() -> None:
    tags = M._eval_tags({
        "eval.company_number": "SC123",
        "eval.model": "google/gemini-2.5-flash",
        "backfill.original_trace_id": "tr-old",
        "mlflow.traceName": "x",
        "mlflow.artifactLocation": "y",
    })
    assert "migrated-from-mlflow" in tags
    assert "company_number:SC123" in tags
    assert "model:google/gemini-2.5-flash" in tags
    assert "original_trace_id:tr-old" in tags
    assert not any(t.startswith("traceName") for t in tags)


def test_assessment_scores_maps_categorical_and_numeric(monkeypatch) -> None:
    created: list[dict] = []

    class _Client:
        def create_score(self, **kwargs):
            created.append(kwargs)

    class _Src:
        source_id = "will"

    class _Cat:
        name = "demand_model"
        value = "b2b"
        source = _Src()

    class _Num:
        name = "field.accuracy"
        value = 0.9
        source = None

    n = M._assessment_scores(_Client(), "tr-1", [_Cat(), _Num()])
    assert n == 2
    by_name = {c["name"]: c for c in created}
    assert by_name["demand_model"]["data_type"] == "CATEGORICAL"
    assert by_name["demand_model"]["value"] == "b2b"
    assert "will" in by_name["demand_model"]["comment"]
    assert by_name["field.accuracy"]["data_type"] == "NUMERIC"


def test_span_type_mapping() -> None:
    assert M._SPAN_TYPE["LLM"] == "generation"
    assert M._SPAN_TYPE["RETRIEVER"] == "retriever"
