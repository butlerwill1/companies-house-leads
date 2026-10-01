from __future__ import annotations

import pytest

from core.companies_house_extractor import filed_report_text, parse_xhtml_narrative, strip_ixbrl_non_visible_blocks
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


def test_a_contents_page_mention_of_the_auditors_report_does_not_taint_real_content() -> None:
    """"Independent auditor's report" is a plain line in the table of
    contents of every filing on hand, sitting right alongside "Strategic
    report" and "Directors' report" near the very start of the document --
    unlike every other auditor-boilerplate phrase, it also legitimately
    names the real heading, so treating it as evidence the following text is
    "inside the auditor's report" wrongly flags the real strategic report
    that starts moments later (confirmed on 55 of 58 filed reports on hand,
    gold set company 00485994 among them)."""
    pages = [
        "Contents Page Strategic report 1 Directors' report 2 Independent auditor's report 5 "
        "Strategic report For the year ended 31 March 2025 "
        "The directors present the strategic report for the year ended 31 March 2025. "
        "Review of the business "
        "The year ended 31 March 2025 was a successful year for the group. Turnover amounted to "
        "10.2 million with a gross margin of 73%. "
        "Principal risks and uncertainties The group has significant exposure to raw material prices."
    ]

    sections = extract_sections(pages)

    assert "successful year for the group" in sections["strategic_report"]["text"]
    assert sections["strategic_report"]["is_auditor_text"] is False


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


def test_a_heading_recurring_in_its_own_body_prose_does_not_fragment_the_section() -> None:
    """Filings routinely restate a heading's own words in the sentence right
    after it -- "Principal activities... The principal activity of the
    company was that of a holding company" is the boilerplate pair used to
    separate group activity from parent-company activity, and it is exactly
    the sentence trading_status_confirmed exists to read. Naive next-heading
    matching mistook the second mention for a new section boundary and
    fragmented the real one, sometimes keeping the fragment that drops the
    decisive sentence (a real case, gold set company 10723179)."""
    pages = [
        "Principal activities "
        "The principal activity of the group continued to be that of conducting and analysing surveys. "
        "The principal activity of the company was that of a holding company. "
        "Results and dividends "
        "The results for the year are set out on page 10."
    ]

    sections = extract_sections(pages)

    assert "holding company" in sections["principal_activity"]["text"]
    assert "conducting and analysing surveys" in sections["principal_activity"]["text"]


def test_a_bare_heading_separated_by_a_different_heading_still_splits() -> None:
    """The fix above must not swallow a genuinely separate bare heading and
    its real content into one candidate just because they share a key --
    only a same-key repeat with nothing else in between is a self-reference.
    Here a different heading (Strategic report) sits between the bare
    "Business review" TOC entry and its real section, so both must still
    become distinct candidates -- this is the same fixture as
    test_a_bare_heading_does_not_beat_a_real_section, re-asserted here to
    document why the self-reference fix does not touch it."""
    pages = [
        "Business review 3 "
        "Strategic report "
        "Business review The company grew revenue across its retail estate during the period "
        "and opened two further sites in the year under review."
    ]

    sections = extract_sections(pages)

    assert "grew revenue across its retail estate" in sections["business_review"]["text"]
    assert sections["business_review"]["text"] != "Business review 3"


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


def test_a_real_heading_beats_the_same_phrase_inside_a_sentence() -> None:
    """The bug that hid 11168409 ZIRCON GROUP's customer type.

    Filings put a boilerplate cross-reference in the accounting-policy notes
    -- "The company's principal activities ... are disclosed in the Director's
    Report" -- which contains the heading phrase mid-sentence and then runs on
    into pages of policy text. Under a pure longest-wins tie-break that
    fragment beat the real Principal-activities section, whose content is a
    single sentence. Block structure is what separates them: the real heading
    is rendered as its own block, so it lands on a line of its own.
    """
    markup = (
        "<html><body>"
        "<div>Principal activities</div>"
        "<p>The principal activity of the company continued to be that of "
        "providing bridging loans to individuals and corporate entities.</p>"
        "<div>Results and dividends</div><p>The results are set out on page 9.</p>"
        "<div>Notes to the financial statements</div>"
        "<p>The company's principal activities and nature of its operations are "
        "disclosed in the Director's Report.</p>"
        "<p>1.1 Accounting convention. " + "Filler accounting policy prose. " * 60 + "</p>"
        "</body></html>"
    )

    activity = parse_xhtml_narrative(markup)["sections"]["principal_activity"]["text"]

    assert "individuals and corporate entities" in activity
    assert "Accounting convention" not in activity


def test_an_activity_sentence_with_no_heading_above_it_is_still_found() -> None:
    """The preference for a real heading must not become a requirement:
    plenty of filings state the activity in prose with no heading at all."""
    markup = (
        "<html><body><div>The principal activity during the year was the "
        "provision of optical goods and services.</div></body></html>"
    )

    activity = parse_xhtml_narrative(markup)["sections"]["principal_activity"]["text"]

    assert "optical goods and services" in activity


def test_ixbrl_tagged_activity_is_recovered_when_the_heading_scan_misses_it() -> None:
    """The filing tags the real sentence with bus:DescriptionPrincipalActivities.
    When the heading scan lands elsewhere, that tag is the recovery path."""
    markup = (
        "<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><body>"
        "<p><ix:nonNumeric name='bus:DescriptionPrincipalActivities'>"
        "The principal activity of the group was that of a licensed restaurant."
        "</ix:nonNumeric></p>"
        "<div>Principal activities</div>"
        "<p>are stated gross of credit card commission and excluding VAT. "
        + "Revenue recognition policy prose. " * 40 + "</p>"
        "</body></html>"
    )

    activity = parse_xhtml_narrative(markup)["sections"]["principal_activity"]["text"]

    assert "licensed restaurant" in activity


def test_ixbrl_continuation_is_followed_so_the_value_is_not_truncated() -> None:
    """iXBRL splits a long tagged value across ix:continuation elements via
    continuedAt. Reading only the first element truncated 01185592 ARDMORE at
    "...continued to be that of", dropping what the company actually does."""
    markup = (
        "<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><body>"
        "<p><ix:nonNumeric continuedAt='C0' name='bus:DescriptionPrincipalActivities'>"
        "The principal activity of the company continued to be that of "
        "</ix:nonNumeric>"
        "<ix:continuation id='C0'>main contractor for the construction of "
        "residential and commercial developments in the UK.</ix:continuation></p>"
        "</body></html>"
    )

    activity = parse_xhtml_narrative(markup)["sections"]["principal_activity"]["text"]

    assert "main contractor for the construction" in activity
    assert "developments in the UK" in activity


def test_blank_principal_activity_placeholder_is_not_treated_as_a_description() -> None:
    """Filing software writes this literal string when the preparer left the
    field empty (10622184 PENKETH, 06995506 SIZE GROUP). It must not displace
    whatever the heading scan found."""
    markup = (
        "<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><body>"
        "<p><ix:nonNumeric name='bus:DescriptionPrincipalActivities'>"
        "No description of principal activity</ix:nonNumeric></p>"
        "<div>Principal activities</div>"
        "<p>The principal activity of the group is civil engineering.</p>"
        "</body></html>"
    )

    activity = parse_xhtml_narrative(markup)["sections"]["principal_activity"]["text"]

    assert "civil engineering" in activity
    assert "No description of principal activity" not in activity


def _filing(heading: str) -> str:
    """A filing skeleton: contents page, directors' report, an audit report
    under `heading`, then the primary statements and notes."""
    return (
        "<html><body>"
        "<p>Contents</p><p>Directors' report 1</p><p>{h} 2</p><p>Statement of comprehensive income 4</p>"
        "<p>Directors' report</p><p>The principal activity of the company is the sale of widgets.</p>"
        "<p>{h}</p><p>To the members of Widgets Limited</p>"
        "<p>We conducted our audit in accordance with ISAs (UK). We have nothing to report in "
        "respect of irregularities, including fraud, or posting inappropriate journal entries.</p>"
        "<p>Statement of comprehensive income</p><p>Turnover 1,000</p>"
        "<p>Notes to the financial statements</p><p>All turnover arises in the United Kingdom.</p>"
        "</body></html>"
    ).format(h=heading)


@pytest.mark.parametrize(
    "heading",
    ["Independent auditor's report", "INDEPENDENT AUDITORS' REPORT", "Independent auditors report"],
)
def test_filed_report_text_drops_the_audit_report_under_every_heading_spelling(heading: str) -> None:
    """One firm writes "auditor's", the partnerships write "auditors'", and
    tag-stripping sometimes loses the apostrophe altogether. Until 2026-09-12
    only the first was matched, and SC190800 (PwC) went to the model with
    its whole audit report -- the one block of text whose account of the
    business is not the company's own."""
    text = filed_report_text(_filing(heading))

    assert "sale of widgets" in text
    assert "All turnover arises in the United Kingdom" in text
    assert "Turnover 1,000" in text
    assert "ISAs (UK)" not in text
    assert "inappropriate journal entries" not in text
    assert "To the members of Widgets Limited" not in text


def test_filed_report_text_survives_the_contents_page_mention() -> None:
    """The contents page names the audit report too. Treating that line as
    the start of the report must not swallow the directors' report that
    follows it -- the strip has to end at the next primary-statement line,
    which on a contents page is the very next entry."""
    text = filed_report_text(_filing("Independent auditor's report"))
    assert "sale of widgets" in text


def test_filed_report_text_is_strip_auditor_report_over_block_text() -> None:
    """The XHTML path and the scanned-PDF transcription path share one
    auditor-stripping rule; the XHTML path is exactly tag-stripping followed
    by that rule, so the two can never drift."""
    from core.companies_house_extractor import strip_auditor_report, strip_tags_preserving_blocks

    xhtml = _filing("Independent auditor's report")
    assert filed_report_text(xhtml) == strip_auditor_report(strip_tags_preserving_blocks(xhtml))


@pytest.mark.parametrize(
    "heading",
    ["Independent auditor's report", "INDEPENDENT AUDITORS' REPORT", "Independent auditors report"],
)
def test_strip_auditor_report_on_plain_text_spans_continued_pages(heading: str) -> None:
    """Plain text straight from a page transcription: the report runs over
    two pages with a 'continued' running header, and the strip must run
    through to the first primary statement regardless."""
    from core.companies_house_extractor import strip_auditor_report

    text = "\n".join([
        "Contents",
        "Directors' report | 1",
        f"{heading} | 2",
        "Statement of comprehensive income | 4",
        "Directors' report",
        "The principal activity of the company is the sale of widgets.",
        heading,
        "To the members of Widgets Limited",
        "We conducted our audit in accordance with ISAs (UK).",
        f"{heading} (continued)",
        "We have nothing to report in respect of irregularities, including fraud.",
        "Statement of comprehensive income",
        "Turnover | 1,000",
        "Notes to the financial statements",
        "All turnover arises in the United Kingdom.",
    ])
    out = strip_auditor_report(text)
    assert "sale of widgets" in out
    assert "Turnover | 1,000" in out
    assert "All turnover arises" in out
    assert "ISAs (UK)" not in out
    assert "irregularities" not in out


def test_inline_tags_do_not_split_words_when_flattening() -> None:
    """Accounts software wraps letters and word fragments in adjacent spans
    ("<span>T</span><span>he</span>"). Those are not word boundaries: over
    the 108 cached filings every adjacent-span join was mid-word, and
    treating them as spaces put "T he company" into 94 of 109 gold texts,
    so a model quoting the sentence correctly failed the verbatim check."""
    from core.companies_house_extractor import strip_tags_preserving_blocks

    markup = (
        "<p><span class='a'>T</span><span class='a'>he</span> <span>compan</span><span>ies</span> "
        "grew <b>turnover</b> to <ix:nonNumeric name='x'>1,000</ix:nonNumeric>.</p>"
        "<p>Next<span> paragraph</span></p><table><tr><td>Turnover</td><td>1,000</td></tr></table>"
    )
    assert strip_tags_preserving_blocks(markup) == "The companies grew turnover to 1,000.\nNext paragraph\nTurnover\n1,000"
