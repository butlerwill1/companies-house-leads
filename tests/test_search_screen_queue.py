import pytest

from scripts.screen import search_screen_queue as queue
from scripts.screen.search_screen_publish import best_label


def _case(**overrides):
    case = {
        "company_number": "00000001",
        "company_name": "ACME LTD",
        "cohort": "random",
        "sections": {"filed_report": "Strategic report\nThe principal activity of the company is retail."},
        "draft_full": {"drafted_by": "m", "search_screen": {"value": "likely", "quote": "retail", "reason": "shop"}},
        "review": {"status": "drafted"},
    }
    case.update(overrides)
    return case


def test_blind_case_stays_out_of_queue_until_labelled():
    assert not queue.in_queue(_case(blind=True))
    assert queue.in_queue(_case(blind=True, blind_review={"value": "likely"}))
    assert queue.in_queue(_case())


def test_case_without_a_draft_is_not_queued():
    assert not queue.in_queue(_case(draft_full=None))


def test_draft_answers_prefill_the_full_text_draft():
    assert queue.draft_answers(_case()) == {"search_screen": "likely"}


def test_verified_label_beats_the_draft():
    case = _case(expected={"search_screen": {"value": "unlikely"}}, review={"status": "verified"})
    assert best_label(case)[1] == "verified"
    assert queue.draft_answers(case) == {"search_screen": "unlikely"}


def test_agreement_keeps_the_draft_evidence():
    case = _case()
    queue.apply_review(case, "likely", None, reviewer="will", now="t")
    assert case["expected"]["search_screen"]["quote"] == "retail"
    assert case["review"]["status"] == "verified"
    assert case["review"]["changed_from"] is None


def test_change_drops_the_old_evidence_and_records_the_original():
    case = _case()
    queue.apply_review(case, "unlikely", "SPV", reviewer="will", now="t")
    label = case["expected"]["search_screen"]
    assert label["value"] == "unlikely" and label["quote"] is None and label["reason"] == "SPV"
    assert case["review"]["changed_from"] == "likely"
    assert case["review"]["draft_source"] == "draft_full"


def test_unknown_label_is_rejected_before_anything_is_written():
    case = _case()
    with pytest.raises(ValueError):
        queue.apply_review(case, "maybe", None, reviewer="will", now="t")
    assert "expected" not in case


def test_trace_input_is_the_whole_filed_report_unshortened():
    case = _case()
    data = queue.trace_input(case)
    assert data["filing_text"] == case["sections"]["filed_report"]
    assert "auditor" in data["text_source"]
    assert "likely" in data["question"]
    assert queue.trace_name(_case()) == "00000001 ACME LTD (search-screen review)"


def test_content_digest_changes_when_the_input_changes():
    a = queue.content_digest({"filing_text": "x"}, {"draft_label": "likely"})
    assert a == queue.content_digest({"filing_text": "x"}, {"draft_label": "likely"})
    assert a != queue.content_digest({"filing_text": "y"}, {"draft_label": "likely"})


def test_blind_trace_shows_no_draft():
    case = _case(blind=True)
    out = queue.blind_trace_output()
    assert "likely" not in str(out).lower().replace("pick a label", "")
    assert "draft_full" not in str(queue.trace_input(case))


def test_blind_label_becomes_verified_gold_and_blind_review():
    case = _case(blind=True)
    queue.apply_blind(case, "possible", "silent filing", reviewer="will", now="t")
    assert case["blind_review"]["value"] == "possible"
    assert case["expected"]["search_screen"]["value"] == "possible"
    assert case["review"]["status"] == "verified" and case["review"]["draft_source"] == "blind"
    assert queue.in_queue(case)  # joins the main queue as already verified
