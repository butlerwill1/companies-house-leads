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


def test_seed_draft_scores_is_idempotent() -> None:
    client = FakeLangfuse()
    A.seed_draft_scores(client, "tr-1", {"demand_model": "b2b"}, {})
    A.seed_draft_scores(client, "tr-1", {"demand_model": "b2c"}, {})  # re-run, changed draft
    drafts = [s for s in client.scores if s.name == "demand_model"]
    assert len(drafts) == 1
    assert drafts[0].value == "b2c"
    # a different trace keeps its own draft
    A.seed_draft_scores(client, "tr-2", {"demand_model": "b2b"}, {})
    assert len(client.scores) == 2


def _human_score(name: str, value: str, trace_id: str, ts: int) -> SimpleNamespace:
    return SimpleNamespace(name=name, value=value, string_value=None, data_type="CATEGORICAL",
                           source="ANNOTATION", comment=None, config_id=None,
                           trace_id=trace_id, timestamp=ts)


def test_read_annotations_keeps_the_latest_human_answer() -> None:
    client = FakeLangfuse()
    A.seed_draft_scores(client, "tr-1", {"sic_agreement": "agree"}, {})
    # reviewer answers, then reopens the item and corrects it (newer timestamp)
    client.scores.append(_human_score("sic_agreement", "agree", "tr-1", ts=1_000))
    client.scores.append(_human_score("sic_agreement", "partial", "tr-1", ts=2_000))
    # order in the API response must not matter
    client.scores.reverse()

    out = A.read_annotations(client, "tr-1", ["sic_agreement"])
    assert out["sic_agreement"] == {"value": "partial", "human": True}


def test_read_annotations_paginates() -> None:
    client = FakeLangfuse()
    for i in range(150):
        client.scores.append(SimpleNamespace(name="q", value=f"v{i}", string_value=None,
                                             data_type="TEXT", source="API", comment=None,
                                             config_id=None, trace_id="tr-1", timestamp=i))
    client.scores.append(_human_score("q", "final", "tr-1", ts=9_999))
    out = A.read_annotations(client, "tr-1", ["q"])
    assert out["q"] == {"value": "final", "human": True}
