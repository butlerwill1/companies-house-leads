from __future__ import annotations

import copy

from scripts.profile.business_profile_review import validate_expected_block


def _case(**expected_overrides) -> dict:
    return {
        "company_number": "00000001",
        "sections": {"principal_activity": "a community football club"},
        "expected": {
            "business_description": "A community football club.",
            "demand_model": {
                "value": "not_customer_facing",
                "confidence": 0.9,
                "quote": "community football club",
                "section": "principal_activity",
            },
            "customer_type": {
                "value": "b2c",
                "confidence": 0.8,
                "quote": "community football club",
                "section": "principal_activity",
            },
            "delivery_model": {
                "value": "professional_service",
                "confidence": 0.6,
                "quote": "community football club",
                "section": "principal_activity",
            },
            "geography_served": {
                "value": "local",
                "confidence": 0.7,
                "quote": "community football club",
                "section": "principal_activity",
            },
            "trading_status_confirmed": {
                "value": "trading",
                "confidence": 0.85,
                "quote": "community football club",
                "section": "principal_activity",
            },
            "sic_agreement": {"value": "agrees", "reason": "Matches sports facility SIC."},
            **expected_overrides,
        },
    }


def test_a_fully_labelled_case_passes() -> None:
    assert validate_expected_block(_case()) == []


def test_a_gold_block_without_any_reason_still_validates() -> None:
    """`reason` is model rationale, `expected` is ground truth -- the 109
    gold blocks carry no reason and are not going to grow one. The review
    path fills a placeholder rather than churning the case files."""
    case = _case()

    assert validate_expected_block(case) == []


def test_validate_expected_block_does_not_mutate_the_case() -> None:
    """The POST handler saves the same parsed body it validated, so a
    placeholder written with setdefault would land in the gold file."""
    case = _case()
    before = copy.deepcopy(case["expected"])

    validate_expected_block(case)

    assert case["expected"] == before


def test_a_field_the_reviewer_has_not_touched_yet_defaults_to_a_valid_unclear() -> None:
    """A field with no value at all (not yet reviewed) must not itself fail
    validation -- that would make an in-progress review indistinguishable
    from a genuinely broken one. The placeholder this substitutes needs a
    real confidence (Phase 3b tightened validate_response to require one
    for every field, "unclear" included), not None, or the substitution
    itself would trip the very check it exists to route around."""
    case = _case(delivery_model={})

    assert validate_expected_block(case) == []


def test_a_bad_quote_in_the_expected_block_is_still_caught() -> None:
    case = _case(
        delivery_model={
            "value": "professional_service",
            "confidence": 0.6,
            "quote": "this text never appeared anywhere",
            "section": "principal_activity",
        }
    )

    errors = validate_expected_block(case)

    assert any("delivery_model.quote" in e for e in errors)
