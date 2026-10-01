#!/usr/bin/env python3
"""Build the search-screen gold set (evals/search_screen/cases/).

The set is separate from the business-profile gold set because it answers a
different question (docs/SEARCH_SCREEN.md). It has two cohorts:

- ``random``: a seeded draw from the target population -- enriched companies
  with turnover and profit figures and a principal_activity section, not in
  the business-profile gold set. The first ``BLIND_COUNT`` of the draw are
  flagged ``blind``: the reviewer labels them before seeing any draft.
- ``hard``: boundary cases copied from the business-profile gold set,
  reported separately as a stress test.

Nothing here calls a model or an external service.

Usage:
    python -m scripts.screen.search_screen_cases build --db companies-house.db
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_extractor import filed_report_text  # noqa: E402
from scripts.profile.business_profile_eval import case_files, load_case, save_case  # noqa: E402
from scripts.profile.companies_house_business_profile import fetch_narrative_context  # noqa: E402
from scripts.screen.search_screen_pack import build_pack, principal_activity_text  # noqa: E402

CASES_DIR = Path("evals/search_screen/cases")
SELECTION_PATH = Path("evals/search_screen/selection.json")
GOLD_CASES_DIR = Path("evals/business_profiles/cases")
RAW_DIR = Path("data/raw/business-profile-xhtml")

CASE_SCHEMA_VERSION = 1
SEED = 20260929
RANDOM_COUNT = 150
RESERVE_COUNT = 60
BLIND_COUNT = 25
HARD_PER_CATEGORY = 6

SCREEN_LABELS = ("likely", "possible", "unlikely")

_HOLDING_NAME = re.compile(r"\b(HOLDINGS?|GROUP|TOPCO|TOP CO)\b", re.I)


def target_population(conn: sqlite3.Connection, exclude: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Enriched companies with turnover and profit figures and a non-empty
    principal_activity section in their latest narrative run."""
    # Joined once, not a correlated exists: narrative_sections has no index on
    # narrative_run_id, so a per-company subquery rescans it thousands of times.
    rows = conn.execute(
        """
        with latest_run as (
            select company_number, max(id) as run_id from narrative_runs group by company_number
        ),
        with_activity as (
            select distinct lr.company_number
            from latest_run lr
            join narrative_sections ns on ns.narrative_run_id = lr.run_id
            where ns.section_key = 'principal_activity'
              and length(trim(coalesce(ns.section_text, ''))) > 0
        ),
        with_financials as (
            select distinct company_number from financial_period_summaries
            where turnover is not null and profit_after_tax is not null
        )
        select a.company_number from with_activity a
        join with_financials f on f.company_number = a.company_number
        order by a.company_number
        """
    ).fetchall()
    return [number for (number,) in rows if number not in exclude]


def draw_random(population: list[str], count: int, seed: int) -> list[str]:
    """Order-independent: the draw depends on the sorted population and the seed."""
    rng = random.Random(seed)
    return rng.sample(sorted(population), min(count, len(population)))


def blind_subset(drawn: list[str], count: int) -> list[str]:
    return drawn[:count]


def _expected_value(gold_case: dict[str, Any], field: str) -> str | None:
    return ((gold_case.get("expected") or {}).get(field) or {}).get("value")


def hard_category(gold_case: dict[str, Any]) -> str | None:
    """Which boundary a business-profile gold case stresses, from its gold labels."""
    trading = _expected_value(gold_case, "trading_status_confirmed")
    demand = _expected_value(gold_case, "demand_model")
    customer = _expected_value(gold_case, "customer_type")
    delivery = _expected_value(gold_case, "delivery_model")
    text = " ".join((gold_case.get("sections") or {}).values())
    if trading in ("spv", "investment_holding"):
        return "captive"
    if trading == "trading" and _HOLDING_NAME.search(gold_case.get("company_name") or ""):
        return "holding_parent"
    if customer in ("b2c", "mixed") and re.search(r"\bNHS\b", text):
        return "nhs_funded"
    if customer == "mixed" and (demand == "consumer_search" or delivery == "product_physical"):
        return "multichannel_retail"
    if customer == "public_sector" or (demand == "relationship_or_contract" and delivery == "contracting"):
        return "tender_only"
    return None


def pick_hard_cases(gold_cases: list[dict[str, Any]], per_category: int, seed: int) -> list[tuple[str, str]]:
    by_category: dict[str, list[str]] = {}
    for gold_case in sorted(gold_cases, key=lambda item: item["company_number"]):
        category = hard_category(gold_case)
        if category:
            by_category.setdefault(category, []).append(gold_case["company_number"])
    rng = random.Random(seed)
    picked: list[tuple[str, str]] = []
    for category in sorted(by_category):
        numbers = by_category[category]
        rng.shuffle(numbers)
        picked.extend((number, category) for number in numbers[:per_category])
    return picked


def _financials(conn: sqlite3.Connection, company_number: str) -> dict[str, Any]:
    row = conn.execute(
        """
        select financial_year, turnover, profit_after_tax, employees, currency_code
        from financial_period_summaries
        where company_number = ? and period_type = 'current'
        order by financial_year desc, id desc limit 1
        """,
        (company_number,),
    ).fetchone()
    keys = ("financial_year", "turnover", "profit_after_tax", "employees", "currency_code")
    return dict(zip(keys, row)) if row else dict.fromkeys(keys)


def _empty_label() -> dict[str, Any]:
    return {"value": None, "quote": None, "section": None, "reason": None}


def build_case(
    conn: sqlite3.Connection,
    company_number: str,
    *,
    cohort: str,
    blind: bool = False,
    hard_category_name: str | None = None,
) -> dict[str, Any] | None:
    context = fetch_narrative_context(conn, company_number)
    if context is None or not context["sections"]:
        return None
    case: dict[str, Any] = {
        "schema_version": CASE_SCHEMA_VERSION,
        "company_number": company_number,
        "company_name": context["company_name"],
        "financial_year": context["financial_year"],
        "sic_code": context["sic_code"],
        "sic_label": context["sic_label"],
        "narrative_run_id": context["narrative_run_id"],
        "sections": context["sections"],
        "financials": _financials(conn, company_number),
        "cohort": cohort,
        "blind": blind,
        "expected": {"search_screen": _empty_label()},
        "draft": {"search_screen": _empty_label(), "drafted_by": None, "drafted_at": None},
        "review": {"status": "unreviewed", "reviewer": None, "reviewed_at": None, "changed_from": None},
    }
    if hard_category_name:
        case["hard_category"] = hard_category_name
    return case


def write_cases(
    conn: sqlite3.Connection,
    plan: list[tuple[str, str, bool, str | None]],
    cases_dir: Path,
) -> int:
    """Write one case file per (company_number, cohort, blind, hard_category).
    An existing file is never overwritten: it may hold a draft or a review."""
    created = 0
    for company_number, cohort, blind, hard_category_name in plan:
        path = cases_dir / f"{company_number}.json"
        if path.exists():
            continue
        case = build_case(conn, company_number, cohort=cohort, blind=blind, hard_category_name=hard_category_name)
        if case is None:
            print(f"  skipped {company_number}: no narrative sections", file=sys.stderr)
            continue
        save_case(path, case)
        created += 1
    return created


def refresh_sections(cases_dir: Path, raw_dir: Path) -> dict[str, list[str]]:
    """Rebuild each case's ``sections`` as one ``filed_report`` (and its
    ``label_pack``, the compact extract drafter and reviewer both read) from its archived
    raw XHTML: the whole filed document minus the auditor's report, the same
    text the business-profile gold set is judged on. The database's stored
    narrative sections are not used: 71 of the first 176 cases had auditor
    boilerplate under ``strategic_report`` and 41 had iXBRL junk as their
    ``principal_activity`` (docs/SEARCH_SCREEN.md).

    Only ``sections`` changes. A case that already carries a draft or a review
    is left alone and reported, because new text could invalidate its quote."""
    result: dict[str, list[str]] = {"refreshed": [], "missing_raw": [], "has_work": [], "empty_text": []}
    for path in case_files(cases_dir):
        case = load_case(path)
        number = case["company_number"]
        draft_value = ((case.get("draft") or {}).get("search_screen") or {}).get("value")
        if draft_value or (case.get("review") or {}).get("status") != "unreviewed":
            result["has_work"].append(number)
            continue
        raw_path = raw_dir / f"{number}.xhtml"
        if not raw_path.exists():
            result["missing_raw"].append(number)
            continue
        xhtml = raw_path.read_text(encoding="utf-8")
        text = filed_report_text(xhtml).strip()
        if not text:
            result["empty_text"].append(number)
            continue
        case["sections"] = {"filed_report": text}
        case["label_pack"] = build_pack(text, principal_activity_text(xhtml))
        case["text_source"] = "raw_xhtml_filed_report"
        save_case(path, case)
        result["refreshed"].append(number)
    return result


def replace_cases(
    conn: sqlite3.Connection,
    failed: list[str],
    cases_dir: Path,
    selection_path: Path,
) -> list[tuple[str, str]]:
    """Swap companies with no usable XHTML filing for the next companies in the
    seeded reserve list. A replacement inherits the ``blind`` flag of the case
    it replaces, so the blind subset stays at its planned size. Returns
    (removed, added) pairs. Only unreviewed, undrafted cases are removed."""
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    reserve = [n for n in selection.get("reserve", []) if n not in selection.get("used_reserve", [])]
    pairs: list[tuple[str, str]] = []
    for number in failed:
        path = cases_dir / f"{number}.json"
        case = load_case(path)
        draft_value = ((case.get("draft") or {}).get("search_screen") or {}).get("value")
        if draft_value or (case.get("review") or {}).get("status") != "unreviewed" or case.get("cohort") != "random":
            continue
        blind = bool(case.get("blind"))
        while reserve:
            candidate = reserve.pop(0)
            selection.setdefault("used_reserve", []).append(candidate)
            replacement = build_case(conn, candidate, cohort="random", blind=blind)
            if replacement is None:
                continue
            path.unlink()
            save_case(cases_dir / f"{candidate}.json", replacement)
            selection["random"] = [candidate if n == number else n for n in selection["random"]]
            if blind:
                selection["blind"] = sorted([candidate if n == number else n for n in selection["blind"]])
            selection.setdefault("replaced", []).append({"removed": number, "added": candidate})
            pairs.append((number, candidate))
            break
    selection_path.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    return pairs


def build(db_path: Path, cases_dir: Path, selection_path: Path, gold_dir: Path, seed: int) -> dict[str, Any]:
    gold_cases = [load_case(path) for path in case_files(gold_dir)]
    gold_numbers = {case["company_number"] for case in gold_cases}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if selection_path.exists():
        # The selection is a record (replacements and reserve use are logged in
        # it), so it is never redrawn. A selection written before the reserve
        # list existed only gains that list.
        try:
            existing = json.loads(selection_path.read_text(encoding="utf-8"))
            if "reserve" not in existing:
                population = target_population(conn, exclude=gold_numbers)
                remaining = [n for n in population if n not in set(existing["random"])]
                existing["reserve"] = draw_random(remaining, RESERVE_COUNT, seed + 1)
                existing["used_reserve"] = []
                selection_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            return {"kept_existing_selection": True, "random": len(existing["random"]),
                    "reserve": len(existing["reserve"]), "hard": len(existing["hard"])}
        finally:
            conn.close()
    try:
        population = target_population(conn, exclude=gold_numbers)
        drawn = draw_random(population, RANDOM_COUNT, seed)
        remaining = [n for n in population if n not in set(drawn)]
        reserve = draw_random(remaining, RESERVE_COUNT, seed + 1)
        blind = set(blind_subset(drawn, BLIND_COUNT))
        hard = pick_hard_cases(gold_cases, HARD_PER_CATEGORY, seed)
        plan: list[tuple[str, str, bool, str | None]] = [(n, "random", n in blind, None) for n in drawn]
        plan += [(n, "hard", False, category) for n, category in hard]
        created = write_cases(conn, plan, cases_dir)
    finally:
        conn.close()
    selection = {
        "seed": seed,
        "population_size": len(population),
        "random": drawn,
        "reserve": reserve,
        "used_reserve": [],
        "blind": sorted(blind),
        "hard": [{"company_number": n, "category": category} for n, category in hard],
    }
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    return {"created": created, "population": len(population), "random": len(drawn), "hard": len(hard)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="Draw the cohorts and write unlabelled case files.")
    build_parser.add_argument("--db", default="companies-house.db")
    build_parser.add_argument("--cases-dir", default=str(CASES_DIR))
    build_parser.add_argument("--selection", default=str(SELECTION_PATH))
    build_parser.add_argument("--gold-dir", default=str(GOLD_CASES_DIR))
    build_parser.add_argument("--seed", type=int, default=SEED)
    refresh_parser = commands.add_parser(
        "refresh-sections", help="Rebuild sections and label_pack from the archived raw filings.")
    refresh_parser.add_argument("--cases-dir", default=str(CASES_DIR))
    refresh_parser.add_argument("--raw-dir", default=str(RAW_DIR))
    replace_parser = commands.add_parser(
        "replace", help="Swap companies with no usable XHTML filing for the seeded reserve.")
    replace_parser.add_argument("--db", default="companies-house.db")
    replace_parser.add_argument("--cases-dir", default=str(CASES_DIR))
    replace_parser.add_argument("--selection", default=str(SELECTION_PATH))
    replace_parser.add_argument("company_numbers", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "build":
        summary = build(Path(args.db), Path(args.cases_dir), Path(args.selection), Path(args.gold_dir), args.seed)
        print(json.dumps(summary))
    elif args.command == "refresh-sections":
        result = refresh_sections(Path(args.cases_dir), Path(args.raw_dir))
        print(json.dumps({key: len(value) for key, value in result.items()}))
        for key in ("missing_raw", "empty_text", "has_work"):
            if result[key]:
                print(f"  {key}: {' '.join(result[key])}")
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        try:
            pairs = replace_cases(conn, args.company_numbers, Path(args.cases_dir), Path(args.selection))
        finally:
            conn.close()
        print(json.dumps([{"removed": a, "added": b} for a, b in pairs]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
