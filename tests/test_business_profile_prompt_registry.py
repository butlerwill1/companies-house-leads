from __future__ import annotations

import pytest

from scripts.profile.business_profile_policy import PROMPT_TEMPLATE
from scripts.profile.business_profile_prompt_registry import (
    _to_mlflow_template,
    verify_prompt_round_trips,
)


def test_single_brace_field_becomes_a_double_brace_mlflow_variable():
    assert _to_mlflow_template("Hello {name}") == "Hello {{name}}"


def test_doubled_braces_collapse_to_a_literal_single_brace():
    """Python's str.format() escape ({{ / }}) for a literal brace has no
    equivalent need in MLflow's templates -- a bare single brace is already
    literal there, since only {{var}} is special."""
    assert _to_mlflow_template('{{"value": "..."}}') == '{"value": "..."}'


def test_mixed_literal_braces_and_a_real_field_convert_correctly():
    template = 'Answer: {{"value": "...", "field": "{name}"}}'
    assert _to_mlflow_template(template) == 'Answer: {"value": "...", "field": "{{name}}"}'


def test_the_real_prompt_template_round_trips_exactly():
    """The actual template this module exists to keep in sync -- converted
    and rendered with representative inputs, it must produce exactly what
    build_prompt() sends the model. This is the guard against a conversion
    mistake reaching the registered prompt silently."""
    converted = _to_mlflow_template(PROMPT_TEMPLATE)
    verify_prompt_round_trips(converted)  # raises on any mismatch


def test_a_broken_conversion_is_caught_not_silently_registered():
    """Sanity check on the checker itself: an obviously wrong conversion
    (a field renamed) must fail the round-trip, not pass silently."""
    broken = _to_mlflow_template(PROMPT_TEMPLATE).replace("{{company_name}}", "{{wrong_field_name}}")
    with pytest.raises(Exception):
        verify_prompt_round_trips(broken)
