from __future__ import annotations

from scripts.business_profile_classifier.business_profile_policy import (
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
    from scripts.business_profile_classifier.business_profile_policy import normalize_quote_text

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

    demand_model_pos = prompt.index("demand_model -- the evidenced acquisition channel")
    sic_label_pos = prompt.index("registered SIC classification")
    assert demand_model_pos < sic_label_pos


def test_prompt_separates_customer_type_demand_and_spv_status() -> None:
    """The retained boundary rules must reach the model, not merely live in docs."""
    prompt = build_prompt(
        company_name="ACME LTD", sections=SECTIONS, sic_label="Sport / fitness / gyms", sic_code="93110",
    )

    assert "relationship_or_contract" in prompt
    assert "A council commissioning an individual's care can be b2c" in prompt
    assert "including a PFI project company" in prompt
    assert "A public-sector contract alone is NOT enough" in prompt
    assert "college subsidiary hiring facilities to outside customers is trading" in prompt


def test_v12_prompt_prioritises_evidence_without_defaulting_to_relationships() -> None:
    """The new tie-break is evidence priority, not business-type inference.

    This protects the intended Flour Power/Miles Better Heat repair without
    allowing a bare website or a retail SIC to manufacture a search channel.
    """
    from scripts.business_profile_classifier.business_profile_policy import PROMPT_VERSION

    prompt = build_prompt(
        company_name="ACME LTD", sections=SECTIONS, sic_label="Sport / fitness / gyms", sic_code="93110",
    )

    assert PROMPT_VERSION == "business-profile-v12"
    assert "scan the whole filing for all substantial external business lines" in prompt
    assert "select it even when relationships, wholesale or marketplaces contribute more revenue" in prompt
    assert "Do not infer that a channel exists from the business type" in prompt
    assert "A bare mention that the company has a website" in prompt
    assert "Absence of search evidence is NOT evidence of relationship_or_contract" in prompt
    assert "Low confidence cannot replace that evidence" in prompt
    assert "franchise licence describes the relationship with the brand owner, not how diners arrive" in prompt
    assert '"Organic growth" does not mean organic search or referrals' in prompt
    assert "Prefer the shortest contiguous passage that supports the answer" in prompt


def test_short_contiguous_quotes_pass_without_relaxing_rewritten_quote_checks() -> None:
    """V12 repairs quote production, not acceptance of altered source evidence."""
    from scripts.business_profile_classifier.business_profile_policy import _quote_errors

    sections = {"filed_report": (
        "The principal activity of the company during the year was that of provision of education services.\n"
        "Gate receipts and match day income\n2,912,552\n1,877,230"
    )}
    assert _quote_errors("delivery_model", "provision of education services", "filed_report", sections) == []
    assert _quote_errors("demand_model", "Gate receipts and match day income", "filed_report", sections) == []
    assert _quote_errors("demand_model", "gate receipts and matchday income", "filed_report", sections)
    assert _quote_errors("customer_type", "Gate receipts and match day income 9,999,999", "filed_report", sections)


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


def test_a_quote_may_read_one_column_of_a_table_but_may_not_skip_words() -> None:
    """A turnover-by-geography note is a two-column table. Quoting the
    current-year column skips only the prior-year numbers, which is a
    faithful reading; skipping a word is not."""
    from scripts.business_profile_classifier.business_profile_policy import quote_reads_table_row

    table = (
        "Turnover analysed by geographical market\nUnited Kingdom | 13,026,917 | 12,787,696\n"
        "North America | 2,363,493 | 1,667,002\nAsia-Pacific | 747,653 | 558,191\nEurope | 56,945 | 61,000\n"
    )
    assert quote_reads_table_row("United Kingdom 13,026,917 North America 2,363,493 Asia-Pacific 747,653", table)
    # skipping a word ("North") is not a table reading
    assert not quote_reads_table_row("United Kingdom 13,026,917 America 2,363,493", table)
    # a made-up number is not in the table at all
    assert not quote_reads_table_row("United Kingdom 99,999 North America 2,363,493", table)
    # too short to mean anything
    assert not quote_reads_table_row("Europe", table)


def test_whole_document_quote_check_accepts_a_table_column_reading() -> None:
    from scripts.business_profile_classifier.business_profile_policy import _quote_errors

    sections = {"filed_report": "Segment | 2025 | 2024\nUnited Kingdom | 22,557,801 | 13,932,695\nAustralia | 4,429,428 | 2,310,205\n"}
    assert _quote_errors("geography_served", "United Kingdom 22,557,801 Australia 4,429,428", None, sections) == []
    assert _quote_errors("geography_served", "United Kingdom 22,557,801 Canada 4,429,428", None, sections)


# The fuzzy-match fixtures below are the real quotes gpt-5.4-mini produced on
# the 2026-09-13 runs, beside the passage each was read from.
def test_fuzzy_match_accepts_one_word_drift_in_an_honest_quote() -> None:
    from scripts.business_profile_classifier.business_profile_policy import quote_match_kind

    source = (
        "Insurance risk: the company meet its liabilities. The company manages this risk by only "
        "dealing with accredited brokers who have been through a detailed approval process. "
        "Following accreditation these brokers are reviewed annually."
    )
    assert quote_match_kind(
        "The company managed this risk by only dealing with accredited brokers who have been "
        "through a detailed approval process",
        source,
    ) == "fuzzy"
    # Review of the business: the model wrote the source's own typo correctly.
    source = "The main principle activity of the subsidiary undertakings was that of providing data centre cooling systems. Results and dividends"
    assert quote_match_kind(
        "The principal activity of the subsidiary undertakings was that of providing data centre cooling systems",
        source,
    ) == "fuzzy"
    # A pronoun for its antecedent at the start of the quote.
    source = "Development and performance: the subsidiary will continue to tender for new contracts within the highly competitive market in which they operate in order to grow."
    assert quote_match_kind(
        "They will continue to tender for new contracts within the highly competitive market in which they operate",
        source,
    ) == "fuzzy"


def test_fuzzy_match_is_only_reported_when_nothing_stricter_matched() -> None:
    from scripts.business_profile_classifier.business_profile_policy import quote_match_kind

    source = "The company manages this risk by only dealing with accredited brokers who have been through a detailed approval process."
    assert quote_match_kind("manages this risk by only dealing with accredited brokers", source) == "exact"
    table = "Segment | 2025 | 2024\nUnited Kingdom | 22,557,801 | 13,932,695\nAustralia | 4,429,428 | 2,310,205\n"
    assert quote_match_kind("United Kingdom 22,557,801 Australia 4,429,428", table) == "table_row"


def test_fuzzy_match_rejects_a_sentence_that_is_not_in_the_document() -> None:
    """Johnsons 1871: the one v7 rejection that was a fabrication, not a
    tidy-up. The words are all ordinary, so only the alignment stops it."""
    from scripts.business_profile_classifier.business_profile_policy import quote_match_kind

    source = (
        "The financial statements have been prepared in accordance with FRS 102, the Financial "
        "Reporting Standard applicable in the UK and Republic of Ireland. Turnover represents "
        "amounts receivable for goods supplied net of VAT."
    )
    assert quote_match_kind("The turnover is generated entirely in the UK", source) is None


def test_fuzzy_match_has_a_budget_of_one_word_per_eight_and_a_minimum_length() -> None:
    from scripts.business_profile_classifier.business_profile_policy import quote_match_kind

    source = "the quick brown fox jumps over the lazy dog and then sleeps under the old oak tree"
    # 8 words, budget 1: one substitution passes, two do not.
    assert quote_match_kind("quick brown fox leaps over the lazy dog", source) == "fuzzy"
    assert quote_match_kind("quick brown cat leaps over the lazy dog", source) is None
    # 16 words, budget 2.
    assert quote_match_kind("quick brown cat leaps over the lazy dog and then sleeps under the old oak tree", source) == "fuzzy"
    assert quote_match_kind("quick brown cat leaps over the lazy cow and then sleeps under the old oak tree", source) is None
    # Below the minimum length every word must match.
    assert quote_match_kind("quick brown fox leaps", source) is None
    assert quote_match_kind("quick brown fox jumps", source) == "exact"


def test_fuzzy_match_never_absorbs_a_number_a_negation_or_a_label_bearing_word() -> None:
    """One differing word is within budget for all of these; each is refused
    because of *which* word differs."""
    from scripts.business_profile_classifier.business_profile_policy import quote_match_kind

    source = "Turnover increased by 12% to £4.2m and the company is not dependent on any single customer for its revenue"
    assert quote_match_kind("Turnover increased by 15% to £4.2m and the company is not dependent on any single customer", source) is None
    assert quote_match_kind("Turnover increased by 12% to £4.2m and the company is dependent on any single customer", source) is None
    source = "The group sells its products to businesses across the United Kingdom and has done so for many years"
    assert quote_match_kind("The group sells its products to consumers across the United Kingdom and has done so for many years", source) is None
    source = 'The company ("STM 360") provides an integrated and co-ordinated approach to construction, property and maintenance solutions in both the public and private sectors'
    assert quote_match_kind(
        "The company provides an integrated and co-ordinated approach to construction, property and maintenance solutions in both the public and private sectors",
        source,
    ) is None


def test_fuzzy_match_does_not_accept_a_rewritten_sentence() -> None:
    """Lemon Pepper Topco: same words, reordered. That is a rewrite, and the
    budget is meant to be too small for it."""
    from scripts.business_profile_classifier.business_profile_policy import quote_match_kind

    source = "Principal activity of the company: the principal activity of the group continued to be that of operating restaurants. Results and dividends"
    assert quote_match_kind("the Group's principal activity continued to be that of operating restaurants", source) is None


def test_mark_quote_matches_records_how_each_quote_was_found() -> None:
    from scripts.business_profile_classifier.business_profile_policy import mark_quote_matches, reject_failed_fields, validate_fields

    sections = {"filed_report": "The company manages this risk by only dealing with accredited brokers who have been through a detailed approval process. Community focused professional football club."}
    payload = {
        **VALID_RESPONSE,
        "demand_model": {
            **VALID_RESPONSE["demand_model"],
            "quote": "The company managed this risk by only dealing with accredited brokers who have been through a detailed approval process",
        },
        "delivery_model": {**VALID_RESPONSE["delivery_model"], "quote": "sells season tickets to households in Nantwich"},
    }

    kinds = mark_quote_matches(payload, sections)

    assert kinds["demand_model"] == "fuzzy"
    assert payload["demand_model"]["quote_match"] == "fuzzy"
    assert kinds["customer_type"] == "exact"
    assert "delivery_model" not in kinds and "quote_match" not in payload["delivery_model"]
    field_errors = validate_fields(payload, sections)
    assert set(field_errors) == {"delivery_model"}
    rejected = reject_failed_fields(payload, field_errors)
    assert rejected["demand_model"]["value"] == VALID_RESPONSE["demand_model"]["value"]
    assert rejected["delivery_model"]["value"] is None


def test_retired_values_are_normalised_before_validation_and_scoring() -> None:
    """v8 retired trading_group_parent. A response saved under v7 that used
    it must score as `trading`, not fail validation as an unknown value."""
    from scripts.business_profile_classifier.business_profile_policy import normalise_retired_values, validate_fields
    from scripts.business_profile_classifier.business_profile_metrics import score_case

    payload = {**VALID_RESPONSE, "trading_status_confirmed": {**VALID_RESPONSE["trading_status_confirmed"], "value": "trading_group_parent"}}
    assert "trading_status_confirmed" in validate_fields(payload, SECTIONS)
    assert normalise_retired_values(payload) == ["trading_status_confirmed"]
    assert payload["trading_status_confirmed"]["value"] == "trading"
    assert "trading_status_confirmed" not in validate_fields(payload, SECTIONS)
    # scoring alone also maps it, for callers that skip validation
    case = {"expected": {"trading_status_confirmed": {"value": "trading"}}}
    raw = {"trading_status_confirmed": {"value": "trading_group_parent"}}
    assert score_case(case, raw, ("trading_status_confirmed",))["fields"]["trading_status_confirmed"]["correct"]


def test_v10_demand_value_is_normalised_before_validation_and_scoring() -> None:
    """Saved v9 responses keep scoring after b2b_relationship was renamed."""
    from scripts.business_profile_classifier.business_profile_policy import normalise_retired_values, validate_fields
    from scripts.business_profile_classifier.business_profile_metrics import score_case

    payload = {**VALID_RESPONSE, "demand_model": {**VALID_RESPONSE["demand_model"], "value": "b2b_relationship"}}
    assert "demand_model" in validate_fields(payload, SECTIONS)
    assert normalise_retired_values(payload) == ["demand_model"]
    assert payload["demand_model"]["value"] == "relationship_or_contract"
    assert "demand_model" not in validate_fields(payload, SECTIONS)
    case = {"expected": {"demand_model": {"value": "relationship_or_contract"}}}
    raw = {"demand_model": {"value": "b2b_relationship"}}
    assert score_case(case, raw, ("demand_model",))["fields"]["demand_model"]["correct"]
