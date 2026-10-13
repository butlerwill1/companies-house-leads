import json
import sqlite3

from companies_house_core.companies_house_sqlite import init_db


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _row(conn, doc, period, year, turnover, profit, source="xhtml", derived=None):
    conn.execute(
        "insert into financial_period_summaries (company_number, document_id, period_type, financial_year, turnover, "
        "profit_after_tax, raw_payload, data_source, derived_payload, currency_validation_status) "
        "values ('1', ?, ?, ?, ?, ?, '{}', ?, ?, 'unknown')", (doc, period, year, turnover, profit, source, derived))


def _history(conn):
    return {r["financial_year"]: r for r in conn.execute("select * from company_financial_history")}


def test_a_year_in_two_filings_becomes_one_row_preferring_the_fuller_reading():
    conn = _conn()
    _row(conn, "newer", "previous", 2023, 100, None)         # comparative in the newer filing: profit missing
    _row(conn, "older", "current", 2023, 100, 7)             # the year's own filing: both present
    history = _history(conn)
    assert len(history) == 1 and history[2023]["profit_after_tax"] == 7 and history[2023]["n_rows"] == 2
    assert history[2023]["status"] == "ok"


def test_equal_readings_prefer_the_filings_own_current_row():
    conn = _conn()
    _row(conn, "newer", "previous", 2022, 100, 5)
    _row(conn, "own", "current", 2022, 101, 5)
    assert _history(conn)[2022]["document_id"] == "own"


def test_status_partial_and_missing():
    conn = _conn()
    _row(conn, "a", "current", 2021, 100, None)
    _row(conn, "b", "current", 2020, None, None)
    history = _history(conn)
    assert history[2021]["status"] == "partial" and history[2020]["status"] == "missing"


def test_source_reports_text_recovery_and_vlm():
    conn = _conn()
    _row(conn, "a", "current", 2024, 1, 1, derived=json.dumps({"text_recovery": {"turnover": {"run_id": 1}}}))
    _row(conn, "b", "current", 2019, 1, 1, source="vlm")
    history = _history(conn)
    assert history[2024]["source"] == "xhtml+text" and history[2019]["source"] == "vlm"


def test_rows_without_a_financial_year_are_left_out():
    conn = _conn()
    _row(conn, "a", "current", None, 1, 1)
    assert _history(conn) == {}
