from __future__ import annotations

import json
import sqlite3

from scripts.companies_house_enrichment.ch_backfill_document_paths import backfill


def test_links_only_exact_document_matches_and_fills_only_empty_paths(tmp_path):
    raw = tmp_path / "data/raw"
    (raw / "xhtml-accounts-for-text-recovery").mkdir(parents=True)
    (raw / "business-profile-filed-reports").mkdir()
    (raw / "search-screen-filings").mkdir()
    for name in ("docA", "docSet", "orphan"):
        (raw / "xhtml-accounts-for-text-recovery" / f"{name}.xhtml").write_text("x")
    (raw / "business-profile-filed-reports/0001.xhtml").write_text("x")
    (raw / "business-profile-filed-reports/0001.metadata.json").write_text(json.dumps({"document_id": "docB"}))
    (raw / "search-screen-filings/0002.xhtml").write_text("x")   # named by company: never linked

    conn = sqlite3.connect(":memory:")
    conn.execute("create table documents (document_id text primary key, company_number text, downloaded_xhtml_path text)")
    conn.executemany("insert into documents values (?, '1', ?)",
                     [("docA", None), ("docB", None), ("docSet", "already/here.xhtml"), ("docC", None)])

    assert backfill(conn, tmp_path, apply=False) == {
        "files_found": 4, "no_document_row": 1, "already_set": 1, "to_update": 2}
    assert conn.execute("select count(*) from documents where downloaded_xhtml_path is not null").fetchone()[0] == 1

    assert backfill(conn, tmp_path, apply=True)["updated"] == 2
    paths = dict(conn.execute("select document_id, downloaded_xhtml_path from documents"))
    assert paths["docA"] == "data/raw/xhtml-accounts-for-text-recovery/docA.xhtml"
    assert paths["docB"] == "data/raw/business-profile-filed-reports/0001.xhtml"
    assert paths["docSet"] == "already/here.xhtml" and paths["docC"] is None
