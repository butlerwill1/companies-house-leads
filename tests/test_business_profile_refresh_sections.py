from __future__ import annotations

from scripts.profile.business_profile_refresh_sections import _existing_quote_breaks


def _case(**expected_overrides) -> dict:
    return {
        "company_number": "00000001",
        "expected": {
            "demand_model": {
                "value": "local_service",
                "quote": "a community football club",
                "section": "principal_activity",
            },
            **expected_overrides,
        },
    }


def test_no_break_when_the_quote_still_matches_the_refreshed_section():
    case = _case()
    new_sections = {"principal_activity": "Principal activities. Runs a community football club in the area."}

    assert _existing_quote_breaks(case, new_sections) == []


def test_flags_when_the_refreshed_section_no_longer_contains_the_quote():
    """The exact failure mode this script exists to catch: a re-extraction
    that changed or lost the text a human-reviewed label's evidence
    actually depends on must not be silently applied."""
    case = _case()
    new_sections = {"principal_activity": "Principal activities. Something else entirely."}

    problems = _existing_quote_breaks(case, new_sections)

    assert len(problems) == 1
    assert "demand_model" in problems[0]
    assert "principal_activity" in problems[0]


def test_flags_when_the_cited_section_disappears_entirely():
    case = _case()
    new_sections = {"strategic_report": "unrelated text"}

    problems = _existing_quote_breaks(case, new_sections)

    assert len(problems) == 1
    assert "no longer present" in problems[0]


def test_unclear_and_empty_fields_are_not_checked():
    """Nothing to break: an "unclear" value has no quote by design, and a
    field the reviewer never filled in has no evidence to protect."""
    case = _case(
        demand_model={"value": "unclear", "quote": "", "section": None},
        customer_type={"value": None, "quote": None, "section": None},
    )

    assert _existing_quote_breaks(case, {}) == []


def _refresh_dirs(monkeypatch, tmp_path, case: dict):
    import json

    from scripts.profile import business_profile_refresh_sections as R

    cases_dir = tmp_path / "cases"
    raw_dir = tmp_path / "raw"
    cases_dir.mkdir()
    raw_dir.mkdir()
    (cases_dir / f"{case['company_number']}.json").write_text(json.dumps(case), encoding="utf-8")
    monkeypatch.setattr(R, "CASES_DIR", cases_dir)
    monkeypatch.setattr(R, "RAW_DIR", raw_dir)
    return R, cases_dir, raw_dir


def test_whole_document_refresh_uses_the_vlm_transcript_when_there_is_no_xhtml(monkeypatch, tmp_path):
    """A scanned, PDF-only filing has no .xhtml. The transcription harness
    leaves <company>.filed_report.txt (already auditor-stripped), and that
    is the whole-document text for the case."""
    import json

    case = _case(demand_model={"value": "unclear", "quote": "", "section": None})
    case["sections"] = {"principal_activity": "old named window"}
    R, cases_dir, raw_dir = _refresh_dirs(monkeypatch, tmp_path, case)
    (raw_dir / f"{case['company_number']}.filed_report.txt").write_text(
        "Directors' report\nRuns a community football club in the area.\n", encoding="utf-8"
    )

    assert R.refresh(dry_run=False, whole_document=True) == 0
    updated = json.loads((cases_dir / f"{case['company_number']}.json").read_text(encoding="utf-8"))
    assert list(updated["sections"]) == ["filed_report"]
    assert "community football club" in updated["sections"]["filed_report"]


def test_named_window_refresh_still_skips_a_pdf_only_case(monkeypatch, tmp_path):
    """The named windows need the iXBRL tags a transcript cannot carry, so
    without --whole-document a transcript-only case is skipped, not refreshed."""
    import json

    case = _case()
    case["sections"] = {"principal_activity": "old named window"}
    R, cases_dir, raw_dir = _refresh_dirs(monkeypatch, tmp_path, case)
    (raw_dir / f"{case['company_number']}.filed_report.txt").write_text("anything", encoding="utf-8")

    assert R.refresh(dry_run=False, whole_document=False) == 0
    untouched = json.loads((cases_dir / f"{case['company_number']}.json").read_text(encoding="utf-8"))
    assert untouched["sections"] == {"principal_activity": "old named window"}
