from __future__ import annotations

import pytest

from scripts.langfuse_eval_helpers import langfuse_runs as R
from tests.langfuse_fakes import FakeLangfuse


def test_dataset_digest_is_order_independent() -> None:
    a = [{"id": "1", "x": 1}, {"id": "2", "x": 2}]
    assert R.dataset_digest(a) == R.dataset_digest(list(reversed(a)))
    assert R.dataset_digest(a) != R.dataset_digest([{"id": "1", "x": 9}, {"id": "2", "x": 2}])


def test_sync_dataset_upserts_items() -> None:
    client = FakeLangfuse()
    items = [{"id": "c1", "input": {"a": 1}, "expected": {"b": 2}, "metadata": {"m": 1}}]
    R.sync_dataset(client, "ds", items)
    R.sync_dataset(client, "ds", items)  # idempotent
    assert len(client.dataset_items["ds"]) == 1
    assert client.dataset_items["ds"][0].expected_output == {"b": 2}


def test_sync_dataset_immutability_guard() -> None:
    client = FakeLangfuse()
    recs = [{"id": "c1", "input": {}}]
    R.sync_dataset(client, "ds", recs, digest="AAA", immutable=True)
    R.sync_dataset(client, "ds", recs, digest="AAA", immutable=True)  # same digest ok
    with pytest.raises(ValueError):
        R.sync_dataset(client, "ds", recs, digest="BBB", immutable=True)


def test_run_experiment_wires_task_and_evaluators() -> None:
    client = FakeLangfuse()
    R.sync_dataset(client, "ds", [
        {"id": "c1", "input": {"q": 1}, "expected": {"a": "yes"}},
        {"id": "c2", "input": {"q": 2}, "expected": {"a": "no"}},
    ])

    def task(*, item, **kw):
        return {"a": "yes"}

    def det(*, input, output, expected_output, metadata, **kw):
        ok = output["a"] == expected_output["a"]
        return R.evaluation("exact", 1.0 if ok else 0.0, data_type="NUMERIC")

    def agg(*, item_results, **kw):
        vals = [e.value for r in item_results for e in r.evaluations if e.name == "exact"]
        return R.evaluation("mean", sum(vals) / len(vals), data_type="NUMERIC")

    result = R.run_experiment(client, dataset_name="ds", run_name="run1",
                              task=task, evaluators=[det], run_evaluators=[agg])
    assert len(result.item_results) == 2
    assert result.run_evaluations[0].value == 0.5
    # scores landed on per-item traces
    exact_scores = [s for s in client.scores if s.name == "exact"]
    assert {s.value for s in exact_scores} == {1.0, 0.0}


def test_run_experiment_item_ids_scopes_the_run() -> None:
    client = FakeLangfuse()
    R.sync_dataset(client, "ds", [
        {"id": "c1", "input": {}, "expected": {}},
        {"id": "c2", "input": {}, "expected": {}},
        {"id": "c3", "input": {}, "expected": {}},
    ])
    result = R.run_experiment(
        client, dataset_name="ds", run_name="r",
        task=lambda *, item, **kw: {}, evaluators=[], run_evaluators=[],
        item_ids=["c1", "c3"],
    )
    assert {ir.item.id for ir in result.item_results} == {"c1", "c3"}
    assert len(client.dataset_items["ds"]) == 3  # dataset itself keeps all


def test_run_score_attaches_to_an_existing_run() -> None:
    client = FakeLangfuse()
    R.sync_dataset(client, "ds", [{"id": "c1", "input": {}, "expected": {}}])
    R.run_experiment(client, dataset_name="ds", run_name="r1",
                     task=lambda *, item, **kw: {}, evaluators=[], run_evaluators=[])

    assert R.run_score(client, "ds", "r1", "recheck", 0.9, data_type="NUMERIC") is True
    posted = [s for s in client.scores if s.name == "recheck"]
    assert len(posted) == 1
    assert posted[0].dataset_run_id == "run-r1"
    assert posted[0].trace_id is None


def test_run_score_warns_and_skips_when_run_missing(capsys) -> None:
    client = FakeLangfuse()
    assert R.run_score(client, "ds", "nope", "recheck", 0.9) is False
    assert [s for s in client.scores if s.name == "recheck"] == []
    assert "not found" in capsys.readouterr().err


def test_experiment_run_name() -> None:
    from datetime import datetime

    when = datetime(2026, 8, 18, 10, 42)
    assert R.experiment_run_name(model="google/gemini-2.5-flash", when=when, label="openrouter-gemini") == (
        "gemini-2.5-flash · 2026-08-18 10:42"
    )
    # a live-harness label that already has a timestamp suffix + model echo
    assert R.experiment_run_name(
        model="google/gemini-2.5-flash", when=when, label="openrouter-gemini-20260902T215142"
    ) == "gemini-2.5-flash · 2026-08-18 10:42"
    # a label that carries real extra context is kept
    assert R.experiment_run_name(
        model="google/gemini-3.7-flash", when=when, label="context-ab-gemini-3.7-flash-whole_document-full"
    ) == "gemini-3.7-flash (whole_document-full) · 2026-08-18 10:42"
    assert R.experiment_run_name(model="qwen/qwen3-vl-235b-a22b-instruct", when=when, label="openrouter-qwen3-vl-235b") == (
        "qwen3-vl-235b-a22b-instruct · 2026-08-18 10:42"
    )


def test_evaluation_builder_omits_none_kwargs() -> None:
    ev = R.evaluation("x", 1.0, data_type="NUMERIC")
    assert ev.name == "x" and ev.value == 1.0
