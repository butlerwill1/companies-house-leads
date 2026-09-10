from __future__ import annotations

import pytest

from scripts.eval_support.langfuse_prompts import to_langfuse_template
from scripts.profile.business_profile_policy import PROMPT_TEMPLATE
from scripts.profile.business_profile_prompt_registry import (
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
    from scripts.profile.business_profile_prompt_registry import register_current_prompt
    from scripts.profile.business_profile_policy import PROMPT_VERSION

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
    from scripts.profile.business_profile_policy import PROMPT_VERSION
    from scripts.profile.business_profile_prompt_registry import register_current_prompt

    client = FakeLangfuse()
    published = register_current_prompt(client)
    assert PROMPT_VERSION in published.labels
    assert "production" in published.labels
    assert PROMPT_VERSION in published.tags
