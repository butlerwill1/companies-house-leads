from __future__ import annotations

import pytest

from scripts.langfuse_eval_helpers.langfuse_prompts import to_langfuse_template
from scripts.business_profile_classifier.business_profile_policy import PROMPT_TEMPLATE
from scripts.business_profile_classifier.business_profile_prompt_registry import (
    registered_prompt_reference,
    verify_prompt_round_trips,
)
from tests.langfuse_fakes import FakeLangfuse


def test_single_brace_field_becomes_a_double_brace_variable():
    assert to_langfuse_template("Hello {name}") == "Hello {{name}}"


def test_doubled_braces_collapse_to_a_literal_single_brace():
    assert to_langfuse_template('{{"value": "..."}}') == '{"value": "..."}'


def test_mixed_literal_braces_and_a_real_field_convert_correctly():
    template = 'Answer: {{"value": "...", "field": "{name}"}}'
    assert to_langfuse_template(template) == 'Answer: {"value": "...", "field": "{{name}}"}'


def test_the_real_prompt_template_round_trips_exactly():
    """The actual template this module keeps in sync -- converted and rendered
    with representative inputs, it must produce exactly what build_prompt()
    sends the model."""
    verify_prompt_round_trips(to_langfuse_template(PROMPT_TEMPLATE))  # raises on mismatch


def test_a_broken_conversion_is_caught_not_silently_registered():
    broken = to_langfuse_template(PROMPT_TEMPLATE).replace("{{company_name}}", "{{wrong_field_name}}")
    with pytest.raises(Exception):
        verify_prompt_round_trips(broken)


def test_registered_prompt_reference_none_without_client():
    assert registered_prompt_reference(None) is None


def test_registered_prompt_reference_matches_version_tag():
    from scripts.business_profile_classifier.business_profile_prompt_registry import register_current_prompt
    from scripts.business_profile_classifier.business_profile_policy import PROMPT_VERSION

    client = FakeLangfuse()
    register_current_prompt(client)
    assert registered_prompt_reference(client, PROMPT_VERSION) == (
        f"business-profile-extraction@{PROMPT_VERSION} [langfuse v1]"
    )
    assert registered_prompt_reference(client, "some-other-version") is None


def test_semantic_version_is_applied_as_a_langfuse_label_not_just_a_tag():
    """Langfuse's own `version` counts registrations and cannot be set, so the
    semantic version has to be a label to be addressable. Without this the
    reference can only quote the auto-number, which is what made a
    `business-profile-v5` code state report as `@4`."""
    from scripts.business_profile_classifier.business_profile_policy import PROMPT_VERSION
    from scripts.business_profile_classifier.business_profile_prompt_registry import register_current_prompt

    client = FakeLangfuse()
    published = register_current_prompt(client)
    assert PROMPT_VERSION in published.labels
    assert "production" in published.labels
    assert PROMPT_VERSION in published.tags


def test_a_later_registration_rewrites_every_versions_tag():
    """Langfuse tags are per-prompt: publishing v7 retags v6's entry too. This
    is why the reference must not identify a version by its tag."""
    from scripts.langfuse_eval_helpers.langfuse_prompts import register_prompt

    client = FakeLangfuse()
    register_prompt(client, name="bp", python_format_template="Hi {name}", version_tag="v6")
    register_prompt(client, name="bp", python_format_template="Hi {name} again", version_tag="v7")

    first, second = client.prompts["bp"]
    assert first.tags == ["v7"], "the older version's tag was rewritten by the newer registration"
    assert second.tags == ["v7"]
    # Labels stay per-version and keep saying what each entry actually is.
    assert "v6" in first.labels
    assert "v7" in second.labels


def test_reference_follows_production_label_not_the_rewritten_tag():
    """Roll production back to an older entry and the reference must report
    that entry's semantic version. Reading tags gave the newest sync's version
    instead, silently claiming a run used a prompt it did not."""
    from scripts.langfuse_eval_helpers.langfuse_prompts import register_prompt, registered_prompt_reference

    client = FakeLangfuse()
    register_prompt(client, name="bp", python_format_template="a {name}", version_tag="v6")
    register_prompt(client, name="bp", python_format_template="b {name}", version_tag="v7")

    older, newer = client.prompts["bp"]
    newer.labels = [lbl for lbl in newer.labels if lbl != "production"]
    older.labels = [*older.labels, "production"]

    assert registered_prompt_reference(client, name="bp", expected_version_tag="v6") == "bp@v6 [langfuse v1]"
    assert registered_prompt_reference(client, name="bp", expected_version_tag="v7") is None


def test_registered_text_contains_the_gloss_not_just_a_placeholder():
    """The defect that made v4 and v5 register byte-identical text: every
    taxonomy change lives in the option blocks, so a skeleton-only entry
    records none of them."""
    from scripts.business_profile_classifier.business_profile_prompt_registry import register_current_prompt

    client = FakeLangfuse()
    published = register_current_prompt(client)

    assert "{{trading_status_confirmed_options}}" not in published.prompt
    assert "including a PFI project company" in published.prompt
    # Per-case variables must still be placeholders -- this is a template.
    for variable in ("{{company_name}}", "{{sections_block}}", "{{sic_label}}", "{{sic_code}}"):
        assert variable in published.prompt


def test_a_gloss_change_alone_produces_a_different_registered_text():
    """The property that was missing: two registrations differing only in a
    gloss must not be byte-identical in Langfuse."""
    from scripts.langfuse_eval_helpers import langfuse_prompts as LP
    from scripts.business_profile_classifier import business_profile_policy as policy
    from scripts.business_profile_classifier.business_profile_prompt_registry import register_current_prompt

    client = FakeLangfuse()
    before = register_current_prompt(client).prompt

    original = policy.FIELD_DEFINITIONS["trading_status_confirmed"]["spv"]
    policy.FIELD_DEFINITIONS["trading_status_confirmed"]["spv"] = "a completely rewritten gloss"
    try:
        after = register_current_prompt(client).prompt
    finally:
        policy.FIELD_DEFINITIONS["trading_status_confirmed"]["spv"] = original

    assert before != after
    assert "a completely rewritten gloss" in after
