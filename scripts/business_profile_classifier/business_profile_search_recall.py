#!/usr/bin/env python3
"""Draft a human-reviewable search-opportunity snapshot from saved profiles.

This is intentionally downstream of the LLM.  It does not add a prompt field,
make a model call, or alter the historical ``is_search_addressable`` metric.
It turns the four existing profile fields into a broad proposal for a separate
commercial question: whether a material external line could be independently
discovered through search.

The JSON review file is the source of proposed labels and human decisions.
The CSVs are presentation artefacts that can be imported as native Google
Sheets.  Re-running the command preserves an existing proposal and any human
review rather than overwriting it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.business_profile_classifier.business_profile_eval import case_files, load_case
from scripts.business_profile_classifier.business_profile_metrics import (
    CATEGORY_FLOOR_DELIVERY_MODELS,
    SEARCH_ADDRESSABLE_VALUES,
    is_search_addressable,
    search_opportunity_from_profile,
)

DEFAULT_CASES_DIR = Path("evals/business_profile_gold_set/cases")
DEFAULT_REVIEW_FILE = Path("evals/business_profile_gold_set/search_opportunity_review.json")


def _value(block: Any) -> str | None:
    return block.get("value") if isinstance(block, dict) else None


def _profile(case: dict[str, Any], *, actual: dict[str, Any] | None = None) -> dict[str, str | None]:
    source = actual if actual is not None else (case.get("expected") or {})
    return {
        "demand_model": _value(source.get("demand_model")),
        "customer_type": _value(source.get("customer_type")),
        "delivery_model": _value(source.get("delivery_model")),
        "trading_status_confirmed": _value(source.get("trading_status_confirmed")),
    }


def _case_sha256(case: dict[str, Any]) -> str:
    frozen = {key: case.get(key) for key in ("company_number", "sections", "expected")}
    text = json.dumps(frozen, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence(case: dict[str, Any], value: str) -> dict[str, Any]:
    expected = case.get("expected") or {}
    if value == "yes":
        candidates = ("delivery_model", "customer_type", "demand_model", "trading_status_confirmed")
    elif value == "no":
        candidates = ("trading_status_confirmed", "demand_model", "delivery_model", "customer_type")
    else:
        candidates = ("customer_type", "delivery_model", "trading_status_confirmed", "demand_model")
    for field in candidates:
        block = expected.get(field) or {}
        if block.get("quote"):
            return {"field": field, "quote": block.get("quote"), "section": block.get("section")}
    return {"field": candidates[0], "quote": None, "section": None}


def _proposal(case: dict[str, Any]) -> dict[str, Any]:
    profile = _profile(case)
    value = search_opportunity_from_profile(
        profile["demand_model"], profile["delivery_model"], profile["customer_type"], profile["trading_status_confirmed"],
    )
    return {
        "value": value,
        "reason": (
            "Existing reviewed profile indicates an external operating line in a search-discoverable category."
            if value == "yes" else
            "Existing reviewed profile indicates captive, holding, or explicitly non-customer-facing activity."
            if value == "no" else
            "Existing reviewed profile does not establish enough about external customers, delivery, or trading status."
        ),
        "source_profile": profile,
        "evidence": _evidence(case, value),
    }


def load_or_draft_review(cases: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    """Create missing proposals once, preserving reviewers' existing work."""
    if path.is_file():
        review = json.loads(path.read_text(encoding="utf-8"))
    else:
        review = {
            "schema_version": 1,
            "definition": (
                "Yes: the filing establishes a core external product or service line that customers could "
                "independently discover and choose through search. No: captive supply, holding activity, or a "
                "fixed concession without independently accessible trade. Unclear: the filing does not establish "
                "external availability, customer choice, or materiality."
            ),
            "status": "draft_for_human_review",
            "cases": {},
        }
    entries = review.setdefault("cases", {})
    for case in cases:
        number = case["company_number"]
        if number not in entries:
            entries[number] = {
                "company_name": case.get("company_name"),
                "case_sha256": _case_sha256(case),
                "proposal": _proposal(case),
                "review": {"status": "pending", "value": None, "reviewer": None, "reviewed_at": None, "notes": None},
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return review


def _result_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(result.get("company_number")): result for result in report.get("results") or []}


def review_rows(cases: list[dict[str, Any]], review: dict[str, Any], report: dict[str, Any]) -> list[list[Any]]:
    header = [
        "company number", "company name", "SIC label", "proposed search opportunity", "proposal reason",
        "evidence field", "evidence quote", "evidence section", "gold demand", "gold customer", "gold delivery",
        "gold trading status", "V10 rule assessment", "V10 demand", "V10 customer", "V10 delivery", "V10 trading status",
        "review verdict (yes/no/unclear)", "reviewer", "reviewed at", "reviewer notes",
    ]
    results = _result_index(report)
    rows: list[list[Any]] = [header]
    for case in sorted(cases, key=lambda item: item["company_number"]):
        entry = review["cases"][case["company_number"]]
        proposal = entry["proposal"]
        evidence = proposal["evidence"]
        gold = proposal["source_profile"]
        result = results.get(case["company_number"], {})
        actual = {field: (result.get("fields", {}).get(field, {}) or {}).get("actual") for field in gold}
        actual_value = search_opportunity_from_profile(
            actual["demand_model"], actual["delivery_model"], actual["customer_type"], actual["trading_status_confirmed"],
        )
        human = entry.get("review") or {}
        rows.append([
            case["company_number"], case.get("company_name"), case.get("sic_label"), proposal["value"], proposal["reason"],
            evidence.get("field"), evidence.get("quote"), evidence.get("section"), gold["demand_model"], gold["customer_type"],
            gold["delivery_model"], gold["trading_status_confirmed"], actual_value, actual["demand_model"], actual["customer_type"],
            actual["delivery_model"], actual["trading_status_confirmed"], human.get("value"), human.get("reviewer"),
            human.get("reviewed_at"), human.get("notes"),
        ])
    return rows


def analysis_rows(cases: list[dict[str, Any]], review: dict[str, Any], report: dict[str, Any]) -> list[list[Any]]:
    """A compact diagnostic, including every lead introduced by a mixed fallback."""
    rows: list[list[Any]] = [["metric", "value", "notes"]]
    results = _result_index(report)
    proposed = Counter(entry["proposal"]["value"] for entry in review["cases"].values())
    predicted = Counter()
    agreement = 0
    for case in cases:
        entry = review["cases"][case["company_number"]]
        result = results.get(case["company_number"], {})
        actual = {field: (result.get("fields", {}).get(field, {}) or {}).get("actual") for field in _profile(case)}
        value = search_opportunity_from_profile(
            actual["demand_model"], actual["delivery_model"], actual["customer_type"], actual["trading_status_confirmed"],
        )
        predicted[value] += 1
        agreement += value == entry["proposal"]["value"]
    rows.extend([
        ["review snapshot status", review.get("status"), "Draft proposals only; human verdicts are the future evaluation labels."],
        ["proposed yes / no / unclear", f"{proposed['yes']} / {proposed['no']} / {proposed['unclear']}", "Derived from reviewed existing fields."],
        ["V10 assessment yes / no / unclear", f"{predicted['yes']} / {predicted['no']} / {predicted['unclear']}", "Derived from saved V10 responses; no model calls."],
        ["V10 agreement with draft proposals", f"{agreement}/{len(cases)}", "A regression diagnostic, not an accuracy claim until review."],
        ["historical search-addressable metric", json.dumps((report.get("metrics") or {}).get("search_addressable") or {}), "Unchanged historical measure."],
        ["", "", ""],
        ["admitted by widening the fallback to mixed (2026-09-28)", "", "Only demand=unclear, qualifying delivery, and mixed customer."],
        ["company number", "company name", "gold historical positive? | V10 demand/customer/delivery | proposed review label"],
    ])
    for case in sorted(cases, key=lambda item: item["company_number"]):
        result = results.get(case["company_number"], {})
        fields = result.get("fields") or {}
        demand = (fields.get("demand_model") or {}).get("actual")
        delivery = (fields.get("delivery_model") or {}).get("actual")
        customer = (fields.get("customer_type") or {}).get("actual")
        # The floor accepted b2c only until 2026-09-28; this block lists what
        # the widening to mixed admitted, so it keeps the old rule inline.
        widened = is_search_addressable(demand, delivery, customer)
        old = demand in SEARCH_ADDRESSABLE_VALUES or (
            demand in (None, "unclear") and delivery in CATEGORY_FLOOR_DELIVERY_MODELS and customer == "b2c"
        )
        if widened and not old:
            gold = _profile(case)
            gold_historical = is_search_addressable(gold["demand_model"], gold["delivery_model"], gold["customer_type"])
            rows.append([
                case["company_number"], case.get("company_name"),
                f"{gold_historical} | {demand}/{customer}/{delivery} | {review['cases'][case['company_number']]['proposal']['value']}",
            ])
    return rows


def write_csv(rows: list[list[Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="Saved V10 report JSON; no model call is made.")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--review-file", type=Path, default=DEFAULT_REVIEW_FILE)
    parser.add_argument("--out-dir", type=Path, default=Path("logs/business-profile-eval/search-recall-review"))
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    cases = []
    for path in case_files(args.cases_dir):
        case = load_case(path)
        if case.get("review", {}).get("status") == "verified":
            cases.append(case)
    review = load_or_draft_review(cases, args.review_file)
    review_path = args.out_dir / "search-opportunity-review.csv"
    analysis_path = args.out_dir / "search-opportunity-analysis.csv"
    write_csv(review_rows(cases, review, report), review_path)
    write_csv(analysis_rows(cases, review, report), analysis_path)
    print(f"Review snapshot: {args.review_file}")
    print(f"Review sheet CSV: {review_path}")
    print(f"Analysis sheet CSV: {analysis_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
