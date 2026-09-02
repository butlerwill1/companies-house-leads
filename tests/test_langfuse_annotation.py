from __future__ import annotations

from types import SimpleNamespace

from scripts.eval_support import langfuse_annotation as A
from tests.langfuse_fakes import FakeLangfuse


def test_question_score_configs_infers_type() -> None:
    configs = A.question_score_configs([
        {"name": "demand_model", "categories": ["b2b", "b2c"]},
        {"name": "business_description"},
    ])
    by_name = {c["name"]: c for c in configs}
    assert by_name["demand_model"]["data_type"] == "CATEGORICAL"
    assert by_name["business_description"]["data_type"] == "TEXT"
    assert by_name["business_description"]["categories"] is None


def test_ensure_score_configs_is_get_or_create() -> None:
    client = FakeLangfuse()
    specs = A.question_score_configs([{"name": "q1", "categories": ["a", "b"]}, {"name": "q2"}])
    first = A.ensure_score_configs(client, specs)
    second = A.ensure_score_configs(client, specs)
    assert first == second
    assert len(client.score_configs._items) == 2


def test_ensure_queue_and_sync_items() -> None:
    client = FakeLangfuse()
    ids = A.ensure_score_configs(client, A.question_score_configs([{"name": "q1"}]))
    qid = A.ensure_queue(client, "review", list(ids.values()))
    assert A.ensure_queue(client, "review", list(ids.values())) == qid  # get-or-create

    res = A.sync_queue_items(client, qid, ["tr-a", "tr-b"], complete=["tr-a"])
    assert res == {"added": 2, "removed": 0, "completed": 1}
    # drop tr-b, keep tr-a
    res = A.sync_queue_items(client, qid, ["tr-a"])
    assert res["removed"] == 1
    items = client.annotation_queues.items[qid]
    assert [i.object_id for i in items] == ["tr-a"]
    assert items[0].status == "COMPLETED"


def test_seed_and_read_annotations_distinguishes_human() -> None:
    client = FakeLangfuse()
    ids = A.ensure_score_configs(client, A.question_score_configs([
        {"name": "demand_model", "categories": ["b2b", "b2c"]},
        {"name": "business_description"},
    ]))
    A.seed_draft_scores(client, "tr-1", {"demand_model": "b2b", "business_description": "widgets"}, ids)
    # a human later annotates demand_model differently
    client.scores.append(SimpleNamespace(name="demand_model", value="b2c", string_value=None,
                                         data_type="CATEGORICAL", source="ANNOTATION", comment=None,
                                         config_id=ids["demand_model"], trace_id="tr-1"))

    out = A.read_annotations(client, "tr-1", ["demand_model", "business_description"])
    assert out["demand_model"] == {"value": "b2c", "human": True}
    assert out["business_description"] == {"value": "widgets", "human": False}


def test_seed_draft_scores_skips_none() -> None:
    client = FakeLangfuse()
    A.seed_draft_scores(client, "tr-1", {"a": None, "b": "x"}, {})
    assert [s.name for s in client.scores] == ["b"]
    assert client.scores[0].comment == A.DRAFT_COMMENT
