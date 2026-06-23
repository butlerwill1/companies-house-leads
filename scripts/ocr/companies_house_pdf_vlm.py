#!/usr/bin/env python3
"""VLM-based PDF extraction using Gemini 2.5 Flash via OpenRouter.

Drop-in alternative to companies_house_pdf_full.py. Instead of local OCR + regex,
this sends rendered PDF page images to a vision LLM which returns structured JSON
covering both narrative sections and financial figures in a single API call.

Usage (standalone):
    python -m scripts.ocr.companies_house_pdf_vlm --pdf path/to/filing.pdf --output-json out.json

Callable as a library:
    from scripts.ocr.companies_house_pdf_vlm import process_pdf_vlm
    payload = process_pdf_vlm(pdf_path, api_key=os.getenv("OPENROUTER_API_KEY"))
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"

EXTRACTION_PROMPT = """\
You are reading pages from a UK Companies House annual accounts filing (PDF).
Extract the information below and return ONLY a single valid JSON object — no markdown, no explanation.

JSON schema to return:
{
  "sections": {
    "strategic_report":      {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "directors_report":      {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "principal_activity":    {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "business_review":       {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "results_and_dividends": {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "going_concern":         {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "future_developments":   {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "principal_risks":       {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"},
    "post_balance_sheet":    {"heading": "<heading text or null>", "page": <int or null>, "text": "<full section text or null>"}
  },
  "performance_statements": [
    {"page": <int>, "text": "<sentence mentioning revenue / profit / growth / customers / pipeline>"}
  ],
  "ocr_financials": {
    "by_period": {
      "current": {
        "turnover":                  <integer pence or null>,
        "cost_of_sales":             <integer pence or null>,
        "gross_profit":              <integer pence or null>,
        "administrative_expenses":   <integer pence or null>,
        "operating_result":          <integer pence or null>,
        "profit_before_tax":         <integer pence or null>,
        "tax":                       <integer pence or null>,
        "profit_after_tax":          <integer pence or null>,
        "current_assets":            <integer pence or null>,
        "cash":                      <integer pence or null>,
        "net_current_assets":        <integer pence or null>,
        "net_assets":                <integer pence or null>,
        "employees":                 <integer count or null>
      },
      "previous": {
        "turnover":                  <integer pence or null>,
        "cost_of_sales":             <integer pence or null>,
        "gross_profit":              <integer pence or null>,
        "administrative_expenses":   <integer pence or null>,
        "operating_result":          <integer pence or null>,
        "profit_before_tax":         <integer pence or null>,
        "tax":                       <integer pence or null>,
        "profit_after_tax":          <integer pence or null>,
        "current_assets":            <integer pence or null>,
        "cash":                      <integer pence or null>,
        "net_current_assets":        <integer pence or null>,
        "net_assets":                <integer pence or null>,
        "employees":                 <integer count or null>
      }
    }
  },
  "business_description": "<1-3 sentences describing what this company does>"
}

Monetary rules:
- All monetary values must be in whole pence (£1 = 100 pence).
- If figures are stated in £000s, multiply by 100,000 to get pence.
- If figures are stated in £millions, multiply by 100,000,000.
- Losses and expenses should be negative integers.
- If a figure is not present in these pages, use null — do NOT guess or invent values.

Sections rules:
- Only populate a section if that heading clearly appears in the document.
- Set omitted sections to null for all fields.
- page number refers to the document page where the section heading appears (1-indexed from the pages provided).
"""


def render_pages_b64(pdf_path: Path, max_pages: int = 30, dpi: int = 144) -> list[str]:
    """Render PDF pages to base64-encoded JPEG strings via PyMuPDF."""
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required — install with: pip install pymupdf")
    doc = fitz.open(str(pdf_path))
    pages: list[str] = []
    for i in range(min(len(doc), max_pages)):
        page = doc[i]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pages.append(base64.b64encode(pix.tobytes("jpeg")).decode())
    doc.close()
    return pages


def _extract_json(text: str) -> dict[str, Any]:
    """Parse JSON from model response, stripping markdown fences if present."""
    text = text.strip()
    # Strip ```json ... ``` fences
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def call_vlm(
    pages_b64: list[str],
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 180,
) -> dict[str, Any]:
    """Send page images to the VLM via OpenRouter and return parsed JSON."""
    content: list[dict[str, Any]] = [{"type": "text", "text": EXTRACTION_PROMPT}]
    for b64 in pages_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    resp = requests.post(
        OPENROUTER_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
        },
        timeout=timeout,
    )
    resp.raise_for_status()

    raw_content = resp.json()["choices"][0]["message"]["content"]
    usage = resp.json().get("usage", {})
    result = _extract_json(raw_content)
    result["_usage"] = usage
    return result


def process_pdf_vlm(
    pdf_path: Path,
    api_key: str,
    max_pages: int = 30,
    dpi: int = 144,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Extract narrative and financial data via VLM. Drop-in for process_pdf().

    Returns a payload dict compatible with insert_narrative_payload() in
    companies_house_sqlite.py.
    """
    pages_b64 = render_pages_b64(pdf_path, max_pages=max_pages, dpi=dpi)
    vlm = call_vlm(pages_b64, api_key=api_key, model=model)

    # Normalise sections: remove keys where all fields are null
    sections: dict[str, Any] = {}
    for key, val in (vlm.get("sections") or {}).items():
        if val and any(v is not None for v in val.values()):
            sections[key] = val

    # Ensure by_period always has both keys (even if empty)
    by_period = vlm.get("ocr_financials", {}).get("by_period", {})
    by_period.setdefault("current", {})
    by_period.setdefault("previous", {})

    return {
        "pdf_path": str(pdf_path),
        "text_source": "vlm",
        "ocr_requested": False,
        "ocr_engine_requested": None,
        "ocr_used": False,
        "ocr_engine_used": None,
        "max_pages": max_pages,
        "render_dpi": dpi,
        "vlm_model": model,
        "tesseract_available": False,
        "rapidocr_available": False,
        "pymupdf_available": fitz is not None,
        "text_quality": {
            "vlm": True,
            "pages_sent": len(pages_b64),
            "usage": vlm.get("_usage", {}),
        },
        "sections": sections,
        "performance_statements": vlm.get("performance_statements") or [],
        "ocr_financials": {
            "metrics_found": sum(
                1 for v in by_period.get("current", {}).values() if v is not None
            ),
            "pages_with_financials": [],
            "metrics": [],
            "by_period": by_period,
        },
        "business_description": vlm.get("business_description"),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="VLM-based PDF extraction via OpenRouter.")
    parser.add_argument("--pdf", required=True, help="Path to the PDF filing.")
    parser.add_argument("--output-json", required=True, help="Path to write extracted JSON.")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model identifier.")
    args = parser.parse_args(argv)

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set in environment or .env", file=sys.stderr)
        return 1

    payload = process_pdf_vlm(
        pdf_path=Path(args.pdf),
        api_key=api_key,
        max_pages=args.max_pages,
        model=args.model,
    )

    Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    pages_sent = payload["text_quality"]["pages_sent"]
    usage = payload["text_quality"].get("usage", {})
    print(json.dumps({
        "output_json": args.output_json,
        "text_source": "vlm",
        "model": args.model,
        "pages_sent": pages_sent,
        "usage": usage,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
