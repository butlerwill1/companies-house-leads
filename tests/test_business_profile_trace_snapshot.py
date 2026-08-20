from __future__ import annotations

import json

import mlflow

from scripts.profile.business_profile_eval import (
    _TRACE_TAG_VALUE_LIMIT,
    _case_trace_inputs,
    _case_trace_outputs,
    _narrative_preview,
    _refresh_trace_snapshot,
    _size_bounded_case_trace_inputs,
)


def _case() -> dict:
    return {
        "company_number": "SC758233",
        "company_name": "AMIRY & GILBRIDE HEALTHCARE LIMITED",
        "sic_code": "47730",
        "sic_label": "Specialist retail",
        "sections": {
            "principal_activity": "that of dispensing chemists",
            "turnover_note": "Pharmacy sales 13,391,763",
        },
        "expected": {"business_description": "Holding company for a pharmacy group."},
    }


def test_case_trace_inputs_includes_every_section_the_case_carries() -> None:
    inputs = _case_trace_inputs(_case())
    assert inputs["company_number"] == "SC758233"
    assert "[principal_activity]" in inputs["narrative_sections"]
    assert "[turnover_note]" in inputs["narrative_sections"]
    assert "Pharmacy sales 13,391,763" in inputs["narrative_sections"]


def test_case_trace_outputs_wraps_the_current_expected_block() -> None:
    outputs = _case_trace_outputs(_case())
    assert outputs == {"draft_expected": _case()["expected"]}


def test_narrative_preview_reflects_sections_added_after_a_trace_already_exists() -> None:
    case = _case()
    before = _narrative_preview({**case, "sections": {"principal_activity": case["sections"]["principal_activity"]}})
    assert "turnover_note" not in before
    after = _narrative_preview(case)
    assert "turnover_note" in after


def test_narrative_preview_keeps_high_priority_sections_and_names_what_it_drops() -> None:
    case = {
        "sections": {
            "directors_report": "boilerplate " * 200,
            "turnover_note": "Pharmacy sales 13,391,763",
            "employee_note": "Total 139",
        }
    }
    preview = _narrative_preview(case, max_chars=260)
    assert "[turnover_note]" in preview
    assert "13,391,763" in preview
    assert "[directors_report]" not in preview
    assert "omitted for length:" in preview
    assert "directors_report" in preview.split("omitted for length:")[1]


def test_narrative_preview_keeps_a_cited_evidence_section_over_generic_priority() -> None:
    # geography_served cites strategic_report -- it must survive even though
    # turnover_note/employee_note otherwise rank first, and even though
    # strategic_report is not in NARRATIVE_SECTION_PRIORITY's front ranks.
    case = {
        "sections": {
            "turnover_note": "t " * 100,
            "employee_note": "e " * 100,
            "principal_activity": "p " * 100,
            "strategic_report": "outperforming national market share growth in Scotland",
        },
        "expected": {
            "geography_served": {"value": "regional", "section": "strategic_report"},
        },
    }
    preview = _narrative_preview(case, max_chars=290)
    assert "[strategic_report]" in preview
    assert "outperforming national market share growth in Scotland" in preview


def test_narrative_preview_truncates_rather_than_drops_an_oversized_cited_section() -> None:
    # strategic_report alone is too big to fit whole, but it is cited --
    # this is the real SC758233 shape: a huge cited section competing with
    # smaller cited sections for a tight budget. It must appear as a
    # truncated excerpt, not vanish from "omitted for length".
    case = {
        "sections": {
            "principal_activity": "p " * 20,
            "turnover_note": "t " * 20,
            "strategic_report": "s " * 2000,
        },
        "expected": {
            "customer_type": {"value": "b2c", "section": "principal_activity"},
            "delivery_model": {"value": "product_physical", "section": "turnover_note"},
            "geography_served": {"value": "regional", "section": "strategic_report"},
        },
    }
    preview = _narrative_preview(case, max_chars=700)
    assert "[strategic_report]" in preview
    assert "chars total]" in preview
    assert "omitted for length" not in preview
    assert "[principal_activity]" in preview
    assert "[turnover_note]" in preview


def test_narrative_preview_drops_a_cited_section_only_when_no_room_for_a_useful_excerpt() -> None:
    case = {
        "sections": {"strategic_report": "s " * 2000},
        "expected": {"geography_served": {"value": "regional", "section": "strategic_report"}},
    }
    preview = _narrative_preview(case, max_chars=10)
    assert "omitted for length: strategic_report" in preview
    assert "[strategic_report]" not in preview


def test_size_bounded_case_trace_inputs_fits_the_trace_tag_limit_even_with_huge_sections() -> None:
    case = {
        "company_number": "SC758233",
        "company_name": "AMIRY & GILBRIDE HEALTHCARE LIMITED",
        "sic_code": "47730",
        "sic_label": "Specialist retail",
        "sections": {
            "turnover_note": "Pharmacy sales 13,391,763",
            "employee_note": "Total 139",
            "directors_report": "x" * 3000,
            "principal_activity": "y" * 3000,
            "strategic_report": "z" * 3000,
        },
    }
    inputs = _size_bounded_case_trace_inputs(case)
    encoded_len = len(json.dumps(inputs, ensure_ascii=False))
    assert encoded_len <= _TRACE_TAG_VALUE_LIMIT
    # The highest-priority evidence must survive the shrink.
    assert "13,391,763" in inputs["narrative_sections"]


def test_size_bounded_case_trace_inputs_leaves_small_cases_untouched() -> None:
    case = {
        "company_number": "SC758233",
        "company_name": "AMIRY & GILBRIDE HEALTHCARE LIMITED",
        "sic_code": "47730",
        "sic_label": "Specialist retail",
        "sections": {"principal_activity": "that of dispensing chemists"},
    }
    assert _size_bounded_case_trace_inputs(case) == _case_trace_inputs(case)


def test_refresh_trace_snapshot_overwrites_the_traceinputs_and_traceoutputs_tags(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeClient:
        def set_trace_tag(self, trace_id: str, key: str, value: str) -> None:
            calls.append((trace_id, key, value))

    monkeypatch.setattr(mlflow, "MlflowClient", FakeClient)

    _refresh_trace_snapshot("tr-123", _case())

    assert [key for _, key, _ in calls] == ["mlflow.traceInputs", "mlflow.traceOutputs"]
    assert all(trace_id == "tr-123" for trace_id, _, _ in calls)
    inputs_payload = json.loads(calls[0][2])
    outputs_payload = json.loads(calls[1][2])
    assert "turnover_note" in inputs_payload["narrative_sections"]
    assert outputs_payload == {"draft_expected": _case()["expected"]}
