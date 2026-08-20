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

import requests

from core.companies_house_extractor import load_dotenv, strip_ixbrl_non_visible_blocks
from scripts.profile.business_profile_eval import case_files, load_case

DEST_DIR = Path("data/raw/business-profile-xhtml")

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
    cleaned = _HEAD_RE.sub(" ", xhtml)
    cleaned = _STYLE_RE.sub(" ", cleaned)
    cleaned = _SCRIPT_RE.sub(" ", cleaned)
    cleaned = strip_ixbrl_non_visible_blocks(cleaned)
    cleaned = _BLOCK_BOUNDARY_RE.sub("\n", cleaned)
    text = unescape(_TAG_RE.sub("", cleaned))
    lines = (_INLINE_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    kept = [_escape_markdown_line_start(line) for line in lines if line]
    return "  \n".join(kept)


def _document_row(conn: sqlite3.Connection, company_number: str) -> sqlite3.Row | None:
    return conn.execute(
        "select document_id, transaction_id, metadata_url, xhtml_url, pdf_url, metadata_payload "
        "from documents where company_number=? and xhtml_url is not null "
        "order by rowid desc limit 1",
        (company_number,),
    ).fetchone()


def save_filing(conn: sqlite3.Connection, case: dict, api_key: str, dest_dir: Path) -> str:
    company_number = case["company_number"]
    row = _document_row(conn, company_number)
    if row is None:
        return "no_xhtml_doc"

    response = requests.get(
        f"https://document-api.company-information.service.gov.uk/document/{row['document_id']}/content",
        auth=(api_key, ""),
        headers={"Accept": "application/xhtml+xml"},
        timeout=60,
    )
    if response.status_code != 200:
        return f"http_{response.status_code}"

    dest_dir.mkdir(parents=True, exist_ok=True)
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
        "document_metadata": json.loads(row["metadata_payload"]) if row["metadata_payload"] else None,
        "fetched_from": f"https://document-api.company-information.service.gov.uk/document/{row['document_id']}/content",
    }
    (dest_dir / f"{company_number}.metadata.json").write_text(
        json.dumps(metadata, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--cases-dir", default="evals/business_profiles/cases")
    parser.add_argument("--dest-dir", default=str(DEST_DIR))
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    api_key = os.environ["COMPANIES_HOUSE_API_KEY"]
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    dest_dir = Path(args.dest_dir)

    results: dict[str, str] = {}
    for path in case_files(Path(args.cases_dir)):
        case = load_case(path)
        results[case["company_number"]] = save_filing(conn, case, api_key, dest_dir)

    ok = sum(1 for status in results.values() if status == "ok")
    print(json.dumps({"saved": ok, "total": len(results)}, indent=2))
    for company_number, status in results.items():
        if status != "ok":
            print(f"  {company_number}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
