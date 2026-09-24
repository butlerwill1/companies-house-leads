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


def test_ensure_score_configs_reconciles_a_drifted_category_list() -> None:
    client = FakeLangfuse()
    A.ensure_score_configs(client, A.question_score_configs([{"name": "demand_model", "categories": ["b2b", "b2c", "unclear"]}]))
    # taxonomy has since grown
    new_cats = ["consumer_search", "local_service", "b2b_relationship", "unclear"]
    ids = A.ensure_score_configs(client, A.question_score_configs([{"name": "demand_model", "categories": new_cats}]))

    assert len(client.score_configs._items) == 1  # updated in place, not duplicated
    cfg = client.score_configs._items[0]
    assert cfg.id == ids["demand_model"]
    assert [c["label"] for c in cfg.categories] == new_cats


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


def test_find_queue_id_and_completed_trace_ids() -> None:
    client = FakeLangfuse()
    ids = A.ensure_score_configs(client, A.question_score_configs([{"name": "q1"}]))
    qid = A.ensure_queue(client, "review", list(ids.values()))
    assert A.find_queue_id(client, "review") == qid
    assert A.find_queue_id(client, "nope") is None

    A.sync_queue_items(client, qid, ["tr-a", "tr-b"], complete=["tr-a"])
    assert A.completed_trace_ids(client, qid) == {"tr-a"}


def test_seed_and_read_annotations_takes_the_newest_value() -> None:
    client = FakeLangfuse()
    ids = A.ensure_score_configs(client, A.question_score_configs([
        {"name": "demand_model", "categories": ["b2b", "b2c"]},
        {"name": "business_description"},
    ]))
    A.seed_draft_scores(client, "tr-1", {"demand_model": "b2b", "business_description": "widgets"}, ids)
    # a reviewer later corrects demand_model
    client.scores.append(SimpleNamespace(name="demand_model", value="b2c", string_value=None,
                                         data_type="CATEGORICAL", source="ANNOTATION", comment=None,
                                         config_id=ids["demand_model"], trace_id="tr-1", timestamp=9_999))

    out = A.read_annotations(client, "tr-1", ["demand_model", "business_description"])
    assert out == {"demand_model": "b2c", "business_description": "widgets"}


def test_seed_draft_scores_skips_none_and_unconfigured() -> None:
    client = FakeLangfuse()
    ids = A.ensure_score_configs(client, A.question_score_configs([{"name": "b"}, {"name": "c"}]))
    A.seed_draft_scores(client, "tr-1", {"a": "no-config", "b": None, "c": "x"}, ids)
    assert [s.name for s in client.scores] == ["c"]  # a: no config_id, b: None
    assert client.scores[0].comment == A.DRAFT_COMMENT
    assert client.scores[0].source == "ANNOTATION"  # so Langfuse pre-fills the form


def test_seed_draft_scores_never_overwrites_an_existing_score() -> None:
    """The annotate panel edits the seeded score in place, so a re-sync that
    re-seeds from disk would put the draft back over the reviewer's answer.
    That happened on 2026-09-09; a second sync must leave every existing
    score untouched, whether the on-disk draft changed or not."""
    client = FakeLangfuse()
    ids = A.ensure_score_configs(client, A.question_score_configs([
        {"name": "demand_model", "categories": ["b2b", "b2c"]},
        {"name": "customer_type", "categories": ["b2b", "b2c"]},
    ]))
    A.seed_draft_scores(client, "tr-1", {"demand_model": "b2b"}, ids)
    # the reviewer corrects it in the UI: same score id, new value
    client.scores[0].value = "b2c"
    # re-run with a changed on-disk draft, plus a field that was None before
    A.seed_draft_scores(client, "tr-1", {"demand_model": "b2b", "customer_type": "b2b"}, ids)
    by_name = {s.name: s for s in client.scores if s.trace_id == "tr-1"}
    assert len(by_name) == 2
    assert by_name["demand_model"].value == "b2c"  # the human's answer survived
    assert by_name["customer_type"].value == "b2b"  # the newly-answerable field was seeded
    # a different trace still gets its own draft
    A.seed_draft_scores(client, "tr-2", {"demand_model": "b2b"}, ids)
    assert len(client.scores) == 3


def _human_score(name: str, value: str, trace_id: str, ts: int) -> SimpleNamespace:
    return SimpleNamespace(name=name, value=value, string_value=None, data_type="CATEGORICAL",
                           source="ANNOTATION", comment=None, config_id=None,
                           trace_id=trace_id, timestamp=ts)


def test_read_annotations_keeps_the_latest_answer() -> None:
    client = FakeLangfuse()
    # reviewer answers, then reopens the item and corrects it (newer timestamp)
    client.scores.append(_human_score("sic_agreement", "agree", "tr-1", ts=1_000))
    client.scores.append(_human_score("sic_agreement", "partial", "tr-1", ts=2_000))
    # order in the API response must not matter
    client.scores.reverse()

    out = A.read_annotations(client, "tr-1", ["sic_agreement"])
    assert out["sic_agreement"] == "partial"


def test_read_annotations_paginates() -> None:
    client = FakeLangfuse()
    for i in range(150):
        client.scores.append(SimpleNamespace(name="q", value=f"v{i}", string_value=None,
                                             data_type="TEXT", source="API", comment=None,
                                             config_id=None, trace_id="tr-1", timestamp=i))
    client.scores.append(_human_score("q", "final", "tr-1", ts=9_999))
    out = A.read_annotations(client, "tr-1", ["q"])
    assert out["q"] == "final"


def test_migrate_retired_scores_rewrites_only_the_retired_value_in_place() -> None:
    from types import SimpleNamespace
    from scripts.eval_support.langfuse_annotation import migrate_retired_scores
    from tests.langfuse_fakes import FakeLangfuse

    lf = FakeLangfuse()
    for name, value in (("trading_status_confirmed", "trading_group_parent"), ("customer_type", "b2b")):
        lf.scores.append(SimpleNamespace(id=f"draft-{name}", name=name, value=value, string_value=None,
                                         data_type="CATEGORICAL", source="ANNOTATION", comment="draft",
                                         config_id=f"cfg-{name}", trace_id="t1", timestamp=1, score_id=f"draft-{name}"))

    touched = migrate_retired_scores(lf, "t1", {"trading_status_confirmed": {"trading_group_parent": "trading"}}, {})

    assert touched == ["trading_status_confirmed"]
    by_name = {s.name: s for s in lf.scores if s.trace_id == "t1"}
    assert by_name["trading_status_confirmed"].value == "trading"
    assert by_name["trading_status_confirmed"].score_id == "draft-trading_status_confirmed"  # same row, edited in place
    assert "taxonomy migration" in by_name["trading_status_confirmed"].comment
    assert by_name["customer_type"].value == "b2b"  # untouched
    # idempotent
    assert migrate_retired_scores(lf, "t1", {"trading_status_confirmed": {"trading_group_parent": "trading"}}, {}) == []
