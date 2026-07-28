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
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

from companies_house_extractor import load_dotenv
from companies_house_sqlite import init_db, insert_vlm_financial_payload

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
)
MONEY_METRICS = set(METRICS) - {"employees"}
CANONICAL_METRICS = (
    "turnover", "gross_profit", "operating_result", "profit_after_tax",
    "cash", "net_assets", "employees",
)
UNIT_MULTIPLIERS = {"GBP": 100, "GBP_THOUSANDS": 100_000, "GBP_MILLIONS": 100_000_000}

LOCATOR_PROMPT = """You are identifying financial statement pages in a UK Companies House accounts PDF.
The images are numbered pages. Return only JSON:
{"pages":[{"page":1,"statement_type":"income_statement|balance_sheet|cash_flow|other","confidence":0.0,"reason":"short"}]}
Mark a page as a statement only if it contains the relevant primary financial table or an obvious continuation of it. Do not extract figures."""

EXTRACTION_PROMPT = """Read these numbered pages from a UK Companies House accounts filing. Extract only rows visibly present in a primary income statement, balance sheet, or cash-flow statement.
Return only JSON using this schema:
{"pages":[{"page":1,"statement_type":"income_statement|balance_sheet|cash_flow|other","unit":"GBP|GBP_THOUSANDS|GBP_MILLIONS|UNKNOWN","rows":[{"metric":"turnover|cost_of_sales|gross_profit|administrative_expenses|operating_result|profit_before_tax|tax|profit_after_tax|current_assets|cash|net_current_assets|net_assets|employees","source_label":"exact row label","current_display":"exact displayed number or null","previous_display":"exact displayed number or null","current_column":"exact current column heading or null","previous_column":"exact previous column heading or null","evidence_text":"short transcription of the row and headings","confidence":0.0}]}]}
Rules: retain the displayed sign, commas, parentheses and scale; do not convert units; never use a year column heading as a value; use null rather than guessing; the current period is the column headed by the most recent financial period end date, not simply the left-most column."""

RATIONALISATION_PROMPT = """You are a text-only financial-data reviewer. Choose only from the supplied candidates, which were transcribed from financial-statement images. Do not invent values or alter digits.

Your job is to rationalise the candidates into the exact canonical financial-summary shape used by the XHTML/iXBRL extraction. Return ONLY JSON in this form:
{"financial_period_summaries":{"current":{"turnover":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"gross_profit":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"operating_result":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"profit_after_tax":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"cash":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"net_assets":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"employees":{"candidate_id":"id","reason":"short","confidence":0.0}|null},"previous":{"turnover":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"gross_profit":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"operating_result":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"profit_after_tax":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"cash":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"net_assets":{"candidate_id":"id","reason":"short","confidence":0.0}|null,"employees":{"candidate_id":"id","reason":"short","confidence":0.0}|null}}}.

For `current`, the code will use the chosen candidate's `current_display`; for `previous`, it will use `previous_display`. Select only a candidate with the same metric name as the target column. Prefer primary-statement rows with clear period headings. Reject dates/year headings, unknown units, conflicting labels, and uncertain candidates. A null field is correct when no suitable evidence exists."""


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


def _json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", cleaned)
    if fenced:
        cleaned = fenced.group(1)
    return json.loads(cleaned)


def render_pages(pdf_path: Path, *, max_pages: int | None, long_edge: int) -> list[RenderedPage]:
    if fitz is None:
        raise RuntimeError("PyMuPDF is required. Install it with: pip install pymupdf")
    document = fitz.open(str(pdf_path))
    try:
        count = min(document.page_count, max_pages) if max_pages else document.page_count
        result: list[RenderedPage] = []
        for number in range(1, count + 1):
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
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": page_content(pages, prompt)}],
                "temperature": 0,
                **self._request_options,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        return ModelCallResult(
            _json_response(body["choices"][0]["message"]["content"]),
            body.get("usage") or {},
            time.perf_counter() - started,
            sum(len(page.image_b64) * 3 // 4 for page in pages),
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
        return ModelCallResult(
            _json_response(content_value),
            usage,
            elapsed,
            sum(len(page.image_b64) * 3 // 4 for page in pages),
            model_reported_seconds,
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


def statement_pages(locator: dict[str, Any], page_count: int) -> list[int]:
    selected: set[int] = set()
    for item in locator.get("pages") or []:
        page = item.get("page")
        if isinstance(page, int) and 1 <= page <= page_count and item.get("statement_type") != "other":
            selected.update(range(max(1, page - 1), min(page_count, page + 1) + 1))
    return sorted(selected)


def normalise_unit(unit: Any) -> str:
    value = str(unit or "UNKNOWN").upper().replace("£", "GBP").replace(" ", "_")
    aliases = {"GBP000": "GBP_THOUSANDS", "GBP_000": "GBP_THOUSANDS", "GBP000S": "GBP_THOUSANDS", "GBPM": "GBP_MILLIONS"}
    return aliases.get(value, value if value in UNIT_MULTIPLIERS else "UNKNOWN")


def to_pence(displayed_value: Any, unit: str, metric: str) -> int | None:
    if displayed_value is None or metric == "employees" or unit not in UNIT_MULTIPLIERS:
        return None
    token = str(displayed_value).strip()
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


def extraction_candidates(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page_item in extraction.get("pages") or []:
        page = page_item.get("page")
        if not isinstance(page, int):
            continue
        unit = normalise_unit(page_item.get("unit"))
        for index, row in enumerate(page_item.get("rows") or []):
            metric = row.get("metric")
            if metric not in METRICS:
                continue
            candidates.append({
                "id": f"p{page}-r{index}", "metric": metric, "page": page, "unit": unit,
                "source_label": row.get("source_label"), "current_display": row.get("current_display"),
                "previous_display": row.get("previous_display"), "current_column": row.get("current_column"),
                "previous_column": row.get("previous_column"), "evidence_text": row.get("evidence_text"),
                "confidence": row.get("confidence"),
            })
    return candidates


def selected_metrics(candidates: list[dict[str, Any]], rationalisation: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in candidates}
    metrics: list[dict[str, Any]] = []
    periods = rationalisation.get("financial_period_summaries") or {}
    for period_type, display_field in (("current", "current_display"), ("previous", "previous_display")):
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
            }
            metrics.append({
                "period_type": period_type,
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


def process_pdf_vlm_financials(
    pdf_path: Path,
    model_client: VlmModelClient,
    *,
    locator_model: str = DEFAULT_LOCATOR_MODEL,
    vision_model: str = DEFAULT_VISION_MODEL,
    rationalisation_model: str = DEFAULT_RATIONALISATION_MODEL,
    max_pages: int | None = 60,
    gbp_per_usd: float = 0.75,
    timeout: int = 180,
) -> dict[str, Any]:
    """Run statement discovery, extraction and text review through one model client."""
    started = time.perf_counter()
    render_started = time.perf_counter()
    thumbnails = render_pages(pdf_path, max_pages=max_pages, long_edge=384)
    thumbnail_render_seconds = time.perf_counter() - render_started
    locator_call = model_client.generate_json(locator_model, LOCATOR_PROMPT, thumbnails, timeout)
    locator = locator_call.payload
    selected = statement_pages(locator, len(thumbnails))
    render_started = time.perf_counter()
    detail_pages = render_pages(pdf_path, max_pages=max_pages, long_edge=1440)
    detail_render_seconds = time.perf_counter() - render_started
    detail_by_page = {item.page: item for item in detail_pages}
    extraction: dict[str, Any] = {"pages": []}
    extraction_call: ModelCallResult | None = None
    rationalisation: dict[str, Any] = {"financial_period_summaries": {}}
    rationalisation_call: ModelCallResult | None = None
    if selected:
        extraction_call = model_client.generate_json(
            vision_model,
            EXTRACTION_PROMPT,
            [detail_by_page[number] for number in selected],
            timeout,
        )
        extraction = extraction_call.payload
        candidates = extraction_candidates(extraction)
        if candidates:
            rationalisation_call = model_client.generate_json(
                rationalisation_model,
                f"{RATIONALISATION_PROMPT}\n\nCANDIDATES:\n{json.dumps({'candidates': candidates}, separators=(',', ':'))}",
                [],
                timeout,
            )
            rationalisation = rationalisation_call.payload
    else:
        candidates = []

    pricing_snapshot = model_client.pricing_snapshot()
    calls = {
        "locator": {
            "model": locator_model,
            "usage": locator_call.usage,
            "elapsed_seconds": round(locator_call.elapsed_seconds, 4),
            "image_payload_bytes": locator_call.image_payload_bytes,
            "model_reported_seconds": locator_call.model_reported_seconds,
        },
        "vision": {
            "model": vision_model,
            "usage": extraction_call.usage if extraction_call else {},
            "elapsed_seconds": round(extraction_call.elapsed_seconds, 4) if extraction_call else None,
            "image_payload_bytes": extraction_call.image_payload_bytes if extraction_call else 0,
            "model_reported_seconds": extraction_call.model_reported_seconds if extraction_call else None,
        },
        "rationalisation": {
            "model": rationalisation_model,
            "usage": rationalisation_call.usage if rationalisation_call else {},
            "elapsed_seconds": round(rationalisation_call.elapsed_seconds, 4)
            if rationalisation_call else None,
            "image_payload_bytes": 0,
            "model_reported_seconds": rationalisation_call.model_reported_seconds
            if rationalisation_call else None,
        },
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
    return {
        "pdf_path": str(pdf_path),
        "status": "complete" if selected else "no_statement_pages_found",
        "provider": model_client.provider_name,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "timing": {
            "thumbnail_render_seconds": round(thumbnail_render_seconds, 4),
            "detail_render_seconds": round(detail_render_seconds, 4),
            "image_payload_bytes": sum(item["image_payload_bytes"] for item in calls.values()),
        },
        "models": {"locator": locator_model, "vision": vision_model, "rationalisation": rationalisation_model},
        "pages_scanned": [item.page for item in thumbnails],
        "candidate_pages": selected,
        "raw_extraction": {"locator": locator, "detail": extraction, "candidates": candidates},
        "rationalisation": rationalisation,
        "metrics": selected_metrics(candidates, rationalisation),
        "usage": calls,
        "cost": {
            "usd": cost_usd,
            "gbp": round(cost_usd * gbp_per_usd, 8) if cost_usd is not None else None,
            "method": "+".join(sorted(methods)),
            "pricing": {"gbp_per_usd": gbp_per_usd, "models": {model: pricing_snapshot.get(model, {}) for model in {locator_model, vision_model, rationalisation_model}}},
        },
    }


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
    parser.add_argument("--rationalisation-model", default=DEFAULT_RATIONALISATION_MODEL)
    parser.add_argument("--gbp-per-usd", type=float, default=0.75)
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
        model_client.health_check({args.locator_model, args.vision_model, args.rationalisation_model})

    payload = process_pdf_vlm_financials(
        Path(args.pdf), model_client, locator_model=args.locator_model, vision_model=args.vision_model,
        rationalisation_model=args.rationalisation_model, max_pages=args.max_pages, gbp_per_usd=args.gbp_per_usd,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
