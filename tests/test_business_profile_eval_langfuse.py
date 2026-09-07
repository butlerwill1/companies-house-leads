"""The Langfuse-backed `run` and annotation paths of business_profile_eval,
exercised against the in-memory FakeLangfuse."""
from __future__ import annotations

import json

import pytest

from scripts.eval_support import langfuse_annotation as A
from scripts.profile import business_profile_eval as E
from tests.langfuse_fakes import FakeLangfuse

VALID = json.dumps({
    "business_description": "A community football club.",
    "demand_model": {"value": "not_customer_facing", "confidence": 0.9, "quote": "football club", "section": "principal_activity"},
    "customer_type": {"value": "b2c", "confidence": 0.8, "quote": "football club", "section": "principal_activity"},
    "delivery_model": {"value": "professional_service", "confidence": 0.6, "quote": "football club", "section": "principal_activity"},
    "geography_served": {"value": "local", "confidence": 0.7, "quote": "football club", "section": "principal_activity"},
    "trading_status_confirmed": {"value": "trading", "confidence": 0.85, "quote": "football club", "section": "principal_activity"},
    "sic_agreement": {"value": "agrees", "reason": "Matches sports facility SIC."},
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
    assert "00482197" in trace_map

    # a reviewer confirms every field in Langfuse (source ANNOTATION)
    from types import SimpleNamespace
    tid = trace_map["00482197"]
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
    assert updated["expected"]["business_description"] == "A club."
