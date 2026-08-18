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

from core.companies_house_extractor import load_dotenv, parse_financial_year
from core.companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.vlm.financial_metric_policy import (
    INSURANCE_METRICS,
    add_canonical_equivalents_by_statement_scope,
    canonical_metric_label_is_compatible,
    canonical_metric_statement_is_compatible,
    insurance_label_is_compatible,
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
    "current_assets", "cash", "net_current_assets", "net_assets", "shareholders_funds", "employees",
) + INSURANCE_METRICS
MONEY_METRICS = set(METRICS) - {"employees"}
CANONICAL_METRICS = (
    "turnover", "gross_profit", "operating_result", "profit_after_tax",
    "cash", "net_assets", "employees",
)
UNIT_MULTIPLIERS = {
    "GBP": 100, "GBP_THOUSANDS": 100_000, "GBP_MILLIONS": 100_000_000,
    "USD": 100, "USD_THOUSANDS": 100_000, "USD_MILLIONS": 100_000_000,
}
PRIMARY_STATEMENT_TYPES = {"income_statement", "balance_sheet", "cash_flow"}
STATEMENT_SCOPES = {"consolidated_group", "company", "unknown"}
EMPLOYEE_EVIDENCE_KINDS = {"numeric", "dash_zero", "narrative_zero", "none"}
NARRATIVE_ZERO_PATTERNS = (
    re.compile(r"\b(?:has|had|have) no employees?\b", re.IGNORECASE),
    re.compile(r"\bemployed no (?:employees?|persons?)\b", re.IGNORECASE),
    re.compile(r"\bdid not employ (?:any )?(?:employees?|persons?)\b", re.IGNORECASE),
    re.compile(
        r"\bdoes? not (?:directly )?employ (?:any )?(?:staff|employees?|persons?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:average\s+)?number of (?:staff|employees?|persons?)"
        r"(?:\s+\w+){0,12}\s+(?:was|were)\s+nil\b",
        re.IGNORECASE,
    ),
)
AMBIGUOUS_NARRATIVE_ZERO_PATTERN = re.compile(
    r"\bno employees?\b[^.;:\n]{0,120}\b(?:other than|except(?:\s+for)?)\b",
    re.IGNORECASE,
)
NARRATIVE_BOTH_PERIODS_PATTERN = re.compile(
    r"\b(?:prior|previous|comparative)\s+period\b"
    r"|\bboth\s+periods\b"
    r"|\b20\d{2}\b[^.;:\n]{0,40}\b(?:nil|zero|no employees?)\b",
    re.IGNORECASE,
)

LOCATOR_PROMPT = """You are identifying financial statement pages in a UK Companies House accounts PDF.
Return only JSON with one object for every supplied image, in exactly the same order:
{"pages":[{"statement_type":"income_statement|balance_sheet|cash_flow|other","statement_scope":"consolidated_group|company|unknown","contains_employee_count":false,"confidence":0.0,"reason":"short"}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result.
Mark an image as a statement only if it contains the relevant primary financial table or an obvious continuation of it. Set `statement_scope` to `consolidated_group` only when its heading says Consolidated, Group, or equivalent; set it to `company` when its heading says Company or Parent Company; otherwise use `unknown`. Set `contains_employee_count` true only when the page visibly discloses a total or average employee/persons-employed count, including an explicit narrative zero such as `has no employees`, `had no employees during the year`, or `employed no persons`. Staff-cost amounts alone, including `no staff costs`, are not employee-count evidence. Do not extract figures."""

EMPLOYEE_LOCATOR_PROMPT = """Read these numbered low-resolution page images from a UK Companies House accounts filing. Find every page that directly discloses a Company or Group employee count, including notes and narrative wording.
Return only JSON with one object for every supplied image, in exactly the same order:
{"pages":[{"statement_type":"other","statement_scope":"consolidated_group|company|unknown","contains_employee_count":false,"confidence":0.0,"reason":"short"}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result. Set `contains_employee_count` true for an explicit total or average employee count, or an unambiguous narrative zero such as `has no employees`, `had no employees during the year`, `does not directly employ any staff`, or `employed no persons`; a numeric table is not required. Set it false for staff-cost disclosures, `no staff costs`, director counts, and qualified wording such as `no employees other than directors` or `no employees during the year except for the directors`. Do not extract figures."""

TARGETED_EMPLOYEE_NOTE_RENDER_LONG_EDGE = 1024
# The locator classifies statement_type, statement_scope and
# contains_employee_count from these images. At the previous 384 px an A4 page
# is roughly 384x272 and a statement heading is a few pixels tall, which is a
# measured cause of balance sheets being labelled `income_statement` at stated
# confidence 0.95 and of employee-count pages never being flagged.
DEFAULT_LOCATOR_RENDER_LONG_EDGE = 768
TARGETED_EMPLOYEE_NOTE_BATCH_SIZE = 6
STATEMENT_COMPLETENESS_MIN_CONFIDENCE = 0.80
STATEMENT_COMPLETENESS_MAX_RECOVERY_PAGES = 3

EXTRACTION_PROMPT = """Read these numbered pages from a UK Companies House accounts filing. Extract only rows visibly present in a primary income statement, balance sheet, or cash-flow statement.
Return only JSON using this schema, with one page object for every supplied image in exactly the same order:
{"pages":[{"statement_type":"income_statement|balance_sheet|cash_flow|other","statement_scope":"consolidated_group|company|unknown","unit":"ISO 4217 currency code, optionally _THOUSANDS or _MILLIONS, or UNKNOWN","rows":[{"metric":"turnover|cost_of_sales|gross_profit|administrative_expenses|operating_result|profit_before_tax|tax|profit_after_tax|current_assets|cash|net_current_assets|net_assets|shareholders_funds|employees|gross_premiums_written|outward_reinsurance_premiums|net_premiums_written|net_change_unearned_premiums|net_earned_premiums|allocated_investment_return|total_technical_income|claims_incurred_net_reinsurance|net_operating_expenses|technical_account_result|investment_income","source_label":"exact row label","current_display":"exact displayed number or null","previous_display":"exact displayed number or null","current_column":"exact current column heading or null","previous_column":"exact previous column heading or null","evidence_text":"short transcription of the row and headings","confidence":0.0}]}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result. If an image has no primary-statement rows, return it with `statement_type` `other` and `rows`: []. Retain the displayed sign, commas, parentheses, dashes and scale; do not convert units; never use a year column heading as a value; the current period is the column headed by the most recent financial period end date, not simply the left-most column. Whenever both period columns are visible, transcribe both displayed cells for every extracted monetary row. A visible dash (`-`, en dash or em dash) is a reported zero: return it literally as `-`, never null. This applies even when every row's comparative cell is a dash, such as a first accounting period or a company newly incorporated in the prior year: transcribe each visible dash individually rather than treating the whole comparative column as absent. Use null only when that cell is genuinely blank, absent, or illegible; never infer a dash from context. Include both period cells in `evidence_text` where practical.

Set `statement_scope` from the visible statement heading using the same meanings as the locator. In either a Group or Company balance sheet, extract a standalone row labelled `Shareholders' funds`, `Shareholder funds`, `Shareholder funds attributable to equity interests`, or `Total equity` as `shareholders_funds`; deterministic code may then create a traceable `net_assets` equivalent while preserving its statement scope. Do not classify a combined balance-sheet total such as `Total liabilities and shareholders' funds` as `shareholders_funds` or `net_assets`. For a general insurance technical account, transcribe the native rows rather than guessing generic equivalents: Gross premiums written = gross_premiums_written; Earned premiums, net of reinsurance = net_earned_premiums; Claims incurred, net of reinsurance = claims_incurred_net_reinsurance; Balance or Result on the technical account for general business = technical_account_result. When a logical insurance row is split across a parent heading and child label, preserve both in `source_label`, for example `Premiums written - Gross amount`; never emit the ambiguous child label `Gross amount` alone. Use the other insurance-specific metric names when their matching rows are visible. Do not relabel these native rows as turnover, gross_profit or operating_result; deterministic code performs that mapping later."""

EMPLOYEE_EXTRACTION_PROMPT = """Read these pages from a UK Companies House accounts filing. They were selected because they may disclose employee numbers.
Return only JSON with one object for every supplied image in exactly the same order:
{"pages":[{"statement_type":"employee_note|other","unit":"COUNT","rows":[{"metric":"employees","source_label":"exact row label or note heading","current_display":"exact displayed number or dash, or null","previous_display":"exact displayed number or dash, or null","current_value_count":"normalised non-negative integer or null","previous_value_count":"normalised non-negative integer or null","current_evidence_kind":"numeric|dash_zero|narrative_zero|none","previous_evidence_kind":"numeric|dash_zero|narrative_zero|none","period_scope":"current|previous|both|unknown","current_column":"exact current column heading or null","previous_column":"exact previous column heading or null","evidence_text":"exact short transcription supporting the count","confidence":0.0}]}]}
Do not include page numbers: the calling code attaches the known PDF page number to each result. Extract only a disclosed total or average number of employees/persons employed. A clear narrative statement such as `The Company has no employees`, `The Company does not directly employ any staff`, or `The average number of staff employed during the period was nil (2023 - nil)` is direct employee evidence: set each explicitly supported value_count to 0, evidence_kind to `narrative_zero`, preserve the sentence exactly in evidence_text, and leave the display field null. Use `period_scope` to state which reported period(s) the wording supports. Treat an unqualified present-tense sentence as current-period evidence only. Do not copy a narrative zero into the comparative period unless the sentence or table explicitly names the prior/comparative period. A displayed dash in an employee-count table is `dash_zero` only when the row and period heading visibly establish it as the employee count. Do not use staff-cost amounts, director counts, `no staff costs`, or individual employee categories when a total is not shown. Reject wording such as `no employees other than directors` or `no employees during the year except for the directors` as ambiguous rather than treating it as zero. If the page has no qualifying employee count, return `statement_type` `other` and `rows`: []. Preserve displayed signs, commas and headings; do not infer a value."""

ROW_VALIDATION_RECOVERY_PROMPT = """Re-read this financial-statement page carefully. A deterministic evidence check found a possible row transcription or classification problem in an earlier pass.
Return the same ordered JSON schema as the normal financial-row extraction. Re-transcribe only rows visibly present in the primary statement, with exact source labels, values, units and column headings. When both period columns are visible, return each row's displayed cell for both periods. A visible dash (`-`, en dash or em dash) must be returned literally as `-`, never null; use null only when that period cell is genuinely blank, absent, or illegible. Do not infer a dash, totals, or a nearby subtotal, and do not use a year heading as a value."""

HIGH_RESOLUTION_RECOVERY_PROMPT = """This is a higher-resolution, single-page fallback for a primary financial statement page that the first vision pass did not transcribe adequately. Re-read the table and return the normal ordered JSON schema with this one page and every relevant visible row. Preserve exact signs, values, units and column headings. Do not return an empty page merely because the layout is difficult."""

STATEMENT_COMPLETENESS_RECOVERY_PROMPT = """Re-read this one primary financial-statement page at high resolution. A deterministic completeness check found that the earlier extraction may have omitted one or more visible rows. Re-transcribe the entire relevant table, not only the suspected metric. Return the normal ordered JSON schema with exact row labels, both displayed periods, units and headings. Preserve dashes, signs and parentheses exactly. Do not infer a value, substitute a subtotal, or invent a missing row."""

RATIONALISATION_PROMPT = """You are a text-only financial-data reviewer. Choose only from the supplied candidates, which were transcribed from financial-statement images. Do not invent values or alter digits.

Your job is to rationalise the candidates into the exact canonical financial-summary shape used by the XHTML/iXBRL extraction. Return ONLY JSON in this form:
{"financial_period_summaries":{"current":{"turnover":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"gross_profit":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"operating_result":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"profit_after_tax":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"cash":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"net_assets":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"employees":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0}},"previous":{"turnover":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"gross_profit":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"operating_result":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"profit_after_tax":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"cash":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"net_assets":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0},"employees":{"candidate_id":"id or null","reason":"short required explanation","confidence":0.0}}}}.

For `current`, the code will use the chosen candidate's `current_display`; for `previous`, it will use `previous_display`. A visible dash (`-`, en dash or em dash) in the matching monetary display field is a valid reported zero, not an absent value. When the same suitable row visibly supplies both period cells, select that row for both periods, including where one cell is a dash. A candidate with a null display field does not supply a value for that period: do not select it or infer a dash from `evidence_text`. Select only a candidate with the same metric name as the target column. The candidates are canonical output candidates; their `source_label` and `derivation.source_candidate_ids` preserve the original document row. Never select an aggregate or component as a proxy for another metric: in particular, `Current assets` is not cash. Use the supplied deterministic `evidence_tier`: lower numbers are stronger, and lower-tier candidates are not supplied when stronger evidence exists for that period. A primary insurance technical-account equivalent (tier 2) is stronger than the profit-before-tax synonym (tier 3); an exact supporting-note fallback is tier 4. A standalone `Shareholders' funds` or `Total equity` row is an eligible net-assets synonym only when supplied as a deterministic candidate. Do not select a combined `Total liabilities and shareholders' funds` balance-sheet total. The candidate list has already applied the filing-scope policy: direct Group evidence is preferred, but a Company income-statement candidate is preferred to a Group cash-flow or other fallback. Company SIC information, if supplied, is advisory registration metadata only: it must never override the visible statement type, source label, unit, scope, or deterministic evidence tier. Reject dates/year headings, unknown units, conflicting labels, and uncertain candidates. Never return bare `null`: when no suitable evidence exists, return an object with `candidate_id`: null and a concise, factual `reason` explaining why no candidate was selected."""


def normalise_company_context(company_context: dict[str, Any] | None) -> dict[str, Any]:
    """Return compact, prompt-safe company context without treating it as evidence."""
    source = company_context or {}
    sic_codes = source.get("sic_codes") or []
    if isinstance(sic_codes, str):
        sic_codes = [sic_codes]
    cleaned_codes = [str(code).strip() for code in sic_codes if str(code).strip()]
    result = {"sic_codes": list(dict.fromkeys(cleaned_codes))}
    company_number = source.get("company_number")
    if company_number is not None and str(company_number).strip():
        result["company_number"] = str(company_number).strip()
    return result


def company_context_diagnostics(
    company_context: dict[str, Any], candidates: list[dict[str, Any]],
    raw_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe whether registration metadata agrees with visible insurance evidence."""
    sic_codes = company_context.get("sic_codes") or []
    sic_indicates_insurance = any(re.match(r"^65[0-9]{3}\b", code) for code in sic_codes)
    primary_candidate_ids = [
        candidate["id"] for candidate in candidates
        if candidate.get("source_role") == "primary_insurance_income_statement"
    ]
    document_indicates_insurance = bool(primary_candidate_ids)
    if not sic_codes:
        alignment = "sic_unavailable"
    elif sic_indicates_insurance == document_indicates_insurance:
        alignment = "agreement"
    else:
        alignment = "document_evidence_overrides_sic"
    return {
        "sic_codes": sic_codes,
        "sic_indicates_insurance": sic_indicates_insurance,
        "document_indicates_insurance": document_indicates_insurance,
        "sic_document_alignment": alignment,
        "primary_insurance_candidate_ids": primary_candidate_ids,
        "exact_note_fallback_candidate_ids": [
            candidate["id"] for candidate in candidates
            if candidate.get("metric") in CANONICAL_METRICS
            and candidate.get("source_role") == "exact_insurance_note"
        ],
        "rejected_insurance_candidate_ids": [
            candidate["id"] for candidate in raw_candidates or []
            if candidate.get("metric") in INSURANCE_METRICS
            and (candidate.get("row_validation") or {}).get("status") == "rejected"
        ],
    }


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


def employee_note_candidate_pages(locator: dict[str, Any], page_count: int) -> list[int]:
    """Return non-statement pages for one direct employee pass.

    The broad locator already supplies a low-cost map of the document. When it
    finds no employee evidence, this bounded fallback avoids another all-page
    vision pass by looking only at pages the locator typed `other`.

    Employee disclosures are not confined to the notes: an explicit "the company
    has no employees" commonly appears in the Directors' or Strategic Report,
    ahead of the primary statements. Restricting this to pages after the first
    statement made two observed failures (gold evidence on pages 4 and 5)
    structurally unreachable, and on a scanned filing the text backstop cannot
    reach them either. All `other` pages are therefore eligible.
    """
    if not located_statement_pages(locator, page_count):
        return []
    return sorted({
        page
        for item in locator.get("pages") or []
        if (page := normalise_page_number(item.get("page"))) is not None
        and 1 <= page <= page_count
        and item.get("statement_type") == "other"
    })


def narrative_zero_employee_pages(pdf_path: Path, max_pages: int | None) -> list[int]:
    """Return text-addressable pages that explicitly disclose zero employees.

    This is a deterministic recall backstop for narrative notes.  It supplements
    the visual locator only for unambiguous phrases, so it cannot turn staff-cost
    text or a directors-only disclosure into an employee count.  Scanned-only
    PDFs remain dependent on the vision locator.
    """
    if fitz is None:
        return []
    try:
        document = fitz.open(str(pdf_path))
    except (OSError, RuntimeError):
        return []
    try:
        count = min(document.page_count, max_pages) if max_pages else document.page_count
        pages: list[int] = []
        for index in range(count):
            text = document.load_page(index).get_text("text")
            if (
                any(pattern.search(text) for pattern in NARRATIVE_ZERO_PATTERNS)
                and not AMBIGUOUS_NARRATIVE_ZERO_PATTERN.search(text)
            ):
                pages.append(index + 1)
        return pages
    finally:
        document.close()


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


def statement_completeness_recovery_pages(
    locator: dict[str, Any], extraction: dict[str, Any], *,
    min_confidence: float = STATEMENT_COMPLETENESS_MIN_CONFIDENCE,
    max_pages: int = STATEMENT_COMPLETENESS_MAX_RECOVERY_PAGES,
) -> dict[str, Any]:
    """Identify high-confidence primary pages that merit one full-table re-read.

    This is intentionally a recall policy, not an assertion that every filing
    must disclose every canonical metric. It only reacts to a partial primary
    statement family or an absent balance-sheet net-assets/equity row.
    """
    locator_by_page = {
        page: item
        for item in locator.get("pages") or []
        if (page := normalise_page_number(item.get("page"))) is not None
    }
    triggers_by_page: dict[int, list[str]] = {}
    eligible_pages: list[int] = []
    for page_item in extraction.get("pages") or []:
        page = normalise_page_number(page_item.get("page"))
        if page is None:
            continue
        located = locator_by_page.get(page) or {}
        # Prefer the type implied by the transcribed rows: a balance sheet the
        # locator mislabelled `income_statement` would otherwise be tested
        # against the income-family triggers and never qualify.
        statement_type = corrected_statement_type(
            {**page_item, "statement_type": located.get("statement_type")}
        )
        if statement_type not in PRIMARY_STATEMENT_TYPES:
            continue
        try:
            confidence = float(located.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            continue
        metrics = {
            row.get("metric")
            for row in page_item.get("rows") or []
            if row.get("metric") in METRICS
        }
        triggers: list[str] = []
        if statement_type == "income_statement":
            income_family = {
                "turnover", "gross_profit", "operating_result",
                "profit_before_tax", "profit_after_tax",
            }
            present = metrics & income_family
            has_operating_measure = bool({"operating_result", "profit_before_tax"} & present)
            complete_family = {
                "turnover", "gross_profit", "profit_after_tax",
            } <= present and has_operating_measure
            # A page yielding a single core metric is the one most likely to be
            # under-transcribed, so it must qualify too. Requiring two already
            # present excluded exactly that case.
            if len(present) >= 1 and not complete_family:
                triggers.append("income_statement_partial_core_family")
        elif statement_type == "balance_sheet":
            has_eligible_equity_row = any(
                row.get("metric") == "net_assets"
                or (
                    row.get("metric") == "shareholders_funds"
                    and _normalised_label(row.get("source_label"))
                    in {"shareholders funds", "shareholder funds", "total equity"}
                )
                for row in page_item.get("rows") or []
            )
            if not has_eligible_equity_row:
                triggers.append("balance_sheet_missing_net_assets")
            if "cash" not in metrics:
                if "current_assets" in metrics:
                    triggers.append("balance_sheet_current_assets_without_cash")
                elif metrics & MONEY_METRICS:
                    # Insurance and investment balance sheets have no `Current
                    # assets` heading at all (assets are grouped as Investments
                    # / Debtors / Other assets), so gating the cash re-read on
                    # `current_assets` silently exempted the filings where the
                    # cash row is hardest to find.
                    triggers.append("balance_sheet_missing_cash")
        elif statement_type == "cash_flow":
            if metrics and "cash" not in metrics:
                triggers.append("cash_flow_missing_cash_row")
        if triggers:
            eligible_pages.append(page)
            triggers_by_page[page] = triggers
    # Rank by trigger severity before applying the cap, so a balance sheet late
    # in the document is not displaced by earlier, less serious pages. Page
    # order only breaks ties.
    severity = {
        "balance_sheet_missing_net_assets": 0,
        "balance_sheet_missing_cash": 1,
        "balance_sheet_current_assets_without_cash": 1,
        "cash_flow_missing_cash_row": 2,
        "income_statement_partial_core_family": 3,
    }

    def _page_priority(page: int) -> tuple[int, int]:
        return (
            min((severity.get(name, 9) for name in triggers_by_page[page]), default=9),
            page,
        )

    recovery_pages = sorted(sorted(eligible_pages, key=_page_priority)[:max_pages])
    return {
        "min_confidence": min_confidence,
        "max_recovery_pages": max_pages,
        "eligible_pages": sorted(eligible_pages),
        "recovery_pages": recovery_pages,
        "skipped_due_to_page_cap": sorted(set(eligible_pages) - set(recovery_pages)),
        "triggers_by_page": {page: triggers_by_page[page] for page in recovery_pages},
        "added_rows_by_page": {},
        "conflicts_by_page": {},
        "warnings": [],
    }


def merge_completeness_recovery_page(
    original: dict[str, Any], recovered: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Add only newly found rows; never replace existing page evidence wholesale."""
    merged = copy.deepcopy(original)
    existing_rows = list(merged.get("rows") or [])
    existing_by_key = {
        (str(row.get("metric") or ""), _normalised_label(row.get("source_label"))): row
        for row in existing_rows
    }
    added: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for row in recovered.get("rows") or []:
        key = (str(row.get("metric") or ""), _normalised_label(row.get("source_label")))
        existing = existing_by_key.get(key)
        if existing is None:
            recovered_row = {**row, "recovery_source": "statement_completeness"}
            existing_rows.append(recovered_row)
            existing_by_key[key] = recovered_row
            added.append({"metric": row.get("metric"), "source_label": row.get("source_label")})
            continue
        if any(
            existing.get(field) != row.get(field)
            for field in ("current_display", "previous_display", "unit")
        ):
            conflicts.append({"metric": row.get("metric"), "source_label": row.get("source_label")})
    merged["rows"] = existing_rows
    return merged, added, conflicts


def normalise_unit(unit: Any) -> str:
    value = str(unit or "UNKNOWN").upper().replace("£", "GBP").replace("$", "USD").replace(" ", "_")
    aliases = {
        "GBP000": "GBP_THOUSANDS", "GBP_000": "GBP_THOUSANDS", "GBP000S": "GBP_THOUSANDS", "GBPM": "GBP_MILLIONS",
        "USD000": "USD_THOUSANDS", "USD_000": "USD_THOUSANDS", "USD000S": "USD_THOUSANDS", "USDM": "USD_MILLIONS",
    }
    value = aliases.get(value, value)
    # ``Ł`` is a common fallback rendering for the pound glyph in manually
    # transcribed review answers. Treat it equivalently to ``£``.
    value = aliases.get(value.replace("\u00a3", "GBP").replace("\u0141", "GBP"), value)
    return value if re.fullmatch(r"[A-Z]{3}(?:_(?:THOUSANDS|MILLIONS))?", value) else "UNKNOWN"


def currency_and_scale(unit: str) -> tuple[str | None, int | None]:
    """Split a document unit into currency and scale; this does not convert FX."""
    match = re.fullmatch(r"([A-Z]{3})(?:_(THOUSANDS|MILLIONS))?", unit or "")
    if not match:
        return None, None
    return match.group(1), {None: 1, "THOUSANDS": 1_000, "MILLIONS": 1_000_000}[match.group(2)]


def reported_value(displayed_value: Any, unit: str, metric: str) -> Decimal | None:
    if displayed_value is None or metric == "employees":
        return None
    _currency, scale = currency_and_scale(unit)
    if scale is None:
        return None
    token = str(displayed_value).strip()
    if re.fullmatch(r"[-\u2013\u2014]+", token):
        return Decimal("0")
    negative = token.startswith("-") or ("(" in token and ")" in token)
    try:
        value = Decimal(re.sub(r"[^0-9.]", "", token)) * Decimal(scale)
    except InvalidOperation:
        return None
    return -value if negative else value


def to_pence(displayed_value: Any, unit: str, metric: str) -> int | None:
    currency, _scale = currency_and_scale(unit)
    value = reported_value(displayed_value, unit, metric)
    if currency != "GBP" or value is None:
        return None
    return int((value * 100).to_integral_value())


def to_count(displayed_value: Any) -> int | None:
    if displayed_value is None:
        return None
    token = str(displayed_value).strip().replace(",", "")
    if re.fullmatch(r"[-\u2013\u2014]+", token):
        return 0
    return int(token) if re.fullmatch(r"\d+", token) else None


def employee_count(candidate: dict[str, Any], period: str) -> int | None:
    """Return a validated employee count without parsing prose as a number."""
    explicit = candidate.get(f"{period}_value_count")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0:
        return explicit
    return to_count(candidate.get(f"{period}_display"))


def employee_evidence_kind(candidate: dict[str, Any], period: str) -> str:
    """Return an explicit evidence kind, preserving compatibility with old rows."""
    kind = candidate.get(f"{period}_evidence_kind")
    if kind is not None:
        return str(kind)
    return "numeric" if to_count(candidate.get(f"{period}_display")) is not None else "none"


def normalise_employee_narrative_scope(candidate: dict[str, Any]) -> dict[str, Any]:
    """Limit unqualified narrative zero evidence to the current period.

    Vision models sometimes copy a present-tense sentence into both periods even
    though the filing does not mention a comparative period. Qualified wording
    is left intact here so deterministic validation can reject the whole row.
    """
    if candidate.get("metric") != "employees":
        return candidate
    evidence_text = str(candidate.get("evidence_text") or "")
    if AMBIGUOUS_NARRATIVE_ZERO_PATTERN.search(evidence_text):
        return candidate
    narrative_periods = {
        period for period in ("current", "previous")
        if employee_evidence_kind(candidate, period) == "narrative_zero"
        and employee_count(candidate, period) is not None
    }
    if (
        narrative_periods == {"current", "previous"}
        and candidate.get("period_scope") == "both"
        and not NARRATIVE_BOTH_PERIODS_PATTERN.search(evidence_text)
    ):
        candidate = dict(candidate)
        candidate["previous_display"] = None
        candidate["previous_value_count"] = None
        candidate["previous_evidence_kind"] = "none"
        candidate["period_scope"] = "current"
    return candidate


_INCOME_FAMILY_METRICS = frozenset({
    "turnover", "cost_of_sales", "gross_profit", "administrative_expenses",
    "operating_result", "profit_before_tax", "tax", "profit_after_tax",
}) | frozenset(INSURANCE_METRICS)
_BALANCE_FAMILY_METRICS = frozenset({
    "current_assets", "net_current_assets", "net_assets", "shareholders_funds",
})


def corrected_statement_type(page_item: dict[str, Any]) -> str | None:
    """Correct a page's statement type when its own rows plainly contradict it.

    The locator can label a balance sheet `income_statement` with high stated
    confidence. That is not a harmless mislabel: `canonical_metric_statement_is_compatible`
    rejects cash and net-assets rows that do not sit on a balance sheet, so
    every correctly transcribed row on the page is discarded as tier-5
    evidence and the metric is reported missing.

    This only overrides the locator when the visible evidence is unambiguous:
    no income-family row at all, and at least two distinct balance-sheet rows.
    It never reclassifies in the other direction, and never promotes `other`.
    """
    statement_type = page_item.get("statement_type")
    if statement_type != "income_statement":
        return statement_type
    metrics = {
        row.get("metric") for row in page_item.get("rows") or []
        if row.get("metric") in METRICS
    }
    if metrics & _INCOME_FAMILY_METRICS:
        return statement_type
    if len(metrics & _BALANCE_FAMILY_METRICS) >= 2:
        return "balance_sheet"
    return statement_type


def document_majority_currency(extraction: dict[str, Any]) -> str | None:
    """Return the currency code shared by most primary-statement pages.

    A single UK filing reports all of its primary statements in one currency.
    A lone page disagreeing with two or more others is far more likely to be a
    misread currency symbol (observed: a degraded "$" scanned as a bare "S",
    read as GBP) than a genuine change of reporting currency partway through
    the primary statements. Returns None when there is no clear majority, so a
    document that is genuinely silent or evenly split is left untouched.
    """
    counts: dict[str, int] = {}
    for page_item in extraction.get("pages") or []:
        if corrected_statement_type(page_item) not in PRIMARY_STATEMENT_TYPES:
            continue
        currency, _scale = currency_and_scale(normalise_unit(page_item.get("unit")))
        if currency:
            counts[currency] = counts.get(currency, 0) + 1
    if not counts:
        return None
    majority_currency, majority_count = max(counts.items(), key=lambda item: item[1])
    other_count = sum(count for currency, count in counts.items() if currency != majority_currency)
    return majority_currency if majority_count >= 2 and majority_count > other_count else None


def corrected_unit(unit: str, majority_currency: str | None) -> str:
    """Replace a minority currency with the document majority, keeping scale.

    No-op when the unit already matches, is unparseable, or there is no
    document majority to defer to.
    """
    if majority_currency is None:
        return unit
    currency, scale = currency_and_scale(unit)
    if currency is None or currency == majority_currency or scale is None:
        return unit
    scale_suffix = {1: "", 1_000: "_THOUSANDS", 1_000_000: "_MILLIONS"}[scale]
    return f"{majority_currency}{scale_suffix}"


def extraction_candidates(
    extraction: dict[str, Any], *, id_prefix: str = ""
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    majority_currency = document_majority_currency(extraction)
    for page_item in extraction.get("pages") or []:
        page = normalise_page_number(page_item.get("page"))
        if page is None or page < 1:
            continue
        statement_type = corrected_statement_type(page_item)
        unit = normalise_unit(page_item.get("unit"))
        if statement_type in PRIMARY_STATEMENT_TYPES:
            unit = corrected_unit(unit, majority_currency)
        for index, row in enumerate(page_item.get("rows") or []):
            metric = row.get("metric")
            if metric not in METRICS:
                continue
            candidate = {
                "id": f"{id_prefix}p{page}-r{index}", "metric": metric, "page": page,
                "extraction_source": row.get("recovery_source") or "normal",
                "statement_type": statement_type,
                "statement_scope": normalise_statement_scope(page_item.get("statement_scope")),
                "unit": unit,
                "source_label": row.get("source_label"), "current_display": row.get("current_display"),
                "previous_display": row.get("previous_display"), "current_column": row.get("current_column"),
                "previous_column": row.get("previous_column"), "evidence_text": row.get("evidence_text"),
                "current_value_count": row.get("current_value_count"),
                "previous_value_count": row.get("previous_value_count"),
                "current_evidence_kind": row.get("current_evidence_kind"),
                "previous_evidence_kind": row.get("previous_evidence_kind"),
                "period_scope": row.get("period_scope"),
                "confidence": row.get("confidence"),
            }
            candidates.append(normalise_employee_narrative_scope(candidate))
    return candidates


def apply_locator_statement_scopes(
    extraction: dict[str, Any], locator: dict[str, Any]
) -> None:
    """Attach reliable locator scope and primary statement type to extracted pages.

    The locator and extractor see the same rendered page but have distinct jobs.
    A direct locator classification takes precedence because it was made from
    the statement heading; extraction scope remains the fallback for neighbour
    pages included only to preserve visual context.
    """
    locator_details = {
        page: {
            "statement_scope": normalise_statement_scope(item.get("statement_scope")),
            "statement_type": item.get("statement_type"),
        }
        for item in locator.get("pages") or []
        if (page := normalise_page_number(item.get("page"))) is not None
    }
    for page_item in extraction.get("pages") or []:
        page = normalise_page_number(page_item.get("page"))
        located = locator_details.get(page, {})
        located_scope = located.get("statement_scope", "unknown")
        extracted_scope = normalise_statement_scope(page_item.get("statement_scope"))
        page_item["statement_scope"] = (
            located_scope if located_scope != "unknown" else extracted_scope
        )
        located_type = located.get("statement_type")
        extracted_type = page_item.get("statement_type")
        if located_type in PRIMARY_STATEMENT_TYPES:
            page_item["statement_type"] = located_type
        elif extracted_type not in PRIMARY_STATEMENT_TYPES:
            page_item["statement_type"] = "other"


def apply_consolidated_scope_policy(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prefer direct Group evidence, but not over a stronger Company candidate.

    A filing can contain a Group note as well as a Company primary statement.
    The former must not suppress a Company row for a metric absent from the
    Group evidence.  Apply the policy after canonical equivalents are created,
    then select between rows for the same final output metric.  Unknown-scope
    rows remain available because their headings could not be classified safely.
    """
    def evidence_tier(candidate: dict[str, Any]) -> int:
        """Read policy annotations, with a safe primary-row fallback for callers."""
        explicit = candidate.get("evidence_tier")
        if explicit is not None:
            return int(explicit)
        return (
            1
            if candidate.get("metric") in CANONICAL_METRICS
            and candidate.get("statement_type") in PRIMARY_STATEMENT_TYPES
            else 5
        )

    canonical = [
        candidate for candidate in candidates
        if candidate.get("metric") in CANONICAL_METRICS
    ]
    group_best_tier = {
        metric: min(
            evidence_tier(candidate)
            for candidate in canonical
            if candidate.get("statement_scope") == "consolidated_group"
            and candidate.get("metric") == metric
        )
        for metric in {
            str(candidate.get("metric"))
            for candidate in canonical
            if candidate.get("statement_scope") == "consolidated_group"
        }
    }
    company_best_tier = {
        metric: min(
            evidence_tier(candidate)
            for candidate in canonical
            if candidate.get("statement_scope") == "company"
            and candidate.get("metric") == metric
        )
        for metric in {
            str(candidate.get("metric"))
            for candidate in canonical
            if candidate.get("statement_scope") == "company"
        }
    }
    direct_group_metrics = {
        metric for metric, tier in group_best_tier.items() if tier <= 2
    }
    excluded = []
    for candidate in candidates:
        metric = str(candidate.get("metric"))
        scope = candidate.get("statement_scope")
        if scope == "company" and metric in direct_group_metrics:
            excluded.append(candidate)
        elif (
            scope == "consolidated_group"
            and metric in company_best_tier
            and metric not in direct_group_metrics
            and evidence_tier(candidate) >= company_best_tier[metric]
        ):
            excluded.append(candidate)
    kept = [candidate for candidate in candidates if candidate not in excluded]
    return kept, {
        "name": "prefer_direct_group_evidence_then_stronger_company_evidence",
        "consolidated_metrics": sorted(direct_group_metrics),
        "excluded_company_candidate_ids": [candidate["id"] for candidate in excluded],
    }


_CLEARLY_INCOMPATIBLE_LABELS = {
    "turnover": ("cost of sales", "gross profit", "administrative", "net assets"),
    "gross_profit": ("cost of sales", "administrative", "total assets", "net assets"),
    "operating_result": ("other operating", "administrative expenses", "cost of sales"),
    "profit_after_tax": ("tax charge", "taxation charge"),
    "cash": ("current assets", "total assets", "net assets", "total equity", "total liabilities"),
    "net_assets": (
        "current assets", "total assets", "cash and cash equivalents", "total liabilities",
        "total equity and liabilities",
    ),
    "shareholders_funds": ("total liabilities", "total equity and liabilities", "total assets"),
    "employees": ("staff costs", "wages and salaries", "social security costs"),
}


def _normalised_label(value: Any) -> str:
    value = re.sub(r"['\u2018\u2019]", "", str(value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


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
    if metric == "employees":
        employee_values = [employee_count(candidate, period) for period in ("current", "previous")]
        if not any(value is not None for value in employee_values):
            issues.append({"code": "missing_period_values", "message": "row has no employee count for either period"})
        for period, value in zip(("current", "previous"), employee_values):
            kind = employee_evidence_kind(candidate, period)
            display = candidate.get(f"{period}_display")
            if kind not in EMPLOYEE_EVIDENCE_KINDS:
                issues.append({"code": "invalid_employee_evidence_kind", "message": f"{period} employee evidence kind is invalid"})
                continue
            if kind == "narrative_zero" and value is not None:
                evidence_text = str(candidate.get("evidence_text") or "")
                if AMBIGUOUS_NARRATIVE_ZERO_PATTERN.search(evidence_text):
                    issues.append({"code": "ambiguous_narrative_zero_evidence", "message": "narrative zero is qualified"})
                elif value != 0 or not any(pattern.search(evidence_text) for pattern in NARRATIVE_ZERO_PATTERNS):
                    issues.append({"code": "invalid_narrative_zero_evidence", "message": "narrative zero lacks a direct no-employees statement"})
            elif kind == "dash_zero" and value is not None:
                if value != 0 or not re.fullmatch(r"[-\u2013\u2014]+", str(display or "").strip()):
                    issues.append({"code": "invalid_dash_zero_evidence", "message": "employee dash zero lacks a visible dash"})
            elif kind == "numeric" and value is not None and to_count(display) is None:
                issues.append({"code": "invalid_numeric_employee_evidence", "message": "employee numeric evidence lacks a displayed integer"})
        narrative_periods = {
            period for period in ("current", "previous")
            if employee_evidence_kind(candidate, period) == "narrative_zero"
            and employee_count(candidate, period) is not None
        }
        if narrative_periods:
            scope = candidate.get("period_scope")
            allowed_periods = {
                "current": {"current"}, "previous": {"previous"}, "both": {"current", "previous"},
            }.get(scope)
            if allowed_periods is None or narrative_periods != allowed_periods:
                issues.append({"code": "narrative_zero_period_scope_mismatch", "message": "narrative zero periods do not match its explicit scope"})
    elif not any(value is not None and str(value).strip() for value in values):
        issues.append({"code": "missing_period_values", "message": "row has no displayed period value"})
    if metric in MONEY_METRICS and candidate.get("unit") not in UNIT_MULTIPLIERS:
        issues.append({"code": "unknown_money_unit", "message": "money row has no recognised unit"})
    if metric in INSURANCE_METRICS and not insurance_label_is_compatible(
        metric, candidate.get("source_label")
    ):
        issues.append({
            "code": "insurance_metric_label_conflict",
            "message": (
                f"{metric} requires its compatible visible insurance row label; "
                f"got '{candidate.get('source_label') or ''}'"
            ),
        })
    if metric in CANONICAL_METRICS and not canonical_metric_label_is_compatible(
        metric, candidate.get("source_label")
    ):
        issues.append({
            "code": "metric_label_conflict",
            "message": f"{metric} is not compatible with source label '{candidate.get('source_label') or ''}'",
        })
    if metric in CANONICAL_METRICS and not canonical_metric_statement_is_compatible(
        metric, candidate.get("statement_type")
    ):
        issues.append({
            "code": "metric_statement_conflict",
            "message": f"{metric} is not a primary metric for {candidate.get('statement_type') or 'unknown'}",
        })
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


def incomplete_two_period_money_row_issue(
    candidate: dict[str, Any],
) -> dict[str, str] | None:
    """Identify recoverable one-sided rows when both monetary columns are present.

    This requests a sharper re-read; it deliberately does not reject the known
    value or infer that the absent cell is zero. A first-year filing or an
    actually blank comparative can therefore remain missing after recovery.
    """
    if candidate.get("metric") not in MONEY_METRICS:
        return None
    if not all(str(candidate.get(f"{period}_column") or "").strip() for period in ("current", "previous")):
        return None
    has_value = {
        period: candidate.get(f"{period}_display") is not None
        and bool(str(candidate.get(f"{period}_display")).strip())
        for period in ("current", "previous")
    }
    if has_value["current"] == has_value["previous"]:
        return None
    missing_period = "previous" if has_value["current"] else "current"
    return {
        "code": "incomplete_two_period_money_row",
        "message": (
            f"both monetary columns are present but the {missing_period} row cell was not transcribed"
        ),
    }


def validate_extraction_candidates(
    extraction: dict[str, Any], *, id_prefix: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Separate usable candidates from rejected evidence and report page-level issues."""
    all_candidates: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    issues_by_page: dict[int, list[dict[str, Any]]] = {}
    incomplete_period_pairs_by_page: dict[int, list[dict[str, Any]]] = {}
    for candidate in extraction_candidates(extraction, id_prefix=id_prefix):
        issues = candidate_validation_issues(candidate)
        incomplete_pair_issue = incomplete_two_period_money_row_issue(candidate)
        annotated = {
            **candidate,
            "row_validation": {
                "status": "accepted" if not issues else "rejected",
                "issues": issues,
                "recoverable_issues": [incomplete_pair_issue] if incomplete_pair_issue else [],
            },
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
            if incomplete_pair_issue:
                incomplete_period_pairs_by_page.setdefault(int(annotated["page"]), []).append({
                    "candidate_id": annotated["id"],
                    "metric": annotated["metric"],
                    "issue": incomplete_pair_issue,
                })
    return all_candidates, accepted, {
        "invalid_pages": sorted(issues_by_page),
        "issues_by_page": issues_by_page,
        "incomplete_period_pair_pages": sorted(incomplete_period_pairs_by_page),
        "incomplete_period_pairs_by_page": incomplete_period_pairs_by_page,
        "recovery_pages": [],
        "replaced_pages": [],
        "remaining_invalid_pages": [],
        "remaining_incomplete_period_pair_pages": [],
        "warnings": [],
    }


def page_row_validation_quality(page: dict[str, Any]) -> tuple[int, int, int, int]:
    """Rank an original/recovery page without trusting model confidence alone."""
    all_candidates, accepted, report = validate_extraction_candidates({"pages": [page]})
    return (
        len(accepted),
        -len(report["incomplete_period_pair_pages"]),
        -len(all_candidates) + len(accepted),
        len(all_candidates),
    )


def _has_usable_period_value(candidate: dict[str, Any], period: str) -> bool:
    """Return whether a selected candidate visibly supplies one period's value."""
    if candidate.get("metric") == "employees":
        return employee_count(candidate, period) is not None
    display = candidate.get(f"{period}_display")
    return to_pence(display, str(candidate.get("unit")), str(candidate.get("metric"))) is not None


def _selected_candidate_id(choice: Any) -> str | None:
    """Return a model-selected candidate ID, excluding explicit no-selection decisions."""
    candidate_id = choice.get("candidate_id") if isinstance(choice, dict) else None
    return candidate_id if isinstance(candidate_id, str) and candidate_id else None


def canonical_rationalisation_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose only candidates that can safely fill a canonical output metric.

    Derived candidates retain their original source-row IDs and labels in
    provenance, so the text reviewer can assess the visible evidence without
    being offered a raw synonym ID that final assembly cannot use.
    """
    return [
        candidate for candidate in candidates
        if candidate.get("metric") in CANONICAL_METRICS
    ]


def resolve_canonical_rationalisation_choices(
    candidates: list[dict[str, Any]], rationalisation: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Translate a selected direct-synonym source ID to its canonical candidate.

    This is a narrow compatibility safety net for a model response that selects
    an original row ID rather than the supplied canonical equivalent. It never
    infers a relationship: only a pre-created reported-equivalent candidate
    whose provenance names that exact source ID can be used.
    """
    resolved = copy.deepcopy(rationalisation)
    summaries = resolved.get("financial_period_summaries")
    if not isinstance(summaries, dict):
        return resolved, []
    candidates_by_id = {str(candidate.get("id")): candidate for candidate in candidates}
    translations: list[dict[str, str]] = []
    for period in ("current", "previous"):
        decisions = summaries.get(period)
        if not isinstance(decisions, dict):
            continue
        for metric in CANONICAL_METRICS:
            choice = decisions.get(metric)
            selected_id = _selected_candidate_id(choice)
            if selected_id is None or not isinstance(choice, dict):
                continue
            selected = candidates_by_id.get(selected_id)
            if selected is not None and selected.get("metric") == metric:
                continue
            equivalents = [
                candidate for candidate in candidates
                if candidate.get("metric") == metric
                and (candidate.get("derivation") or {}).get("kind") == "reported_equivalent"
                and selected_id in (candidate.get("derivation") or {}).get("source_candidate_ids", [])
            ]
            if len(equivalents) != 1:
                continue
            canonical = equivalents[0]
            choice["candidate_id"] = canonical["id"]
            choice["canonicalised_from_candidate_id"] = selected_id
            translations.append({
                "period": period,
                "metric": metric,
                "source_candidate_id": selected_id,
                "canonical_candidate_id": canonical["id"],
            })
    return resolved, translations


def rationalisation_diagnostics(
    candidates: list[dict[str, Any]],
    raw_candidates: list[dict[str, Any]],
    rationalisation: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Explain selection gaps using deterministic candidate evidence, not model inference.

    These diagnostics deliberately describe pipeline facts (available candidates,
    rejected candidates, and usable period cells). They complement rather than
    replace the model's own selection/no-selection reason.
    """
    periods = rationalisation.get("financial_period_summaries") or {}
    diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
    for period in ("current", "previous"):
        decisions = periods.get(period) or {}
        diagnostics[period] = {}
        for metric in CANONICAL_METRICS:
            choice = decisions.get(metric)
            selected_id = _selected_candidate_id(choice)
            model_reason = choice.get("reason") if isinstance(choice, dict) else None
            usable = [
                candidate for candidate in candidates
                if candidate.get("metric") == metric and _has_usable_period_value(candidate, period)
            ]
            rejected = [
                candidate for candidate in raw_candidates
                if candidate.get("metric") == metric
                and (candidate.get("row_validation") or {}).get("status") == "rejected"
            ]
            if selected_id is not None:
                status = "selected"
                message = "Model selected an accepted candidate."
            elif usable:
                status = "unselected_despite_usable_candidate"
                message = "Accepted candidate evidence supplied a usable period value, but none was selected."
            elif rejected:
                status = "only_rejected_candidates"
                message = "Candidate evidence was extracted but rejected by deterministic row validation."
            else:
                matching = [candidate for candidate in candidates if candidate.get("metric") == metric]
                status = "no_usable_candidate" if matching else "no_candidate_extracted"
                message = (
                    "Candidates existed but none supplied a usable value for this period."
                    if matching else "No candidate for this canonical metric was extracted."
                )
            diagnostics[period][metric] = {
                "status": status,
                "message": message,
                "model_candidate_id": selected_id,
                "model_reason": model_reason,
                "usable_candidate_ids": [candidate["id"] for candidate in usable],
                "rejected_candidates": [
                    {"id": candidate["id"], "issues": (candidate.get("row_validation") or {}).get("issues", [])}
                    for candidate in rejected
                ],
            }
    return diagnostics


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
            if _selected_candidate_id(target_choices.get(metric)) is not None or not isinstance(source_choice, dict):
                continue
            candidate = by_id.get(str(source_choice.get("candidate_id")))
            if (
                candidate is None
                or _selected_candidate_id(source_choice) is None
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
            if candidate is None or candidate["metric"] != metric:
                continue
            display = candidate[display_field]
            unit = candidate["unit"]
            value_count = employee_count(candidate, period_type) if metric == "employees" else None
            if metric == "employees" and value_count is None:
                continue
            if metric != "employees" and display is None:
                continue
            validation = {
                "unit_known": metric == "employees" or currency_and_scale(unit)[0] is not None,
                "looks_like_year": str(display).strip("() -") in {"2022", "2023", "2024", "2025", "2026"},
                "evidence_kind": employee_evidence_kind(candidate, period_type) if metric == "employees" else None,
                "period_scope": candidate.get("period_scope") if metric == "employees" else None,
                "review_reason": choice.get("reason"),
                "rationalised_column": metric,
                "derivation": candidate.get("derivation"),
                "row_validation": candidate.get("row_validation"),
            }
            currency_code, scale_multiplier = currency_and_scale(unit)
            amount = reported_value(display, unit, metric)
            metrics.append({
                "period_type": period_type,
                "financial_year": parse_financial_year(candidate.get(column_field)),
                "metric_name": metric,
                "value_pence": to_pence(display, unit, metric),
                "value_count": value_count,
                "displayed_value": str(display) if display is not None else None,
                "unit": unit if metric in MONEY_METRICS else "COUNT",
                "currency_code": currency_code if metric in MONEY_METRICS else None,
                "scale_multiplier": scale_multiplier if metric in MONEY_METRICS else None,
                "reported_value": str(amount) if amount is not None else None,
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
            if choice is None:
                raise ValueError(
                    f"response {period}.{metric} must be a decision object with a reason, not null"
                )
            if not isinstance(choice, dict):
                raise ValueError(f"response {period}.{metric} must be a decision object")
            candidate_id = choice.get("candidate_id")
            if candidate_id is not None and not isinstance(candidate_id, str):
                raise ValueError(f"response {period}.{metric}.candidate_id must be a string or null")
            if candidate_id is None and (
                not isinstance(choice.get("reason"), str) or not choice["reason"].strip()
            ):
                raise ValueError(f"response {period}.{metric}.reason must be a non-empty string")


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
    locator_render_long_edge: int = DEFAULT_LOCATOR_RENDER_LONG_EDGE,
    recovery_render_long_edge: int = 2048,
    json_max_attempts: int = 2,
    gbp_per_usd: float = 0.75,
    timeout: int = 180,
    company_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run statement discovery, extraction and text review through one model client."""
    company_context = normalise_company_context(company_context)
    if locator_render_long_edge < 256:
        raise ValueError("locator_render_long_edge must be at least 256")
    started = time.perf_counter()
    render_started = time.perf_counter()
    thumbnails = render_pages(
        pdf_path, max_pages=max_pages, long_edge=locator_render_long_edge
    )
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
    employee_locator_calls: list[ModelCallResult] = []
    employee_note_extraction_calls: list[ModelCallResult] = []
    extraction_calls: list[ModelCallResult] = []
    recovery_vision_calls: list[ModelCallResult] = []
    rationalisation_calls: list[ModelCallResult] = []
    soft_vision_failures: list[dict[str, Any]] = []
    failed: StageCallError | None = None
    locator: dict[str, Any] = {"pages": []}
    selected: list[int] = []
    employee_pages: list[int] = []
    targeted_employee_note_pages: list[int] = []
    targeted_employee_evidence_pages: list[int] = []
    detail_render_seconds = 0.0
    employee_note_render_seconds = 0.0
    recovery_render_seconds = 0.0
    extraction: dict[str, Any] = {"pages": []}
    employee_extraction: dict[str, Any] = {"pages": []}
    targeted_employee_extraction: dict[str, Any] = {"pages": []}
    rationalisation: dict[str, Any] = {"financial_period_summaries": {}}
    candidates: list[dict[str, Any]] = []
    statement_scope_policy: dict[str, Any] = {
        "name": "prefer_consolidated_group_per_canonical_metric",
        "consolidated_metrics": [],
        "excluded_company_candidate_ids": [],
    }
    extraction_batches: list[list[RenderedPage]] = []
    employee_extraction_batches: list[list[RenderedPage]] = []
    employee_note_extraction_batches: list[list[RenderedPage]] = []
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
    statement_completeness: dict[str, Any] = {
        "min_confidence": STATEMENT_COMPLETENESS_MIN_CONFIDENCE,
        "max_recovery_pages": STATEMENT_COMPLETENESS_MAX_RECOVERY_PAGES,
        "eligible_pages": [],
        "recovery_pages": [],
        "skipped_due_to_page_cap": [],
        "triggers_by_page": {},
        "added_rows_by_page": {},
        "conflicts_by_page": {},
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
        locator_employee_pages = employee_evidence_pages(locator, len(thumbnails))
        text_narrative_employee_pages = narrative_zero_employee_pages(pdf_path, max_pages)
        employee_pages = sorted({*locator_employee_pages, *text_narrative_employee_pages})
        if pdf_path.exists() and not employee_pages:
            targeted_employee_note_pages = employee_note_candidate_pages(
                locator, len(thumbnails)
            )
            if targeted_employee_note_pages:
                render_started = time.perf_counter()
                targeted_employee_details = render_pages(
                    pdf_path,
                    max_pages=max_pages,
                    long_edge=TARGETED_EMPLOYEE_NOTE_RENDER_LONG_EDGE,
                    page_numbers=targeted_employee_note_pages,
                )
                employee_note_render_seconds = time.perf_counter() - render_started
                employee_note_extraction_batches = [
                    targeted_employee_details[index:index + TARGETED_EMPLOYEE_NOTE_BATCH_SIZE]
                    for index in range(
                        0, len(targeted_employee_details), TARGETED_EMPLOYEE_NOTE_BATCH_SIZE
                    )
                ]
                employee_note_call_batches: list[tuple[ModelCallResult, list[RenderedPage]]] = []
                for batch_number, batch in enumerate(employee_note_extraction_batches, start=1):
                    try:
                        employee_note_call = generate_json_reliably(
                            model_client,
                            vision_model,
                            EMPLOYEE_EXTRACTION_PROMPT,
                            batch,
                            timeout,
                            stage="employee_note_extraction",
                            validator=lambda payload: validate_page_response(
                                payload, require_rows=True, expected_page_count=len(batch)
                            ),
                            max_attempts=json_max_attempts,
                            batch_number=batch_number,
                        )
                    except StageCallError as error:
                        failed = error
                        break
                    employee_note_extraction_calls.append(employee_note_call)
                    employee_note_call_batches.append((employee_note_call, batch))
                targeted_employee_extraction["pages"] = merge_extraction_pages(
                    employee_note_call_batches
                )
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

    if targeted_employee_extraction["pages"]:
        employee_extraction["pages"] = sorted(
            [
                *targeted_employee_extraction["pages"],
                *employee_extraction["pages"],
            ],
            key=lambda page: int(page["page"]),
        )

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
        recovery_pages = sorted(set(
            financial_validation["invalid_pages"]
            + financial_validation["incomplete_period_pair_pages"]
        ))
        for page_number in recovery_pages:
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
        financial_validation["remaining_incomplete_period_pair_pages"] = (
            final_financial_validation["incomplete_period_pair_pages"]
        )
        financial_validation["issues_by_page"] = final_financial_validation["issues_by_page"]
        financial_validation["incomplete_period_pairs_by_page"] = (
            final_financial_validation["incomplete_period_pairs_by_page"]
        )

        statement_completeness = statement_completeness_recovery_pages(locator, extraction)
        for page_number in statement_completeness["recovery_pages"]:
            recovery_page = render_focused_recovery_page(page_number)
            triggers = statement_completeness["triggers_by_page"][page_number]
            try:
                recovery_call = generate_json_reliably(
                    model_client,
                    effective_recovery_vision_model,
                    (
                        f"{EXTRACTION_PROMPT}\n\n{STATEMENT_COMPLETENESS_RECOVERY_PROMPT}\n\n"
                        f"Completeness signals for this page: {', '.join(triggers)}."
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
                statement_completeness["warnings"].append({
                    "code": "statement_completeness_recovery_failed",
                    "page": page_number,
                    "message": str(error),
                })
                continue
            recovery_vision_calls.append(recovery_call)
            recovered_page = attach_document_pages(
                recovery_call.payload.get("pages") or [], [recovery_page]
            )[0]
            merged_page, added_rows, conflicts = merge_completeness_recovery_page(
                page_by_number[page_number], recovered_page
            )
            statement_completeness["added_rows_by_page"][page_number] = added_rows
            statement_completeness["conflicts_by_page"][page_number] = conflicts
            if added_rows:
                page_by_number[page_number] = merged_page
            else:
                statement_completeness["warnings"].append({
                    "code": "statement_completeness_recovery_added_no_rows",
                    "page": page_number,
                    "message": "Focused re-extraction added no previously unseen statement rows.",
                })
        extraction["pages"] = [page_by_number[page] for page in sorted(page_by_number)]
        apply_locator_statement_scopes(extraction, locator)
        all_financial_candidates, accepted_financial_candidates, final_financial_validation = (
            validate_extraction_candidates(extraction)
        )
        financial_validation["remaining_invalid_pages"] = final_financial_validation["invalid_pages"]
        financial_validation["remaining_incomplete_period_pair_pages"] = (
            final_financial_validation["incomplete_period_pair_pages"]
        )
        financial_validation["issues_by_page"] = final_financial_validation["issues_by_page"]
        financial_validation["incomplete_period_pairs_by_page"] = (
            final_financial_validation["incomplete_period_pairs_by_page"]
        )

        all_employee_candidates, accepted_employee_candidates, employee_validation = (
            validate_extraction_candidates(employee_extraction, id_prefix="employee-")
        )
        row_validation["employees"] = employee_validation
        targeted_employee_evidence_pages = sorted({
            int(candidate["page"])
            for candidate in accepted_employee_candidates
            if candidate.get("metric") == "employees"
            and int(candidate["page"]) in targeted_employee_note_pages
        })
        employee_pages = sorted({*employee_pages, *targeted_employee_evidence_pages})
        all_raw_candidates = all_financial_candidates + all_employee_candidates
        candidates_with_equivalents = add_canonical_equivalents_by_statement_scope(
            accepted_financial_candidates + accepted_employee_candidates
        )
        candidates, statement_scope_policy = apply_consolidated_scope_policy(
            candidates_with_equivalents
        )
        rationalisation_candidates = canonical_rationalisation_candidates(candidates)
        if rationalisation_candidates:
            try:
                rationalisation_call = generate_json_reliably(
                    model_client,
                    rationalisation_model,
                    (
                        f"{RATIONALISATION_PROMPT}\n\n"
                        f"COMPANY_CONTEXT_ADVISORY_ONLY:\n"
                        f"{json.dumps(company_context, separators=(',', ':'))}\n\n"
                        f"CANDIDATES:\n{json.dumps({'candidates': rationalisation_candidates}, separators=(',', ':'))}"
                    ),
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

    canonical_rationalisation, canonical_choice_translations = (
        resolve_canonical_rationalisation_choices(candidates, rationalisation)
    )
    resolved_rationalisation, paired_period_completions = complete_paired_period_choices(
        candidates, canonical_rationalisation
    )
    diagnostics = rationalisation_diagnostics(
        candidates, all_raw_candidates, resolved_rationalisation
    )
    insurance_diagnostics = company_context_diagnostics(
        company_context, candidates, all_raw_candidates
    )

    pricing_snapshot = model_client.pricing_snapshot()
    failed_by_stage = {
        stage: failed.attempts if failed is not None and failed.stage == stage else []
        for stage in (
            "locator", "employee_locator", "employee_note_extraction", "vision",
            "vision_recovery", "rationalisation",
        )
    }
    calls = {
        "locator": _stage_call_summary(locator_model, locator_calls, failed_by_stage["locator"]),
        "employee_locator": _stage_call_summary(
            locator_model, employee_locator_calls, failed_by_stage["employee_locator"]
        ),
        "employee_note_extraction": _stage_call_summary(
            vision_model,
            employee_note_extraction_calls,
            failed_by_stage["employee_note_extraction"],
        ),
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
        "company_context": company_context,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "timing": {
            "thumbnail_render_seconds": round(thumbnail_render_seconds, 4),
            "detail_render_seconds": round(detail_render_seconds, 4),
            "employee_note_render_seconds": round(employee_note_render_seconds, 4),
            "recovery_render_seconds": round(recovery_render_seconds, 4),
            "image_payload_bytes": sum(item["image_payload_bytes"] for item in calls.values()),
            "locator_batches": len(batches),
            "extraction_batches": len(extraction_batches),
            "employee_extraction_batches": len(employee_extraction_batches),
            "employee_note_extraction_batches": len(employee_note_extraction_batches),
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
        "employee_evidence_pages_by_source": {
            "locator": locator_employee_pages if failed is None else [],
            "text_narrative_zero": text_narrative_employee_pages if failed is None else [],
            "targeted_note_extraction": (
                targeted_employee_evidence_pages if failed is None else []
            ),
        },
        "raw_extraction": {
            "locator": locator,
            "employee_locator": (
                {"pages": locator.get("employee_pages") or []}
                if employee_locator_calls else None
            ),
            "targeted_employee_note_pages": targeted_employee_note_pages,
            "detail": extraction,
            "employee_detail": employee_extraction,
            "candidates": all_raw_candidates,
            "accepted_candidates": candidates,
            "coverage": extraction_coverage,
            "row_validation": row_validation,
            "statement_completeness": statement_completeness,
        },
        "rationalisation": rationalisation,
        "resolved_rationalisation": resolved_rationalisation,
        "rationalisation_policy": {
            "canonical_choice_translations": canonical_choice_translations,
            "paired_period_completions": paired_period_completions,
        },
        "rationalisation_diagnostics": diagnostics,
        "insurance_policy_diagnostics": insurance_diagnostics,
        "metrics": selected_metrics(candidates, resolved_rationalisation),
        "warnings": (
            extraction_coverage["warnings"]
            + row_validation["financial"]["warnings"]
            + row_validation["employees"]["warnings"]
            + statement_completeness["warnings"]
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


def company_context_from_sqlite(
    db_path: Path, company_number: str | None, explicit_sic_codes: list[str] | None = None
) -> dict[str, Any]:
    """Load existing registration metadata without requiring an API request."""
    sic_codes = list(explicit_sic_codes or [])
    if company_number and db_path.is_file():
        import sqlite3

        try:
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "select sic_1 from leads where company_number = ?", (company_number,)
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            sic_codes.append(str(row[0]))
    return normalise_company_context({
        "company_number": company_number,
        "sic_codes": sic_codes,
    })


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="VLM financial-statement extraction; no local OCR is used.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--db", help="Optional SQLite database to receive the VLM run and metrics.")
    parser.add_argument("--company-number")
    parser.add_argument(
        "--sic-code",
        action="append",
        default=[],
        help="Advisory SIC code/description; repeat for multiple registrations.",
    )
    parser.add_argument("--document-id")
    parser.add_argument("--output-json")
    parser.add_argument("--max-pages", type=int, default=60)
    parser.add_argument("--locator-model", default=DEFAULT_LOCATOR_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--recovery-vision-model")
    parser.add_argument("--rationalisation-model", default=DEFAULT_RATIONALISATION_MODEL)
    parser.add_argument("--locator-render-long-edge", type=int, default=DEFAULT_LOCATOR_RENDER_LONG_EDGE,
                        help="Long edge in px for locator thumbnails.")
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

    company_context = company_context_from_sqlite(
        Path(args.db) if args.db else Path(), args.company_number, args.sic_code
    )
    payload = process_pdf_vlm_financials(
        Path(args.pdf), model_client, locator_model=args.locator_model, vision_model=args.vision_model,
        recovery_vision_model=args.recovery_vision_model,
        rationalisation_model=args.rationalisation_model, max_pages=args.max_pages, gbp_per_usd=args.gbp_per_usd,
        json_max_attempts=args.json_max_attempts, recovery_render_long_edge=args.recovery_render_long_edge,
        locator_render_long_edge=args.locator_render_long_edge,
        company_context=company_context,
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
