#!/usr/bin/env python3
"""Revalidate saved classifier JSONL records without making model calls.

The input is never changed.  This tool writes a second JSONL audit so an old
run can be inspected under the current structural contract without pretending
that it was re-run against newly fetched source material.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from companies_house_core.llm_validation import JsonResponseError, not_checked, parse_json_object, validate_object


def _records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            records.append({"_line": line_number, "_input_problem": "invalid JSONL line"})
            continue
        if isinstance(record, dict):
            records.append(record | {"_line": line_number})
    return records


def _context(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    return {str(item.get("company_number") or item.get("case_id")): item for item in _records(path)
            if item.get("company_number") is not None or item.get("case_id") is not None}


def _raw(record: dict[str, Any]) -> str | None:
    for key in ("raw", "raw_response"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return None


def _identity(record: dict[str, Any]) -> str | None:
    value = record.get("company_number") or record.get("case_id")
    return str(value) if value is not None else None


def _structural(raw: str | None, model: type[Any] | None = None) -> dict[str, Any]:
    try:
        parsed = parse_json_object(raw)
    except JsonResponseError as exc:
        return exc.validation
    if model is None:
        return parsed.validation
    _, validation = validate_object(parsed, model)
    return validation


def _audit_business(record: dict[str, Any], context: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    from scripts.business_profile_classifier import business_profile_policy as policy

    raw = _raw(record)
    try:
        payload, parsed_validation = policy.parse_json_response_with_validation(raw or "")
    except JsonResponseError as exc:
        return exc.validation, None
    validation = policy.structure_validation(payload)
    validation = {**parsed_validation, **validation, "repairs": parsed_validation["repairs"],
                  "parse_method": parsed_validation["parse_method"]}
    if context and isinstance(context.get("sections"), dict):
        field_errors = policy.validate_fields(payload, context["sections"])
        if field_errors:
            validation["status"] = "partial"
            validation["errors"] += [
                {"path": field, "code": "policy_validation", "message": message}
                for field, messages in field_errors.items() for message in messages
            ]
    else:
        validation["evidence"] = not_checked("saved source sections were not supplied")
    return validation, payload


def _audit_screen(record: dict[str, Any], context: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    from scripts.search_screen_classifier import search_screen_policy as policy

    text = (context or {}).get("text") or record.get("text")
    parsed = policy.parse_answer(_raw(record), text or "")
    validation = parsed["validation"]
    if not text:
        validation["evidence"] = not_checked("saved screen text was not supplied")
    return validation, parsed


def _audit_web_profile(record: dict[str, Any], context: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    from scripts.website_analysis import web_profile_policy as policy

    context = context or {}
    parsed = policy.parse_profile(_raw(record), context.get("text") or record.get("text") or "",
                                  principal_activity=context.get("principal_activity"),
                                  listing_category=context.get("listing_category"),
                                  allowed_categories=context.get("categories") or (),
                                  brand_terms=context.get("brand_terms") or ())
    validation = parsed["validation"]
    if not (context.get("text") or record.get("text")):
        validation["evidence"] = not_checked("saved website text was not supplied")
    return validation, parsed


def _audit_identity(record: dict[str, Any], context: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    from scripts.website_analysis import web_settle_model as policy

    inputs = (context or {}).get("inputs") or record.get("inputs")
    if not isinstance(inputs, dict):
        validation = _structural(_raw(record), policy._IdentityVerdictResponse)
        validation["evidence"] = not_checked("saved identity inputs were not supplied")
        return validation, None
    parsed = policy.parse_verdict(_raw(record), inputs)
    return parsed["validation"], parsed


def _audit_vlm(record: dict[str, Any], context: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    from scripts.pdf_vision_extraction import companies_house_pdf_vlm_financials as policy

    raw = _raw(record)
    try:
        parsed = parse_json_object(raw)
    except JsonResponseError as exc:
        return exc.validation, None
    try:
        validation = policy.validate_page_response(
            parsed.payload, require_rows=bool((context or {}).get("require_rows", False)),
            expected_page_count=(context or {}).get("expected_page_count"),
        )
    except Exception as exc:  # validation adapters intentionally return an audit rather than stop a batch
        validation = getattr(exc, "validation", parsed.validation)
    if context is None:
        validation["evidence"] = not_checked("saved VLM page-count settings were not supplied")
    return validation, parsed.payload


ADAPTERS: dict[str, Callable[[dict[str, Any], dict[str, Any] | None], tuple[dict[str, Any], Any]]] = {
    "business-profile": _audit_business,
    "search-screen": _audit_screen,
    "web-profile": _audit_web_profile,
    "website-identity": _audit_identity,
    "vlm": _audit_vlm,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", choices=sorted(ADAPTERS), required=True)
    parser.add_argument("--input", type=Path, required=True, help="existing JSONL results or checkpoint")
    parser.add_argument("--output", type=Path, required=True, help="new JSONL audit file")
    parser.add_argument("--context", type=Path, help="optional JSONL with source context keyed by company_number")
    args = parser.parse_args(argv)

    contexts = _context(args.context)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in _records(args.input):
            identifier = _identity(record)
            validation, parsed = ADAPTERS[args.pipeline](record, contexts.get(identifier) if identifier else None)
            original = {key: record.get(key) for key in ("answer", "passes", "verdict", "problem", "extracted") if key in record}
            updated = {key: parsed.get(key) for key in ("answer", "passes", "verdict", "problem") if isinstance(parsed, dict) and key in parsed}
            audit = {"line": record["_line"], "company_number": identifier, "pipeline": args.pipeline,
                     "validation": validation, "original": original, "revalidated": updated,
                     "changed": original != updated}
            handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
