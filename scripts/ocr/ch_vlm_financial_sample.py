#!/usr/bin/env python3
"""Benchmark the VLM financial pipeline over a small local PDF sample."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from core.companies_house_extractor import load_dotenv
from core.companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.ocr.companies_house_pdf_vlm_financials import (
    DEFAULT_LOCATOR_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_RATIONALISATION_MODEL,
    DEFAULT_VISION_MODEL,
    OllamaVlmModelClient,
    OpenRouterVlmModelClient,
    VlmModelClient,
    process_pdf_vlm_financials,
)


def identifiers_from_filename(pdf_path: Path) -> tuple[str | None, str | None]:
    match = re.match(r"(?P<company>\d{8})-(?P<document>[^.]+)\.pdf$", pdf_path.name)
    return (match.group("company"), match.group("document")) if match else (None, None)


def comparison_sample(db_path: Path, sample_size: int) -> list[tuple[Path, dict[str, Any]]]:
    """Pick existing OCR runs with turnover/revenue or a profit value to compare."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """
            select nr.pdf_path, nr.company_number, nr.document_id,
                   ofs.turnover, ofs.operating_result, ofs.profit_before_tax,
                   ofs.profit_after_tax
            from narrative_runs nr
            join ocr_financial_period_summaries ofs on ofs.narrative_run_id = nr.id
            where ofs.period_type = 'current'
              and (
                  ofs.turnover is not null
                  or ofs.operating_result is not null
                  or ofs.profit_before_tax is not null
                  or ofs.profit_after_tax is not null
              )
            order by nr.id
            """
        ).fetchall()
    finally:
        conn.close()
    sample: list[tuple[Path, dict[str, Any]]] = []
    for pdf_path, company_number, document_id, turnover, operating_result, profit_before_tax, profit_after_tax in rows:
        path = Path(pdf_path)
        if not path.exists():
            continue
        sample.append((path, {
            "company_number": company_number, "document_id": document_id,
            "turnover": turnover, "operating_result": operating_result,
            "profit_before_tax": profit_before_tax, "profit_after_tax": profit_after_tax,
        }))
        if len(sample) == sample_size:
            break
    return sample


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Benchmark VLM financial extraction; no local OCR.")
    parser.add_argument("--db", help="Optional SQLite database to receive VLM rows. Omit when it is in use.")
    parser.add_argument("--pdf-dir", default="ocr-noxhtml-pdfs")
    parser.add_argument(
        "--comparison-db",
        help="Read-only database used to select filings with existing turnover/revenue or profit OCR values.",
    )
    parser.add_argument("--output-dir", default="logs/vlm-financial-sample")
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=60)
    parser.add_argument("--locator-model", default=DEFAULT_LOCATOR_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--rationalisation-model", default=DEFAULT_RATIONALISATION_MODEL)
    parser.add_argument("--gbp-per-usd", type=float, default=0.75)
    parser.add_argument("--provider", choices=("openrouter", "ollama"), default="openrouter")
    parser.add_argument("--ollama-base-url", default=os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL))
    args = parser.parse_args(argv)
    load_dotenv(Path.cwd() / ".env")
    if args.provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            parser.error("OPENROUTER_API_KEY must be set in the environment or .env")
        model_client: VlmModelClient = OpenRouterVlmModelClient(api_key)
    else:
        model_client = OllamaVlmModelClient(args.ollama_base_url)

    selected = comparison_sample(Path(args.comparison_db), args.sample_size) if args.comparison_db else [
        (path, {}) for path in sorted(Path(args.pdf_dir).glob("*.pdf"))[:args.sample_size]
    ]
    if not selected:
        parser.error(f"No PDFs found in {args.pdf_dir}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db) if args.db else None
    if conn is not None:
        init_db(conn)
    summaries: list[dict[str, Any]] = []
    try:
        for index, (pdf_path, existing_ocr) in enumerate(selected, start=1):
            company_number, document_id = identifiers_from_filename(pdf_path)
            company_number = existing_ocr.get("company_number") or company_number
            document_id = existing_ocr.get("document_id") or document_id
            print(f"[{index}/{len(selected)}] {pdf_path.name}", file=sys.stderr)
            try:
                payload = process_pdf_vlm_financials(
                    pdf_path, model_client, locator_model=args.locator_model, vision_model=args.vision_model,
                    rationalisation_model=args.rationalisation_model, max_pages=args.max_pages,
                    gbp_per_usd=args.gbp_per_usd,
                )
                run_id = insert_vlm_financial_payload(conn, payload, company_number, document_id) if conn is not None else None
                output_path = output_dir / f"{pdf_path.stem}-vlm-financials.json"
                output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                summary = {
                    "pdf": pdf_path.name, "database_run_id": run_id, "status": payload["status"],
                    "candidate_pages": payload["candidate_pages"], "metrics": len(payload["metrics"]),
                    "cost_gbp": payload["cost"]["gbp"], "cost_method": payload["cost"]["method"],
                    "provider": payload["provider"], "elapsed_seconds": payload["elapsed_seconds"],
                    "existing_ocr": existing_ocr,
                }
            except Exception as exc:
                if conn is not None:
                    conn.rollback()
                summary = {"pdf": pdf_path.name, "status": "error", "error": str(exc)}
            summaries.append(summary)
            print(json.dumps(summary), file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()

    known_costs = [item["cost_gbp"] for item in summaries if item.get("cost_gbp") is not None]
    report = {
        "files": len(summaries), "complete": sum(item.get("status") == "complete" for item in summaries),
        "errors": sum(item.get("status") == "error" for item in summaries),
        "total_cost_gbp": round(sum(known_costs), 8), "cost_known_for": len(known_costs),
        "provider": args.provider,
        "models": {"locator": args.locator_model, "vision": args.vision_model, "rationalisation": args.rationalisation_model},
        "runs": summaries,
    }
    (output_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
