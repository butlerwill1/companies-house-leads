"""Export the filings and publish the search-screen cases to Langfuse.

The text is the whole filed report with only the independent auditor's report
removed (``sections.filed_report``, built by ``filed_report_text``). Note the
drafts in ``draft_full`` were written from a shortened version of it
(``clean_full_text``, 19% of the characters), not from this text; see
docs/SEARCH_SCREEN.md. The reviewer checks them against the whole report.

``export-text`` writes one readable file per case. ``publish-langfuse`` upserts
a dataset whose expected output is the best label a case has: a human verdict
when there is one, otherwise the shortened-text draft. Every item says which, in
``metadata.label_status``, so a draft is never mistaken for a verified label.

Blind cases without a ``blind_review`` are skipped, as in ``review_rows``:
publishing the draft would show the reviewer the answer before they label it.
Items upsert on ``id``, so republishing after review overwrites the drafts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

from scripts.eval_support.langfuse_runs import dataset_digest, sync_dataset  # noqa: E402
from scripts.eval_support.langfuse_tracing import langfuse_from_config  # noqa: E402
from scripts.business_profile_classifier.business_profile_eval import case_files, load_case  # noqa: E402
from scripts.screen.search_screen_cases import CASES_DIR  # noqa: E402

TEXT_DIR = Path("logs/search-screen/full-text")
DATASET_NAME = "search-screen-gold-draft"
# Item ids are unique across the whole Langfuse project, and the business-profile
# dataset already uses bare company numbers, so these are prefixed.
ID_PREFIX = "search-screen:"
FULL_TEXT_NOTE = "Whole filed report; only the independent auditor's report is removed."


def full_filing_text(case: dict[str, Any]) -> str:
    return case["sections"]["filed_report"]


LANGFUSE_CONFIG = {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}


def best_label(case: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The label to publish and where it came from."""
    review = case.get("review") or {}
    expected = (case.get("expected") or {}).get("search_screen") or {}
    if review.get("status") == "verified" and expected.get("value"):
        return expected, "verified"
    draft_full = ((case.get("draft_full") or {}).get("search_screen")) or {}
    if draft_full.get("value"):
        return draft_full, "draft-unverified"
    return None, "none"


def _publishable(case: dict[str, Any]) -> bool:
    if case.get("blind") and not case.get("blind_review"):
        return False
    return best_label(case)[0] is not None


def _header(case: dict[str, Any], label: dict[str, Any] | None, status: str) -> str:
    money = case.get("financials") or {}
    lines = [
        f"{case['company_number']}  {case['company_name']}",
        f"SIC: {case.get('sic_label')}   financial year: {case.get('financial_year')}   "
        f"cohort: {case['cohort']}" + (f" / {case['hard_category']}" if case.get("hard_category") else ""),
        f"financials: {money}",
    ]
    if label and not (case.get("blind") and not case.get("blind_review")):
        lines += [f"label ({status}): {label['value']}", f"quote: {label.get('quote')}", f"reason: {label.get('reason')}"]
    else:
        lines.append("label: hidden (blind case, not yet labelled)")
    return "\n".join(lines)


def export_text(cases_dir: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for path in case_files(cases_dir):
        case = load_case(path)
        text = full_filing_text(case)
        label, status = best_label(case)
        body = f"{_header(case, label, status)}\n{FULL_TEXT_NOTE}\n\n{'=' * 72}\n\n{text}\n"
        (out_dir / f"{case['company_number']}.txt").write_text(body, encoding="utf-8")
        shown = "hidden" if case.get("blind") and not case.get("blind_review") else (label or {}).get("value")
        index.append(f"{case['company_number']}\t{case['cohort']}\t{shown}\t{len(text)}\t{case['company_name']}")
    (out_dir / "INDEX.tsv").write_text("company\tcohort\tlabel\tchars\tname\n" + "\n".join(index) + "\n", encoding="utf-8")
    return len(index)


def dataset_records(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for case in cases:
        label, status = best_label(case)
        if label is None or (case.get("blind") and not case.get("blind_review")):
            continue
        text = full_filing_text(case)
        records.append({
            "id": f"{ID_PREFIX}{case['company_number']}",
            "input": {
                "company_name": case["company_name"],
                "sic_label": case.get("sic_label"),
                "financial_year": case.get("financial_year"),
                "financials": case.get("financials"),
                "filing_text": text,
            },
            "expected": {"search_screen": label["value"], "quote": label.get("quote"), "reason": label.get("reason")},
            "metadata": {
                "company_number": case["company_number"],
                "company_name": case["company_name"],
                "cohort": case["cohort"],
                "hard_category": case.get("hard_category"),
                "label_status": status,
                "text_source": "filed_report_minus_auditor_report",
                "drafted_by": (case.get("draft_full") or {}).get("drafted_by"),
            },
        })
    return records


def publish(cases_dir: Path, dataset: str) -> tuple[int, int]:
    load_dotenv(Path(".env"))
    client = langfuse_from_config(LANGFUSE_CONFIG)
    if client is None:
        raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
    cases = [load_case(p) for p in case_files(cases_dir)]
    records = dataset_records(cases)
    sync_dataset(
        client,
        dataset,
        records,
        description="Search-screen cases: cleaned full filing in, likely/possible/unlikely out. "
        "Labels are drafts unless metadata.label_status is 'verified'.",
    )
    client.flush()
    return len(records), len(cases) - len(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-text")
    export.add_argument("--cases-dir", default=str(CASES_DIR))
    export.add_argument("--out-dir", default=str(TEXT_DIR))
    pub = sub.add_parser("publish-langfuse")
    pub.add_argument("--cases-dir", default=str(CASES_DIR))
    pub.add_argument("--dataset", default=DATASET_NAME)
    pub.add_argument("--dry-run", action="store_true", help="count and hash the records without calling Langfuse")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.command == "export-text":
        print(f"wrote {export_text(Path(args.cases_dir), Path(args.out_dir))} files to {args.out_dir}")
        return 0
    if args.dry_run:
        cases = [load_case(p) for p in case_files(Path(args.cases_dir))]
        records = dataset_records(cases)
        print(f"{len(records)} records, {len(cases) - len(records)} withheld, digest {dataset_digest(records)[:12]}")
        return 0
    published, withheld = publish(Path(args.cases_dir), args.dataset)
    print(f"published {published} items to Langfuse dataset {args.dataset!r}; {withheld} withheld (blind, unlabelled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
