from __future__ import annotations

import pytest

from scripts.eval_support import langfuse_runs as R
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


def test_evaluation_builder_omits_none_kwargs() -> None:
    ev = R.evaluation("x", 1.0, data_type="NUMERIC")
    assert ev.name == "x" and ev.value == 1.0
