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
        "ocr_financial_period_summaries",
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
