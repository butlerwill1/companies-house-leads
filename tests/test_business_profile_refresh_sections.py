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
