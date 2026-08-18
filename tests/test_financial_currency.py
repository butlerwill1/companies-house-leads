from __future__ import annotations

import sqlite3
from decimal import Decimal

from core.companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.analysis.enrich_financial_fx import convert_pending, import_rates
from scripts.vlm.companies_house_pdf_vlm_financials import reported_value, selected_metrics, to_pence
from scripts.vlm.financial_metric_policy import add_canonical_equivalents


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


def test_ebitda_style_subtotal_is_not_a_compatible_operating_result_label() -> None:
    """An EBITDA-style subtotal must not be accepted as the bottom-line operating result.

    Observed on a real filing: the vision model transcribed only "Operating
    profit before non-recurring items, amortisation and depreciation"
    (8,012,041) and never the true bottom line "Group operating loss"
    (-1,993,390), several lines further down the same statement. The plain
    prefix match accepted the subtotal because it also starts with "operating
    profit".
    """
    from scripts.vlm.financial_metric_policy import canonical_metric_label_is_compatible

    assert canonical_metric_label_is_compatible(
        "operating_result", "Operating profit before non-recurring items, amortisation and depreciation"
    ) is False
    assert canonical_metric_label_is_compatible("operating_result", "Operating profit") is True
    assert canonical_metric_label_is_compatible("operating_result", "Group operating loss") is True
    # The two legitimate prefixes containing "before" as part of the label
    # itself must still be accepted.
    assert canonical_metric_label_is_compatible(
        "operating_result", "Profit on ordinary activities before interest"
    ) is True


def test_operating_result_prefers_the_bottom_line_over_an_ebitda_subtotal() -> None:
    candidates = [
        {
            "id": "p14-r1", "metric": "operating_result", "page": 14,
            "statement_type": "income_statement", "statement_scope": "consolidated_group",
            "unit": "GBP", "source_label": "Operating profit before non-recurring items, amortisation and depreciation",
            "current_display": "8,012,041", "previous_display": "7,170,324", "confidence": 0.95,
        },
        {
            "id": "p14-r2", "metric": "operating_result", "page": 14,
            "statement_type": "income_statement", "statement_scope": "consolidated_group",
            "unit": "GBP", "source_label": "Group operating loss",
            "current_display": "(1,993,390)", "previous_display": "(2,043,604)", "confidence": 0.9,
        },
    ]

    result = add_canonical_equivalents(candidates)
    operating = [c for c in result if c.get("metric") == "operating_result" and c.get("current_display")]

    assert [c["id"] for c in operating] == ["p14-r2"]


def test_operating_result_prefix_allows_a_leading_scope_word() -> None:
    """"Group operating loss" / "Company operating profit" are common bottom-line labels."""
    from scripts.vlm.financial_metric_policy import canonical_metric_label_is_compatible

    assert canonical_metric_label_is_compatible("operating_result", "Group operating loss") is True
    assert canonical_metric_label_is_compatible("operating_result", "Company operating profit") is True
    assert canonical_metric_label_is_compatible("operating_result", "Consolidated operating result") is True
    # A scope word must not rescue an excluded qualified subtotal.
    assert canonical_metric_label_is_compatible(
        "operating_result", "Group operating profit before exceptional items"
    ) is False


def test_profit_before_tax_evidence_requires_a_before_tax_label() -> None:
    """A mistagged component row must not tie with the real profit-before-tax row.

    Observed on a real filing: a duplicate/garbled page mistagged "Other
    operating expenses" and "Interest income" as profit_before_tax. Because
    that branch previously granted tier 3 from statement_type alone with no
    label check, both tied with the correct row and self-reported confidence
    picked the wrong one, silently corrupting the derived operating_result.
    """
    candidates = [
        {
            "id": "p13-r7", "metric": "profit_before_tax", "page": 13,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "USD", "source_label": "Profit on ordinary activities before taxation",
            "current_display": "586,575", "previous_display": "221,357", "confidence": 0.9,
        },
        {
            "id": "p20-r5", "metric": "profit_before_tax", "page": 20,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "USD", "source_label": "Other operating expenses",
            "current_display": "12,952,802", "previous_display": "4,427,146", "confidence": 0.99,
        },
        {
            "id": "p20-r6", "metric": "profit_before_tax", "page": 20,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "USD", "source_label": "Interest income",
            "current_display": "39,236,825", "previous_display": "6,985,097", "confidence": 0.99,
        },
    ]

    result = add_canonical_equivalents(candidates)
    operating = [c for c in result if c.get("metric") == "operating_result" and c.get("current_display")]

    assert len(operating) == 1
    assert operating[0]["current_display"] == "586,575"
    assert operating[0]["derivation"]["source_candidate_ids"] == ["p13-r7"]
