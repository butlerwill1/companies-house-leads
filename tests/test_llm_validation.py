from __future__ import annotations

import pytest
from pydantic import Field

from companies_house_core.llm_validation import JsonResponseError, StrictResponseModel, parse_json_object, validate_object


class _Response(StrictResponseModel):
    name: str
    confidence: float = Field(ge=0, le=1)


def test_parser_records_fence_and_trailing_comma_repairs_without_touching_strings() -> None:
    parsed = parse_json_object('```json\n{"name":"x,}","confidence":0.5,}\n```')
    assert parsed.payload == {"name": "x,}", "confidence": 0.5}
    assert parsed.validation["repairs"] == ["removed_markdown_fence", "removed_trailing_commas"]


@pytest.mark.parametrize("raw", [
    '{"name":"x"',
    '{"name":"x"} {"confidence":0.5}',
    '{"name":"x","name":"y"}',
    '{"name":NaN,"confidence":0.5}',
    '["not", "an", "object"]',
])
def test_parser_rejects_ambiguous_or_unsafe_json(raw: str) -> None:
    with pytest.raises(JsonResponseError):
        parse_json_object(raw)


def test_pydantic_validation_is_strict_and_warns_for_unknown_fields() -> None:
    parsed = parse_json_object('{"name":"x","confidence":0.5,"extra":"kept only in raw"}')
    response, validation = validate_object(parsed, _Response)
    assert response is not None
    assert validation["status"] == "partial"
    assert validation["warnings"][0]["path"] == "extra"

    _, invalid = validate_object(parse_json_object('{"name":"x","confidence":true}'), _Response)
    assert invalid["status"] == "invalid"
    assert invalid["errors"][0]["path"] == "confidence"
