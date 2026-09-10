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
        "quote": "community focused professional football club",
        "section": "principal_activity",
        "reason": "The club sells matchday admission to the public, so no separate customer channel is described.",
        "value": "not_customer_facing",
        "confidence": 0.9,
    },
    "customer_type": {
        "quote": "community focused professional football club",
        "section": "principal_activity",
        "reason": "Supporters attending the club are individuals, so the customer is the individual.",
        "value": "b2c",
        "confidence": 0.8,
    },
    "delivery_model": {
        "quote": "community focused professional football club",
        "section": "principal_activity",
        "reason": "Operating a football club is a people-delivered service.",
        "value": "professional_service",
        "confidence": 0.6,
    },
    "geography_served": {
        "quote": "community focused professional football club",
        "section": "principal_activity",
        "reason": "A community focused club serves its immediate area.",
        "value": "local",
        "confidence": 0.7,
    },
    "trading_status_confirmed": {
        "quote": "community focused professional football club",
        "section": "principal_activity",
        "reason": "The company operates the club itself.",
        "value": "trading",
        "confidence": 0.85,
    },
    "sic_agreement": {
        "quote": "community focused professional football club",
        "section": "principal_activity",
        "reason": "Sports facility operation matches SIC 93110.",
        "value": "agrees",
    },
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


def test_a_recased_quote_still_passes_verbatim_check() -> None:
    """A model quoting real source text sometimes re-cases its first letter
    once embedded in a JSON string value ("The..." -> "the...") without
    changing what it's actually claiming -- confirmed live in the
    2026-09-02 Phase 3d smoke test, where this rejected two genuinely
    correct extractions as if they were fabrications. The check cares
    whether the words came from the source, not whether capitalization
    survived the round trip."""
    response = {
        **VALID_RESPONSE,
        "customer_type": {
            **VALID_RESPONSE["customer_type"],
            "quote": "COMMUNITY focused Professional Football Club",
        },
    }

    assert validate_response(response, SECTIONS) == []


def test_hyphen_spacing_difference_still_passes_verbatim_check() -> None:
    """Confirmed live in the 2026-09-02 Phase 3d smoke test: a model quoted
    "long - term" (spaced-out hyphen) where the source read "long-term"
    (no spaces), for otherwise identical, correctly-quoted text. Stripping
    punctuation outright rather than replacing it with a space made these
    normalize to different strings ("longterm" vs "long term") purely
    because of whether the source happened to space its hyphen -- unrelated
    to whether the words themselves came from the source."""
    from scripts.profile.business_profile_policy import normalize_quote_text

    assert normalize_quote_text("long-term success") == normalize_quote_text("long - term success")


def test_quote_referencing_a_section_not_supplied_is_rejected() -> None:
    wrong_section = {**VALID_RESPONSE, "geography_served": {**VALID_RESPONSE["geography_served"], "section": "going_concern"}}

    errors = validate_response(wrong_section, SECTIONS)

    assert any("going_concern" in e and "is not one of the sections" in e for e in errors)


def test_unclear_value_needs_no_quote() -> None:
    """unclear is a correct, expected answer -- the taxonomy exists to be
    refused when the text does not support a confident call."""
    response = {
        **VALID_RESPONSE,
        "delivery_model": {
            "quote": "",
            "section": None,
            "reason": "The text never says what the club delivers beyond running the club itself.",
            "value": "unclear",
            "confidence": 0.0,
        },
    }

    assert validate_response(response, SECTIONS) == []


def test_a_confident_value_without_a_quote_is_rejected() -> None:
    response = {
        **VALID_RESPONSE,
        "customer_type": {"value": "b2c", "confidence": 0.9, "quote": "", "section": "principal_activity"},
    }

    errors = validate_response(response, SECTIONS)

    assert any("no supporting quote" in e for e in errors)


def test_missing_confidence_is_rejected() -> None:
    """Confidence is requested and returned but was never checked -- a
    response missing it entirely used to pass validation exactly like a
    real one. Phase 3a made confidence the only way uncertainty gets
    expressed, so a response without one is unusable, not just untidy."""
    response = {
        **VALID_RESPONSE,
        "customer_type": {k: v for k, v in VALID_RESPONSE["customer_type"].items() if k != "confidence"},
    }

    errors = validate_response(response, SECTIONS)

    assert any("customer_type.confidence" in e for e in errors)


def test_confidence_outside_zero_to_one_is_rejected() -> None:
    response = {**VALID_RESPONSE, "customer_type": {**VALID_RESPONSE["customer_type"], "confidence": 1.5}}

    errors = validate_response(response, SECTIONS)

    assert any("customer_type.confidence" in e for e in errors)


def test_non_numeric_confidence_is_rejected() -> None:
    response = {**VALID_RESPONSE, "customer_type": {**VALID_RESPONSE["customer_type"], "confidence": "high"}}

    errors = validate_response(response, SECTIONS)

    assert any("customer_type.confidence" in e for e in errors)


def test_boolean_confidence_is_rejected() -> None:
    """bool is an int subclass in Python -- True would otherwise silently
    pass the numeric-range check as 1.0."""
    response = {**VALID_RESPONSE, "customer_type": {**VALID_RESPONSE["customer_type"], "confidence": True}}

    errors = validate_response(response, SECTIONS)

    assert any("customer_type.confidence" in e for e in errors)


def test_unclear_value_still_needs_a_valid_confidence() -> None:
    """An "unclear" answer is exempt from needing a quote, not from
    reporting a confidence -- every real response on hand already includes
    one (0.0) alongside "unclear", so this tightens nothing in use."""
    response = {
        **VALID_RESPONSE,
        "delivery_model": {"value": "unclear", "quote": "", "section": None, "reason": "Not stated."},
    }

    errors = validate_response(response, SECTIONS)

    assert any("delivery_model.confidence" in e for e in errors)


def test_a_field_without_a_reason_is_rejected() -> None:
    """v6 makes the reason the step that produces the value, so a response
    that skips it has not done the work the prompt asked for."""
    response = {**VALID_RESPONSE, "customer_type": {**VALID_RESPONSE["customer_type"], "reason": ""}}

    errors = validate_response(response, SECTIONS)

    assert any("customer_type.reason" in e for e in errors)


def test_an_unclear_value_still_needs_a_reason() -> None:
    """The reason check sits before the "unclear" short-circuit on purpose.
    An unclear answer has no quote to inspect, so its reason is the only
    record of what was looked for and not found."""
    response = {
        **VALID_RESPONSE,
        "delivery_model": {"value": "unclear", "confidence": 0.0, "quote": "", "section": None},
    }

    errors = validate_response(response, SECTIONS)

    assert any("delivery_model.reason" in e for e in errors)


def test_sic_agreement_verdict_without_a_quote_is_rejected() -> None:
    """Until v6 sic_agreement was the one field with no verbatim-quote guard
    at all -- its value was checked against the taxonomy and nothing else."""
    response = {**VALID_RESPONSE, "sic_agreement": {"value": "agrees", "reason": "Matches.", "quote": ""}}

    errors = validate_response(response, SECTIONS)

    assert any("sic_agreement" in e and "quote" in e for e in errors)


def test_sic_agreement_quote_must_be_verbatim() -> None:
    response = {
        **VALID_RESPONSE,
        "sic_agreement": {
            "quote": "a community focused football team",
            "section": "principal_activity",
            "reason": "Matches.",
            "value": "agrees",
        },
    }

    errors = validate_response(response, SECTIONS)

    assert any("sic_agreement.quote" in e for e in errors)


def test_gold_blocks_are_exempt_from_the_sic_quote_requirement() -> None:
    """The 109 gold expected blocks predate sic_agreement having a quote and
    cannot be given one without re-reading every filing, so the review path
    passes require_sic_quote=False. Model responses stay held to it."""
    response = {**VALID_RESPONSE, "sic_agreement": {"value": "agrees", "reason": "Matches."}}

    assert validate_response(response, SECTIONS, require_sic_quote=False) == []
    assert validate_response(response, SECTIONS) != []


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


def test_response_shape_puts_evidence_before_the_answer() -> None:
    """The whole point of v6: the model must emit quote, section and reason
    before it commits to a value, so the evidence conditions the answer
    instead of being retrofitted to one already chosen."""
    prompt = build_prompt(
        company_name="ACME LTD", sections=SECTIONS, sic_label="Sport / fitness / gyms", sic_code="93110",
    )
    shape = prompt[prompt.index("Respond with ONLY") :]

    for field in ("demand_model", "customer_type", "delivery_model", "geography_served",
                  "trading_status_confirmed", "sic_agreement"):
        line = next(ln for ln in shape.splitlines() if ln.strip().startswith(f'"{field}"'))
        assert line.index('"quote"') < line.index('"reason"') < line.index('"value"'), line
