from __future__ import annotations

from core.companies_house_extractor import parse_xhtml_narrative, strip_ixbrl_non_visible_blocks
from core.companies_house_pdf_text import MAX_SECTION_CHARS, extract_sections


def test_ixbrl_header_block_is_stripped_before_text_extraction() -> None:
    """The <ix:header> block holds context definitions, units and hidden
    facts. Stripping tags without removing it first leaves its text content
    behind, so a section reads "principal activity ... 07554163
    bus:Director2 2024-01-01" instead of prose. This affected roughly 17% of
    principal_activity rows."""
    markup = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
      <body>
        <ix:header>
          <ix:hidden><ix:nonNumeric name="bus:Director1">Ranald Allan</ix:nonNumeric></ix:hidden>
          <ix:resources>
            <xbrli:context id="C_OO_OP"><xbrli:identifier>07554163</xbrli:identifier>
              <xbrldi:explicitMember dimension="bus:EntityOfficersDimension">bus:Director2</xbrldi:explicitMember>
            </xbrli:context>
          </ix:resources>
        </ix:header>
        <div>The principal activity during the year was the provision of optical goods and services.</div>
      </body></html>"""

    result = parse_xhtml_narrative(markup)
    activity = result["sections"]["principal_activity"]["text"]

    assert "optical goods and services" in activity
    assert "bus:Director2" not in activity
    assert "Ranald Allan" not in activity
    assert "07554163" not in activity


def test_stripping_handles_whatever_namespace_prefix_the_filer_used() -> None:
    markup = '<html><body><foo:header><foo:hidden>junk text</foo:hidden></foo:header><p>real prose</p></body></html>'

    cleaned = strip_ixbrl_non_visible_blocks(markup)

    assert "junk text" not in cleaned
    assert "real prose" in cleaned


def test_last_section_in_a_document_cannot_swallow_the_remainder() -> None:
    """A section runs to the next heading, but the final heading has none --
    so it would otherwise capture everything to the end of the document.
    going_concern recurs late in accounting policies, which is how it ended
    up averaging thousands of words."""
    tail = "filler sentence about accounting policies. " * 900
    pages = [f"Going concern The directors have a reasonable expectation. {tail}"]

    sections = extract_sections(pages)

    assert len(sections["going_concern"]["text"]) <= MAX_SECTION_CHARS


def test_company_authored_section_is_preferred_over_the_auditors_wording() -> None:
    """An auditor's report quotes the same headings the company uses. The
    company's own account of its risks must win over the auditor's
    description of its audit procedures."""
    pages = [
        "Independent auditor's report. In our opinion the financial statements give a true and fair view. "
        "The principal risks related to posting inappropriate journal entries to revenue. "
        "Audit procedures performed by the engagement team included inspecting correspondence. "
        "Strategic report "
        "Principal risks and uncertainties Property occupancy costs. As a high street retailer, "
        "occupancy costs and rent reviews are the main risk facing the business."
    ]

    sections = extract_sections(pages)

    assert "high street retailer" in sections["principal_risks"]["text"]
    assert sections["principal_risks"]["is_auditor_text"] is False


def test_auditor_wording_is_kept_but_flagged_when_it_is_all_there_is() -> None:
    """Some filings only ever mention a heading inside the auditor's report.
    Dropping it loses information; using it silently misleads. Keep it and
    mark it so the caller can tell the difference."""
    pages = [
        "Independent auditor's report. We have audited the financial statements. "
        "The principal risks were related to management bias in accounting estimates."
    ]

    sections = extract_sections(pages)

    assert sections["principal_risks"]["is_auditor_text"] is True


def test_a_bare_heading_does_not_beat_a_real_section() -> None:
    """Contents-page entries match the same patterns as real headings."""
    pages = [
        "Business review 3 "
        "Strategic report "
        "Business review The company grew revenue across its retail estate during the period "
        "and opened two further sites in the year under review."
    ]

    sections = extract_sections(pages)

    assert "grew revenue across its retail estate" in sections["business_review"]["text"]


def test_turnover_note_is_extracted_from_its_distinctive_opening_phrase() -> None:
    """geography_served and customer_type often turn on this note, not the
    qualitative narrative -- anchored to phrasing distinctive of the actual
    note, not the word "turnover" alone, which recurs constantly in KPI
    prose elsewhere in a filing."""
    pages = [
        "Strategic report Turnover for the year was up 12% on last year, driven by strong demand. "
        "3 Turnover Turnover analysed by class of business Pharmacy sales 13,391,763 "
        "4 Operating loss Operating loss for the period is stated after charging: Depreciation 49,958"
    ]

    sections = extract_sections(pages)

    assert "Pharmacy sales 13,391,763" in sections["turnover_note"]["text"]
    # The unrelated KPI mention in the strategic report must not itself
    # anchor a match -- only the note's own opening phrasing should.
    assert "up 12% on last year" not in sections["turnover_note"]["text"]


def test_employee_note_is_extracted_from_its_standard_opening_phrase() -> None:
    pages = [
        "Principal risks The group monitors headcount closely. "
        "6 Employees The average monthly number of persons (including directors) employed by "
        "the group and company during the period was: Pharmacy 116 Management 20 Total 139"
    ]

    sections = extract_sections(pages)

    assert "Pharmacy 116" in sections["employee_note"]["text"]
