from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.search_screen_classifier import search_screen_review as R


def _case(number="00000001", *, blind=False, cohort="random", draft=None):
    label = {"value": None, "quote": None, "section": None, "reason": None}
    return {
        "company_number": number,
        "company_name": "EXAMPLE LTD",
        "sic_label": "Specialist retail",
        "cohort": cohort,
        "blind": blind,
        "sections": {"principal_activity": "The company sells shoes online."},
        "financials": {"turnover": 1000.0, "profit_after_tax": 10.0, "employees": 5},
        "expected": {"search_screen": dict(label)},
        "draft": {
            "search_screen": draft or dict(label),
            "drafted_by": "claude" if draft else None,
            "drafted_at": None,
        },
        "review": {"status": "unreviewed", "reviewer": None, "reviewed_at": None, "changed_from": None},
    }


def _write(directory: Path, case) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case['company_number']}.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    return path


def _draft(value="likely"):
    return {"value": value, "quote": "The company sells shoes online.", "section": "principal_activity",
            "reason": "Online shoe sales."}


def test_blind_rows_hide_the_draft_and_include_only_blind_cases(tmp_path):
    _write(tmp_path, _case("00000001", blind=True, draft=_draft()))
    _write(tmp_path, _case("00000002", blind=False, draft=_draft()))
    rows = R.blind_rows(tmp_path)
    assert [row[0] for row in rows[1:]] == ["00000001"]
    flat = json.dumps(rows[1:])
    assert "likely" not in flat and "claude" not in flat


def test_review_rows_show_the_draft_for_every_drafted_case(tmp_path):
    _write(tmp_path, _case("00000001", draft=_draft("possible")))
    _write(tmp_path, _case("00000002"))
    rows = R.review_rows(tmp_path)
    assert [row[0] for row in rows[1:]] == ["00000001"]
    assert "possible" in rows[1]


def _csv(tmp_path, rows):
    path = tmp_path / "verdicts.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([["company number", "verdict", "notes"], *rows])
    return path


def test_import_blind_sets_the_blind_label_and_expected(tmp_path):
    cases = tmp_path / "cases"
    path = _write(cases, _case("00000001", blind=True, draft=_draft("likely")))
    R.import_verdicts(_csv(tmp_path, [["1", "possible", "unsure"]]), cases, kind="blind", reviewer="will")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["blind_review"]["value"] == "possible"
    assert saved["expected"]["search_screen"]["value"] == "possible"
    assert saved["review"]["status"] == "verified"


def test_import_review_agree_keeps_the_draft_and_disagree_records_changed_from(tmp_path):
    cases = tmp_path / "cases"
    keep = _write(cases, _case("00000001", draft=_draft("likely")))
    change = _write(cases, _case("00000002", draft=_draft("likely")))
    R.import_verdicts(_csv(tmp_path, [["00000001", "agree", ""], ["00000002", "unlikely", "holding"]]),
                      cases, kind="review", reviewer="will")
    kept = json.loads(keep.read_text(encoding="utf-8"))
    changed = json.loads(change.read_text(encoding="utf-8"))
    assert kept["expected"]["search_screen"]["value"] == "likely"
    assert kept["review"]["changed_from"] is None
    assert changed["expected"]["search_screen"]["value"] == "unlikely"
    assert changed["review"]["changed_from"] == "likely"


def test_import_rejects_unknown_labels_and_missing_cases(tmp_path):
    cases = tmp_path / "cases"
    _write(cases, _case("00000001", draft=_draft()))
    with pytest.raises(ValueError, match="maybe"):
        R.import_verdicts(_csv(tmp_path, [["00000001", "maybe", ""]]), cases, kind="review", reviewer="will")
    with pytest.raises(ValueError, match="00000099"):
        R.import_verdicts(_csv(tmp_path, [["99", "likely", ""]]), cases, kind="review", reviewer="will")


def test_blank_verdicts_are_skipped(tmp_path):
    cases = tmp_path / "cases"
    path = _write(cases, _case("00000001", draft=_draft()))
    assert R.import_verdicts(_csv(tmp_path, [["00000001", "", ""]]), cases, kind="review", reviewer="will") == 0
    assert json.loads(path.read_text(encoding="utf-8"))["review"]["status"] == "unreviewed"


def test_agreement_compares_blind_labels_with_drafts(tmp_path):
    cases = tmp_path / "cases"
    for number, blind_value, draft_value in [("00000001", "likely", "likely"), ("00000002", "likely", "possible"),
                                             ("00000003", "unlikely", "unlikely")]:
        case = _case(number, blind=True, draft=_draft(draft_value))
        case["blind_review"] = {"value": blind_value}
        _write(cases, case)
    summary = R.blind_agreement(cases)
    assert summary["n"] == 3 and summary["agree"] == 2
    assert summary["pass_reject_agree"] == 3


def test_long_text_is_truncated_with_a_visible_marker(tmp_path):
    case = _case("00000001", blind=True)
    case["sections"]["principal_activity"] = "x" * (R.MAX_TEXT + 500)
    _write(tmp_path, case)
    text = R.blind_rows(tmp_path)[1][7]
    kept, marker = text.split(" [TRUNCATED: ")
    assert len(kept) == R.MAX_TEXT
    assert marker.endswith("more characters in the case file]")


def test_the_sheet_shows_the_label_pack_when_the_case_has_one(tmp_path):
    case = _case("00000001", blind=True)
    case["label_pack"] = "[principal activity] Sells shoes.\n[key lines] Online shop | Three stores"
    _write(tmp_path, case)
    assert R.blind_rows(tmp_path)[1][7] == "[principal activity] Sells shoes. [key lines] Online shop | Three stores"


def _drafts(tmp_path, payload):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _case_with_text(number="00000001", text="The company sells shoes online to consumers."):
    case = _case(number)
    case["sections"] = {"filed_report": text}
    return case


def test_apply_drafts_writes_the_draft_and_leaves_expected_empty(tmp_path):
    cases = tmp_path / "cases"
    path = _write(cases, _case_with_text())
    drafts = _drafts(tmp_path, {"00000001": {"value": "likely", "quote": "sells shoes  online", "reason": "Online shoe shop."}})
    assert R.apply_drafts(drafts, cases, drafter="claude-sonnet-5-5") == 1
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["draft"]["search_screen"]["value"] == "likely"
    assert saved["draft"]["drafted_by"] == "claude-sonnet-5-5"
    assert saved["expected"]["search_screen"]["value"] is None
    assert saved["review"]["status"] == "drafted"


def test_apply_drafts_rejects_a_quote_that_is_not_in_the_filing_before_writing_anything(tmp_path):
    cases = tmp_path / "cases"
    good = _write(cases, _case_with_text("00000001"))
    _write(cases, _case_with_text("00000002"))
    drafts = _drafts(tmp_path, {
        "00000001": {"value": "likely", "quote": "sells shoes online", "reason": "ok"},
        "00000002": {"value": "likely", "quote": "sells boots in Norway", "reason": "made up"},
    })
    with pytest.raises(ValueError, match="00000002"):
        R.apply_drafts(drafts, cases, drafter="claude-sonnet-5-5")
    assert json.loads(good.read_text(encoding="utf-8"))["draft"]["search_screen"]["value"] is None


def test_apply_drafts_requires_a_quote_from_the_filing(tmp_path):
    cases = tmp_path / "cases"
    _write(cases, _case_with_text())
    drafts = _drafts(tmp_path, {"00000001": {"value": "likely", "quote": "", "reason": "no quote"}})
    with pytest.raises(ValueError, match="quote"):
        R.apply_drafts(drafts, cases, drafter="claude-sonnet-5-5")


def test_apply_drafts_refuses_to_overwrite_a_verified_case(tmp_path):
    cases = tmp_path / "cases"
    case = _case_with_text()
    case["review"]["status"] = "verified"
    _write(cases, case)
    drafts = _drafts(tmp_path, {"00000001": {"value": "likely", "quote": "sells shoes online", "reason": "ok"}})
    with pytest.raises(ValueError, match="verified"):
        R.apply_drafts(drafts, cases, drafter="claude-sonnet-5-5")


def test_review_rows_hide_blind_cases_until_the_reviewer_has_labelled_them(tmp_path):
    hidden = _case("00000001", blind=True, draft=_draft("likely"))
    labelled = _case("00000002", blind=True, draft=_draft("possible"))
    labelled["blind_review"] = {"value": "likely"}
    ordinary = _case("00000003", blind=False, draft=_draft("unlikely"))
    for case in (hidden, labelled, ordinary):
        _write(tmp_path, case)
    assert [row[0] for row in R.review_rows(tmp_path)[1:]] == ["00000002", "00000003"]


def test_apply_drafts_can_write_a_second_full_text_draft_beside_the_first(tmp_path):
    cases = tmp_path / "cases"
    case = _case_with_text()
    case["draft"]["search_screen"] = _draft("possible")
    case["review"]["status"] = "drafted"
    path = _write(cases, case)
    drafts = _drafts(tmp_path, {"00000001": {"value": "likely", "quote": "sells shoes online", "reason": "Online shop."}})
    R.apply_drafts(drafts, cases, drafter="claude-sonnet-5-5", key="draft_full")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["draft"]["search_screen"]["value"] == "possible"       # the pack draft is untouched
    assert saved["draft_full"]["search_screen"]["value"] == "likely"
    assert saved["review"]["status"] == "drafted"
    with pytest.raises(ValueError, match="key"):
        R.apply_drafts(drafts, cases, drafter="x", key="expected")
