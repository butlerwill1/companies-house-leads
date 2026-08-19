from __future__ import annotations

import sqlite3

from core.companies_house_extractor import CompaniesHouseExtractor, parse_financial_year
from core.companies_house_sqlite import init_db
from scripts.vlm.backfill_financial_years import unambiguous_candidate_years
from scripts.vlm.companies_house_pdf_vlm_financials import selected_metrics


def test_parse_financial_year_requires_an_explicit_four_digit_year() -> None:
    assert parse_financial_year("31 December 2025") == 2025
    assert parse_financial_year("2023/2024") == 2024
    assert parse_financial_year("current") is None
    assert parse_financial_year(2025) == 2025
    assert parse_financial_year(True) is None


def test_xhtml_accounts_attach_years_from_fact_contexts() -> None:
    xhtml = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="urn:core">
      <body>
        <xbrli:context id="C"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="F"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <ix:nonFraction name="core:ProfitLoss" contextRef="C">100</ix:nonFraction>
        <ix:nonFraction name="core:ProfitLoss" contextRef="F">90</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["financial_year"] == 2024
    assert result["years"]["previous"]["financial_year"] == 2023


def test_xhtml_years_do_not_depend_on_vendor_context_ids() -> None:
    xhtml = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="urn:core">
      <body>
        <xbrli:context id="CURRENT_FY"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="PREVIOUS_FY"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
        <ix:nonFraction name="core:ProfitLoss" contextRef="CURRENT_FY">100</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["financial_year"] == 2025
    assert result["years"]["previous"]["financial_year"] == 2024


def test_vlm_selected_metrics_retain_existing_column_years() -> None:
    candidates = [{
        "id": "p4-r0",
        "metric": "turnover",
        "page": 4,
        "unit": "GBP",
        "current_display": "1,234",
        "previous_display": "1,100",
        "current_column": "2025",
        "previous_column": "2024",
    }]
    rationalisation = {"financial_period_summaries": {
        "current": {"turnover": {"candidate_id": "p4-r0", "confidence": 0.9}},
        "previous": {"turnover": {"candidate_id": "p4-r0", "confidence": 0.9}},
    }}

    metrics = selected_metrics(candidates, rationalisation)

    assert [(metric["period_type"], metric["financial_year"]) for metric in metrics] == [
        ("current", 2025),
        ("previous", 2024),
    ]


def test_existing_database_gets_nullable_financial_year_columns() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    for table in (
        "financial_period_summaries",
        "vlm_financial_metrics",
    ):
        columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
        assert "financial_year" in columns


def test_vlm_backfill_accepts_only_unambiguous_saved_headings() -> None:
    assert unambiguous_candidate_years({"candidates": [{
        "current_column": "Year ended 2025",
        "previous_column": "2024",
    }]}) == {"current": 2025, "previous": 2024}
    assert unambiguous_candidate_years({"candidates": [
        {"current_column": "2025"},
        {"current_column": "2024"},
    ]}) == {}


def test_ixbrl_metrics_resolve_regardless_of_namespace_prefix() -> None:
    """Some filing agents tag facts with a company-specific namespace prefix
    (e.g. ns5:ProfitLoss) instead of the common core: taxonomy prefix, and use
    their own context ids instead of literal C/F/B/E. The resolver must match
    on the local element name and the context's real period year, not on a
    hardcoded prefix+context-ref string."""
    xhtml = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ns5="urn:vendor">
      <body>
        <xbrli:context id="FY_31_12_2024"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="FY_31_12_2023"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="cfwd_31_12_2024"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
        <xbrli:context id="cfwd_31_12_2023"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2023-12-31</xbrli:instant></xbrli:period></xbrli:context>
        <ix:nonFraction name="ns5:TurnoverRevenue" contextRef="FY_31_12_2024">352,538,885</ix:nonFraction>
        <ix:nonFraction name="ns5:TurnoverRevenue" contextRef="FY_31_12_2023">491,231,404</ix:nonFraction>
        <ix:nonFraction name="ns5:ProfitLoss" contextRef="FY_31_12_2024">116,143,613</ix:nonFraction>
        <ix:nonFraction name="ns5:ProfitLoss" contextRef="FY_31_12_2023">100</ix:nonFraction>
        <ix:nonFraction name="ns5:AverageNumberEmployeesDuringPeriod" contextRef="FY_31_12_2024">483</ix:nonFraction>
        <ix:nonFraction name="ns5:AverageNumberEmployeesDuringPeriod" contextRef="FY_31_12_2023">511</ix:nonFraction>
        <ix:nonFraction name="ns5:NetAssetsLiabilities" contextRef="cfwd_31_12_2024">16,149,652</ix:nonFraction>
        <ix:nonFraction name="ns5:NetAssetsLiabilities" contextRef="cfwd_31_12_2023">425,006,039</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    current = result["years"]["current"]
    previous = result["years"]["previous"]
    assert current["turnover"] == 352538885
    assert previous["turnover"] == 491231404
    assert current["profit_after_tax"] == 116143613
    assert previous["profit_after_tax"] == 100
    assert current["employees"] == 483
    assert previous["employees"] == 511
    assert current["net_assets"] == 16149652
    assert previous["net_assets"] == 425006039


def test_ixbrl_metrics_ignore_dimensional_segment_contexts() -> None:
    """A concept can be tagged twice for the same year: once on the whole-entity
    context (the real total) and once on a context carrying an xbrli:segment
    dimension (a component of a segmental breakdown note). Only the
    non-dimensional context is a valid total."""
    xhtml = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
        xmlns:ns5="urn:vendor">
      <body>
        <xbrli:context id="FY_31_12_2024"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="Segment_FY_31_12_2024"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="ns5:OperatingSegmentsDimension">ns5:Segment1</xbrldi:explicitMember></xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <ix:nonFraction name="ns5:TurnoverRevenue" contextRef="FY_31_12_2024">352,538,885</ix:nonFraction>
        <ix:nonFraction name="ns5:TurnoverRevenue" contextRef="Segment_FY_31_12_2024">120,000,000</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] == 352538885


def _turnover_row_html(*values: str, next_label: str = "Cost of sales") -> str:
    cells = "".join(f'<div class="crn fn1">{v}</div>' for v in values)
    return (
        '<html><body><div class="clb fn1">Turnover</div>'
        f"{cells}"
        f'<div class="cln fn1">{next_label}</div><div class="crn fn1">1</div><div class="crn fn1">1</div>'
        "</body></html>"
    )


def test_visible_row_reads_current_and_previous_from_a_plain_two_column_row() -> None:
    xhtml = _turnover_row_html("1,000", "900")

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] == 1000
    assert result["years"]["previous"]["turnover"] == 900


def test_visible_row_picks_the_total_column_from_a_continuing_discontinued_split() -> None:
    """A statement of comprehensive income that separately discloses
    continuing/discontinued operations renders six number cells per row
    (continuing, discontinued, total, per year x2). Naively taking the first
    two cells reads a discontinued-operations sub-total as if it were the
    prior year's total, silently corrupting the figure."""
    xhtml = _turnover_row_html(
        "335,217,225", "17,321,660", "352,538,885",
        "469,322,023", "21,909,381", "491,231,404",
    )

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] == 352538885
    assert result["years"]["previous"]["turnover"] == 491231404


def test_visible_row_declines_to_guess_on_an_ambiguous_column_count() -> None:
    xhtml = _turnover_row_html("1,000", "900", "800", "700")

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] is None
    assert result["years"]["previous"]["turnover"] is None


def test_visible_row_keeps_a_nil_dash_cell_positional() -> None:
    """A "-" cell means nil, not "no second column" — dropping it from the
    list before assigning current/previous would shift the real value into
    the wrong slot."""
    xhtml = _turnover_row_html("2,500,000", "-")

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] == 2500000
    assert result["years"]["previous"]["turnover"] is None


def test_visible_row_stops_at_a_justified_label_not_just_left_aligned_ones() -> None:
    """Some templates render a sub-heading row (e.g. a "highlights" summary)
    with a "cjn" (justified) label class rather than "clb"/"cln". Failing to
    recognise it as a row boundary lets the window swallow unrelated rows
    below, corrupting the column count."""
    xhtml = (
        '<html><body><div class="clb fn1">Turnover</div>'
        '<div class="crn fn1">1,000</div><div class="crn fn1">900</div>'
        '<div class="cjn fn1">Turnover margin</div>'
        '<div class="crn fn1">10%</div><div class="crn fn1">9%</div>'
        "</body></html>"
    )

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] == 1000
    assert result["years"]["previous"]["turnover"] == 900


def test_visible_row_reads_a_bold_current_year_column() -> None:
    """Some templates bold the current-year column ("crb fn1") while the
    comparative stays normal weight ("crn fn1")."""
    xhtml = (
        '<html><body><div class="clb fn1">Turnover</div>'
        '<div class="crb fn1">4,205,342</div><div class="crn fn1">5,315,498</div>'
        '<div class="cln fn1">Cost of sales</div>'
        '<div class="crb fn1">1</div><div class="crn fn1">1</div>'
        "</body></html>"
    )

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["turnover"] == 4205342
    assert result["years"]["previous"]["turnover"] == 5315498


def test_visible_row_ignores_bare_year_headers_in_a_notes_subheading() -> None:
    """A notes sub-heading can reuse a metric's row label (e.g. "Operating
    profit" introducing a note on what it includes) followed by bare-year
    column headers rendered in the same bold style as a real value cell.
    Those headers must not be read as the figure itself."""
    xhtml = (
        '<html><body><div class="clb fn1">Operating profit</div>'
        '<div class="crb fn1">2024</div><div class="crb fn1">2023</div>'
        '<div class="cln fn1">Operating profit for the year is stated after charging:</div>'
        "</body></html>"
    )

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["operating_result"] is None
    assert result["years"]["previous"]["operating_result"] is None


def _context(context_id: str, start: str, end: str) -> str:
    return (
        f'<xbrli:context id="{context_id}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>'
        f"<xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def test_ixbrl_metrics_match_period_end_date_not_calendar_year() -> None:
    """A company with a shifted accounting reference date can have its
    current and comparative periods both end in the same calendar year
    (e.g. current ends 2024-12-31, comparative ends 2024-03-31). Matching by
    calendar year alone would treat both contexts as "year 2024" and make
    the metric ambiguous; matching by exact period end date does not."""
    xhtml = f"""<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ns5="urn:vendor">
      <body>
        {_context("C", "2024-04-01", "2024-12-31")}
        {_context("F", "2023-04-01", "2024-03-31")}
        <ix:nonFraction name="ns5:ProfitLoss" contextRef="C">1151524</ix:nonFraction>
        <ix:nonFraction name="ns5:ProfitLoss" contextRef="F">2748178</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["profit_after_tax"] == 1151524
    assert result["years"]["previous"]["profit_after_tax"] == 2748178


def test_ixbrl_metrics_decline_when_synonym_concepts_disagree() -> None:
    """Net assets can be tagged under more than one taxonomy concept
    (NetAssetsLiabilities, Equity). When they resolve to different values —
    e.g. a filer that tagged a parenthesised negative figure without the
    sign attribute on one of the two — that is a genuine conflict in the
    source filing, not something to guess through."""
    xhtml = f"""<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="urn:core">
      <body>
        {_context("B", "2024-01-01", "2024-12-31")}
        <ix:nonFraction name="core:NetAssetsLiabilities" contextRef="B">79000</ix:nonFraction>
        <ix:nonFraction name="core:Equity" contextRef="B" sign="-">79000</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["net_assets"] is None


def _employees_xhtml(displayed: str, scale_attr: str) -> str:
    return f"""<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="urn:core">
      <body>
        {_context("C", "2024-01-01", "2024-12-31")}
        <ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" contextRef="C"
            unitRef="Pure" decimals="2" scale="{scale_attr}">{displayed}</ix:nonFraction>
      </body></html>"""


def test_employee_count_ignores_a_negative_scale_tagging_error() -> None:
    """Some filers render a headcount of 483 but tag it scale="-2", which
    would make it 4.83 employees at a company with hundreds of staff. A
    headcount is never legitimately reported in hundredths of a person, so
    the rendered figure wins."""
    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(_employees_xhtml("483", "-2"))

    assert result["years"]["current"]["employees"] == 483


def test_employee_count_keeps_a_genuinely_fractional_average() -> None:
    """An average headcount really can be fractional (part-time
    equivalents). Stripping the decimal point the way the monetary path
    does would turn 2.5 employees into 25."""
    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(_employees_xhtml("2.5", "0"))

    assert result["years"]["current"]["employees"] == 2.5


def test_employee_count_still_honours_a_positive_scale() -> None:
    """Reporting headcount in thousands is unusual but legitimate, and
    unlike a negative scale it is not a tagging error."""
    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(_employees_xhtml("12", "3"))

    assert result["years"]["current"]["employees"] == 12000


def test_monetary_values_are_unaffected_by_the_count_concept_handling() -> None:
    xhtml = f"""<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="urn:core">
      <body>
        {_context("C", "2024-01-01", "2024-12-31")}
        <ix:nonFraction name="core:ProfitLoss" contextRef="C" scale="3">1,234</ix:nonFraction>
      </body></html>"""

    result = CompaniesHouseExtractor(api_key=None).parse_xhtml_accounts(xhtml)

    assert result["years"]["current"]["profit_after_tax"] == 1234000
