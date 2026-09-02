from __future__ import annotations

from scripts.profile.business_profile_eval import (
    _case_trace_inputs,
    _case_trace_outputs,
    _draft_answers,
    _narrative_preview,
    _review_field_names,
    _review_question_specs,
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
        "expected": {
            "business_description": "Holding company for a pharmacy group.",
            "demand_model": {"value": "local_service"},
            "sic_agreement": {"value": "agrees"},
        },
    }


def test_case_trace_inputs_includes_every_section() -> None:
    inputs = _case_trace_inputs(_case())
    assert inputs["company_number"] == "SC758233"
    assert "[principal_activity]" in inputs["narrative_sections"]
    assert "[turnover_note]" in inputs["narrative_sections"]
    assert "Pharmacy sales 13,391,763" in inputs["narrative_sections"]


def test_case_trace_outputs_wraps_the_current_expected_block() -> None:
    outputs = _case_trace_outputs(_case())
    assert outputs == {"draft_expected": _case()["expected"]}


def test_narrative_preview_is_not_truncated() -> None:
    """Langfuse stores full observation IO -- unlike the old MLflow trace-tag
    path, a long section is kept whole, not cut to fit a size limit."""
    case = {"sections": {"directors_report": "boilerplate " * 500, "turnover_note": "Pharmacy sales 13,391,763"}}
    preview = _narrative_preview(case)
    assert preview.count("boilerplate") == 500
    assert "13,391,763" in preview


def test_narrative_preview_reflects_sections_added_later() -> None:
    case = _case()
    before = _narrative_preview({**case, "sections": {"principal_activity": case["sections"]["principal_activity"]}})
    assert "turnover_note" not in before
    after = _narrative_preview(case)
    assert "turnover_note" in after


def test_draft_answers_flattens_expected_block() -> None:
    answers = _draft_answers(_case())
    assert answers["business_description"] == "Holding company for a pharmacy group."
    assert answers["demand_model"] == "local_service"
    assert answers["sic_agreement"] == "agrees"
    # every question name is present (None where unreviewed)
    assert set(answers) == set(_review_field_names())


def test_review_question_specs_cover_every_field() -> None:
    specs = {s["name"] for s in _review_question_specs()}
    assert specs == set(_review_field_names())
    by_name = {s["name"]: s for s in _review_question_specs()}
    assert "categories" not in by_name["business_description"]
    assert by_name["demand_model"]["categories"]
