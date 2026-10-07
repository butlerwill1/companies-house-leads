"""Shared, conservative validation for JSON returned by language models.

This module deliberately separates three concerns:

* turning a response into a JSON object without making up content;
* reporting structural problems in a stable, serialisable form; and
* validating an object with a Pydantic model.

It does not decide whether an answer is supported by a filing or a web page.
Those checks remain with the policy that knows the source material.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

VALIDATION_VERSION = "classifier-validation-v1"
T = TypeVar("T", bound=BaseModel)


class StrictResponseModel(BaseModel):
    """Base model for model responses.

    Unknown keys are retained by Pydantic only long enough to report them. They
    are never included when a caller serialises the typed model for storage.
    """

    model_config = ConfigDict(strict=True, extra="allow")


@dataclass(frozen=True)
class JsonResponse:
    payload: dict[str, Any]
    validation: dict[str, Any]


class JsonResponseError(ValueError):
    """A response could not safely become a JSON object."""

    def __init__(self, message: str, validation: dict[str, Any]) -> None:
        super().__init__(message)
        self.validation = validation


class ResponseValidationError(ValueError):
    """A parsed response did not fit a response model."""

    def __init__(self, message: str, validation: dict[str, Any]) -> None:
        super().__init__(message)
        self.validation = validation


def _issue(path: tuple[Any, ...] | list[Any] | str, code: str, message: str) -> dict[str, Any]:
    if isinstance(path, str):
        rendered = path
    else:
        rendered = ".".join(str(part) for part in path) or "$"
    return {"path": rendered, "code": code, "message": message}


def _base(*, status: str = "valid", parse_method: str = "strict", repairs: list[str] | None = None,
          normalisations: list[dict[str, Any]] | None = None, errors: list[dict[str, Any]] | None = None,
          warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "version": VALIDATION_VERSION,
        "status": status,
        "parse_method": parse_method,
        "repairs": repairs or [],
        "normalisations": normalisations or [],
        "errors": errors or [],
        "warnings": warnings or [],
    }


def not_checked(reason: str) -> dict[str, Any]:
    return _base(status="not_checked", errors=[_issue("$", "not_checked", reason)])


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _loads_object(text: str) -> dict[str, Any]:
    value = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def _remove_trailing_commas(text: str) -> str:
    """Remove commas before closing containers without touching string data."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _complete_object_spans(text: str) -> list[tuple[int, int]]:
    """Return complete top-level object spans, ignoring braces in strings."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start, index + 1))
    return spans


def parse_json_object(raw: str | None) -> JsonResponse:
    """Parse JSON with only syntax repairs that cannot introduce values."""
    if raw is None or not raw.strip():
        validation = _base(status="invalid", errors=[_issue("$", "empty_response", "model response is empty")])
        raise JsonResponseError("model response is empty", validation)

    original = raw.strip()
    candidates: list[tuple[str, str, list[str]]] = [("strict", original, [])]
    fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", original, re.IGNORECASE)
    if fence:
        candidates.append(("markdown_fence", fence.group(1), ["removed_markdown_fence"]))

    seen: set[str] = set()
    parse_errors: list[str] = []
    for method, candidate, repairs in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        variants = [(method, candidate, repairs)]
        repaired = _remove_trailing_commas(candidate)
        if repaired != candidate:
            variants.append((method + "+trailing_commas", repaired, repairs + ["removed_trailing_commas"]))
        for variant_method, text, variant_repairs in variants:
            try:
                return JsonResponse(_loads_object(text), _base(parse_method=variant_method, repairs=variant_repairs))
            except (ValueError, json.JSONDecodeError) as exc:
                parse_errors.append(str(exc))

    # Only extract when there is exactly one complete top-level object. This
    # avoids choosing a response when prose contains two possible answers.
    spans = _complete_object_spans(original)
    if len(spans) == 1:
        start, end = spans[0]
        extracted = original[start:end]
        for method, text, repairs in [
            ("extracted_object", extracted, ["extracted_json_object"]),
            ("extracted_object+trailing_commas", _remove_trailing_commas(extracted),
             ["extracted_json_object", "removed_trailing_commas"]),
        ]:
            try:
                return JsonResponse(_loads_object(text), _base(parse_method=method, repairs=repairs))
            except (ValueError, json.JSONDecodeError) as exc:
                parse_errors.append(str(exc))
    elif len(spans) > 1:
        parse_errors.append("response contains multiple complete JSON objects")

    message = parse_errors[-1] if parse_errors else "response is not valid JSON"
    validation = _base(status="invalid", errors=[_issue("$", "invalid_json", message)])
    raise JsonResponseError(message, validation)


def _extra_warnings(model: BaseModel, prefix: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for name in (model.model_extra or {}):
        warnings.append(_issue((*prefix, name), "unexpected_field", "field is not part of the response contract"))
    for name in type(model).model_fields:
        value = getattr(model, name)
        path = (*prefix, name)
        if isinstance(value, BaseModel):
            warnings.extend(_extra_warnings(value, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, BaseModel):
                    warnings.extend(_extra_warnings(item, (*path, index)))
    return warnings


def validate_object(parsed: JsonResponse, model_type: type[T]) -> tuple[T | None, dict[str, Any]]:
    """Validate a parsed object and return a serialisable diagnostic report."""
    try:
        model = model_type.model_validate(parsed.payload)
    except ValidationError as exc:
        errors = [_issue(tuple(error["loc"]), str(error["type"]), error["msg"]) for error in exc.errors()]
        validation = {**parsed.validation, "status": "invalid", "errors": errors}
        return None, validation
    warnings = _extra_warnings(model)
    validation = {
        **parsed.validation,
        "status": "partial" if warnings else "valid",
        "warnings": warnings,
    }
    return model, validation


def validation_message(validation: dict[str, Any]) -> str:
    issues = validation.get("errors") or []
    if not issues:
        return "response failed validation"
    return "; ".join(f"{issue.get('path')}: {issue.get('message')}" for issue in issues)


def merge_validation(base: dict[str, Any], *reports: dict[str, Any]) -> dict[str, Any]:
    """Combine independent field checks without losing parse information."""
    errors = [*base.get("errors", [])]
    warnings = [*base.get("warnings", [])]
    normalisations = [*base.get("normalisations", [])]
    for report in reports:
        errors.extend(report.get("errors", []))
        warnings.extend(report.get("warnings", []))
        normalisations.extend(report.get("normalisations", []))
    status = "invalid" if errors else "partial" if warnings else base.get("status", "valid")
    return {**base, "status": status, "errors": errors, "warnings": warnings,
            "normalisations": normalisations}


def strict_confidence(value: Any) -> float:
    """Shared check useful for policies with dynamically selected enums."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("confidence must be a finite number")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    return float(value)
