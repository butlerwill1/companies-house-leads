from __future__ import annotations

from scripts.profile.business_profile_policy import (
    build_prompt,
    parse_json_response,
    select_narrative_sections,
    validate_response,
)

VALID_RESPONSE = {
    "business_description": "A community football club operating a stadium and youth academy.",
    "demand_model": {
        "value": "not_customer_facing",
        "confidence": 0.9,
        "quote": "community focused professional football club",
        "section": "principal_activity",
    },
    "customer_type": {
        "value": "b2c",
        "confidence": 0.8,
        "quote": "community focused professional football club",
        "section": "principal_activity",
    },
    "delivery_model": {
        "value": "professional_service",
        "confidence": 0.6,
        "quote": "community focused professional football club",
        "section": "principal_activity",
    },
    "geography_served": {
        "value": "local",
        "confidence": 0.7,
        "quote": "community focused professional football club",
        "section": "principal_activity",
    },
    "trading_status_confirmed": {
        "value": "trading",
        "confidence": 0.85,
        "quote": "community focused professional football club",
        "section": "principal_activity",
    },
    "sic_agreement": {"value": "agrees", "reason": "Sports facility operation matches SIC 93110."},
}

SECTIONS = {
    "principal_activity": (
        "The principal activity of the company continues to be the operation of a "
        "community focused professional football club together with related commercial activities."
    )
}


def test_valid_response_passes_validation() -> None:
    assert validate_response(VALID_RESPONSE, SECTIONS) == []


def test_quote_must_appear_verbatim_in_the_named_section() -> None:
    """This is the whole point of the design: a quote that is not a
    substring of the section it claims to come from is a fabrication, and
    must be rejected regardless of how confident the model claims to be."""
    tampered = {**VALID_RESPONSE, "demand_model": {**VALID_RESPONSE["demand_model"], "quote": "a business that manufactures aircraft parts"}}

    errors = validate_response(tampered, SECTIONS)

    assert any("does not appear verbatim" in e for e in errors)


def test_paraphrased_quote_is_rejected_not_just_wrong_words() -> None:
    """Guards against the subtler failure than outright fabrication: the
    model summarising instead of quoting."""
    paraphrased = {
        **VALID_RESPONSE,
        "customer_type": {**VALID_RESPONSE["customer_type"], "quote": "runs a football club for the local community"},
    }

    errors = validate_response(paraphrased, SECTIONS)

    assert any("customer_type.quote does not appear verbatim" in e for e in errors)


def test_quote_referencing_a_section_not_supplied_is_rejected() -> None:
    wrong_section = {**VALID_RESPONSE, "geography_served": {**VALID_RESPONSE["geography_served"], "section": "going_concern"}}

    errors = validate_response(wrong_section, SECTIONS)

    assert any("going_concern" in e and "is not one of the sections" in e for e in errors)


def test_unclear_value_needs_no_quote() -> None:
    """unclear is a correct, expected answer -- the taxonomy exists to be
    refused when the text does not support a confident call."""
    response = {
        **VALID_RESPONSE,
        "delivery_model": {"value": "unclear", "confidence": 0.0, "quote": "", "section": None},
    }

    assert validate_response(response, SECTIONS) == []


def test_a_confident_value_without_a_quote_is_rejected() -> None:
    response = {
        **VALID_RESPONSE,
        "customer_type": {"value": "b2c", "confidence": 0.9, "quote": "", "section": "principal_activity"},
    }

    errors = validate_response(response, SECTIONS)

    assert any("no supporting quote" in e for e in errors)


def test_value_outside_the_allowed_taxonomy_is_rejected() -> None:
    response = {**VALID_RESPONSE, "customer_type": {**VALID_RESPONSE["customer_type"], "value": "enterprise"}}

    errors = validate_response(response, SECTIONS)

    assert any("customer_type.value 'enterprise'" in e for e in errors)


def test_missing_business_description_is_rejected() -> None:
    response = {**VALID_RESPONSE, "business_description": ""}

    errors = validate_response(response, SECTIONS)

    assert any("business_description" in e for e in errors)


def test_missing_sic_agreement_is_rejected() -> None:
    response = {k: v for k, v in VALID_RESPONSE.items() if k != "sic_agreement"}

    errors = validate_response(response, SECTIONS)

    assert any("sic_agreement" in e for e in errors)


def test_parse_json_response_strips_a_markdown_fence() -> None:
    text = '```json\n{"business_description": "x"}\n```'

    assert parse_json_response(text) == {"business_description": "x"}


def test_parse_json_response_rejects_a_non_object() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_json_response("[1, 2, 3]")


def test_select_narrative_sections_excludes_auditor_flagged_text() -> None:
    """Sections flagged is_auditor_text are the auditor describing its
    audit, not the company describing itself, and must never reach the
    prompt."""
    all_sections = {
        "principal_activity": {"text": "we sell widgets to businesses", "is_auditor_text": False},
        "principal_risks": {"text": "we have audited the financial statements", "is_auditor_text": True},
        "going_concern": {"text": "not in the priority list anyway", "is_auditor_text": False},
    }

    selected = select_narrative_sections(all_sections)

    assert selected == {"principal_activity": "we sell widgets to businesses"}


def test_build_prompt_places_sic_label_after_the_classification_fields() -> None:
    """The SIC label must appear textually after the four classification
    field prompts, so an autoregressive model reads the business
    description task before it is told what the company is registered as."""
    prompt = build_prompt(
        company_name="ACME LTD", sections=SECTIONS, sic_label="Sport / fitness / gyms", sic_code="93110",
    )

    demand_model_pos = prompt.index("demand_model -- how customers")
    sic_label_pos = prompt.index("registered SIC classification")
    assert demand_model_pos < sic_label_pos
