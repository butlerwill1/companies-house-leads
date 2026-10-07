"""The Langfuse-backed `run` and annotation paths of business_profile_eval,
exercised against the in-memory FakeLangfuse."""
from __future__ import annotations

import json

import pytest

from scripts.langfuse_eval_helpers import langfuse_annotation as A
from scripts.business_profile_classifier import business_profile_eval as E
from tests.langfuse_fakes import FakeLangfuse

VALID = json.dumps({
    "business_description": "A community football club.",
    "demand_model": {"quote": "football club", "section": "principal_activity", "reason": "No customer channel is described.", "value": "not_customer_facing", "confidence": 0.9},
    "customer_type": {"quote": "football club", "section": "principal_activity", "reason": "Supporters are individuals.", "value": "b2c", "confidence": 0.8},
    "delivery_model": {"quote": "football club", "section": "principal_activity", "reason": "Running a club is people-delivered.", "value": "professional_service", "confidence": 0.6},
    "geography_served": {"quote": "football club", "section": "principal_activity", "reason": "A community club serves its area.", "value": "local", "confidence": 0.7},
    "trading_status_confirmed": {"quote": "football club", "section": "principal_activity", "reason": "The company operates the club itself.", "value": "trading", "confidence": 0.85},
    "sic_agreement": {"quote": "football club", "section": "principal_activity", "reason": "Matches sports facility SIC.", "value": "agrees"},
})


class _FakeBPClient:
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, model: str, prompt: str, timeout: int) -> str:
        return self.text


def _case(company_number: str = "00482197") -> dict:
    return {
        "company_number": company_number,
        "company_name": "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED",
        "financial_year": 2023,
        "sic_code": "93110",
        "sic_label": "Sports facility operation",
        "sections": {"principal_activity": "football club"},
        "expected": {
            "business_description": "A football club.",
            "demand_model": {"value": "not_customer_facing"},
            "customer_type": {"value": "b2c"},
            "delivery_model": {"value": "professional_service"},
            "geography_served": {"value": "local"},
            "trading_status_confirmed": {"value": "trading"},
            "sic_agreement": {"value": "agrees"},
        },
        "review": {"status": "verified"},
    }


def test_run_one_case_scores_a_valid_extraction() -> None:
    outcome = E._run_one_case(_FakeBPClient(VALID), "m", 30, _case())
    assert outcome["extracted"]["demand_model"]["value"] == "not_customer_facing"
    assert outcome["scored"]["fields"]["demand_model"]["correct"] is True
    assert outcome["errors"] == []


def test_run_one_case_never_raises_on_request_failure() -> None:
    class _Boom:
        def generate(self, *a, **k):
            raise RuntimeError("network down")

    outcome = E._run_one_case(_Boom(), "m", 30, _case())
    assert outcome["extracted"] is None
    assert "network down" in outcome["errors"][0]
    assert outcome["prompt"]  # rebuilt for the trace


def test_score_langfuse_runs_experiment_and_attaches_scores(monkeypatch) -> None:
    monkeypatch.setattr(E.deepeval_judges, "judge_enabled", lambda cfg: False)
    lf = FakeLangfuse()
    cases = [_case("00000001"), _case("00000002")]
    outcomes = E._score_langfuse(lf, {"langfuse": {}}, _FakeBPClient(VALID), "m", 30, cases, cases, "run-x")
    assert len(outcomes) == 2
    field_scores = [s for s in lf.scores if s.name == "field.demand_model"]
    assert field_scores and all(s.value == 1.0 for s in field_scores)
    assert any(s.name == "mean_field_accuracy" for s in lf.scores)  # aggregate
    assert "business-profile-gold" in lf.datasets_store


def test_run_checkpoint_replays_a_saved_outcome_without_calling_model(tmp_path, monkeypatch) -> None:
    checkpoint = tmp_path / "v10.jsonl"
    first = E._run_one_case(_FakeBPClient(VALID), "m", 30, _case())
    E._append_run_checkpoint(checkpoint, "m", 30, first)
    saved = E._load_run_checkpoint(checkpoint, "m", 30, [_case()])
    assert saved["00482197"]["extracted"]["demand_model"]["value"] == "not_customer_facing"

    class _NoCall:
        def generate(self, *args, **kwargs):
            raise AssertionError("checkpoint replay must not call the model")

    monkeypatch.setattr(E.deepeval_judges, "judge_enabled", lambda cfg: False)
    replayed = E._score_langfuse(
        FakeLangfuse(), {"langfuse": {}}, _NoCall(), "m", 30,
        [_case()], [_case()], "checkpoint-replay", checkpoint,
    )
    assert len(replayed) == 1
    assert replayed[0]["raw"] == first["raw"]


def test_run_checkpoint_refuses_a_changed_frozen_case(tmp_path) -> None:
    checkpoint = tmp_path / "v10.jsonl"
    first = E._run_one_case(_FakeBPClient(VALID), "m", 30, _case())
    E._append_run_checkpoint(checkpoint, "m", 30, first)
    changed = _case()
    changed["expected"]["demand_model"] = {"value": "unclear"}

    with pytest.raises(ValueError, match="cannot be reused"):
        E._load_run_checkpoint(checkpoint, "m", 30, [changed])


def test_sync_and_export_annotations_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(E, "ANNOTATION_TRACE_MAP", tmp_path / "traces.json")
    monkeypatch.setattr(E, "load_config", lambda p: {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}})
    monkeypatch.setattr(E, "load_dotenv", lambda p: None)
    lf = FakeLangfuse()
    monkeypatch.setattr(E, "langfuse_from_config", lambda cfg: lf)

    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    case = _case()
    case["review"] = {"status": "unreviewed"}
    (cases_dir / "00482197.json").write_text(json.dumps(case), encoding="utf-8")

    import contextlib
    monkeypatch.setattr("langfuse.propagate_attributes", lambda **k: contextlib.nullcontext(), raising=False)

    args = type("A", (), {"config": "x", "cases_dir": str(cases_dir), "queue_name": E.ANNOTATION_QUEUE_NAME})()
    assert E.sync_annotation_queue(args) == 0
    trace_map = json.loads((tmp_path / "traces.json").read_text())
    assert trace_map["00482197"]["name"] == "00482197 CAMBRIDGE UNITED FOOTBALL CLUB LIMITED (gold review)"
    assert lf.observations[-1]["name"] == trace_map["00482197"]["name"]

    # a reviewer confirms every field in Langfuse (source ANNOTATION)
    from types import SimpleNamespace
    tid = trace_map["00482197"]["trace_id"]
    for name in E._review_field_names():
        value = "A club." if name == "business_description" else _case()["expected"][name]["value"]
        lf.scores.append(SimpleNamespace(name=name, value=value, string_value=None, data_type="CATEGORICAL",
                                         source="ANNOTATION", comment=None, config_id=None, trace_id=tid,
                                         timestamp=1_000_000))  # newer than the seeded draft

    # ...but does not sign the item off yet -> export imports nothing
    assert E.export_annotations(args) == 0
    updated = json.loads((cases_dir / "00482197.json").read_text())
    assert updated["review"]["status"] == "unreviewed"
    assert updated["expected"]["business_description"] == _case()["expected"]["business_description"]

    # reviewer hits Complete on the queue item -> case imports and verifies
    queue_id = A.find_queue_id(lf, E.ANNOTATION_QUEUE_NAME)
    item_id = lf.annotation_queues.items[queue_id][0].id
    lf.annotation_queues.update_queue_item(queue_id, item_id, status="COMPLETED")
    assert E.export_annotations(args) == 0
    updated = json.loads((cases_dir / "00482197.json").read_text())
    assert updated["review"]["status"] == "verified"

    # the reviewer later changes one field in Langfuse: a plain export leaves
    # the verified case alone, --corrections applies just that change
    for score in lf.scores:
        if score.trace_id == tid and score.name == "delivery_model" and score.source == "ANNOTATION":
            score.value = "contracting"
            score.timestamp = 2_000_000
    assert E.export_annotations(args) == 0
    assert json.loads((cases_dir / "00482197.json").read_text())["expected"]["delivery_model"]["value"] != "contracting"
    corrections = type("A", (), {"config": "x", "cases_dir": str(cases_dir), "queue_name": E.ANNOTATION_QUEUE_NAME, "corrections": True})()
    assert E.export_annotations(corrections) == 0
    corrected = json.loads((cases_dir / "00482197.json").read_text())
    assert corrected["expected"]["delivery_model"]["value"] == "contracting"
    assert "corrected_at" in corrected["review"]
    assert "delivery_model" in corrected["review"]["changed_fields"]
    assert updated["expected"]["business_description"] == "A club."


def _drafted_case() -> dict:
    case = _case()
    case["expected"]["demand_model"] = {
        "value": "b2b_relationship", "quote": "long established relationships",
        "section": "filed_report", "confidence": 0.45,
    }
    case["expected"]["customer_type"] = {
        "value": "b2b", "quote": "customers and suppliers", "section": "filed_report", "confidence": 0.5,
    }
    case["expected"]["sic_agreement"] = {"value": "agrees", "reason": "auctions are sales"}
    case["review"] = {"status": "drafted", "reviewed_at": None, "reviewer": "google/gemini-3.7-flash (model draft)"}
    return case


def test_apply_annotations_keeps_the_draft_and_records_what_changed() -> None:
    case = _drafted_case()
    original = json.loads(json.dumps(case["expected"]))
    answers = {name: (
        "An auction house." if name == "business_description"
        else "unclear" if name == "demand_model"
        else "disagrees" if name == "sic_agreement"
        else original[name]["value"]
    ) for name in E._review_field_names()}

    E.apply_annotations(case, answers, E._review_field_names())

    # the model's draft is kept verbatim, with who drafted it
    assert case["draft"]["expected"] == original
    assert case["draft"]["drafted_by"].startswith("google/gemini-3.7-flash")
    # a field the reviewer kept carries the draft's evidence through
    assert case["expected"]["customer_type"] == original["customer_type"]
    # a field the reviewer changed carries the new value and no stale evidence
    assert case["expected"]["demand_model"] == {"value": "unclear", "quote": None, "section": None, "confidence": None}
    assert case["expected"]["sic_agreement"] == {"value": "disagrees", "reason": None}
    assert case["expected"]["business_description"] == "An auction house."
    assert case["review"]["status"] == "verified"
    assert case["review"]["changed_fields"] == ["business_description", "demand_model", "sic_agreement"]


def test_apply_annotations_unchanged_review_is_an_empty_change_list() -> None:
    case = _drafted_case()
    answers = {name: (case["expected"][name] if name == "business_description" else case["expected"][name]["value"])
               for name in E._review_field_names()}
    E.apply_annotations(case, answers, E._review_field_names())
    assert case["review"]["changed_fields"] == []
    assert case["expected"] == case["draft"]["expected"]


def test_migrate_retired_gold_labels_keeps_draft_and_is_idempotent(tmp_path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    case = _drafted_case()
    case["review"] = {"status": "verified", "reviewed_at": "2026-09-24T00:00:00+00:00"}
    draft = json.loads(json.dumps(case["expected"]))
    case["draft"] = {"expected": draft, "drafted_by": "drafting-model"}
    (cases_dir / "00482197.json").write_text(json.dumps(case), encoding="utf-8")

    migrated = E.migrate_retired_gold_labels(cases_dir)

    updated = json.loads((cases_dir / "00482197.json").read_text())
    assert migrated == {"cases": 1, "fields": 1}
    assert updated["expected"]["demand_model"]["value"] == "relationship_or_contract"
    assert updated["draft"]["expected"] == draft  # migration never rewrites the historic model draft
    record = updated["review"]["taxonomy_migrations"][-1]
    assert record["field"] == "demand_model"
    assert record["from"] == "b2b_relationship"
    assert record["to"] == "relationship_or_contract"
    assert E.migrate_retired_gold_labels(cases_dir) == {"cases": 0, "fields": 0}
    unchanged = json.loads((cases_dir / "00482197.json").read_text())
    assert unchanged["review"]["taxonomy_migrations"] == updated["review"]["taxonomy_migrations"]


def test_apply_annotations_on_a_verified_case_records_a_correction() -> None:
    """Booth Welsh Nexus (2026-09-14): a verified case whose delivery_model
    the reviewer later decided was wrong. The correction lands on top of the
    first review -- draft untouched, changed_fields grown, reviewed_at kept,
    corrected_at added -- rather than as a second, fresh review."""
    case = _drafted_case()
    field_names = E._review_field_names()
    first = {name: (
        "unclear" if name == "demand_model"
        else case["expected"][name] if name == "business_description"
        else case["expected"][name]["value"]
    ) for name in field_names}
    E.apply_annotations(case, first, field_names)
    draft = json.loads(json.dumps(case["draft"]))
    reviewed_at = case["review"]["reviewed_at"]
    assert case["review"]["changed_fields"] == ["demand_model"]

    assert not E._annotations_differ(case, first, field_names)
    corrected = {**first, "delivery_model": "contracting"}
    assert E._annotations_differ(case, corrected, field_names)
    E.apply_annotations(case, corrected, field_names)

    assert case["draft"] == draft
    assert case["expected"]["delivery_model"] == {"value": "contracting", "quote": None, "section": None, "confidence": None}
    assert case["expected"]["demand_model"]["value"] == "unclear"
    assert case["review"]["status"] == "verified"
    assert case["review"]["reviewed_at"] == reviewed_at
    assert case["review"]["corrected_at"] is not None
    assert case["review"]["changed_fields"] == ["delivery_model", "demand_model"]


def test_apply_annotations_does_not_invent_a_draft_for_an_unreviewed_stub() -> None:
    case = _case()
    case["review"] = {"status": "unreviewed"}
    answers = {name: (case["expected"][name] if name == "business_description" else case["expected"][name]["value"])
               for name in E._review_field_names()}
    E.apply_annotations(case, answers, E._review_field_names())
    assert "draft" not in case  # nothing modelled it; there is no draft to keep
    assert case["review"]["status"] == "verified"


def test_write_responses_records_verdict_score_raw_and_prompt(tmp_path) -> None:
    case = _case()
    accepted = {
        "case": case, "extracted": {"x": 1}, "errors": [], "prompt": "PROMPT TEXT", "raw": '{"a": 1}',
        "scored": {"company_number": case["company_number"], "fields": {"demand_model": {"expected": "b2c", "actual": "b2c", "correct": True}}},
    }
    rejected = {
        "case": {**case, "company_number": "00000002"}, "extracted": None,
        "errors": ["demand_model.quote does not appear verbatim in the filed document: 'x'"],
        "prompt": "PROMPT TEXT", "raw": '{"b": 2}', "scored": {"fields": {}},
    }
    out = E.write_responses([accepted, rejected], tmp_path / "responses", model="m")
    ok = (out / "00482197.md").read_text(encoding="utf-8")
    assert "accepted" in ok and '{"a": 1}' in ok and "PROMPT TEXT" in ok
    assert "| demand_model | b2c | b2c | yes |" in ok
    bad = (out / "00000002.md").read_text(encoding="utf-8")
    assert bad.count("REJECTED (no usable JSON): demand_model.quote") == 1 and '{"b": 2}' in bad


def test_rescore_reads_saved_responses_and_scores_per_field(tmp_path, monkeypatch) -> None:
    """A saved run can be scored again under the current rules with no model
    call: the failing field is dropped, the rest of the response scores."""
    case = _case()
    case["sections"] = {"filed_report": "The club plays football in Cambridge for local supporters."}
    for field in ("demand_model", "customer_type", "delivery_model", "geography_served", "trading_status_confirmed"):
        case["expected"][field].update({"quote": None, "section": None})
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "00482197.json").write_text(json.dumps(case), encoding="utf-8")

    def field(value: str, quote: str) -> dict:
        return {"quote": quote, "section": "filed_report", "reason": "because", "value": value, "confidence": 0.9}

    response = {
        "business_description": "A football club.",
        "demand_model": field("not_customer_facing", "plays football in Cambridge"),
        "customer_type": field("b2c", "this sentence is not in the filing"),
        "delivery_model": field("professional_service", "for local supporters"),
        "geography_served": field("local", "in Cambridge"),
        "trading_status_confirmed": field("trading", "The club plays football"),
        "sic_agreement": {"quote": "plays football", "section": "filed_report", "reason": "r", "value": "agrees"},
    }
    responses_dir = tmp_path / "responses-x"
    responses_dir.mkdir()
    (responses_dir / "00482197.md").write_text(
        "# CAMBRIDGE (00482197) -- some/model @ v7\n\n## Model response (verbatim)\n\n```json\n"
        + json.dumps(response, indent=1) + "\n```\n\n## Prompt sent (verbatim)\n\n```text\nP\n```\n",
        encoding="utf-8",
    )
    args = type("A", (), {"responses_dir": str(responses_dir), "cases_dir": str(cases_dir),
                          "output_dir": str(tmp_path / "out"), "model": None, "config": None})()
    assert E.rescore_responses(args) == 0
    report = json.loads(next((tmp_path / "out").glob("report-*-rescore.json")).read_text(encoding="utf-8"))
    assert report["model"] == "some/model"
    assert report["responses_rejected_outright"] == 0
    assert report["responses_with_dropped_fields"] == 1 and report["fields_rejected"] == 1
    fields = report["results"][0]["fields"]
    assert fields["customer_type"]["actual"] is None  # dropped: quote not in the filing
    assert fields["demand_model"]["correct"] and fields["geography_served"]["correct"]


def test_sync_restates_a_review_trace_whose_name_or_content_is_stale(monkeypatch, tmp_path) -> None:
    """The 109 traces created before 2026-09-14 are all named
    ``business_profile_review``; the map holds a bare trace id for them. A
    sync renames each in place -- one span into the existing trace -- rather
    than creating a second trace that would orphan the reviewer's scores."""
    monkeypatch.setattr(E, "ANNOTATION_TRACE_MAP", tmp_path / "traces.json")
    monkeypatch.setattr(E, "load_config", lambda p: {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}})
    monkeypatch.setattr(E, "load_dotenv", lambda p: None)
    lf = FakeLangfuse()
    monkeypatch.setattr(E, "langfuse_from_config", lambda cfg: lf)
    import contextlib
    monkeypatch.setattr("langfuse.propagate_attributes", lambda **k: contextlib.nullcontext(), raising=False)
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "00482197.json").write_text(json.dumps(_case()), encoding="utf-8")
    (tmp_path / "traces.json").write_text(json.dumps({"00482197": "old-trace-id"}), encoding="utf-8")

    args = type("A", (), {"config": "x", "cases_dir": str(cases_dir), "queue_name": E.ANNOTATION_QUEUE_NAME})()
    assert E.sync_annotation_queue(args) == 0

    renames = [o for o in lf.observations if o.get("name") == "identity"]
    assert len(renames) == 1
    assert renames[0]["trace_context"] == {"trace_id": "old-trace-id"}
    # the restating span carries the case's current input and output: a
    # bare rename would become the trace's newest root event and blank them
    assert renames[0]["input"] == E._case_trace_inputs(_case())
    assert renames[0]["output"] == E._case_trace_outputs(_case())
    saved = json.loads((tmp_path / "traces.json").read_text())
    assert saved["00482197"]["trace_id"] == "old-trace-id"
    assert saved["00482197"]["name"] == "00482197 CAMBRIDGE UNITED FOOTBALL CLUB LIMITED (gold review)"
    assert saved["00482197"]["content"]
    # no second trace was opened for the case
    assert not [o for o in lf.observations if o.get("name", "").endswith("(gold review)")]

    # a second sync is a no-op: name and content are already recorded
    assert E.sync_annotation_queue(args) == 0
    assert len([o for o in lf.observations if o.get("name") == "identity"]) == 1

    # ...until the case changes (here: its text), which restates the trace
    changed = _case()
    changed["sections"] = {"filed_report": "A different, whole-document text."}
    (cases_dir / "00482197.json").write_text(json.dumps(changed), encoding="utf-8")
    assert E.sync_annotation_queue(args) == 0
    renames = [o for o in lf.observations if o.get("name") == "identity"]
    assert len(renames) == 2
    assert renames[-1]["input"] == E._case_trace_inputs(changed)
