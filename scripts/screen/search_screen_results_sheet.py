#!/usr/bin/env python3
"""Results tabs for the search-screen comparison, as CSV files for the
publish-eval-sheet route (one Drive file per tab).

Raters scored against the verified gold labels: the model-drafted labels (from
the shortened filing and from the evidence pack), and the screen run with two
inputs. Reads the case files and the run checkpoint; makes no model calls.

Usage:
    python -m scripts.screen.search_screen_results_sheet --out-dir logs/search-screen/results
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.screen.search_screen_baseline import baseline_passes, load_features  # noqa: E402
from scripts.screen.search_screen_cases import CASES_DIR, SCREEN_LABELS  # noqa: E402
from scripts.screen.search_screen_eval import (  # noqa: E402
    DEFAULT_MODEL,
    _key,
    cost,
    gold_cases,
    gold_label,
    load_checkpoint,
    model_prices,
    score,
)
from scripts.screen.search_screen_policy import PASSING, PROMPT_VERSION_V1  # noqa: E402

GPT_SHORT = "GPT-5.4-mini, short extract"
GPT_FULL = "GPT-5.4-mini, full filing"
CLAUDE_FULL = "Claude Sonnet 5.5 draft (from shortened filing)"
CLAUDE_PACK = "Claude draft (from evidence pack)"
BASELINE = "Free baseline (SQLite rule, no model)"
RATERS = (CLAUDE_FULL, CLAUDE_PACK, GPT_SHORT, GPT_FULL, BASELINE)
BASELINE_PASSES: dict[str, bool] = {}
RUN_TEXT = {GPT_SHORT: "short", GPT_FULL: "full"}
DRAFT_KEY = {CLAUDE_FULL: "draft_full", CLAUDE_PACK: "draft"}


def _pct(pair: tuple[int, int]) -> str:
    return f"{pair[0]}/{pair[1]} ({pair[0] / pair[1]:.0%})" if pair[1] else "n/a"


def _answer(case: dict[str, Any], rater: str, records: dict[str, dict[str, dict[str, Any]]]) -> str:
    if rater == BASELINE:
        return "pass" if BASELINE_PASSES[case["company_number"]] else "reject"
    if rater in DRAFT_KEY:
        return ((case.get(DRAFT_KEY[rater]) or {}).get("search_screen") or {}).get("value") or ""
    record = records[RUN_TEXT[rater]].get(case["company_number"]) or {}
    return record.get("answer") or f"(none: {record.get('problem')})"


def _scored(cases: list[dict[str, Any]], rater: str, records: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    if rater == BASELINE:
        return score(cases, [{"company_number": c["company_number"], "answer": None,
                              "passes": BASELINE_PASSES[c["company_number"]]} for c in cases])
    if rater in DRAFT_KEY:
        return score(cases, [], draft_key=DRAFT_KEY[rater])
    return score(cases, [records[RUN_TEXT[rater]][c["company_number"]] for c in cases])


def summary_rows(cases: list[dict[str, Any]], records: dict[str, dict[str, dict[str, Any]]],
                 prices: tuple[float, float] | None) -> list[list[Any]]:
    rows: list[list[Any]] = [["Search screen: scores against the verified gold labels", *([""] * len(RATERS))],
                             ["metric", *RATERS]]
    random_cases = [c for c in cases if c["cohort"] == "random"]
    for title, subset in (("ALL VERIFIED CASES", cases), ("RANDOM COHORT ONLY (the acceptance criteria are on this)", random_cases)):
        scores = {rater: _scored(subset, rater, records) for rater in RATERS}
        gold_counts = next(iter(scores.values()))["gold_counts"]
        rows.append([title, *([""] * len(RATERS))])
        rows.append(["cases (gold: likely / possible / unlikely)", *[f"{len(subset)} ({gold_counts['likely']} / {gold_counts['possible']} / {gold_counts['unlikely']})"] * len(RATERS)])
        rows.append(["exact label agreement", *[("n/a (pass or reject only)" if r == BASELINE else f"{scores[r]['exact_agreement']:.1%}") for r in RATERS]])
        rows.append(["recall on gold likely (passes the screen)  [target 95%]", *[_pct(scores[r]["recall_likely"]) for r in RATERS]])
        rows.append(["recall on gold likely+possible  [target 90%]", *[_pct(scores[r]["recall_likely_or_possible"]) for r in RATERS]])
        rows.append(["share removed (rejected as unlikely)  [target at least 30%]", *[f"{scores[r]['removal_rate']:.1%}" for r in RATERS]])
        rows.append(["gold unlikely correctly rejected", *[_pct(scores[r]["gold_unlikely_rejected"]) for r in RATERS]])
    rows.append(["COST AND QUALITY OF THE MODEL RUNS (all verified cases)", *([""] * len(RATERS))])
    for label, getter in (
        ("input tokens", lambda r: f"{cost(r, prices)['prompt_tokens']:,}"),
        ("cost (USD)", lambda r: f"${cost(r, prices)['usd']:.2f}" if prices else "n/a"),
        ("cost per company (USD)", lambda r: f"${cost(r, prices)['usd'] / len(r):.4f}" if prices else "n/a"),
        ("quote found verbatim in the text shown", lambda r: f"{sum(bool(x.get('quote_ok')) for x in r)}/{len(r)}"),
        ("unparseable or failed responses (counted as a pass)", lambda r: str(sum(bool(x.get('problem')) for x in r))),
    ):
        rows.append([label, "", "", *[getter([records[RUN_TEXT[r]][c["company_number"]] for c in cases]) for r in (GPT_SHORT, GPT_FULL)], ""])
    rows += [["", *([""] * len(RATERS))], ["NOTES", *([""] * len(RATERS))]]
    for note in (
        f"Prompt {PROMPT_VERSION_V1}, {DEFAULT_MODEL}, temperature 0, one run per input. No run-to-run noise check yet.",
        "Gold = 151 cases you verified in the Langfuse queue. The 25 blind cases are not labelled yet and are not in this.",
        "You saw the Claude draft pre-filled and changed 5 of 151, so the Claude agreement figure is an upper bound (anchoring). The blind 25 is the unanchored test.",
        "The GPT prompt states only the definitions and rules already in SEARCH_SCREEN.md; hints learned during the review were left out so the gold is not used to tune it.",
        "Short extract = principal activity plus the first 1,800 characters of the strategic or directors' report. Full = whole filed report minus the auditor's report.",
        "Exact agreement counts likely vs possible as a miss, but both pass the screen. For the screen's job read recall, share removed and the confusion tab.",
        "Recall = share of gold cases the screen lets through. A rejected case is one answered unlikely; anything unparseable passes (fails open).",
    ):
        rows.append([note, *([""] * len(RATERS))])
    return rows


def confusion_rows(cases: list[dict[str, Any]], records: dict[str, dict[str, dict[str, Any]]]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for rater in RATERS[:4]:
        table = _scored(cases, rater, records)["confusion"]
        rows.append([rater])
        rows.append(["gold (down) / rater said (across)", *SCREEN_LABELS, "none"])
        for gold in SCREEN_LABELS:
            rows.append([gold, *[table[gold][a] for a in (*SCREEN_LABELS, "none")]])
        rows.append([])
    return rows


def case_rows(cases: list[dict[str, Any]], records: dict[str, dict[str, dict[str, Any]]]) -> list[list[Any]]:
    head = ["company number", "company name", "SIC", "cohort", "gold label", "gold changed from (your edit)",
            *RATERS[:4], "baseline (pass/reject)", "claude=gold", "gpt short=gold", "gpt full=gold",
            "gpt full reason"]
    rows: list[list[Any]] = [head]
    for case in sorted(cases, key=lambda c: c["company_number"]):
        gold = gold_label(case)
        answers = [_answer(case, r, records) for r in RATERS]
        full = records["full"][case["company_number"]]
        rows.append([case["company_number"], case["company_name"], case.get("sic_label"), case["cohort"], gold,
                     (case.get("review") or {}).get("changed_from") or "", *answers,
                     "yes" if answers[0] == gold else "NO", "yes" if answers[2] == gold else "NO",
                     "yes" if answers[3] == gold else "NO", _clip(full.get("reason"), 150)])
    return rows


def disagreement_rows(cases: list[dict[str, Any]], records: dict[str, dict[str, dict[str, Any]]]) -> list[list[Any]]:
    """Cases where the screen (either input) disagrees with the gold about the
    pass or reject decision: the cases that cost a lead or let a reject through."""
    rows: list[list[Any]] = [["company number", "company name", "gold label", "what went wrong", "gpt short said", "gpt full said",
                              "gpt full reason", "your verdict (gold right / gpt right)", "notes"]]
    for case in sorted(cases, key=lambda c: c["company_number"]):
        gold = gold_label(case)
        gold_pass = gold in PASSING
        short = records["short"][case["company_number"]]
        full = records["full"][case["company_number"]]
        problems = []
        for name, record in (("short", short), ("full", full)):
            if bool(record.get("passes")) != gold_pass:
                problems.append(f"{name}: {'rejected a lead' if gold_pass else 'passed an unlikely'}")
        if problems:
            rows.append([case["company_number"], case["company_name"], gold, "; ".join(problems),
                         short.get("answer") or "", full.get("answer") or "", _clip(full.get("reason")), "", ""])
    return rows


def _clip(text: Any, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_csv(rows: list[list[Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-dir", default=str(CASES_DIR))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--prompt-version", default=PROMPT_VERSION_V1,
                        help="which saved run to tabulate (the published 2026-09-30 sheets are v1)")
    parser.add_argument("--out-dir", default="logs/search-screen/results")
    args = parser.parse_args(argv)
    cases = gold_cases(Path(args.cases_dir))
    done = load_checkpoint()
    records = {kind: {c["company_number"]: done[_key(args.model, kind, c["company_number"], args.prompt_version)]
                      for c in cases}
               for kind in ("short", "full")}
    prices = model_prices(args.model)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    features = load_features(conn, [c["company_number"] for c in cases])
    BASELINE_PASSES.update({c["company_number"]: baseline_passes(features[c["company_number"]]) for c in cases})
    out = Path(args.out_dir)
    tabs = {"summary": summary_rows(cases, records, prices), "confusion": confusion_rows(cases, records),
            "cases": case_rows(cases, records), "disagreements": disagreement_rows(cases, records)}
    for name, rows in tabs.items():
        write_csv(rows, out / f"{name}.csv")
        print(f"{name}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
