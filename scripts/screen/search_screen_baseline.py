#!/usr/bin/env python3
"""The free baseline the search screen has to beat (docs/SEARCH_SCREEN.md).

A rule over data already in SQLite: Gate A's ``trading_status`` and
``name_suggests_holding`` signals plus the primary SIC group. No model call.
If a prompt cannot beat this on recall or on removal by a clear margin, the
baseline ships instead.

The rule was written from domain reasoning before any label was scored, and is
not tuned to the gold set: a company that is not trading, or whose name says
it is a holding vehicle, is rejected; so are the two SIC groups whose customers
almost never arrive through search (property developers, who sell through
agents and portals, and lenders). Everything else passes.

Usage:
    python -m scripts.screen.search_screen_baseline --db companies-house.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.business_profile_classifier.business_profile_eval import case_files, load_case  # noqa: E402
from scripts.screen.search_screen_cases import CASES_DIR, target_population  # noqa: E402

REJECT_TRADING_STATUS = frozenset({"dormant", "holding", "non_trading"})
REJECT_SIC_GROUPS = frozenset({"Property development", "Banking / lending / credit"})
PASSING_LABELS = frozenset({"likely", "possible"})


def baseline_passes(features: dict[str, Any]) -> bool:
    """True when the baseline keeps the company. Missing signals never reject:
    a screen must fail open."""
    if features.get("trading_status") in REJECT_TRADING_STATUS:
        return False
    if features.get("name_suggests_holding"):
        return False
    if features.get("sic_label") in REJECT_SIC_GROUPS:
        return False
    return True


def load_features(conn: sqlite3.Connection, numbers: list[str]) -> dict[str, dict[str, Any]]:
    """Gate A signals and SIC label for each company number."""
    features: dict[str, dict[str, Any]] = {n: {} for n in numbers}
    for number in numbers:
        row = conn.execute(
            "select g.sic_label from companies c left join sic_groups g on g.sic_code = c.sic_code_primary "
            "where c.company_number = ?",
            (number,),
        ).fetchone()
        features[number]["sic_label"] = row[0] if row else None
    for number, key, text, flag in conn.execute(
        "select company_number, signal_key, signal_text, signal_bool from company_signals "
        f"where signal_key in ('trading_status', 'name_suggests_holding') and company_number in ({','.join('?' * len(numbers))})",
        numbers,
    ):
        features[number][key] = text if key == "trading_status" else bool(flag)
    return features


def _label(case: dict[str, Any]) -> tuple[str | None, bool]:
    """(label, provisional). A verified human label wins; a draft is provisional."""
    if (case.get("review") or {}).get("status") == "verified":
        value = ((case.get("expected") or {}).get("search_screen") or {}).get("value")
        if value:
            return value, False
    value = ((case.get("draft") or {}).get("search_screen") or {}).get("value")
    return value, True


def evaluate(cases_dir: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    cases = [load_case(path) for path in case_files(cases_dir)]
    features = load_features(conn, [case["company_number"] for case in cases])
    report: dict[str, Any] = {}
    for cohort in ("random", "hard"):
        rows = [(case, *_label(case)) for case in cases if case.get("cohort") == cohort]
        rows = [(case, label, provisional) for case, label, provisional in rows if label]
        likely = [case for case, label, _ in rows if label == "likely"]
        passing = [case for case, label, _ in rows if label in PASSING_LABELS]
        kept = lambda group: sum(baseline_passes(features[c["company_number"]]) for c in group)  # noqa: E731
        report[cohort] = {
            "n": len(rows),
            "provisional_labels": sum(1 for *_, provisional in rows if provisional),
            "recall_on_likely": [kept(likely), len(likely)],
            "recall_on_likely_or_possible": [kept(passing), len(passing)],
            "removed_share": round(1 - kept([c for c, *_ in rows]) / len(rows), 3) if rows else None,
        }
    return report


def population_removal(conn: sqlite3.Connection) -> dict[str, Any]:
    numbers = target_population(conn)
    features = load_features(conn, numbers)
    kept = sum(baseline_passes(features[n]) for n in numbers)
    return {"n": len(numbers), "kept": kept, "removed_share": round(1 - kept / len(numbers), 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--cases-dir", default=str(CASES_DIR))
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        result = {"gold": evaluate(Path(args.cases_dir), conn), "target_population": population_removal(conn)}
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
