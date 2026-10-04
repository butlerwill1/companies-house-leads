import json
import sqlite3

from core.companies_house_sqlite import init_db
from scripts.enrichment import reparse_financial_scale as rp


class _FakeExtractor:
    def __init__(self, years, derived):
        self._result = {"years": years, "derived": derived}

    def parse_xhtml_accounts(self, xhtml):
        return self._result


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _row(conn, period, turnover, source="xhtml", derived=None):
    conn.execute(
        "insert into financial_period_summaries (company_number, document_id, period_type, turnover, gross_profit, "
        "operating_result, raw_payload, data_source, derived_payload, currency_validation_status) "
        "values ('1','D',?,?,?,?,'{}',?,?,'unknown')",
        (period, turnover, 300, 200, source, json.dumps(derived or {"gross_margin_current_pct": 0.2})))


def _fix(conn, current, previous, derived=None):
    years = {"current": current, "previous": previous}
    return rp.repair_document(conn, _FakeExtractor(years, derived or {"gross_margin_current_pct": 0.25}), "1", "D", "<x/>")


def test_an_unscaled_figure_is_corrected_and_logged_with_old_and_new():
    conn = _db()
    _row(conn, "current", 1288)
    _row(conn, "previous", 1292)
    changes = _fix(conn, {"turnover": 1_288_000, "gross_profit": 300, "operating_result": 200},
                   {"turnover": 1_292_000, "gross_profit": 300, "operating_result": 200})
    assert {(c["period_type"], c["old"], c["new"]) for c in changes} == {("current", 1288, 1_288_000),
                                                                         ("previous", 1292, 1_292_000)}
    assert [r["turnover"] for r in conn.execute("select turnover from financial_period_summaries order by period_type")] \
        == [1_288_000, 1_292_000]


def test_ratios_stored_beside_the_figures_are_refreshed():
    conn = _db()
    _row(conn, "current", 1288)
    _fix(conn, {"turnover": 1_288_000, "gross_profit": 300, "operating_result": 200}, {})
    assert json.loads(conn.execute("select derived_payload from financial_period_summaries").fetchone()[0]) \
        == {"gross_margin_current_pct": 0.25}


def test_nothing_changes_when_the_parse_agrees():
    conn = _db()
    _row(conn, "current", 1288)
    assert _fix(conn, {"turnover": 1288, "gross_profit": 300, "operating_result": 200}, {}) == []


def test_a_vlm_row_is_never_touched():
    conn = _db()
    _row(conn, "current", 1288, source="vlm")
    assert _fix(conn, {"turnover": 1_288_000}, {}) == []
    assert conn.execute("select turnover from financial_period_summaries").fetchone()[0] == 1288


def test_a_value_the_text_tool_filled_is_left_alone():
    conn = _db()
    _row(conn, "current", 1288, derived={"text_recovery": {"turnover": {"run_id": 1}}})
    assert _fix(conn, {"turnover": 5, "gross_profit": 300, "operating_result": 200}, {}) == []


def test_a_missing_parse_value_does_not_blank_a_stored_one():
    conn = _db()
    _row(conn, "current", 1288)
    assert _fix(conn, {"turnover": None, "gross_profit": 300, "operating_result": 200}, {}) == []
    assert conn.execute("select turnover from financial_period_summaries").fetchone()[0] == 1288
