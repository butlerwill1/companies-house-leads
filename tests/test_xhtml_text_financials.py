import json
import sqlite3

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.pdf_vision_extraction import xhtml_text_financials as tf

FILED = "\n".join([
    "CONTENTS", "Profit and loss account", "Balance sheet", "STRATEGIC REPORT",
    "The company traded well.",
    "PROFIT AND LOSS ACCOUNT", "FOR THE YEAR ENDED 31 DECEMBER 2024", "£'000", "2024", "2023",
    "Turnover", "12,345", "11,000", "Cost of sales", "(8,000)", "(7,500)", "Gross profit", "4,345", "3,500",
    "Profit for the financial year", "1,200", "(300)",
])


def _answer(**overrides):
    base = {"unit": "GBP_THOUSANDS", "statement_scope": "company", "no_profit_and_loss": False, "periods": {
        "current": {"period_end": "2024-12-31",
                    "turnover": {"label": "Turnover", "displayed": "12,345", "quote": "Turnover 12,345 11,000"},
                    "profit_after_tax": {"label": "Profit for the financial year", "displayed": "1,200",
                                         "quote": "Profit for the financial year 1,200 (300)"}},
        "previous": {"period_end": "2023-12-31",
                     "turnover": {"label": "Turnover", "displayed": "11,000", "quote": "Turnover 12,345 11,000"},
                     "profit_after_tax": {"label": "Profit for the financial year", "displayed": "(300)",
                                          "quote": "Profit for the financial year 1,200 (300)"}}}}
    base.update(overrides)
    return base


def test_window_finds_the_statement_and_skips_the_contents_mention():
    text, found = tf.statement_windows(FILED)
    assert found and "Turnover" in text and "£'000" in text
    assert "[profit and loss statement]" in text


def test_no_statement_means_not_found():
    _, found = tf.statement_windows("CONTENTS\nProfit and loss account\nNotes\nThe company is dormant.")
    assert not found


def test_a_good_answer_is_recovered_and_scaled_to_pounds():
    text, found = tf.statement_windows(FILED)
    result = tf.validate_recovery(_answer(), text, found)
    assert result["status"] == "recovered"
    assert result["periods"]["current"]["turnover"]["reported_value"] == "12345000"
    assert result["periods"]["previous"]["profit_after_tax"]["reported_value"] == "-300000"


def test_a_quote_that_is_not_in_the_text_is_rejected():
    text, _ = tf.statement_windows(FILED)
    row = {"label": "Turnover", "displayed": "999", "quote": "Turnover 999 888"}
    assert tf.validate_row("turnover", row, text, "GBP")["reason"] == "quote not found in the text sent"


def test_number_must_appear_in_its_quote():
    text, _ = tf.statement_windows(FILED)
    row = {"label": "Turnover", "displayed": "99,999", "quote": "Turnover 12,345 11,000"}
    assert tf.validate_row("turnover", row, text, "GBP")["reason"] == "displayed number not in the quote"


@pytest.mark.parametrize("label", ["Gross profit", "Cost of sales", "Other operating income"])
def test_a_non_turnover_row_cannot_be_turnover(label):
    text = f"{label}\n4,345\n3,500"
    row = {"label": label, "displayed": "4,345", "quote": f"{label} 4,345 3,500"}
    assert tf.validate_row("turnover", row, text, "GBP")["status"] == "rejected"


def test_profit_before_tax_cannot_be_profit_after_tax():
    text = "Profit before taxation\n900\n800"
    row = {"label": "Profit before taxation", "displayed": "900", "quote": "Profit before taxation 900 800"}
    assert tf.validate_row("profit_after_tax", row, text, "GBP")["status"] == "rejected"


def test_unknown_unit_is_rejected_not_guessed():
    text, _ = tf.statement_windows(FILED)
    row = {"label": "Turnover", "displayed": "12,345", "quote": "Turnover 12,345 11,000"}
    assert tf.validate_row("turnover", row, text, "UNKNOWN")["reason"] == "unit unknown"


def test_no_statement_and_the_model_agrees_is_not_filed():
    result = tf.validate_recovery({"unit": "GBP", "no_profit_and_loss": True, "periods": {}}, "", False)
    assert result["status"] == "not_filed"


def test_model_claiming_no_statement_over_text_that_has_one_is_unresolved():
    result = tf.validate_recovery({"unit": "GBP", "no_profit_and_loss": True, "periods": {}}, "x", True)
    assert result["status"] == "unresolved"


def test_a_bad_model_reply_is_unresolved_not_an_exception():
    xhtml = "<html><body>" + "".join(f"<p>{line}</p>" for line in FILED.splitlines()) + "</body></html>"
    result = tf.recover_from_text(lambda prompt: ("not json at all", {}), "ACME", xhtml)
    assert result["status"] == "unresolved" and result["problem"]


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for period, year, turnover in (("current", 2024, None), ("previous", 2023, 5)):
        conn.execute("insert into financial_period_summaries (company_number, document_id, period_type, financial_year,"
                     " turnover, raw_payload, data_source, currency_validation_status) "
                     "values ('1','D1',?,?,?,'{}','xhtml','unknown')", (period, year, turnover))
    return conn


def test_store_fills_only_empty_cells_and_never_overwrites_a_tagged_value():
    conn = _db()
    text, found = tf.statement_windows(FILED)
    result = {"prompt_version": tf.PROMPT_VERSION, "usage": {}, "raw": "{}", "problem": None, "found_statement": found,
              **tf.validate_recovery(_answer(), text, found)}
    filled = tf.store_recovery(conn, "1", "D1", result, "m", 0.001)
    rows = {r["period_type"]: r for r in conn.execute("select * from financial_period_summaries")}
    assert rows["current"]["turnover"] == 12345000            # was empty: filled
    assert rows["previous"]["turnover"] == 5                  # tagged value kept
    assert rows["current"]["profit_after_tax"] == 1200000
    assert filled["turnover"] == 1
    assert "text_recovery" in json.loads(rows["current"]["derived_payload"])
    assert conn.execute("select count(*) from vlm_financial_metrics").fetchone()[0] == 4   # every figure is audited
    assert conn.execute("select vision_model from vlm_financial_extraction_runs").fetchone()[0] == "xhtml_text"
