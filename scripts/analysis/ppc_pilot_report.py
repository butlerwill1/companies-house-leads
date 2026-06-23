#!/usr/bin/env python3
"""
Build a compact PPC pilot CSV from browser evidence.

This report builder summarizes browser evidence into human-reviewable columns:
selected domain, inferred business model, short business description, and PPC
fit signals. It is intended to run after `scripts/browser/ppc_browser_pilot.mjs`
has collected search and website evidence.

Usage:
    python -m scripts.analysis.ppc_pilot_report --input evidence.json --output data/ppc-pilot-report.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


KEYWORD_MODELS: list[tuple[str, list[str]]] = [
    ("E-commerce retail", ["shop", "checkout", "basket", "cart", "product", "collection"]),
    ("Car dealership", ["cars", "vehicles", "dealer", "motorhome", "used cars", "new cars"]),
    ("Construction and trades", ["construction", "electrical", "maintenance", "installation", "refurbishment"]),
    ("Property and real estate", ["property", "real estate", "letting", "estate agent", "accommodation"]),
    ("Finance and lending", ["finance", "loan", "mortgage", "credit", "bridging"]),
    ("Healthcare and dental", ["dental", "clinic", "medical", "healthcare", "practice"]),
    ("Hospitality and catering", ["hotel", "catering", "restaurant", "food service", "event catering"]),
    ("Business services", ["services", "solutions", "support", "provider", "consultancy"]),
]


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def trim_words(value: str, max_words: int = 100) -> str:
    words = normalize_whitespace(value).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(" ,;:.") + "..."


def choose_description(entry: dict) -> str:
    website = entry.get("website") or {}
    chosen_result = entry.get("chosen_result") or {}
    company_name = entry.get("company_name", "").title()
    title = normalize_whitespace(website.get("title", ""))
    meta = normalize_whitespace(website.get("meta_description", ""))
    og = normalize_whitespace(website.get("og_description", ""))
    h1s = [normalize_whitespace(v) for v in website.get("h1s", []) if normalize_whitespace(v)]
    body = normalize_whitespace(website.get("body_sample", ""))
    result_title = normalize_whitespace(chosen_result.get("title", ""))
    result_snippet = normalize_whitespace(chosen_result.get("snippet", ""))
    sic_label = normalize_whitespace(entry.get("sic_label", ""))
    sic_1 = normalize_whitespace(entry.get("sic_1", ""))

    for candidate in (meta, og):
        if len(candidate.split()) >= 8:
            return trim_words(candidate, 45)

    if h1s and body:
        return trim_words(f"{company_name} appears to be a {h1s[0].lower()}. {body}", 45)

    if title and body:
        return trim_words(f"{company_name} appears to trade via {title}. {body}", 45)

    if title:
        return trim_words(f"{company_name} appears to operate as {title}.", 30)

    if result_snippet:
        return trim_words(result_snippet, 35)

    if result_title:
        return trim_words(f"{company_name} appears to operate as {result_title}.", 30)

    if sic_label or sic_1:
        return trim_words(f"{company_name} is likely a {sic_label.lower()} business under SIC {sic_1}.", 25)

    return ""


def infer_business_model(entry: dict) -> str:
    website = entry.get("website") or {}
    text = " ".join(
        [
            website.get("title", ""),
            website.get("meta_description", ""),
            website.get("og_description", ""),
            website.get("body_sample", ""),
            " ".join(website.get("nav_links", [])[:15]),
            " ".join(website.get("ctas", [])[:10]),
            (entry.get("chosen_result") or {}).get("title", ""),
            (entry.get("chosen_result") or {}).get("snippet", ""),
            entry.get("sic_1", ""),
            entry.get("sic_label", ""),
        ]
    ).lower()
    best_label = ""
    best_score = 0
    for label, keywords in KEYWORD_MODELS:
        score = sum(1 for keyword in keywords if keyword in text)
        if score > best_score:
            best_label = label
            best_score = score
    return best_label or entry.get("sic_label", "")


def signal_summary(entry: dict) -> str:
    website = entry.get("website") or {}
    signals: list[str] = []
    if website.get("has_checkout"):
        signals.append("checkout")
    if website.get("has_store_locator"):
        signals.append("store_locator")
    if website.get("has_quote_form"):
        signals.append("quote")
    if website.get("has_booking"):
        signals.append("booking")
    if website.get("has_demo"):
        signals.append("demo")
    if website.get("has_finance"):
        signals.append("finance")
    return ",".join(signals)


def build_rows(entries: list[dict]) -> list[dict[str, str | float]]:
    rows: list[dict[str, str | float]] = []
    for entry in entries:
        website = entry.get("website") or {}
        chosen_result = entry.get("chosen_result") or {}
        rows.append(
            {
                "company_number": entry.get("company_number", ""),
                "company_name": entry.get("company_name", ""),
                "sic_1": entry.get("sic_1", ""),
                "sic_label": entry.get("sic_label", ""),
                "account_category": entry.get("account_category", ""),
                "turnover": entry.get("turnover", ""),
                "estimated_monthly_ppc_spend": round(float(entry.get("estimated_monthly_ppc_spend", 0.0)), 2),
                "status": entry.get("status", ""),
                "website_url": website.get("final_url", "") or chosen_result.get("target_url", ""),
                "business_model": infer_business_model(entry),
                "website_signals": signal_summary(entry),
                "business_description": choose_description(entry),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a pilot CSV from browser evidence JSON.")
    parser.add_argument("--input", required=True, help="Browser evidence JSON path.")
    parser.add_argument("--output", required=True, help="CSV output path.")
    args = parser.parse_args()

    entries = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = build_rows(entries)

    if not rows:
        raise SystemExit("No rows to write.")

    with Path(args.output).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
