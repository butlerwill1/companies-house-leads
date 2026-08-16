from __future__ import annotations

import sqlite3
from decimal import Decimal

from core.companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.analysis.enrich_financial_fx import convert_pending, import_rates
from scripts.vlm.companies_house_pdf_vlm_financials import reported_value, selected_metrics, to_pence


def test_reported_value_preserves_currency_scale_and_never_assigns_usd_pence() -> None:
    assert reported_value("(1.25)", "USD_THOUSANDS", "turnover") == Decimal("-1250.00")
    assert reported_value("-", "EUR_MILLIONS", "cash") == Decimal("0")
    assert to_pence("15,073,418", "USD", "turnover") is None
    assert to_pence("1.25", "GBP_THOUSANDS", "turnover") == 125_000


def test_selected_usd_metric_keeps_reported_value() -> None:
    candidates = [{"id": "one", "metric": "turnover", "unit": "USD", "current_display": "15,073,418", "previous_display": None, "current_column": "2024", "previous_column": None, "page": 13, "confidence": 1.0}]
    result = selected_metrics(candidates, {"financial_period_summaries": {"current": {"turnover": {"candidate_id": "one", "confidence": 1.0}}, "previous": {}}})[0]
    assert result["currency_code"] == "USD"
    assert result["reported_value"] == "15073418"
    assert result["value_pence"] is None


def test_sqlite_persists_usd_reported_value_without_treating_it_as_gbp() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    payload = {
        "pdf_path": "usd-accounts.pdf",
        "status": "complete",
        "models": {"locator": "locator", "vision": "vision", "rationalisation": "review"},
        "metrics": [{
            "period_type": "current",
            "metric_name": "turnover",
            "value_pence": None,
            "value_count": None,
            "displayed_value": "15,073,418",
            "unit": "USD",
            "currency_code": "USD",
            "scale_multiplier": 1,
            "reported_value": "15073418",
            "source_page": 13,
            "validation": {"unit_known": True},
        }],
    }

    run_id = insert_vlm_financial_payload(conn, payload, "14527692", "document")
    metric = conn.execute(
        """select currency_code, scale_multiplier, reported_value, value_pence
           from vlm_financial_metrics where extraction_run_id=?""",
        (run_id,),
    ).fetchone()
    summary = conn.execute(
        """select currency_code, currency_validation_status, turnover_reported_value
           from financial_period_summaries
           where company_number='14527692' and document_id='document' and period_type='current'"""
    ).fetchone()

    assert metric == ("USD", 1, "15073418", None)
    assert summary == ("USD", "valid", "15073418")


def test_fx_uses_prior_rate_and_decimal_half_even_rounding() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("""insert into financial_period_summaries (company_number,period_type,raw_payload,data_source,currency_code,currency_source,period_end_on,currency_validation_status,turnover,turnover_reported_value)
                    values ('14527692','current','{}','vlm','USD','vlm_statement','2024-01-08','valid',15073418,'15073418')""")
    payload = b"DATE,XUDLGBD\n05 Jan 2024,2\n"
    assert import_rates(conn, "USD", "XUDLGBD", payload, "https://example.test/boe") == 1
    assert import_rates(conn, "USD", "XUDLGBD", payload, "https://example.test/boe") == 0
    assert convert_pending(conn) == 1
    converted = conn.execute("select conversion_status, turnover_gbp_pence from financial_period_conversions").fetchone()
    assert tuple(converted) == ("converted", 753670900)
