from __future__ import annotations

import pytest

from scripts.eval_support import langfuse_prompts as P
from tests.langfuse_fakes import FakeLangfuse


def test_single_brace_field_becomes_double_brace() -> None:
    assert P.to_langfuse_template("Hello {name}") == "Hello {{name}}"


def test_doubled_braces_collapse_to_literal() -> None:
    assert P.to_langfuse_template('{{"value": "..."}}') == '{"value": "..."}'


def test_mixed_literal_and_field() -> None:
    template = 'Answer: {{"value": "...", "field": "{name}"}}'
    assert P.to_langfuse_template(template) == 'Answer: {"value": "...", "field": "{{name}}"}'


def test_round_trip_render_matches_python_format() -> None:
    template = 'Company {name} in {sic_label} ({sic_code}). {{"json": true}}'
    values = {"name": "ACME LTD", "sic_label": "Retail", "sic_code": "47910"}
    langfuse_template = P.to_langfuse_template(template)
    assert P.render_langfuse_template(langfuse_template, values) == template.format(**values)


def test_registered_prompt_reference_matches_and_mismatches() -> None:
    client = FakeLangfuse()
    P.register_prompt(client, name="bp", python_format_template="Hi {name}", version_tag="v2")
    assert P.registered_prompt_reference(client, name="bp", expected_version_tag="v2") == "bp@1"
    assert P.registered_prompt_reference(client, name="bp", expected_version_tag="v9") is None
    assert P.registered_prompt_reference(client, name="missing", expected_version_tag="v2") is None
