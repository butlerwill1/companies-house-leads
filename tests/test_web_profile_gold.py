from __future__ import annotations

import pytest

from scripts.business_profile_classifier.business_profile_eval import load_case
from scripts.web import web_profile_gold as G

DRAFT = {
    "customer_type": ["consumer", "parents"], "conversion_action": ["book", "Book a visit"],
    "geography": ["local", "two towns"], "wins_by_tender": ["no", "parents come directly"], "main_town": "Wallington", "urgency": ["considered", "months ahead"],
    "ticket_band": ["over_10000", "a year's place"], "channel_fit": ["search", "searches"],
    "google_category": ["nursery_school", "listing right"], "search_phrases": ["day nursery", "baby nursery"],
    "phrase_note": "location in W4", "model_phrases_verdict": "partly_good", "model_phrases_note": "near me",
}
INPUT = {"company_number": "06330138", "company_name": "JANCETT LTD", "domain": "jancett.co.uk",
         "principal_activity": "childcare", "listing_category": "Nursery school", "text": "[home] We offer childcare."}


def _cases(tmp_path, **draft_over):
    drafts = {"label_source": "opus draft", "cases": {"06330138": {**DRAFT, **draft_over}}}
    written = G.build_cases(drafts, {"06330138": INPUT}, {"06330138": ["nursery near me"]}, cases_dir=tmp_path)
    return written, load_case(tmp_path / "06330138.json")


def test_case_files_carry_the_draft_and_the_model_phrases(tmp_path):
    written, case = _cases(tmp_path)
    assert written == ["06330138"] and case["expected"] is None
    assert case["draft"]["labels"]["customer_type"] == {"value": "consumer", "reason": "parents"}
    assert case["draft"]["labels"]["main_town"]["value"] == "Wallington"
    assert case["draft"]["model_phrases"] == ["nursery near me"] and case["draft"]["label_source"] == "opus draft"


def test_a_company_without_site_text_gets_no_case(tmp_path):
    drafts = {"cases": {"06330138": DRAFT}}
    assert G.build_cases(drafts, {"06330138": {**INPUT, "text": " "}}, {}, cases_dir=tmp_path) == []


@pytest.mark.parametrize("cohort", ["random-draw-2026-10-04", None])
def test_rebuilding_preserves_the_original_selection_cohort(tmp_path, cohort):
    drafts = {"cases": {"06330138": DRAFT}}
    if cohort is not None:
        drafts["set"] = cohort
    G.build_cases(drafts, {"06330138": INPUT}, {}, cases_dir=tmp_path)
    expected_cohort = cohort or "test-companies"
    assert load_case(tmp_path / "06330138.json")["set"] == expected_cohort

    G.build_cases({**drafts, "set": "later-draw"}, {"06330138": INPUT}, {}, cases_dir=tmp_path)
    assert load_case(tmp_path / "06330138.json")["set"] == expected_cohort


def test_rebuilding_keeps_a_finished_review(tmp_path):
    _, case = _cases(tmp_path)
    G.apply_label_review(case, G.label_answers(case), "will")
    from scripts.business_profile_classifier.business_profile_eval import save_case
    save_case(tmp_path / "06330138.json", case)
    _, again = _cases(tmp_path, search_phrases=["nursery"])
    assert again["review"]["status"] == "verified" and again["expected"]["customer_type"] == "consumer"
    assert again["draft"]["search_phrases"] == ["nursery"]


def test_the_form_is_prefilled_with_prefixed_score_names(tmp_path):
    _, case = _cases(tmp_path)
    answers = G.label_answers(case)
    assert answers["web_customer_type"] == "consumer" and answers["web_google_category"] == "nursery_school"
    assert G.phrase_answers(case) == {"web_search_phrases": "day nursery, baby nursery", "web_model_phrases": "partly_good"}
    names = {s["name"] for s in G.score_specs(G.LABEL_QUEUE)}
    assert all(n.startswith("web_") for n in names) and "web_main_town" in names


def test_label_review_records_what_the_reviewer_changed(tmp_path):
    _, case = _cases(tmp_path)
    answers = {**G.label_answers(case), "web_customer_type": "mixed", "web_main_town": " Carshalton "}
    assert G.apply_label_review(case, answers, "will") == ["customer_type", "main_town"]
    assert case["expected"]["customer_type"] == "mixed" and case["expected"]["main_town"] == "Carshalton"
    assert case["review"]["label_source"] == "opus draft"


def test_label_review_rejects_a_missing_or_invalid_value(tmp_path):
    _, case = _cases(tmp_path)
    with pytest.raises(ValueError, match="geography"):
        G.apply_label_review(case, {**G.label_answers(case), "web_geography": "galactic"}, "will")


def test_phrase_review_cleans_and_dedupes(tmp_path):
    _, case = _cases(tmp_path)
    changed = G.apply_phrase_review(case, {"web_search_phrases": "Day Nursery,  baby nursery,\nday nursery, preschool",
                                           "web_model_phrases": "poor"}, "will")
    assert changed and case["expected_phrases"] == ["day nursery", "baby nursery", "preschool"]
    assert case["phrase_review"]["model_phrases"] == "poor"
    with pytest.raises(ValueError):
        G.apply_phrase_review(case, {"web_search_phrases": " , "}, "will")


def test_phrase_trace_shows_volumes_or_says_not_looked_up(tmp_path):
    _, case = _cases(tmp_path)
    _, output = G.phrase_trace(case, {"day nursery": 2400, "nursery near me": None})
    assert output["reference_phrases (monthly UK searches)"] == ["day nursery (2400)", "baby nursery (not looked up)"]
    assert output["model_phrases gpt-5.4-mini (monthly UK searches)"] == ["nursery near me (not looked up)"]


def test_a_second_company_on_the_same_site_is_excluded(tmp_path):
    _, case = _cases(tmp_path, duplicate_of="05272723")
    assert case["excluded"] == {"reason": "same website as 05272723", "duplicate_of": "05272723"}
    _, case = _cases(tmp_path)
    assert case["excluded"] is None
