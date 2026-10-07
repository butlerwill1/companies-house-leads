"""Save the raw XHTML filing (plus its Companies House document metadata)
for every business-profile gold-set case, so a label can be checked against
the filed text directly rather than through the extracted narrative.

Writes data/raw/business-profile-xhtml/<company_number>.xhtml,
<company_number>.metadata.json, and <company_number>.md. data/ is
gitignored -- nothing here is committed. Free document-API calls only, no
model calls.

Companies House's own filed XHTML is a single unbroken line (no newlines at
all) -- readable in a browser, where whitespace does not matter, but
unreadable in a text editor. The .md sibling is a Markdown rendition with
one heading/paragraph/table-cell per line, built with the same
strip_ixbrl_non_visible_blocks() the real narrative extractor uses so the
visible text matches; only the block-boundary line breaks and Markdown
escaping are new. Any local, human-readable rendition of a raw filing or
similar single-line document should go through to_readable_markdown() below
rather than a fresh one-off flattening -- see AGENTS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from html import unescape
from pathlib import Path
from typing import Any, Iterable

import requests

from core.companies_house_extractor import load_dotenv, strip_ixbrl_non_visible_blocks
from scripts.business_profile_classifier.business_profile_eval import case_files, load_case

DEST_DIR = Path("data/raw/business-profile-xhtml")
# PDF-only filings (no XHTML resource) go here for the transcription harness.
PDF_DIR = Path("data/raw/business-profile-pdf")

# Elements whose boundaries mark a natural line break when flattening to
# Markdown -- headings, paragraphs, table rows/cells, list items.
_BLOCK_TAGS = (
    "p", "div", "tr", "td", "th", "table", "thead", "tbody", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "br",
)
_BLOCK_BOUNDARY_RE = re.compile(rf"</?(?:{'|'.join(_BLOCK_TAGS)})\b[^>]*>", re.I)
_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.I | re.S)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WHITESPACE_RE = re.compile(r"[ \t]+")

# Filed accounts are full of lines a Markdown renderer would otherwise
# reinterpret as structure -- "- 1 -" page markers read as bullets, "1. the
# nature of..." clauses read as an ordered list. Escape just the leading
# control character so the rendered text matches the source exactly.
_LIST_BULLET_RE = re.compile(r"^[-*+]\s")
_ATX_HEADING_RE = re.compile(r"^#{1,6}(\s|$)")
_ORDERED_LIST_RE = re.compile(r"^(\d+)([.)])(\s)")


def _escape_markdown_line_start(line: str) -> str:
    if _LIST_BULLET_RE.match(line) or _ATX_HEADING_RE.match(line) or line.startswith(">"):
        return "\\" + line
    match = _ORDERED_LIST_RE.match(line)
    if match:
        return f"{match.group(1)}\\{match.group(2)}{line[match.end(2):]}"
    return line


def to_readable_markdown(xhtml: str) -> str:
    """Flatten filed XHTML to Markdown, one visible heading/paragraph/table
    cell per line, so the filing can be read top to bottom in any editor or
    Markdown viewer. Lines are joined with Markdown hard line breaks
    (trailing double space) so a rendered preview keeps the same one-item-
    per-line layout as plain text, rather than collapsing them into a single
    flowing paragraph."""
    return readable_markdown_from_lines(xhtml_visible_lines(xhtml))


def xhtml_visible_lines(xhtml: str) -> list[str]:
    """The filing's visible text, one block element per line, blank lines
    dropped -- the XHTML half of to_readable_markdown."""
    cleaned = _HEAD_RE.sub(" ", xhtml)
    cleaned = _STYLE_RE.sub(" ", cleaned)
    cleaned = _SCRIPT_RE.sub(" ", cleaned)
    cleaned = strip_ixbrl_non_visible_blocks(cleaned)
    cleaned = _BLOCK_BOUNDARY_RE.sub("\n", cleaned)
    text = unescape(_TAG_RE.sub("", cleaned))
    lines = (_INLINE_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return [line for line in lines if line]


def readable_markdown_from_lines(lines: Iterable[str]) -> str:
    """Lines of already-flat text (from XHTML, or a page transcription) to
    the same hard-line-break Markdown to_readable_markdown produces, so a
    transcript reads the same way a filed XHTML rendition does."""
    return "  \n".join(_escape_markdown_line_start(line) for line in lines if line)


_DOCUMENT_COLUMNS = "document_id, transaction_id, metadata_url, xhtml_url, pdf_url, metadata_payload"


def _document_row(conn: sqlite3.Connection, case: dict) -> sqlite3.Row | None:
    """The filing the case's sections were extracted from -- the one its
    ``narrative_run_id`` points at -- and only failing that, the newest
    document on file.

    Until 2026-09-12 this took the most recently *inserted* document, which
    after the history backfill is the oldest filing, not the current one:
    06379728 and 11810776 had their 2022 accounts cached against sections
    taken from their 2025 accounts, and the whole-document refresh flagged
    every label's quote as missing from a document it was never in."""
    run_id = case.get("narrative_run_id")
    if run_id is not None:
        row = conn.execute(
            f"select {_DOCUMENT_COLUMNS} from documents "
            "where document_id = (select document_id from narrative_runs where id = ?)",
            (run_id,),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        f"select {_DOCUMENT_COLUMNS} from documents where company_number=? "
        "order by (xhtml_url is null), rowid desc limit 1",
        (case["company_number"],),
    ).fetchone()


def _latest_filing_from_api(company_number: str, api_key: str) -> dict | None:
    """Fallback for a case with no ``documents`` row at all (08029548, the
    pilot company, predates the current database): the newest accounts
    filing from the public filing-history API, shaped like a documents row
    so save_filing can treat it the same. Two free API calls."""
    from core.companies_house_extractor import CompaniesHouseExtractor, pick_latest_accounts_filing

    extractor = CompaniesHouseExtractor(api_key=api_key)
    filing = pick_latest_accounts_filing(extractor.get_accounts_history(company_number, max_filings=1))
    if not filing:
        return None
    urls = extractor.get_document_urls(company_number, filing)
    content_url = urls.get("xhtml") or urls.get("pdf")
    if not content_url:
        return None
    document_id = content_url.rstrip("/").split("/")[-2]
    return {
        "document_id": document_id,
        "transaction_id": filing.get("transaction_id"),
        "metadata_url": urls.get("metadata"),
        "xhtml_url": urls.get("xhtml"),
        "pdf_url": urls.get("pdf"),
        "filing_date": filing.get("date"),
        "metadata_payload": json.dumps({"filing": filing, "source": "filing-history API fallback"}),
    }


def _filing_date(conn: sqlite3.Connection, row: Any) -> str | None:
    """The filing's date, from the API fallback row or the filings table."""
    try:
        if row["filing_date"]:
            return row["filing_date"]
    except (KeyError, IndexError):
        pass
    try:
        found = conn.execute(
            "select filing_date from filings where transaction_id = ?", (row["transaction_id"],)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return found[0] if found else None


def save_filing(
    conn: sqlite3.Connection, case: dict, api_key: str, dest_dir: Path, pdf_dir: Path = PDF_DIR
) -> str:
    """Cache the case's filing. XHTML filings land as <company>.xhtml + .md;
    a filing that only exists as a PDF (a paper or scanned filing --
    08029548 SMART CURRENCY GROUP files nothing else) is downloaded to
    ``pdf_dir`` and reported as ``pdf_only``, for the transcription harness
    (scripts/vlm/companies_house_pdf_transcribe.py) to turn into text."""
    company_number = case["company_number"]
    row = _document_row(conn, case)
    if row is None:
        row = _latest_filing_from_api(company_number, api_key)
    if row is None:
        return "no_document"

    content_url = f"https://document-api.company-information.service.gov.uk/document/{row['document_id']}/content"
    pdf_only = not row["xhtml_url"]
    response = requests.get(
        content_url,
        auth=(api_key, ""),
        headers={"Accept": "application/pdf" if pdf_only else "application/xhtml+xml"},
        timeout=120 if pdf_only else 60,
    )
    if response.status_code != 200:
        return f"http_{response.status_code}"

    dest_dir.mkdir(parents=True, exist_ok=True)
    filing_date = _filing_date(conn, row)
    pdf_path: Path | None = None
    if pdf_only:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / f"{company_number}-{filing_date or row['document_id']}.pdf"
        pdf_path.write_bytes(response.content)
    else:
        (dest_dir / f"{company_number}.xhtml").write_text(response.text, encoding="utf-8")
        title = f"# {case.get('company_name') or company_number} ({company_number})\n\n"
        markdown = title + to_readable_markdown(response.text)
        (dest_dir / f"{company_number}.md").write_text(markdown, encoding="utf-8")

    metadata = {
        "company_number": company_number,
        "company_name": case.get("company_name"),
        "document_id": row["document_id"],
        "transaction_id": row["transaction_id"],
        "metadata_url": row["metadata_url"],
        "xhtml_url": row["xhtml_url"],
        "pdf_url": row["pdf_url"],
        "filing_date": filing_date,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "document_metadata": json.loads(row["metadata_payload"]) if row["metadata_payload"] else None,
        "fetched_from": content_url,
    }
    (dest_dir / f"{company_number}.metadata.json").write_text(
        json.dumps(metadata, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return "pdf_only" if pdf_only else "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--cases-dir", default="evals/business_profiles/cases")
    parser.add_argument("--dest-dir", default=str(DEST_DIR))
    parser.add_argument("--pdf-dir", default=str(PDF_DIR), help="Where PDF-only filings are saved.")
    parser.add_argument("--company", action="append", default=None,
                        help="Only these company numbers (repeatable). Default: every case.")
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    api_key = os.environ["COMPANIES_HOUSE_API_KEY"]
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    dest_dir = Path(args.dest_dir)
    pdf_dir = Path(args.pdf_dir)

    results: dict[str, str] = {}
    for path in case_files(Path(args.cases_dir)):
        case = load_case(path)
        if args.company and case["company_number"] not in args.company:
            continue
        results[case["company_number"]] = save_filing(conn, case, api_key, dest_dir, pdf_dir)

    ok = sum(1 for status in results.values() if status == "ok")
    pdf_only = sum(1 for status in results.values() if status == "pdf_only")
    print(json.dumps({"saved": ok, "pdf_only": pdf_only, "total": len(results)}, indent=2))
    for company_number, status in results.items():
        if status not in ("ok", "pdf_only"):
            print(f"  {company_number}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
