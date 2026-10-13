#!/usr/bin/env python3
"""
Record where already-downloaded accounts XHTML sits on disk, in documents.downloaded_xhtml_path.

The raw filings were saved under data/raw/ without the documents table being told,
so every downloaded_*_path column is empty. This links the files that can be matched
to a document row exactly, and nothing else:

    data/raw/xhtml-accounts-for-text-recovery/<document_id>.xhtml
    data/raw/business-profile-filed-reports/<company>.xhtml, via <company>.metadata.json's document_id

Not linked, on purpose: data/raw/search-screen-filings/<company>.xhtml (named by company,
not by document, so the document is ambiguous) and data/raw/pdf-only-accounts-history/*.pdf
(those filings have no documents row). Only empty columns are filled; paths are stored
relative to the repository root. Dry run unless --apply is given. No network, no model.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW = Path("data/raw")


def find_xhtml_files(root: Path) -> dict[str, Path]:
    """Map document_id -> XHTML path under root, for the layouts that name the document exactly."""
    found: dict[str, Path] = {}
    recovery = root / RAW / "xhtml-accounts-for-text-recovery"
    if recovery.is_dir():
        for path in recovery.glob("*.xhtml"):
            found[path.stem] = path
    reports = root / RAW / "business-profile-filed-reports"
    if reports.is_dir():
        for meta in reports.glob("*.metadata.json"):
            xhtml = meta.with_name(meta.name.removesuffix(".metadata.json") + ".xhtml")
            try:
                document_id = json.loads(meta.read_text(encoding="utf-8")).get("document_id")
            except (OSError, ValueError):
                continue
            if document_id and xhtml.is_file():
                found.setdefault(document_id, xhtml)
    return found


def backfill(conn: sqlite3.Connection, root: Path, *, apply: bool) -> dict[str, int]:
    files = find_xhtml_files(root)
    known = {row[0]: row[1] for row in conn.execute("select document_id, downloaded_xhtml_path from documents")}
    updates = [(path.relative_to(root).as_posix(), document_id)
               for document_id, path in files.items()
               if document_id in known and not known[document_id]]
    if apply and updates:
        with conn:
            conn.executemany("update documents set downloaded_xhtml_path = ? "
                             "where document_id = ? and downloaded_xhtml_path is null", [(p, d) for p, d in updates])
    return {"files_found": len(files), "no_document_row": sum(d not in known for d in files),
            "already_set": sum(bool(known.get(d)) for d in files),
            "to_update" if not apply else "updated": len(updates)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(REPO_ROOT / "companies-house.db"))
    parser.add_argument("--apply", action="store_true", help="Write the paths (default is a dry run).")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db, timeout=30)
    try:
        print(json.dumps(backfill(conn, REPO_ROOT, apply=args.apply), indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
