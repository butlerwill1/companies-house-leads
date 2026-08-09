#!/usr/bin/env python3
"""VLM extraction for financial statements in Companies House PDFs.

This pipeline never invokes local OCR. A low-resolution vision pass finds
statement pages, a second vision pass reads only those pages, and a text-only
LLM rationalises the resulting evidence-backed candidates. The model transport
is replaceable: OpenRouter and a private Ollama GPU use the identical process.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import requests

from companies_house_extractor import load_dotenv, parse_financial_year
from companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.ocr.financial_metric_policy import (
    INSURANCE_METRICS,
    add_canonical_equivalents,
)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_LOCATOR_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash"
DEFAULT_RATIONALISATION_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
PRIVATE_OLLAMA_BASE_URL_ENV = "PRIVATE_OLLAMA_BASE_URL"
METRICS = (
    "turnover", "cost_of_sales", "gross_profit", "administrative_expenses",
    "operating_result", "profit_before_tax", "tax", "profit_after_tax",
    "current_assets", "cash", "net_current_assets", "net_assets", "employees",
) + INSURANCE_METRICS
MONEY_METRICS = set(METRICS) - {"employees"}
CANONICAL_METRICS = (
    "turnover", "gross_profit", "operating_result", "profit_after_tax",
    "cash", "net_assets", "employees",
)
UNIT_MULTIPLIERS = {"GBP": 100, "GBP_THOUSANDS": 100_000, "GBP_MILLIONS": 100_000_000}
PRIMARY_STATEMENT_TYPES = {"income_statement", "balance_sheet", "cash_flow"}
STATEMENT_SCOPES = {"consolidated_group", "company", "unknown"}

LOCATOR_PROMPT = """You are identifying financial statement pages in a UK Companies House accounts PDF.
Return only JSON with one object for every supplied image, in exactly the same order:
{"pages":[{"statement_type":"income_statement|balance_sheet|cash_flow|other","statement_scope":"consolidated_group|company|unknown","contains_employee_count":false,"confidence":0.0,"reason":"short"}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result.
Mark an image as a statement only if it contains the relevant primary financial table or an obvious continuation of it. Set `statement_scope` to `consolidated_group` only when its heading says Consolidated, Group, or equivalent; set it to `company` when its heading says Company or Parent Company; otherwise use `unknown`. Set `contains_employee_count` true only when the page visibly discloses a total or average employee/persons-employed count; staff-cost amounts alone are not employee-count evidence. Do not extract figures."""

EXTRACTION_PROMPT = """Read these numbered pages from a UK Companies House accounts filing. Extract only rows visibly present in a primary income statement, balance sheet, or cash-flow statement.
Return only JSON using this schema, with one page object for every supplied image in exactly the same order:
{"pages":[{"statement_type":"income_statement|balance_sheet|cash_flow|other","statement_scope":"consolidated_group|company|unknown","unit":"GBP|GBP_THOUSANDS|GBP_MILLIONS|UNKNOWN","rows":[{"metric":"turnover|cost_of_sales|gross_profit|administrative_expenses|operating_result|profit_before_tax|tax|profit_after_tax|current_assets|cash|net_current_assets|net_assets|employees|gross_premiums_written|outward_reinsurance_premiums|net_premiums_written|net_change_unearned_premiums|net_earned_premiums|allocated_investment_return|total_technical_income|claims_incurred_net_reinsurance|net_operating_expenses|technical_account_result|investment_income","source_label":"exact row label","current_display":"exact displayed number or null","previous_display":"exact displayed number or null","current_column":"exact current column heading or null","previous_column":"exact previous column heading or null","evidence_text":"short transcription of the row and headings","confidence":0.0}]}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result. If an image has no primary-statement rows, return it with `statement_type` `other` and `rows`: []. Retain the displayed sign, commas, parentheses, dashes and scale; do not convert units; never use a year column heading as a value; use null rather than guessing; the current period is the column headed by the most recent financial period end date, not simply the left-most column.

Set `statement_scope` from the visible statement heading using the same meanings as the locator. For a general insurance technical account, transcribe the native rows rather than guessing generic equivalents: Gross premiums written = gross_premiums_written; Earned premiums, net of reinsurance = net_earned_premiums; Claims incurred, net of reinsurance = claims_incurred_net_reinsurance; Balance on the technical account for general business = technical_account_result. Use the other insurance-specific metric names when their matching rows are visible. Do not relabel these native rows as turnover, gross_profit or operating_result; deterministic code performs that mapping later."""

EMPLOYEE_EXTRACTION_PROMPT = """Read these pages from a UK Companies House accounts filing. They were selected because they may disclose employee numbers.
Return only JSON with one object for every supplied image in exactly the same order:
{"pages":[{"statement_type":"employee_note|other","unit":"COUNT","rows":[{"metric":"employees","source_label":"exact row label","current_display":"exact displayed number or null","previous_display":"exact displayed number or null","current_column":"exact current column heading or null","previous_column":"exact previous column heading or null","evidence_text":"short transcription of the row and headings","confidence":0.0}]}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result. Extract only a disclosed total or average number of employees/persons employed. Do not use staff-cost amounts, director counts, or individual employee categories when a total is not shown. If the page has no qualifying employee count, return `statement_type` `other` and `rows`: []. Preserve displayed signs, commas and headings; do not infer a value."""

ROW_VALIDATION_RECOVERY_PROMPT = """Re-read this financial-statement page carefully. A deterministic evidence check found a possible row transcription or classification problem in an earlier pass.
Return the same ordered JSON schema as the normal financial-row extraction. Re-transcribe only rows visibly present in the primary statement, with exact source labels, values, units and column headings. Do not infer totals, substitute a nearby subtotal, or use a year heading as a value."""

HIGH_RESOLUTION_RECOVERY_PROMPT = """This is a higher-resolution, single-page fallback for a primary financial statement page that the first vision pass did not transcribe adequately. Re-read the table and return the normal ordered JSON schema with this one page and every relevant visible row. Preserve exact signs, values, units and column headings. Do not return an empty page merely because the layout is difficult."""

RATIONALISATION_PROMPT = """You are a text-only financial-data reviewer. Choose only from the supplied candidates, which were transcribed from financial-statement images. Do not invent values or alter digits.

Your job is to rationalise the candidates into the exact canonical financial-summary shape used by the XHTML/iXBRL extraction. Return ONLY JSON in this form:
{"financial_period_summaries":{"current":{"turnover":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"gross_profit":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"operating_result":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"profit_after_tax":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"cash":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"net_assets":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"employees":{"candidate_id":"id","reason":"short","confidence":0.0}|null},"previous":{"turnover":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"gross_profit":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"operating_result":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"profit_after_tax":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"cash":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"net_assets":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"employees":{"candidate_id":"id","reason":"short","confidence":0.0}|null}}}.

For `current`, the code will use the chosen candidate's `current_display`; for `previous`, it will use `previous_display`. A visible dash (`-`, en dash or em dash) in a monetary statement cell is a valid reported zero, not an absent value. When the same suitable row visibly supplies both period cells, select that row for both periods, including where one cell is a dash. Select only a candidate with the same metric name as the target column. Prefer primary-statement rows with clear period headings. The candidate list has already applied the filing-scope policy: when both Consolidated Group and Company statements exist for a statement type, Company candidates for that statement type are excluded. Candidates with `derivation.policy` set to `general_insurance` are deterministic canonical equivalents produced from transcribed insurance rows; they are valid when their source rows and units are clear, and you must not recalculate them. Reject dates/year headings, unknown units, conflicting labels, and uncertain candidates. A null field is correct when no suitable evidence exists."""


@dataclass(frozen=True)
class RenderedPage:
    page: int
    image_b64: str


@dataclass(frozen=True)
class ModelCallResult:
    """Provider-neutral result for one JSON-producing model call."""

    payload: dict[str, Any]
    usage: dict[str, Any]
    elapsed_seconds: float
    image_payload_bytes: int = 0
    model_reported_seconds: float | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    raw_response: str | None = None
    response_handling: dict[str, Any] = field(default_factory=dict)
    response_attempts: list[dict[str, Any]] = field(default_factory=list)


class ModelResponseError(RuntimeError):
    """A provider responded, but its text could not become valid stage JSON."""

    def __init__(self, message: str, attempt: dict[str, Any]) -> None:
        super().__init__(message)
        self.attempt = attempt


class StageCallError(RuntimeError):
    """All permitted response attempts for one pipeline stage failed."""

    def __init__(self, stage: str, attempts: list[dict[str, Any]]) -> None:
        last_error = attempts[-1].get("error") if attempts else "unknown response error"
        super().__init__(f"{stage} failed after {len(attempts)} attempt(s): {last_error}")
        self.stage = stage
        self.attempts = attempts


class VlmModelClient(Protocol):
    """Boundary between the PDF extraction flow and a model transport."""

    provider_name: str

    def generate_json(
        self, model: str, prompt: str, pages: list[RenderedPage], timeout: int
    ) -> ModelCallResult:
        """Return one JSON response for text plus zero or more rendered PDF pages."""

        ...

    def pricing_snapshot(self) -> dict[str, dict[str, str]]:
        """Return current token prices when the provider exposes them."""

        ...


def _remove_trailing_json_commas(text: str) -> str:
    """Remove commas before closing containers, but never commas inside strings."""
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


def _json_response_with_handling(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse JSON, allowing only syntax repairs that cannot invent values."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", cleaned)
    if fenced:
        cleaned = fenced.group(1)
    try:
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError("model response must be a JSON object")
        return payload, {"method": "strict", "repaired": False}
    except (json.JSONDecodeError, ValueError) as strict_error:
        candidates: list[tuple[str, str]] = []
        trailing_commas_removed = _remove_trailing_json_commas(cleaned)
        if trailing_commas_removed != cleaned:
            candidates.append(("removed_trailing_commas", trailing_commas_removed))
        first_brace, last_brace = cleaned.find("{"), cleaned.rfind("}")
        if first_brace > 0 and last_brace > first_brace:
            extracted = cleaned[first_brace:last_brace + 1]
            candidates.append(("extracted_json_object", extracted))
            extracted_without_trailing = _remove_trailing_json_commas(extracted)
            if extracted_without_trailing != extracted:
                candidates.append(
                    ("extracted_json_object+removed_trailing_commas", extracted_without_trailing)
                )
        for method, candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload, {
                    "method": method,
                    "repaired": True,
                    "strict_error": str(strict_error),
                }
        raise strict_error


def _json_response(text: str) -> dict[str, Any]:
    return _json_response_with_handling(text)[0]


def _response_result(
    raw_response: Any,
    *,
    usage: dict[str, Any],
    elapsed_seconds: float,
    image_payload_bytes: int,
    model_reported_seconds: float | None = None,
    provider_metadata: dict[str, Any] | None = None,
) -> ModelCallResult:
    """Build a parsed result or an exception that retains the complete response."""
    if not isinstance(raw_response, str):
        error = "model response content must be text"
        raise ModelResponseError(
            error,
            {
                "status": "invalid_json",
                "error": error,
                "raw_response": raw_response,
                "usage": usage,
                "elapsed_seconds": round(elapsed_seconds, 4),
                "image_payload_bytes": image_payload_bytes,
                "model_reported_seconds": model_reported_seconds,
                "provider_metadata": provider_metadata or {},
            },
        )
    try:
        payload, handling = _json_response_with_handling(raw_response)
    except (json.JSONDecodeError, ValueError) as error:
        attempt = {
            "status": "invalid_json",
            "error": str(error),
            "raw_response": raw_response,
            "usage": usage,
            "elapsed_seconds": round(elapsed_seconds, 4),
            "image_payload_bytes": image_payload_bytes,
            "model_reported_seconds": model_reported_seconds,
            "provider_metadata": provider_metadata or {},
        }
        raise ModelResponseError(str(error), attempt) from error
    return ModelCallResult(
        payload=payload,
        usage=usage,
        elapsed_seconds=elapsed_seconds,
        image_payload_bytes=image_payload_bytes,
        model_reported_seconds=model_reported_seconds,
        provider_metadata=provider_metadata or {},
        raw_response=raw_response,
        response_handling=handling,
    )


def render_pages(
    pdf_path: Path,
    *,
    max_pages: int | None,
    long_edge: int,
    page_numbers: list[int] | None = None,
) -> list[RenderedPage]:
    if fitz is None:
        raise RuntimeError("PyMuPDF is required. Install it with: pip install pymupdf")
    document = fitz.open(str(pdf_path))
    try:
        count = min(document.page_count, max_pages) if max_pages else document.page_count
        result: list[RenderedPage] = []
        numbers = page_numbers if page_numbers is not None else list(range(1, count + 1))
        for number in numbers:
            if not 1 <= number <= count:
                raise ValueError(f"requested page {number} is outside the rendered range")
            page = document.load_page(number - 1)
            scale = long_edge / max(page.rect.width, page.rect.height)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            result.append(RenderedPage(number, base64.b64encode(pixmap.tobytes("jpeg")).decode("ascii")))
        return result
    finally:
        document.close()


def page_content(pages: list[RenderedPage], prompt: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in pages:
        content.extend((
            {"type": "text", "text": f"Document page {item.page}"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{item.image_b64}"}},
        ))
    return content


def combine_model_calls(calls: list[ModelCallResult], *, pages: list[dict[str, Any]]) -> ModelCallResult:
    """Combine independent page-batch calls into one provider-neutral result."""
    usage: dict[str, Any] = {}
    for call in calls:
        for key, value in call.usage.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    reported = [call.model_reported_seconds for call in calls if call.model_reported_seconds is not None]
    provider_calls = [call.provider_metadata for call in calls if call.provider_metadata]
    attempts = [attempt for call in calls for attempt in call.response_attempts]
    return ModelCallResult(
        {"pages": pages},
        usage,
        sum(call.elapsed_seconds for call in calls),
        sum(call.image_payload_bytes for call in calls),
        sum(reported) if reported else None,
        {"calls": provider_calls} if provider_calls else {},
        response_attempts=attempts,
    )


def attach_document_pages(
    returned_pages: list[dict[str, Any]], batch: list[RenderedPage]
) -> list[dict[str, Any]]:
    """Attach code-owned PDF page numbers to ordered VLM image results.

    A model can see an image's position and optional audit label, but it has no
    reliable access to PDF metadata. Page identity therefore stays outside the
    model contract: schema validation ensures one result per supplied image,
    then this function assigns each result its originating rendered page.
    Any model-supplied ``page`` key is deliberately discarded.
"""
    if len(returned_pages) != len(batch):
        raise ValueError(
            "response.pages count must equal the number of supplied images "
            f"({len(batch)}); received {len(returned_pages)}"
        )
    return [
        {key: value for key, value in item.items() if key != "page"}
        | {"page": rendered.page}
        for item, rendered in zip(returned_pages, batch, strict=True)
    ]


class OpenRouterVlmModelClient:
    """OpenRouter implementation of the VLM transport boundary."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, request_options: dict[str, Any] | None = None) -> None:
        self._api_key = api_key
        self._request_options = request_options or {}

    def generate_json(
        self, model: str, prompt: str, pages: list[RenderedPage], timeout: int
    ) -> ModelCallResult:
        started = time.perf_counter()
        response = requests.post(
            OPENROUTER_API_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-OpenRouter-Metadata": "enabled",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": page_content(pages, prompt)}],
                "temperature": 0,
                **self._request_options,
            },
            timeout=timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(f"OpenRouter request failed: {detail}") from error
        body = response.json()
        if body.get("error") is not None:
            raise RuntimeError(f"OpenRouter request failed: {body['error']}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenRouter response did not contain completion choices")
        provider_metadata = {
            key: value
            for key, value in {
                "generation_id": body.get("id") or response.headers.get("X-Generation-Id"),
                "model": body.get("model"),
                "provider": body.get("provider"),
                "openrouter_metadata": body.get("openrouter_metadata"),
            }.items()
            if value is not None
        }
        return _response_result(
            choices[0]["message"]["content"],
            usage=body.get("usage") or {},
            elapsed_seconds=time.perf_counter() - started,
            image_payload_bytes=sum(len(page.image_b64) * 3 // 4 for page in pages),
            provider_metadata=provider_metadata,
        )

    def pricing_snapshot(self) -> dict[str, dict[str, str]]:
        return fetch_pricing()


class OllamaVlmModelClient:
    """Private Ollama transport, normally reached through the local SSM tunnel."""

    provider_name = "ollama"

    def __init__(self, base_url: str | None = None) -> None:
        base_url = (
            base_url
            or os.getenv(PRIVATE_OLLAMA_BASE_URL_ENV)
            or os.getenv("OLLAMA_BASE_URL")
            or DEFAULT_OLLAMA_BASE_URL
        )
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("A valid Ollama base URL is required")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama must be reached through a local SSH or SSM tunnel")
        self._base_url = base_url.rstrip("/")

    def health_check(self, expected_models: set[str] | None = None, timeout: int = 10) -> list[str]:
        """Confirm the local tunnel and expected already-loaded Ollama models.

        This only queries ``/api/tags``. It never pulls, starts, or changes a model.
        """
        try:
            response = requests.get(f"{self._base_url}/api/tags", timeout=timeout)
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError(
                f"Ollama health check failed at {self._base_url}/api/tags. "
                "Start the local SSH or SSM tunnel and ensure Ollama is serving the private GPU."
            ) from error
        models = [
            str(item.get("name") or item.get("model"))
            for item in body.get("models") or []
            if isinstance(item, dict) and (item.get("name") or item.get("model"))
        ]
        missing = [
            model
            for model in expected_models or set()
            if model not in models and not any(available.split(":", 1)[0] == model for available in models)
        ]
        if missing:
            available = ", ".join(models) or "none"
            raise RuntimeError(
                f"Ollama is reachable but required model(s) are unavailable: {', '.join(sorted(missing))}. "
                f"Available model(s): {available}. The benchmark will not pull or start another model."
            )
        return models

    def generate_json(
        self, model: str, prompt: str, pages: list[RenderedPage], timeout: int
    ) -> ModelCallResult:
        page_notes = "\n".join(f"Image {index} is document page {page.page}." for index, page in enumerate(pages, start=1))
        content = f"{prompt}\n\n{page_notes}" if page_notes else prompt
        message: dict[str, Any] = {"role": "user", "content": content}
        if pages:
            message["images"] = [page.image_b64 for page in pages]
        started = time.perf_counter()
        response = requests.post(
            f"{self._base_url}/api/chat",
            json={
                "model": model,
                "messages": [message],
                "stream": False,
                "think": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        content_value = (body.get("message") or {}).get("content")
        if not isinstance(content_value, str):
            raise RuntimeError("Ollama returned an invalid response")
        usage = {
            "prompt_tokens": body.get("prompt_eval_count"),
            "completion_tokens": body.get("eval_count"),
            "total_duration_ns": body.get("total_duration"),
            "load_duration_ns": body.get("load_duration"),
            "prompt_eval_duration_ns": body.get("prompt_eval_duration"),
            "eval_duration_ns": body.get("eval_duration"),
        }
        elapsed = time.perf_counter() - started
        total_duration = body.get("total_duration")
        model_reported_seconds = (
            float(total_duration) / 1_000_000_000 if isinstance(total_duration, (int, float)) else None
        )
        return _response_result(
            content_value,
            usage=usage,
            elapsed_seconds=elapsed,
            image_payload_bytes=sum(len(page.image_b64) * 3 // 4 for page in pages),
            model_reported_seconds=model_reported_seconds,
        )

    def pricing_snapshot(self) -> dict[str, dict[str, str]]:
        return {}


def fetch_pricing() -> dict[str, dict[str, str]]:
    """Get a reproducible OpenRouter price snapshot; empty on transient failure."""
    try:
        body = requests.get(OPENROUTER_MODELS_URL, timeout=20).json()
    except (requests.RequestException, ValueError):
        return {}
    return {item["id"]: item.get("pricing") or {} for item in body.get("data") or [] if item.get("id")}


def usage_cost_usd(usage: dict[str, Any], pricing: dict[str, str]) -> tuple[float | None, str]:
    if usage.get("cost") is not None:
        return float(usage["cost"]), "provider_reported"
    try:
        prompt = Decimal(str(usage.get("prompt_tokens", 0))) * Decimal(pricing["prompt"])
        completion = Decimal(str(usage.get("completion_tokens", 0))) * Decimal(pricing["completion"])
        return float(prompt + completion), "estimated_from_token_usage"
    except (KeyError, InvalidOperation):
        return None, "unavailable"


def normalise_page_number(value: Any) -> int | None:
    """Accept integer-like and explicitly labelled page values from JSON models."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
        labelled = re.fullmatch(r"(?:document\s+)?page\s+(\d+)", cleaned, re.IGNORECASE)
        if labelled:
            return int(labelled.group(1))
    return None


def normalise_statement_scope(value: Any) -> str:
    """Normalise the filing scope visible in a statement heading."""
    scope = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "consolidated": "consolidated_group",
        "group": "consolidated_group",
        "group_accounts": "consolidated_group",
        "parent_company": "company",
    }
    scope = aliases.get(scope, scope)
    return scope if scope in STATEMENT_SCOPES else "unknown"


def statement_pages(locator: dict[str, Any], page_count: int) -> list[int]:
    """Return statement pages plus neighbouring context pages for extraction."""
    selected: set[int] = set()
    for page in located_statement_pages(locator, page_count):
        selected.update(range(max(1, page - 1), min(page_count, page + 1) + 1))
    return sorted(selected)


def located_statement_pages(locator: dict[str, Any], page_count: int) -> list[int]:
    """Return only pages the locator classified as a primary statement."""
    pages: set[int] = set()
    for item in locator.get("pages") or []:
        page = normalise_page_number(item.get("page"))
        if (
            page is not None
            and 1 <= page <= page_count
            and item.get("statement_type") in PRIMARY_STATEMENT_TYPES
        ):
            pages.add(page)
    return sorted(pages)


def employee_evidence_pages(locator: dict[str, Any], page_count: int) -> list[int]:
    """Return pages the locator identified as containing employee-count evidence.

    Employee counts commonly live in notes rather than primary statements, so
    this intentionally does not expand to neighbouring pages. The specialised
    high-resolution extraction is only invoked for direct evidence pages.
    """
    pages: set[int] = set()
    for item in locator.get("pages") or []:
        page = normalise_page_number(item.get("page"))
        if page is not None and 1 <= page <= page_count and item.get("contains_employee_count") is True:
            pages.add(page)
    return sorted(pages)


def incomplete_statement_extractions(
    returned_pages: list[dict[str, Any]], required_pages: set[int]
) -> list[int]:
    """Find located statement pages omitted from an extraction or returned without rows."""
    missing, empty = statement_extraction_gaps(returned_pages, required_pages)
    return sorted({*missing, *empty})


def statement_extraction_gaps(
    returned_pages: list[dict[str, Any]], required_pages: set[int]
) -> tuple[list[int], list[int]]:
    """Separate absent page responses from explicit page responses with no rows."""
    rows_by_page = {
        page: item.get("rows")
        for item in returned_pages
        if (page := normalise_page_number(item.get("page"))) is not None
    }
    missing = sorted(page for page in required_pages if page not in rows_by_page)
    empty = sorted(page for page in required_pages if page in rows_by_page and not rows_by_page[page])
    return missing, empty


def merge_extraction_pages(
    call_pages: list[tuple[ModelCallResult, list[RenderedPage]]]
) -> list[dict[str, Any]]:
    """Merge code-mapped batch and recovery results, preferring more complete rows."""
    merged: dict[int, dict[str, Any]] = {}
    for call, batch in call_pages:
        for item in attach_document_pages(call.payload.get("pages") or [], batch):
            page = item["page"]
            existing = merged.get(page)
            if existing is None or len(item.get("rows") or []) >= len(existing.get("rows") or []):
                merged[page] = item
    return [merged[page] for page in sorted(merged)]


def normalise_unit(unit: Any) -> str:
    value = str(unit or "UNKNOWN").upper().replace("£", "GBP").replace(" ", "_")
    aliases = {"GBP000": "GBP_THOUSANDS", "GBP_000": "GBP_THOUSANDS", "GBP000S": "GBP_THOUSANDS", "GBPM": "GBP_MILLIONS"}
    return aliases.get(value, value if value in UNIT_MULTIPLIERS else "UNKNOWN")


def to_pence(displayed_value: Any, unit: str, metric: str) -> int | None:
    if displayed_value is None or metric == "employees" or unit not in UNIT_MULTIPLIERS:
        return None
    token = str(displayed_value).strip()
    if re.fullmatch(r"[-\u2013\u2014]+", token):
        return 0
    negative = token.startswith("-") or ("(" in token and ")" in token)
    cleaned = re.sub(r"[^0-9.]", "", token)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    pence = int((value * UNIT_MULTIPLIERS[unit]).to_integral_value())
    return -pence if negative else pence


def to_count(displayed_value: Any) -> int | None:
    if displayed_value is None:
        return None
    digits = re.sub(r"\D", "", str(displayed_value))
    return int(digits) if digits else None


def extraction_candidates(
    extraction: dict[str, Any], *, id_prefix: str = ""
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page_item in extraction.get("pages") or []:
        page = normalise_page_number(page_item.get("page"))
        if page is None or page < 1:
            continue
        unit = normalise_unit(page_item.get("unit"))
        for index, row in enumerate(page_item.get("rows") or []):
            metric = row.get("metric")
            if metric not in METRICS:
                continue
            candidates.append({
                "id": f"{id_prefix}p{page}-r{index}", "metric": metric, "page": page,
                "statement_type": page_item.get("statement_type"),
                "statement_scope": normalise_statement_scope(page_item.get("statement_scope")),
                "unit": unit,
                "source_label": row.get("source_label"), "current_display": row.get("current_display"),
                "previous_display": row.get("previous_display"), "current_column": row.get("current_column"),
                "previous_column": row.get("previous_column"), "evidence_text": row.get("evidence_text"),
                "confidence": row.get("confidence"),
            })
    return candidates


def apply_locator_statement_scopes(
    extraction: dict[str, Any], locator: dict[str, Any]
) -> None:
    """Attach the reliable locator scope to extracted pages, where available.

    The locator and extractor see the same rendered page but have distinct jobs.
    A direct locator classification takes precedence because it was made from
    the statement heading; extraction scope remains the fallback for neighbour
    pages included only to preserve visual context.
    """
    locator_scopes = {
        page: normalise_statement_scope(item.get("statement_scope"))
        for item in locator.get("pages") or []
        if (page := normalise_page_number(item.get("page"))) is not None
    }
    for page_item in extraction.get("pages") or []:
        page = normalise_page_number(page_item.get("page"))
        located_scope = locator_scopes.get(page, "unknown")
        extracted_scope = normalise_statement_scope(page_item.get("statement_scope"))
        page_item["statement_scope"] = (
            located_scope if located_scope != "unknown" else extracted_scope
        )


def apply_consolidated_scope_policy(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prefer consolidated evidence where a filing contains both statement scopes.

    Scope is applied per primary statement type rather than per individual row:
    when a consolidated balance sheet is available, company balance-sheet rows
    are excluded; the same rule applies independently to income statements and
    cash-flow statements. Unknown-scope rows remain available because their
    headings could not be classified safely.
    """
    group_statement_types = {
        str(candidate.get("statement_type"))
        for candidate in candidates
        if candidate.get("statement_scope") == "consolidated_group"
        and candidate.get("statement_type") in PRIMARY_STATEMENT_TYPES
    }
    excluded = [
        candidate
        for candidate in candidates
        if candidate.get("statement_scope") == "company"
        and candidate.get("statement_type") in group_statement_types
    ]
    kept = [candidate for candidate in candidates if candidate not in excluded]
    return kept, {
        "name": "prefer_consolidated_group_statement_scope",
        "consolidated_statement_types": sorted(group_statement_types),
        "excluded_company_candidate_ids": [candidate["id"] for candidate in excluded],
    }


_CLEARLY_INCOMPATIBLE_LABELS = {
    "turnover": ("cost of sales", "gross profit", "administrative", "net assets"),
    "gross_profit": ("cost of sales", "administrative", "total assets", "net assets"),
    "operating_result": ("other operating", "administrative expenses", "cost of sales"),
    "profit_after_tax": ("profit before tax", "loss before tax", "tax charge", "taxation charge"),
    "cash": ("current assets", "total assets", "net assets", "total equity", "total liabilities"),
    "net_assets": (
        "current assets", "total assets", "cash and cash equivalents", "total liabilities",
        "total equity and liabilities",
    ),
    "employees": ("staff costs", "wages and salaries", "social security costs"),
}


def _normalised_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def candidate_validation_issues(candidate: dict[str, Any]) -> list[dict[str, str]]:
    """Return conservative, deterministic blockers for unusable row evidence.

    These checks catch structural category/unit errors. They do not claim to
    verify OCR-like digit transcription from pixels, which requires an
    independent visual model or human review.
    """
    issues: list[dict[str, str]] = []
    metric = str(candidate.get("metric") or "")
    label = _normalised_label(candidate.get("source_label"))
    values = [candidate.get("current_display"), candidate.get("previous_display")]
    if not label:
        issues.append({"code": "missing_source_label", "message": "row has no source label"})
    if not any(value is not None and str(value).strip() for value in values):
        issues.append({"code": "missing_period_values", "message": "row has no displayed period value"})
    if metric in MONEY_METRICS and candidate.get("unit") not in UNIT_MULTIPLIERS:
        issues.append({"code": "unknown_money_unit", "message": "money row has no recognised unit"})
    for value in values:
        if str(value or "").strip("() -") in {"2022", "2023", "2024", "2025", "2026"}:
            issues.append({"code": "year_used_as_value", "message": "a year heading was returned as a value"})
            break
    incompatible = next(
        (phrase for phrase in _CLEARLY_INCOMPATIBLE_LABELS.get(metric, ()) if phrase in label),
        None,
    )
    if incompatible is not None:
        issues.append({
            "code": "metric_label_conflict",
            "message": f"{metric} conflicts with source label containing '{incompatible}'",
        })
    return issues


def validate_extraction_candidates(
    extraction: dict[str, Any], *, id_prefix: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Separate usable candidates from rejected evidence and report page-level issues."""
    all_candidates: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    issues_by_page: dict[int, list[dict[str, Any]]] = {}
    for candidate in extraction_candidates(extraction, id_prefix=id_prefix):
        issues = candidate_validation_issues(candidate)
        annotated = {
            **candidate,
            "row_validation": {"status": "accepted" if not issues else "rejected", "issues": issues},
        }
        all_candidates.append(annotated)
        if issues:
            issues_by_page.setdefault(int(annotated["page"]), []).append({
                "candidate_id": annotated["id"],
                "metric": annotated["metric"],
                "issues": issues,
            })
        else:
            accepted.append(annotated)
    return all_candidates, accepted, {
        "invalid_pages": sorted(issues_by_page),
        "issues_by_page": issues_by_page,
        "recovery_pages": [],
        "replaced_pages": [],
        "remaining_invalid_pages": [],
        "warnings": [],
    }


def page_row_validation_quality(page: dict[str, Any]) -> tuple[int, int, int]:
    """Rank an original/recovery page without trusting model confidence alone."""
    all_candidates, accepted, _ = validate_extraction_candidates({"pages": [page]})
    return (len(accepted), -len(all_candidates) + len(accepted), len(all_candidates))


def _has_usable_period_value(candidate: dict[str, Any], period: str) -> bool:
    """Return whether a selected candidate visibly supplies one period's value."""
    display = candidate.get(f"{period}_display")
    if candidate.get("metric") == "employees":
        return to_count(display) is not None
    return to_pence(display, str(candidate.get("unit")), str(candidate.get("metric"))) is not None


def complete_paired_period_choices(
    candidates: list[dict[str, Any]], rationalisation: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Fill an omitted period from the same evidence row, never from a new row.

    A financial-statement row normally presents current and comparative values
    together. If the rationaliser selected it for one period but left the other
    null, this deterministic completion retains exactly the same candidate only
    when the counterpart value is visibly usable. The raw model output remains
    available separately for audit.
    """
    resolved = copy.deepcopy(rationalisation)
    summaries = resolved.get("financial_period_summaries")
    if not isinstance(summaries, dict):
        return resolved, []
    by_id = {str(candidate.get("id")): candidate for candidate in candidates}
    completions: list[dict[str, str]] = []
    current = summaries.get("current")
    previous = summaries.get("previous")
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return resolved, completions
    for metric in CANONICAL_METRICS:
        current_choice = current.get(metric)
        previous_choice = previous.get(metric)
        for source_period, target_period, source_choice, target_choices in (
            ("current", "previous", current_choice, previous),
            ("previous", "current", previous_choice, current),
        ):
            if target_choices.get(metric) is not None or not isinstance(source_choice, dict):
                continue
            candidate = by_id.get(str(source_choice.get("candidate_id")))
            if (
                candidate is None
                or candidate.get("metric") != metric
                or not _has_usable_period_value(candidate, source_period)
                or not _has_usable_period_value(candidate, target_period)
            ):
                continue
            target_choices[metric] = {
                "candidate_id": candidate["id"],
                "reason": "paired_period_same_statement_row",
                "confidence": source_choice.get("confidence", candidate.get("confidence")),
            }
            completions.append({
                "metric": metric,
                "source_period": source_period,
                "target_period": target_period,
                "candidate_id": candidate["id"],
            })
    return resolved, completions


def selected_metrics(candidates: list[dict[str, Any]], rationalisation: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in candidates}
    metrics: list[dict[str, Any]] = []
    periods = rationalisation.get("financial_period_summaries") or {}
    for period_type, display_field in (("current", "current_display"), ("previous", "previous_display")):
        column_field = f"{period_type}_column"
        period = periods.get(period_type) or {}
        for metric in CANONICAL_METRICS:
            choice = period.get(metric)
            if not isinstance(choice, dict):
                continue
            candidate = by_id.get(choice.get("candidate_id"))
            if candidate is None or candidate["metric"] != metric or candidate.get(display_field) is None:
                continue
            display = candidate[display_field]
            unit = candidate["unit"]
            validation = {
                "unit_known": metric == "employees" or unit in UNIT_MULTIPLIERS,
                "looks_like_year": str(display).strip("() -") in {"2022", "2023", "2024", "2025", "2026"},
                "review_reason": choice.get("reason"),
                "rationalised_column": metric,
                "derivation": candidate.get("derivation"),
                "row_validation": candidate.get("row_validation"),
            }
            metrics.append({
                "period_type": period_type,
                "financial_year": parse_financial_year(candidate.get(column_field)),
                "metric_name": metric,
                "value_pence": to_pence(display, unit, metric),
                "value_count": to_count(display) if metric == "employees" else None,
                "displayed_value": str(display),
                "unit": unit if metric in MONEY_METRICS else "COUNT",
                "source_page": candidate["page"],
                "source_label": candidate.get("source_label"),
                "evidence_text": candidate.get("evidence_text"),
                "confidence": choice.get("confidence", candidate.get("confidence")),
                "validation": validation,
            })
    best_by_period_metric: dict[tuple[str, str], dict[str, Any]] = {}
    for item in metrics:
        key = (item["period_type"], item["metric_name"])
        existing = best_by_period_metric.get(key)
        score = (int(item["validation"]["unit_known"]), float(item.get("confidence") or 0))
        existing_score = (
            int(existing["validation"]["unit_known"]), float(existing.get("confidence") or 0)
        ) if existing else None
        if existing is None or score > existing_score:
            best_by_period_metric[key] = item
    return [best_by_period_metric[key] for key in sorted(best_by_period_metric)]


def validate_page_response(
    payload: dict[str, Any], *, require_rows: bool, expected_page_count: int | None = None
) -> None:
    """Validate ordered per-image results before code attaches PDF page numbers."""
    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise ValueError("response.pages must be a list")
    if expected_page_count is not None and len(pages) != expected_page_count:
        raise ValueError(
            "response.pages count must equal the number of supplied images "
            f"({expected_page_count}); received {len(pages)}"
        )
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise ValueError(f"response.pages[{index}] must be an object")
        if require_rows and not isinstance(page.get("rows"), list):
            raise ValueError(f"response.pages[{index}].rows must be a list")
        if require_rows:
            for row_index, row in enumerate(page["rows"]):
                if not isinstance(row, dict):
                    raise ValueError(
                        f"response.pages[{index}].rows[{row_index}] must be an object"
                    )


def validate_rationalisation_response(payload: dict[str, Any]) -> None:
    summaries = payload.get("financial_period_summaries")
    if not isinstance(summaries, dict):
        raise ValueError("response.financial_period_summaries must be an object")
    for period in ("current", "previous"):
        values = summaries.get(period)
        if not isinstance(values, dict):
            raise ValueError(f"response.financial_period_summaries.{period} must be an object")
        for metric, choice in values.items():
            if metric not in CANONICAL_METRICS:
                continue
            if choice is not None and not isinstance(choice, dict):
                raise ValueError(f"response {period}.{metric} must be an object or null")
            if isinstance(choice, dict) and not isinstance(choice.get("candidate_id"), str):
                raise ValueError(f"response {period}.{metric}.candidate_id must be a string")


def _sum_numeric_usage(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for attempt in attempts:
        for key, value in (attempt.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    return usage


def generate_json_reliably(
    model_client: VlmModelClient,
    model: str,
    prompt: str,
    pages: list[RenderedPage],
    timeout: int,
    *,
    stage: str,
    validator: Callable[[dict[str, Any]], None],
    max_attempts: int,
    batch_number: int | None = None,
) -> ModelCallResult:
    """Parse, validate and retry one model stage without concealing failed responses."""
    if max_attempts < 1:
        raise ValueError("json_max_attempts must be at least one")
    attempts: list[dict[str, Any]] = []
    for attempt_number in range(1, max_attempts + 1):
        try:
            result = model_client.generate_json(model, prompt, pages, timeout)
        except ModelResponseError as error:
            attempt = dict(error.attempt)
        else:
            try:
                validator(result.payload)
            except ValueError as error:
                attempt = {
                    "status": "invalid_schema",
                    "error": str(error),
                    "raw_response": result.raw_response,
                    "usage": result.usage,
                    "elapsed_seconds": round(result.elapsed_seconds, 4),
                    "image_payload_bytes": result.image_payload_bytes,
                    "model_reported_seconds": result.model_reported_seconds,
                    "provider_metadata": result.provider_metadata,
                    "response_handling": result.response_handling,
                }
            else:
                attempt = {
                    "status": "repaired" if result.response_handling.get("repaired") else "parsed",
                    "error": None,
                    "raw_response": result.raw_response,
                    "usage": result.usage,
                    "elapsed_seconds": round(result.elapsed_seconds, 4),
                    "image_payload_bytes": result.image_payload_bytes,
                    "model_reported_seconds": result.model_reported_seconds,
                    "provider_metadata": result.provider_metadata,
                    "response_handling": result.response_handling,
                }
                attempt.update(
                    {"stage": stage, "attempt": attempt_number, "batch": batch_number}
                )
                attempts.append(attempt)
                reported = [
                    item["model_reported_seconds"]
                    for item in attempts
                    if isinstance(item.get("model_reported_seconds"), (int, float))
                ]
                return replace(
                    result,
                    usage=_sum_numeric_usage(attempts),
                    elapsed_seconds=sum(float(item.get("elapsed_seconds") or 0) for item in attempts),
                    image_payload_bytes=sum(
                        int(item.get("image_payload_bytes") or 0) for item in attempts
                    ),
                    model_reported_seconds=sum(reported) if reported else None,
                    response_attempts=attempts,
                )
        attempt.update({"stage": stage, "attempt": attempt_number, "batch": batch_number})
        attempts.append(attempt)
    raise StageCallError(stage, attempts)


def _stage_call_summary(
    model: str,
    calls: list[ModelCallResult],
    failed_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attempts = [attempt for call in calls for attempt in call.response_attempts]
    attempts.extend(failed_attempts or [])
    reported = [
        attempt["model_reported_seconds"]
        for attempt in attempts
        if isinstance(attempt.get("model_reported_seconds"), (int, float))
    ]
    return {
        "model": model,
        "usage": _sum_numeric_usage(attempts),
        "elapsed_seconds": round(
            sum(float(attempt.get("elapsed_seconds") or 0) for attempt in attempts), 4
        ) if attempts else None,
        "image_payload_bytes": sum(int(attempt.get("image_payload_bytes") or 0) for attempt in attempts),
        "model_reported_seconds": sum(reported) if reported else None,
        "provider_metadata": {
            "calls": [
                attempt.get("provider_metadata") or {}
                for attempt in attempts
                if attempt.get("provider_metadata")
            ]
        },
        "reliability": {
            "attempt_count": len(attempts),
            "retry_count": sum(int(attempt.get("attempt") or 1) > 1 for attempt in attempts),
            "repaired_count": sum(attempt.get("status") == "repaired" for attempt in attempts),
            "failed_attempt_count": sum(
                attempt.get("status") in {
                    "invalid_json", "invalid_schema", "missing_statement_page_response"
                }
                for attempt in attempts
            ),
            "attempts": attempts,
        },
    }


def process_pdf_vlm_financials(
    pdf_path: Path,
    model_client: VlmModelClient,
    *,
    locator_model: str = DEFAULT_LOCATOR_MODEL,
    vision_model: str = DEFAULT_VISION_MODEL,
    recovery_vision_model: str | None = None,
    rationalisation_model: str = DEFAULT_RATIONALISATION_MODEL,
    max_pages: int | None = 60,
    locator_batch_size: int | None = None,
    extraction_batch_size: int | None = None,
    recovery_render_long_edge: int = 2048,
    json_max_attempts: int = 2,
    gbp_per_usd: float = 0.75,
    timeout: int = 180,
) -> dict[str, Any]:
    """Run statement discovery, extraction and text review through one model client."""
    started = time.perf_counter()
    render_started = time.perf_counter()
    thumbnails = render_pages(pdf_path, max_pages=max_pages, long_edge=384)
    thumbnail_render_seconds = time.perf_counter() - render_started
    if locator_batch_size is not None and locator_batch_size < 1:
        raise ValueError("locator_batch_size must be positive when supplied")
    if extraction_batch_size is not None and extraction_batch_size < 1:
        raise ValueError("extraction_batch_size must be positive when supplied")
    if recovery_render_long_edge < 1440:
        raise ValueError("recovery_render_long_edge must be at least 1440")
    effective_recovery_vision_model = recovery_vision_model or vision_model
    batches = (
        [thumbnails[index:index + locator_batch_size] for index in range(0, len(thumbnails), locator_batch_size)]
        if locator_batch_size and len(thumbnails) > locator_batch_size
        else [thumbnails]
    )
    locator_calls: list[ModelCallResult] = []
    extraction_calls: list[ModelCallResult] = []
    recovery_vision_calls: list[ModelCallResult] = []
    rationalisation_calls: list[ModelCallResult] = []
    soft_vision_failures: list[dict[str, Any]] = []
    failed: StageCallError | None = None
    locator: dict[str, Any] = {"pages": []}
    selected: list[int] = []
    employee_pages: list[int] = []
    detail_render_seconds = 0.0
    recovery_render_seconds = 0.0
    extraction: dict[str, Any] = {"pages": []}
    employee_extraction: dict[str, Any] = {"pages": []}
    rationalisation: dict[str, Any] = {"financial_period_summaries": {}}
    candidates: list[dict[str, Any]] = []
    statement_scope_policy: dict[str, Any] = {
        "name": "prefer_consolidated_group_statement_scope",
        "consolidated_statement_types": [],
        "excluded_company_candidate_ids": [],
    }
    extraction_batches: list[list[RenderedPage]] = []
    employee_extraction_batches: list[list[RenderedPage]] = []
    extraction_call_batches: list[tuple[ModelCallResult, list[RenderedPage]]] = []
    extraction_coverage: dict[str, Any] = {
        "required_statement_pages": [],
        "returned_statement_pages": [],
        "recovery_pages": [],
        "missing_after_recovery_pages": [],
        "empty_after_recovery_pages": [],
        "unrecovered_pages": [],
        "warnings": [],
    }

    def render_focused_recovery_page(page_number: int) -> RenderedPage:
        """Re-render one failed statement page without paying to re-render a PDF."""
        nonlocal recovery_render_seconds
        render_started = time.perf_counter()
        pages = render_pages(
            pdf_path,
            max_pages=max_pages,
            long_edge=recovery_render_long_edge,
            page_numbers=[page_number],
        )
        recovery_render_seconds += time.perf_counter() - render_started
        if len(pages) != 1 or pages[0].page != page_number:
            raise RuntimeError(f"Could not render recovery image for document page {page_number}")
        return pages[0]

    for batch_number, batch in enumerate(batches, start=1):
        try:
            locator_calls.append(
                generate_json_reliably(
                    model_client,
                    locator_model,
                    LOCATOR_PROMPT,
                    batch,
                    timeout,
                    stage="locator",
                    validator=lambda payload: validate_page_response(
                        payload, require_rows=False, expected_page_count=len(batch)
                    ),
                    max_attempts=json_max_attempts,
                    batch_number=batch_number,
                )
            )
        except StageCallError as error:
            failed = error
            break

    if failed is None:
        locator["pages"] = [
            page
            for call, batch in zip(locator_calls, batches, strict=True)
            for page in attach_document_pages(call.payload.get("pages") or [], batch)
        ]
        extraction_coverage["required_statement_pages"] = located_statement_pages(
            locator, len(thumbnails)
        )
        selected = statement_pages(locator, len(thumbnails))
        employee_pages = employee_evidence_pages(locator, len(thumbnails))
        detail_page_numbers = sorted({*selected, *employee_pages})
        render_started = time.perf_counter()
        detail_pages = render_pages(
            pdf_path,
            max_pages=max_pages,
            long_edge=1440,
            page_numbers=detail_page_numbers,
        )
        detail_render_seconds = time.perf_counter() - render_started
        detail_by_page = {item.page: item for item in detail_pages}

    if failed is None and selected:
        selected_details = [detail_by_page[number] for number in selected]
        extraction_batches = (
            [
                selected_details[index:index + extraction_batch_size]
                for index in range(0, len(selected_details), extraction_batch_size)
            ]
            if extraction_batch_size and len(selected_details) > extraction_batch_size
            else [selected_details]
        )
        required_statement_pages = set(extraction_coverage["required_statement_pages"])
        for batch_number, batch in enumerate(extraction_batches, start=1):
            try:
                extraction_call = generate_json_reliably(
                    model_client,
                    vision_model,
                    EXTRACTION_PROMPT,
                    batch,
                    timeout,
                    stage="vision",
                    validator=lambda payload: validate_page_response(
                        payload, require_rows=True, expected_page_count=len(batch)
                    ),
                    max_attempts=json_max_attempts,
                    batch_number=batch_number,
                )
                extraction_calls.append(extraction_call)
                extraction_call_batches.append((extraction_call, batch))
            except StageCallError as error:
                failed = error
                break

            returned = attach_document_pages(
                extraction_call.payload.get("pages") or [], batch
            )
            missing = incomplete_statement_extractions(
                returned, required_statement_pages & {page.page for page in batch}
            )
            for page_number in missing:
                recovery_page = render_focused_recovery_page(page_number)
                extraction_coverage["recovery_pages"].append(page_number)
                recovery_prompt = (
                    f"{EXTRACTION_PROMPT}\n\n"
                    f"Coverage recovery: return the rows for Document page {page_number}. "
                    "This page was classified as a primary financial statement. "
                    "Do not omit it and do not return any other page.\n\n"
                    f"{HIGH_RESOLUTION_RECOVERY_PROMPT}"
                )
                try:
                    recovery_call = generate_json_reliably(
                        model_client,
                        effective_recovery_vision_model,
                        recovery_prompt,
                        [recovery_page],
                        timeout,
                        stage="vision_recovery",
                        validator=lambda payload: validate_page_response(
                            payload, require_rows=True, expected_page_count=1
                        ),
                        max_attempts=json_max_attempts,
                        batch_number=batch_number,
                    )
                except StageCallError as error:
                    failed = error
                    break
                recovery_vision_calls.append(recovery_call)
                extraction_call_batches.append((recovery_call, [recovery_page]))
                recovered = attach_document_pages(
                    recovery_call.payload.get("pages") or [], [recovery_page]
                )
                missing_after_recovery, empty_after_recovery = statement_extraction_gaps(
                    recovered, {page_number}
                )
                if missing_after_recovery:
                    failed = StageCallError("vision", [{
                        "status": "missing_statement_page_response",
                        "error": (
                            f"Document page {page_number} was classified as a statement "
                            "but was still absent from the focused recovery response"
                        ),
                        "raw_response": recovery_call.raw_response,
                        "usage": {},
                        "elapsed_seconds": 0,
                        "image_payload_bytes": 0,
                        "model_reported_seconds": None,
                        "provider_metadata": {},
                    }])
                    extraction_coverage["missing_after_recovery_pages"].append(page_number)
                    extraction_coverage["unrecovered_pages"].append(page_number)
                    break
                if empty_after_recovery:
                    extraction_coverage["empty_after_recovery_pages"].append(page_number)
                    extraction_coverage["unrecovered_pages"].append(page_number)
                    extraction_coverage["warnings"].append({
                        "code": "empty_statement_page_rows_after_recovery",
                        "page": page_number,
                        "message": (
                            f"Document page {page_number} was classified as a statement "
                            "but returned no financial rows after focused recovery"
                        ),
                    })
            if failed is not None:
                break
        extraction["pages"] = merge_extraction_pages(extraction_call_batches)
        extracted_page_numbers = {
            normalise_page_number(item.get("page")) for item in extraction["pages"]
        }
        extraction_coverage["returned_statement_pages"] = sorted(
            page for page in required_statement_pages if page in extracted_page_numbers
        )

    if failed is None and employee_pages:
        employee_details = [detail_by_page[number] for number in employee_pages]
        employee_extraction_batches = (
            [
                employee_details[index:index + extraction_batch_size]
                for index in range(0, len(employee_details), extraction_batch_size)
            ]
            if extraction_batch_size and len(employee_details) > extraction_batch_size
            else [employee_details]
        )
        employee_call_batches: list[tuple[ModelCallResult, list[RenderedPage]]] = []
        for batch_number, batch in enumerate(employee_extraction_batches, start=1):
            try:
                employee_call = generate_json_reliably(
                    model_client,
                    vision_model,
                    EMPLOYEE_EXTRACTION_PROMPT,
                    batch,
                    timeout,
                    stage="vision",
                    validator=lambda payload: validate_page_response(
                        payload, require_rows=True, expected_page_count=len(batch)
                    ),
                    max_attempts=json_max_attempts,
                    batch_number=batch_number,
                )
            except StageCallError as error:
                failed = error
                break
            extraction_calls.append(employee_call)
            employee_call_batches.append((employee_call, batch))
        employee_extraction["pages"] = merge_extraction_pages(employee_call_batches)

    all_raw_candidates: list[dict[str, Any]] = []
    row_validation: dict[str, Any] = {
        "financial": {
            "invalid_pages": [],
            "issues_by_page": {},
            "recovery_pages": [],
            "replaced_pages": [],
            "remaining_invalid_pages": [],
            "warnings": [],
        },
        "employees": {
            "invalid_pages": [],
            "issues_by_page": {},
            "recovery_pages": [],
            "replaced_pages": [],
            "remaining_invalid_pages": [],
            "warnings": [],
        },
    }
    if failed is None:
        apply_locator_statement_scopes(extraction, locator)
        all_financial_candidates, accepted_financial_candidates, financial_validation = (
            validate_extraction_candidates(extraction)
        )
        row_validation["financial"] = financial_validation
        page_by_number = {
            int(page["page"]): page
            for page in extraction.get("pages") or []
            if normalise_page_number(page.get("page")) is not None
        }
        for page_number in financial_validation["invalid_pages"]:
            recovery_page = render_focused_recovery_page(page_number)
            financial_validation["recovery_pages"].append(page_number)
            try:
                recovery_call = generate_json_reliably(
                    model_client,
                    effective_recovery_vision_model,
                    (
                        f"{EXTRACTION_PROMPT}\n\n{ROW_VALIDATION_RECOVERY_PROMPT}\n\n"
                        f"{HIGH_RESOLUTION_RECOVERY_PROMPT}"
                    ),
                    [recovery_page],
                    timeout,
                    stage="vision_recovery",
                    validator=lambda payload: validate_page_response(
                        payload, require_rows=True, expected_page_count=1
                    ),
                    max_attempts=json_max_attempts,
                )
            except StageCallError as error:
                soft_vision_failures.extend(error.attempts)
                financial_validation["warnings"].append({
                    "code": "row_validation_recovery_failed",
                    "page": page_number,
                    "message": str(error),
                })
                continue
            recovery_vision_calls.append(recovery_call)
            recovered_page = attach_document_pages(
                recovery_call.payload.get("pages") or [], [recovery_page]
            )[0]
            original_page = page_by_number[page_number]
            if page_row_validation_quality(recovered_page) > page_row_validation_quality(original_page):
                page_by_number[page_number] = recovered_page
                financial_validation["replaced_pages"].append(page_number)
            else:
                financial_validation["warnings"].append({
                    "code": "row_validation_recovery_not_better",
                    "page": page_number,
                    "message": "Focused re-extraction did not improve deterministic row quality.",
                })
        extraction["pages"] = [page_by_number[page] for page in sorted(page_by_number)]
        apply_locator_statement_scopes(extraction, locator)
        all_financial_candidates, accepted_financial_candidates, final_financial_validation = (
            validate_extraction_candidates(extraction)
        )
        financial_validation["remaining_invalid_pages"] = final_financial_validation["invalid_pages"]
        financial_validation["issues_by_page"] = final_financial_validation["issues_by_page"]

        all_employee_candidates, accepted_employee_candidates, employee_validation = (
            validate_extraction_candidates(employee_extraction, id_prefix="employee-")
        )
        row_validation["employees"] = employee_validation
        all_raw_candidates = all_financial_candidates + all_employee_candidates
        scoped_candidates, statement_scope_policy = apply_consolidated_scope_policy(
            accepted_financial_candidates + accepted_employee_candidates
        )
        candidates = add_canonical_equivalents(
            scoped_candidates
        )
        if candidates:
            try:
                rationalisation_call = generate_json_reliably(
                    model_client,
                    rationalisation_model,
                    f"{RATIONALISATION_PROMPT}\n\nCANDIDATES:\n{json.dumps({'candidates': candidates}, separators=(',', ':'))}",
                    [],
                    timeout,
                    stage="rationalisation",
                    validator=validate_rationalisation_response,
                    max_attempts=json_max_attempts,
                )
                rationalisation_calls.append(rationalisation_call)
                rationalisation = rationalisation_call.payload
            except StageCallError as error:
                failed = error

    resolved_rationalisation, paired_period_completions = complete_paired_period_choices(
        candidates, rationalisation
    )

    pricing_snapshot = model_client.pricing_snapshot()
    failed_by_stage = {
        stage: failed.attempts if failed is not None and failed.stage == stage else []
        for stage in ("locator", "vision", "vision_recovery", "rationalisation")
    }
    calls = {
        "locator": _stage_call_summary(locator_model, locator_calls, failed_by_stage["locator"]),
        "vision": _stage_call_summary(
            vision_model,
            extraction_calls,
            failed_by_stage["vision"] + soft_vision_failures,
        ),
        "vision_recovery": _stage_call_summary(
            effective_recovery_vision_model,
            recovery_vision_calls,
            failed_by_stage["vision_recovery"],
        ),
        "rationalisation": _stage_call_summary(
            rationalisation_model,
            rationalisation_calls,
            failed_by_stage["rationalisation"],
        ),
    }
    total_usd = Decimal("0")
    methods: set[str] = set()
    have_cost = False
    for item in calls.values():
        usd, method = usage_cost_usd(item["usage"], pricing_snapshot.get(item["model"], {}))
        item["cost_usd"] = usd
        item["cost_method"] = method
        methods.add(method)
        if usd is not None:
            total_usd += Decimal(str(usd))
            have_cost = True
    cost_usd = float(total_usd) if have_cost else None
    payload = {
        "pdf_path": str(pdf_path),
        "status": (
            "error" if failed is not None
            else "complete" if selected or employee_pages
            else "no_statement_pages_found"
        ),
        "provider": model_client.provider_name,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "timing": {
            "thumbnail_render_seconds": round(thumbnail_render_seconds, 4),
            "detail_render_seconds": round(detail_render_seconds, 4),
            "recovery_render_seconds": round(recovery_render_seconds, 4),
            "image_payload_bytes": sum(item["image_payload_bytes"] for item in calls.values()),
            "locator_batches": len(batches),
            "extraction_batches": len(extraction_batches),
            "employee_extraction_batches": len(employee_extraction_batches),
        },
        "models": {
            "locator": locator_model,
            "vision": vision_model,
            "vision_recovery": effective_recovery_vision_model,
            "rationalisation": rationalisation_model,
        },
        "pages_scanned": [item.page for item in thumbnails],
        "candidate_pages": selected,
        "statement_scope_policy": statement_scope_policy,
        "employee_evidence_pages": employee_pages,
        "raw_extraction": {
            "locator": locator,
            "detail": extraction,
            "employee_detail": employee_extraction,
            "candidates": all_raw_candidates,
            "accepted_candidates": candidates,
            "coverage": extraction_coverage,
            "row_validation": row_validation,
        },
        "rationalisation": rationalisation,
        "resolved_rationalisation": resolved_rationalisation,
        "rationalisation_policy": {"paired_period_completions": paired_period_completions},
        "metrics": selected_metrics(candidates, resolved_rationalisation),
        "warnings": (
            extraction_coverage["warnings"]
            + row_validation["financial"]["warnings"]
            + row_validation["employees"]["warnings"]
        ),
        "usage": calls,
        "cost": {
            "usd": cost_usd,
            "gbp": round(cost_usd * gbp_per_usd, 8) if cost_usd is not None else None,
            "method": "+".join(sorted(methods)),
            "pricing": {"gbp_per_usd": gbp_per_usd, "models": {model: pricing_snapshot.get(model, {}) for model in {locator_model, vision_model, effective_recovery_vision_model, rationalisation_model}}},
        },
    }
    if failed is not None:
        payload["error_stage"] = failed.stage
        payload["error"] = str(failed)
    return payload


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="VLM financial-statement extraction; no local OCR is used.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--db", help="Optional SQLite database to receive the VLM run and metrics.")
    parser.add_argument("--company-number")
    parser.add_argument("--document-id")
    parser.add_argument("--output-json")
    parser.add_argument("--max-pages", type=int, default=60)
    parser.add_argument("--locator-model", default=DEFAULT_LOCATOR_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--recovery-vision-model")
    parser.add_argument("--rationalisation-model", default=DEFAULT_RATIONALISATION_MODEL)
    parser.add_argument("--recovery-render-long-edge", type=int, default=2048)
    parser.add_argument("--gbp-per-usd", type=float, default=0.75)
    parser.add_argument("--json-max-attempts", type=int, default=2)
    parser.add_argument("--provider", choices=("openrouter", "ollama"), default="openrouter")
    parser.add_argument(
        "--ollama-base-url",
        default=os.getenv(PRIVATE_OLLAMA_BASE_URL_ENV, os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)),
    )
    args = parser.parse_args(argv)
    load_dotenv(Path.cwd() / ".env")
    if args.provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            parser.error("OPENROUTER_API_KEY must be set when --provider=openrouter.")
        model_client: VlmModelClient = OpenRouterVlmModelClient(api_key)
    else:
        model_client = OllamaVlmModelClient(args.ollama_base_url)
        model_client.health_check(
            {args.locator_model, args.vision_model, args.rationalisation_model}
            | ({args.recovery_vision_model} if args.recovery_vision_model else set())
        )

    payload = process_pdf_vlm_financials(
        Path(args.pdf), model_client, locator_model=args.locator_model, vision_model=args.vision_model,
        recovery_vision_model=args.recovery_vision_model,
        rationalisation_model=args.rationalisation_model, max_pages=args.max_pages, gbp_per_usd=args.gbp_per_usd,
        json_max_attempts=args.json_max_attempts, recovery_render_long_edge=args.recovery_render_long_edge,
    )
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.db:
        import sqlite3
        conn = sqlite3.connect(args.db)
        try:
            init_db(conn)
            payload["database_run_id"] = insert_vlm_financial_payload(conn, payload, args.company_number, args.document_id)
        finally:
            conn.close()
    print(json.dumps({"status": payload["status"], "candidate_pages": payload["candidate_pages"], "metrics": len(payload["metrics"]), "cost": payload["cost"], "database_run_id": payload.get("database_run_id")}, indent=2))
    return 1 if payload["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
