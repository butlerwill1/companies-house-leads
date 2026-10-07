"""Refresh the `sections` text stored in the gold-set case files
(evals/business_profile_gold_set/cases/*.json) from the current, fixed extraction
path -- companies_house_core.companies_house_extractor.parse_xhtml_narrative, which applies
both the iXBRL principal-activity tag recovery and the block-structure-aware
heading scan.

A case file's `sections` is a static snapshot taken once, at case-creation
time, from the narrative_sections DB table (itself populated by an earlier
extraction pass). Fixing extract_sections() -- the leading-article
truncation and auditor-boilerplate misclassification bugs found during
Phase 3d -- changes what a *future* extraction produces; it does nothing to
the 57 case files already on disk, which is exactly why the Langfuse smoke
test on 2026-09-04 reproduced the same rejections Phase 3d had already
fixed at the code level. This script closes that gap for the gold set.

Regenerates only `sections`, from the archived raw filed document
(data/raw/business-profile-filed-reports/{company_number}.xhtml) -- never touches
`expected`, `review`, or anything else. Every existing gold-label quote is
re-verified against the refreshed sections before a case is written: if any
quote no longer matches verbatim, the new extraction changed something that
label's evidence actually depends on, and the case is left unmodified and
reported instead of silently overwritten -- that is a case for a human to
look at, not a case this script gets to decide about.

Usage:
    python -m scripts.business_profile_classifier.business_profile_refresh_sections [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from companies_house_core.companies_house_extractor import (  # noqa: E402
    filed_report_text,
    parse_xhtml_narrative,
)
from scripts.business_profile_classifier.business_profile_eval import case_files, load_case, save_case  # noqa: E402
from scripts.business_profile_classifier.business_profile_policy import (  # noqa: E402
    FIELD_VALUES,
    normalize_quote_text,
    select_narrative_sections,
)

CASES_DIR = Path("evals/business_profile_gold_set/cases")
RAW_DIR = Path("data/raw/business-profile-filed-reports")


def _existing_quote_breaks(
    case: dict[str, Any], new_sections: dict[str, str], whole_document: bool = False
) -> list[str]:
    """Every non-empty, non-`unclear` quote in this case's `expected` block,
    re-checked against the refreshed sections the same way validate_response
    checks a live model response. A break here means the refreshed
    extraction lost or altered text this case's label depends on."""
    problems = []
    expected = case.get("expected") or {}
    for field in FIELD_VALUES:
        entry = expected.get(field)
        if not isinstance(entry, dict) or entry.get("value") in (None, "unclear"):
            continue
        quote = entry.get("quote") or ""
        section_name = entry.get("section")
        if not quote or not section_name:
            continue
        # In whole-document mode every quote lives in the one `filed_report`
        # section, so the recorded section name is stale by construction and
        # checking against it would flag every case. The question that still
        # matters is whether the evidence survives at all.
        section_text = (
            " ".join(new_sections.values()) if whole_document else new_sections.get(section_name)
        )
        if section_text is None:
            problems.append(f"{field}: section {section_name!r} no longer present")
        elif normalize_quote_text(quote) not in normalize_quote_text(section_text):
            problems.append(f"{field}: quote no longer verbatim in {section_name!r}: {quote!r}")
    return problems


def refresh(dry_run: bool, whole_document: bool = False) -> int:
    updated = grown = shrank = unchanged = flagged = missing_raw = transcribed = 0

    for path in case_files(CASES_DIR):
        case = load_case(path)
        company_number = case["company_number"]
        # The .xhtml, not the .md rendition: the filing's iXBRL tags are the
        # authoritative source for principal_activity, and flattening to
        # Markdown throws them away. parse_xhtml_narrative applies the tag
        # recovery and the block-structure-aware heading scan together, which
        # is what a live extraction run does -- reading the .md here would
        # refresh the gold set against a weaker path than production uses.
        raw_path = RAW_DIR / f"{company_number}.xhtml"
        # A filing that only exists as a scanned PDF has no .xhtml; the
        # transcription harness (scripts/pdf_vision_extraction/companies_house_pdf_transcribe.py)
        # leaves its auditor-stripped text as <company>.filed_report.txt,
        # which is already the whole-document shape. Whole-document mode only:
        # the named windows need the iXBRL tags a transcript cannot carry.
        transcript_path = RAW_DIR / f"{company_number}.filed_report.txt"
        if raw_path.exists():
            raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
            if whole_document:
                new_sections = {"filed_report": filed_report_text(raw_text)}
            else:
                new_sections = select_narrative_sections(parse_xhtml_narrative(raw_text)["sections"])
        elif whole_document and transcript_path.exists():
            new_sections = {"filed_report": transcript_path.read_text(encoding="utf-8")}
            transcribed += 1
            print(f"  {company_number}: using VLM transcription {transcript_path.name}", file=sys.stderr)
        else:
            print(f"  {company_number}: SKIPPED -- no raw document at {raw_path}", file=sys.stderr)
            missing_raw += 1
            continue
        old_sections = case.get("sections") or {}

        problems = _existing_quote_breaks(case, new_sections, whole_document)
        if problems:
            flagged += 1
            print(f"  {company_number}: FLAGGED, not updated -- existing label evidence would break:", file=sys.stderr)
            for problem in problems:
                print(f"    {problem}", file=sys.stderr)
            continue

        for key in set(old_sections) | set(new_sections):
            old_len = len(old_sections.get(key, ""))
            new_len = len(new_sections.get(key, ""))
            if new_len > old_len:
                grown += 1
            elif new_len < old_len:
                shrank += 1
            else:
                unchanged += 1

        if new_sections == old_sections:
            continue

        updated += 1
        if not dry_run:
            case["sections"] = new_sections
            save_case(path, case)

    print(
        f"\n{updated} cases updated, {flagged} flagged (left unmodified), "
        f"{missing_raw} skipped (no raw document), {transcribed} from a VLM transcription."
    )
    print(f"section-level: {grown} grew, {shrank} shrank, {unchanged} unchanged.")
    if dry_run:
        print("(--dry-run: nothing written)")
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing anything.")
    parser.add_argument(
        "--whole-document",
        action="store_true",
        help="Store the whole filed document minus the auditor's report as a single "
             "`filed_report` section, instead of the named narrative windows.",
    )
    args = parser.parse_args(argv)
    return refresh(dry_run=args.dry_run, whole_document=args.whole_document)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
