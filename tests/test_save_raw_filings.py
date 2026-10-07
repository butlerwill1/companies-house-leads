from scripts.business_profile_classifier.save_raw_filings import to_readable_markdown


def test_to_readable_markdown_breaks_paragraphs_onto_separate_lines():
    xhtml = "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
    assert to_readable_markdown(xhtml).split("\n") == ["First paragraph.  ", "Second paragraph."]


def test_to_readable_markdown_breaks_table_cells_onto_separate_lines():
    xhtml = "<table><tr><td>Turnover</td><td>13,391,763</td></tr></table>"
    assert to_readable_markdown(xhtml).split("\n") == ["Turnover  ", "13,391,763"]


def test_to_readable_markdown_drops_ixbrl_header_metadata():
    xhtml = (
        "<html><body>"
        '<ix:header xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        "<ix:hidden>context-and-unit-junk 2024-01-01</ix:hidden>"
        "</ix:header>"
        "<p>Principal activity was dispensing chemists.</p>"
        "</body></html>"
    )
    text = to_readable_markdown(xhtml)
    assert "context-and-unit-junk" not in text
    assert "Principal activity was dispensing chemists." in text


def test_to_readable_markdown_drops_head_style_and_script_blocks():
    xhtml = (
        "<html><head><style>.a{color:red}</style>"
        "<script>var x = 1;</script></head>"
        "<body><p>Visible text.</p></body></html>"
    )
    text = to_readable_markdown(xhtml)
    assert "color:red" not in text
    assert "var x" not in text
    assert text == "Visible text."


def test_to_readable_markdown_unescapes_entities_and_collapses_inline_whitespace():
    xhtml = "<p>Fish &amp;   chips</p>"
    assert to_readable_markdown(xhtml) == "Fish & chips"


def test_to_readable_markdown_drops_blank_lines():
    xhtml = "<div><p></p><p>Only this survives.</p><div>   </div></div>"
    assert to_readable_markdown(xhtml) == "Only this survives."


def test_to_readable_markdown_escapes_page_marker_bullets():
    # "- 1 -" is a page number in the filing, not a Markdown bullet.
    xhtml = "<div>- 1 -</div>"
    assert to_readable_markdown(xhtml) == "\\- 1 -"


def test_to_readable_markdown_escapes_numbered_clauses():
    xhtml = "<p>1. the nature of the industry and sector.</p>"
    assert to_readable_markdown(xhtml) == "1\\. the nature of the industry and sector."


def test_to_readable_markdown_escapes_atx_heading_and_blockquote_markers():
    xhtml = "<div># 5 Employees</div><p>&gt; carried forward</p>"
    assert to_readable_markdown(xhtml).split("\n") == ["\\# 5 Employees  ", "\\> carried forward"]


def test_to_readable_markdown_leaves_ordinary_dashes_and_numbers_untouched():
    xhtml = "<p>Turnover was up 12% year-on-year to 13,391,763.</p>"
    assert to_readable_markdown(xhtml) == "Turnover was up 12% year-on-year to 13,391,763."


def _db_with_two_filings() -> "sqlite3.Connection":
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table documents (document_id text, transaction_id text, company_number text,
            metadata_url text, xhtml_url text, pdf_url text, metadata_payload text);
        create table narrative_runs (id integer primary key, document_id text, company_number text);
        insert into documents values ('doc-2025', 't1', '06379728', null, 'x', null, null);
        insert into narrative_runs values (235, 'doc-2025', '06379728');
        -- the history backfill inserts older filings *after* the current one
        insert into documents values ('doc-2022', 't2', '06379728', null, 'x', null, null);
        """
    )
    return conn


def test_document_row_follows_the_case_narrative_run_not_the_newest_insert():
    from scripts.business_profile_classifier.save_raw_filings import _document_row

    conn = _db_with_two_filings()
    row = _document_row(conn, {"company_number": "06379728", "narrative_run_id": 235})
    assert row["document_id"] == "doc-2025"


def test_document_row_falls_back_to_the_newest_document_without_a_run():
    from scripts.business_profile_classifier.save_raw_filings import _document_row

    conn = _db_with_two_filings()
    row = _document_row(conn, {"company_number": "06379728", "narrative_run_id": None})
    assert row["document_id"] == "doc-2022"
    assert _document_row(conn, {"company_number": "00000000", "narrative_run_id": 999}) is None


def test_save_filing_downloads_the_pdf_and_reports_pdf_only_when_there_is_no_xhtml(monkeypatch, tmp_path):
    """A paper filing has a PDF resource and nothing else. It goes to the
    PDF folder for the transcription harness; no .xhtml or .md is written."""
    import json
    import sqlite3

    from scripts.business_profile_classifier import save_raw_filings as S

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table documents (document_id text, transaction_id text, company_number text,
            metadata_url text, xhtml_url text, pdf_url text, metadata_payload text);
        create table narrative_runs (id integer primary key, document_id text, company_number text);
        create table filings (transaction_id text, filing_date text);
        insert into documents values ('doc-pdf', 'tx-1', '08029548', null, null, 'https://x/pdf', null);
        insert into filings values ('tx-1', '2026-06-10');
        """
    )
    calls = []

    class _Resp:
        status_code = 200
        content = b"%PDF-1.4 fake"
        text = "not used"

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["headers"]["Accept"]))
        return _Resp()

    monkeypatch.setattr(S.requests, "get", fake_get)
    dest = tmp_path / "xhtml"
    pdf_dir = tmp_path / "pdf"

    status = S.save_filing(conn, {"company_number": "08029548", "company_name": "SMART", "narrative_run_id": None}, "key", dest, pdf_dir)

    assert status == "pdf_only"
    assert calls == [("https://document-api.company-information.service.gov.uk/document/doc-pdf/content", "application/pdf")]
    assert (pdf_dir / "08029548-2026-06-10.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert not (dest / "08029548.xhtml").exists() and not (dest / "08029548.md").exists()
    metadata = json.loads((dest / "08029548.metadata.json").read_text(encoding="utf-8"))
    assert metadata["document_id"] == "doc-pdf" and metadata["filing_date"] == "2026-06-10"
    assert metadata["pdf_path"].endswith("08029548-2026-06-10.pdf")


def test_readable_markdown_from_lines_escapes_and_joins_with_hard_breaks():
    from scripts.business_profile_classifier.save_raw_filings import readable_markdown_from_lines

    assert readable_markdown_from_lines(["# heading", "", "- 1 -", "plain"]) == "\# heading  \n\- 1 -  \nplain"
