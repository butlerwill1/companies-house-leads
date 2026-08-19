from __future__ import annotations

import sqlite3

import pytest

from core.companies_house_sqlite import init_db
from scripts.enrichment.ch_backfill_history import (
    backfill_company,
    existing_transaction_ids,
    resolve_company,
    select_turnover_band_sample,
)


def _insert_company(conn: sqlite3.Connection, company_number: str, company_name: str) -> None:
    conn.execute(
        """
        insert into companies (company_number, company_name, company_status, company_type,
            date_of_creation, source_mode, profile_payload, updated_at)
        values (?, ?, 'active', 'ltd', '2020-01-01', 'public_api', ?, '2026-06-22T10:00:00+00:00')
        """,
        (company_number, company_name, f'{{"company_number":"{company_number}","company_name":"{company_name}"}}'),
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    init_db(connection)
    return connection


def test_resolve_company_by_exact_number(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")

    assert resolve_company(conn, "01407612") == "01407612"


def test_resolve_company_by_unambiguous_name(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")

    assert resolve_company(conn, "car giant") == "01407612"


def test_resolve_company_errors_on_ambiguous_name(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")
    _insert_company(conn, "09999999", "CAR GIANT PARTS LIMITED")

    with pytest.raises(SystemExit):
        resolve_company(conn, "car giant")


def test_resolve_company_errors_when_not_found(conn: sqlite3.Connection) -> None:
    with pytest.raises(SystemExit):
        resolve_company(conn, "no such company")


def test_select_turnover_band_sample_requires_turnover_and_profit(conn: sqlite3.Connection) -> None:
    for company_number in ("11111111", "22222222", "33333333"):
        _insert_company(conn, company_number, f"COMPANY {company_number}")
    conn.execute(
        "insert into financial_period_summaries (company_number, document_id, period_type, turnover, profit_after_tax, raw_payload) "
        "values (?, 'doc-1', 'current', 12000000, 500000, '{}')",
        ("11111111",),
    )
    conn.execute(
        "insert into financial_period_summaries (company_number, document_id, period_type, turnover, profit_after_tax, raw_payload) "
        "values (?, 'doc-2', 'current', 15000000, 500000, '{}')",
        ("22222222",),
    )
    # In the turnover band but missing profit_after_tax — must be excluded.
    conn.execute(
        "insert into financial_period_summaries (company_number, document_id, period_type, turnover, profit_after_tax, raw_payload) "
        "values (?, 'doc-3', 'current', 13000000, NULL, '{}')",
        ("33333333",),
    )
    conn.commit()

    result = select_turnover_band_sample(conn, 10000000, 20000000, sample_size=10, seed=1)

    assert set(result) == {"11111111", "22222222"}


def test_select_turnover_band_sample_respects_sample_size(conn: sqlite3.Connection) -> None:
    for i in range(5):
        company_number = f"{i:08d}"
        _insert_company(conn, company_number, f"COMPANY {i}")
        conn.execute(
            "insert into financial_period_summaries (company_number, document_id, period_type, turnover, profit_after_tax, raw_payload) "
            "values (?, ?, 'current', 12000000, 500000, '{}')",
            (company_number, f"doc-{i}"),
        )
    conn.commit()

    result = select_turnover_band_sample(conn, 10000000, 20000000, sample_size=2, seed=1)

    assert len(result) == 2


def test_existing_transaction_ids_reads_documents_table(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")
    conn.execute(
        "insert into filings (transaction_id, company_number, filing_payload) values (?, ?, '{}')",
        ("tx-1", "01407612"),
    )
    conn.execute(
        "insert into documents (document_id, transaction_id, company_number) values (?, ?, ?)",
        ("doc-1", "tx-1", "01407612"),
    )
    conn.commit()

    assert existing_transaction_ids(conn, "01407612") == {"tx-1"}
    assert existing_transaction_ids(conn, "00000000") == set()


class _FakeLimiter:
    def wait(self) -> None:
        return None


class _FakeExtractor:
    def __init__(self, history: list[dict], xhtml_by_transaction: dict[str, str]) -> None:
        self._history = history
        self._xhtml_by_transaction = xhtml_by_transaction
        from core.companies_house_extractor import CompaniesHouseExtractor

        self._real = CompaniesHouseExtractor(api_key=None)

    def get_accounts_history(self, company_number: str, *, years: int, max_filings: int) -> list[dict]:
        return self._history

    def get_document_urls(self, company_number: str, filing: dict) -> dict:
        return {
            "metadata": f"https://example/meta-{filing['transaction_id']}",
            "xhtml": f"https://example/{filing['transaction_id']}",
        }

    def fetch_document(self, url: str, content_type: str | None = None) -> bytes:
        transaction_id = url.rsplit("/", 1)[-1]
        return self._xhtml_by_transaction[transaction_id].encode("utf-8")

    def parse_xhtml_accounts(self, xhtml_text: str) -> dict:
        return self._real.parse_xhtml_accounts(xhtml_text)


_SIMPLE_XHTML = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
    xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="urn:core">
  <body>
    <xbrli:context id="C"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>{cur_start}</xbrli:startDate><xbrli:endDate>{cur_end}</xbrli:endDate></xbrli:period></xbrli:context>
    <xbrli:context id="F"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>{prev_start}</xbrli:startDate><xbrli:endDate>{prev_end}</xbrli:endDate></xbrli:period></xbrli:context>
    <ix:nonFraction name="core:ProfitLoss" contextRef="C">{cur_value}</ix:nonFraction>
    <ix:nonFraction name="core:ProfitLoss" contextRef="F">{prev_value}</ix:nonFraction>
  </body></html>"""


def test_backfill_company_skips_filings_already_in_documents(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")
    conn.execute(
        "insert into filings (transaction_id, company_number, filing_payload) values (?, ?, '{}')",
        ("tx-known", "01407612"),
    )
    conn.execute(
        "insert into documents (document_id, transaction_id, company_number) values (?, ?, ?)",
        ("doc-known", "tx-known", "01407612"),
    )
    conn.commit()

    history = [{"transaction_id": "tx-known", "date": "2025-01-01", "description_values": {"made_up_date": "2024-12-31"}}]
    extractor = _FakeExtractor(history, {})

    counts = backfill_company(extractor, _FakeLimiter(), conn, "01407612", years=5, max_filings=4, dry_run=False)

    assert counts == {"inserted": 0, "skipped_existing": 1, "no_xhtml": 0, "error": 0}


def test_backfill_company_inserts_a_new_historical_filing_and_computes_overlap(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")
    # Already-known newer filing whose "previous" period should overlap with
    # the historical filing's "current" period once it's backfilled.
    conn.execute(
        "insert into financial_period_summaries (company_number, document_id, period_type, period_end_on, profit_after_tax, raw_payload) "
        "values (?, 'doc-2024', 'previous', '2023-12-31', 100, '{}')",
        ("01407612",),
    )
    conn.commit()

    history = [{
        "transaction_id": "tx-2023",
        "date": "2024-01-01",
        "description_values": {"made_up_date": "2023-12-31"},
        "action_date": "2023-12-31",
    }]
    xhtml = _SIMPLE_XHTML.format(
        cur_start="2023-01-01", cur_end="2023-12-31", prev_start="2022-01-01", prev_end="2022-12-31",
        cur_value="100", prev_value="90",
    )
    extractor = _FakeExtractor(history, {"tx-2023": xhtml})

    counts = backfill_company(extractor, _FakeLimiter(), conn, "01407612", years=5, max_filings=4, dry_run=False)

    assert counts == {"inserted": 1, "skipped_existing": 0, "no_xhtml": 0, "error": 0}
    new_row = conn.execute(
        "select financial_year, profit_after_tax from financial_period_summaries "
        "where company_number = '01407612' and period_type = 'current' and document_id != 'doc-2024'"
    ).fetchone()
    assert new_row == (2023, 100)
    overlap_status = conn.execute(
        "select comparative_overlap_status from financial_period_summaries where document_id = 'doc-2024' and period_type = 'previous'"
    ).fetchone()[0]
    assert overlap_status == "match"


def test_backfill_company_dry_run_makes_no_writes(conn: sqlite3.Connection) -> None:
    _insert_company(conn, "01407612", "CAR GIANT LIMITED")
    conn.commit()
    history = [{"transaction_id": "tx-2023", "date": "2024-01-01", "description_values": {"made_up_date": "2023-12-31"}}]
    extractor = _FakeExtractor(history, {})

    counts = backfill_company(extractor, _FakeLimiter(), conn, "01407612", years=5, max_filings=4, dry_run=True)

    assert counts["inserted"] == 1
    assert conn.execute("select count(*) from documents").fetchone()[0] == 0
