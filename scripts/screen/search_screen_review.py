#!/usr/bin/env python3
"""Review sheets and verdict import for the search-screen gold set.

Two sheets, both plain CSV for the publish-eval-sheet route:

- ``blind``: the ``blind`` cases with the draft column absent. The reviewer
  labels these first, so agreement with the drafter is a fair measurement.
- ``review``: every drafted case with the draft shown; the reviewer writes
  ``agree`` or a different label.

``import-verdicts`` writes the verdicts back into the case files. Google Sheets
drops the leading zeros of company numbers, so numbers are matched with
``zfill(8)`` as a fallback. Nothing here calls a model.

Usage:
    python -m scripts.screen.search_screen_review export --sheet blind --out logs/search-screen/blind.csv
    python -m scripts.screen.search_screen_review import-verdicts --csv verdicts.csv --kind blind --reviewer will
    python -m scripts.screen.search_screen_review agreement
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.business_profile_classifier.business_profile_eval import case_files, load_case, save_case, utc_now  # noqa: E402
from scripts.screen.search_screen_cases import CASES_DIR, SCREEN_LABELS  # noqa: E402

PASSING = frozenset({"likely", "possible"})
MAX_TEXT = 4000


def _text(case: dict[str, Any]) -> str:
    if case.get("label_pack"):
        return " ".join(case["label_pack"].split())
    sections = case.get("sections") or {}
    parts = [f"[{key}] {sections[key]}" for key in ("principal_activity", "strategic_report", "business_review",
                                                   "directors_report", "turnover_note", "employee_note",
                                                   "principal_risks", "future_developments") if sections.get(key)]
    text = " ".join(parts)
    if len(text) <= MAX_TEXT:
        return text
    return f"{text[:MAX_TEXT]} [TRUNCATED: {len(text) - MAX_TEXT} more characters in the case file]"


def _money(value: Any) -> Any:
    return "" if value is None else round(value)


def _base(case: dict[str, Any]) -> list[Any]:
    financials = case.get("financials") or {}
    return [case["company_number"], case.get("company_name"), case.get("sic_label"), case.get("cohort"),
            _money(financials.get("turnover")), _money(financials.get("profit_after_tax")),
            financials.get("employees"), _text(case)]


def blind_rows(cases_dir: Path) -> list[list[Any]]:
    header = ["company number", "company name", "SIC label", "cohort", "turnover", "profit after tax",
              "employees", "evidence pack (extract of the filed report)", "verdict (likely/possible/unlikely)", "notes"]
    rows: list[list[Any]] = [header]
    for path in case_files(cases_dir):
        case = load_case(path)
        if case.get("blind"):
            rows.append([*_base(case), "", ""])
    return rows


def review_rows(cases_dir: Path) -> list[list[Any]]:
    header = ["company number", "company name", "SIC label", "cohort", "turnover", "profit after tax",
              "employees", "evidence pack (extract of the filed report)", "draft", "draft quote", "draft reason",
              "verdict (agree, or likely/possible/unlikely)", "notes"]
    rows: list[list[Any]] = [header]
    for path in case_files(cases_dir):
        case = load_case(path)
        draft = (case.get("draft") or {}).get("search_screen") or {}
        if case.get("blind") and not case.get("blind_review"):
            continue  # the reviewer has not labelled this one blind yet; showing the draft would anchor them
        if draft.get("value"):
            rows.append([*_base(case), draft.get("value"), draft.get("quote"), draft.get("reason"), "", ""])
    return rows


def write_csv(rows: list[list[Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _resolve(number: str, cases_dir: Path) -> Path:
    number = number.strip()
    for candidate in (number, number.zfill(8)):
        path = cases_dir / f"{candidate}.json"
        if path.exists():
            return path
    raise ValueError(f"no case file for company number {number!r} (also tried {number.zfill(8)!r})")


def _verdict_column(fieldnames: list[str]) -> str:
    for name in fieldnames:
        if name.lower().startswith("verdict"):
            return name
    raise ValueError("the CSV has no 'verdict' column")


def _number_column(fieldnames: list[str]) -> str:
    for name in fieldnames:
        if name.lower().replace("_", " ").startswith("company number"):
            return name
    raise ValueError("the CSV has no 'company number' column")


def import_verdicts(csv_path: Path, cases_dir: Path, *, kind: str, reviewer: str) -> int:
    """Apply a verdict CSV. ``kind`` is ``blind`` or ``review``. Every row is
    validated before any case file is written, so a typo changes nothing."""
    if kind not in ("blind", "review"):
        raise ValueError(f"kind must be 'blind' or 'review', not {kind!r}")
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        number_key = _number_column(reader.fieldnames or [])
        verdict_key = _verdict_column(reader.fieldnames or [])
        rows = [(row[number_key], (row.get(verdict_key) or "").strip().lower(), (row.get("notes") or "").strip())
                for row in reader]

    updates: list[tuple[Path, str, str]] = []
    for number, verdict, notes in rows:
        if not verdict:
            continue
        path = _resolve(number, cases_dir)
        allowed = SCREEN_LABELS + (("agree",) if kind == "review" else ())
        if verdict not in allowed:
            raise ValueError(f"{number}: verdict {verdict!r} is not one of {allowed}")
        updates.append((path, verdict, notes))

    now = utc_now()
    for path, verdict, notes in updates:
        case = load_case(path)
        draft = (case.get("draft") or {}).get("search_screen") or {}
        label = dict(draft) if verdict == "agree" else {
            "value": verdict, "quote": None, "section": None, "reason": notes or None}
        if verdict == "agree" and not draft.get("value"):
            raise ValueError(f"{path.stem}: cannot agree with a case that has no draft")
        if kind == "blind":
            case["blind_review"] = {"value": verdict, "reviewer": reviewer, "reviewed_at": now, "notes": notes or None}
            changed_from = None
        else:
            changed_from = draft.get("value") if label["value"] != draft.get("value") else None
        case["expected"] = {"search_screen": label}
        case["review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": now,
                          "changed_from": changed_from, "notes": notes or None}
        save_case(path, case)
    return len(updates)


def _normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


DRAFT_KEYS = ("draft", "draft_full")


def apply_drafts(drafts_path: Path, cases_dir: Path, *, drafter: str, key: str = "draft") -> int:
    """Write drafted labels into the case files. ``drafts_path`` is a JSON object
    keyed by company number: ``{"value", "quote", "reason"}``. Every draft is
    validated before any file is written: a known label, a non-empty reason,
    and a quote that appears verbatim (whitespace and case aside) in the filed
    report. A verified case is never overwritten.

    ``key`` picks the slot. ``draft`` is the first, evidence-pack draft and moves
    the case to ``drafted``; ``draft_full`` is the second draft written from the
    full filing, kept beside the first so the two can be compared, and it leaves
    the review status alone."""
    if key not in DRAFT_KEYS:
        raise ValueError(f"key must be one of {DRAFT_KEYS}, not {key!r}")
    drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
    checked: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for number, draft in drafts.items():
        path = _resolve(number, cases_dir)
        case = load_case(path)
        if (case.get("review") or {}).get("status") == "verified":
            raise ValueError(f"{number}: case is verified; a draft would overwrite reviewed work")
        value = (draft.get("value") or "").strip().lower()
        if value not in SCREEN_LABELS:
            raise ValueError(f"{number}: value {value!r} is not one of {SCREEN_LABELS}")
        if not (draft.get("reason") or "").strip():
            raise ValueError(f"{number}: a draft needs a reason")
        quote = (draft.get("quote") or "").strip()
        if not quote:
            raise ValueError(f"{number}: a draft needs a quote from the filing")
        report = " ".join((case.get("sections") or {}).values())
        if _normalise(quote) not in _normalise(report):
            raise ValueError(f"{number}: quote is not in the filed report: {quote[:80]!r}")
        checked.append((path, case, {"value": value, "quote": quote, "section": "filed_report",
                                     "reason": draft["reason"].strip()}))
    now = utc_now()
    for path, case, label in checked:
        case[key] = {"search_screen": label, "drafted_by": drafter, "drafted_at": now}
        if key == "draft":
            case["review"] = {**(case.get("review") or {}), "status": "drafted"}
        save_case(path, case)
    return len(checked)


def blind_agreement(cases_dir: Path) -> dict[str, Any]:
    """How often the reviewer's blind label matches the drafter's, exactly and
    on the pass/reject decision the screen actually makes."""
    n = agree = pass_reject = 0
    for path in case_files(cases_dir):
        case = load_case(path)
        blind = (case.get("blind_review") or {}).get("value")
        draft = ((case.get("draft") or {}).get("search_screen") or {}).get("value")
        if not blind or not draft:
            continue
        n += 1
        agree += blind == draft
        pass_reject += (blind in PASSING) == (draft in PASSING)
    return {"n": n, "agree": agree, "pass_reject_agree": pass_reject}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-dir", default=str(CASES_DIR))
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Write a review sheet as CSV.")
    export.add_argument("--sheet", choices=("blind", "review"), required=True)
    export.add_argument("--out", required=True)
    imported = commands.add_parser("import-verdicts", help="Write a verdict CSV back into the case files.")
    imported.add_argument("--csv", required=True)
    imported.add_argument("--kind", choices=("blind", "review"), required=True)
    imported.add_argument("--reviewer", required=True)
    drafts = commands.add_parser("apply-drafts", help="Write drafted labels (JSON keyed by company number).")
    drafts.add_argument("--drafts", required=True)
    drafts.add_argument("--drafter", required=True)
    drafts.add_argument("--target", choices=DRAFT_KEYS, default="draft")
    commands.add_parser("agreement", help="Blind-label agreement between reviewer and drafter.")
    args = parser.parse_args(argv)
    cases_dir = Path(args.cases_dir)
    if args.command == "export":
        rows = blind_rows(cases_dir) if args.sheet == "blind" else review_rows(cases_dir)
        write_csv(rows, Path(args.out))
        print(f"wrote {len(rows) - 1} rows to {args.out}")
    elif args.command == "apply-drafts":
        print(f"drafted {apply_drafts(Path(args.drafts), cases_dir, drafter=args.drafter, key=args.target)} cases")
    elif args.command == "import-verdicts":
        count = import_verdicts(Path(args.csv), cases_dir, kind=args.kind, reviewer=args.reviewer)
        print(f"applied {count} verdicts")
    else:
        print(blind_agreement(cases_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
