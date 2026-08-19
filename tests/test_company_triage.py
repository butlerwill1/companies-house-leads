from __future__ import annotations

import sqlite3

import pytest

from core.companies_house_sqlite import init_db
from core.company_triage import (
    DORMANT,
    HOLDING,
    NON_TRADING,
    TRADING,
    UNKNOWN,
    classify_trading_status,
    gross_margin_pct,
    name_suggests_holding,
    refresh_all_company_signals,
    revenue_per_employee,
    sic_is_catch_all,
    triage_rows,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    init_db(connection)
    return connection


def _company(conn: sqlite3.Connection, number: str, name: str, *, sic: str | None = "62020", status: str = "active") -> None:
    conn.execute(
        """
        insert into companies (company_number, company_name, company_status, company_type,
            date_of_creation, source_mode, profile_payload, updated_at, sic_codes, sic_code_primary)
        values (?, ?, ?, 'ltd', '2020-01-01', 'public_api', '{}', '2026-06-22T10:00:00+00:00', ?, ?)
        """,
        (number, name, status, f'["{sic}"]' if sic else "[]", sic),
    )


def _financials(
    conn: sqlite3.Connection, number: str, *, turnover=None, employees=None,
    gross_profit=None, period_end_on="2024-12-31", financial_year=2024,
) -> None:
    conn.execute(
        """
        insert into financial_period_summaries (
            company_number, document_id, period_type, financial_year, period_end_on,
            turnover, employees, gross_profit, raw_payload
        ) values (?, ?, 'current', ?, ?, ?, ?, ?, '{}')
        """,
        (number, f"doc-{number}", financial_year, period_end_on, turnover, employees, gross_profit),
    )


def test_name_suggests_holding_matches_structure_names_only() -> None:
    assert name_suggests_holding("LEMON PEPPER TOPCO LIMITED")
    assert name_suggests_holding("MTALX GLOBAL HOLDINGS LIMITED")
    assert name_suggests_holding("SOMETHING BIDCO LTD")
    assert not name_suggests_holding("CAR GIANT LIMITED")
    # "GROUP" alone is far too common among ordinary trading companies to
    # treat as a holding signal on its own.
    assert not name_suggests_holding("PENKETH GROUP LIMITED")


def test_sic_is_catch_all_flags_not_elsewhere_classified_buckets() -> None:
    assert sic_is_catch_all("96090")
    assert sic_is_catch_all("82990")
    assert not sic_is_catch_all("45111")
    assert not sic_is_catch_all(None)


def test_revenue_per_employee_guards_against_zero_and_missing() -> None:
    assert revenue_per_employee(408107838, 24) == 17004493
    assert revenue_per_employee(1000000, 0) is None
    assert revenue_per_employee(None, 10) is None
    assert revenue_per_employee(0, 10) is None


def test_gross_margin_drops_arithmetically_impossible_values() -> None:
    """32 current-period rows in the live data report gross profit exceeding
    turnover, which cannot be true -- passing them downstream would poison
    any margin-based reasoning."""
    assert gross_margin_pct(1000, 250) == 25.0
    assert gross_margin_pct(1000, 1500) is None
    assert gross_margin_pct(1000, -50) is None
    assert gross_margin_pct(0, 100) is None


def test_non_active_company_status_is_not_trading() -> None:
    status, reason = classify_trading_status(
        company_name="SOME COMPANY LTD", company_status="administration",
        sic_code_primary="62020", turnover=1000000, employees=10, has_financials=True,
    )
    assert status == NON_TRADING
    assert "administration" in reason


def test_holding_name_with_zero_employees_is_a_holding_vehicle() -> None:
    """MTALX GLOBAL HOLDINGS reports £423m turnover against zero employees,
    and was the single largest PPC estimate the old SIC-ratio model
    produced."""
    status, reason = classify_trading_status(
        company_name="MTALX GLOBAL HOLDINGS LIMITED", company_status="active",
        sic_code_primary="96090", turnover=423471489, employees=0, has_financials=True,
    )
    assert status == HOLDING
    assert "holding-style name" in reason


def test_turnover_with_zero_employees_alone_is_not_enough_to_call_it_holding() -> None:
    """Checked against filed narratives, roughly half of zero-employee
    companies with turnover are genuine traders whose staff sit in
    subsidiaries -- HEDIN AUTOMOTIVE reports zero employees on £412m and
    describes itself as "motor car retailers and repairers". Without
    corroboration this must stay unknown, not be forced into holding."""
    status, reason = classify_trading_status(
        company_name="HEDIN AUTOMOTIVE LTD", company_status="active",
        sic_code_primary="45111", turnover=412848773, employees=0, has_financials=True,
    )
    assert status == UNKNOWN
    assert "narrative confirmation" in reason


def test_zero_employees_plus_duplicate_turnover_is_conclusive() -> None:
    """Zero employees on its own is ambiguous, but a company that also
    duplicates another company's turnover to the pound is the structure
    entity in a group, not a second business."""
    status, reason = classify_trading_status(
        company_name="PERCO (NORTH EAST) LIMITED", company_status="active",
        sic_code_primary="45111", turnover=93518791, employees=0,
        has_financials=True, is_duplicate=True,
    )
    assert status == HOLDING
    assert "duplicates another company" in reason


def test_holding_sic_code_is_decisive_regardless_of_name() -> None:
    status, _ = classify_trading_status(
        company_name="ORDINARY TRADING NAME LTD", company_status="active",
        sic_code_primary="64209", turnover=5000000, employees=40, has_financials=True,
    )
    assert status == HOLDING


def test_holding_name_with_real_staff_is_unknown_not_holding() -> None:
    """A holding-style name alongside genuine trading indicators is
    ambiguous -- many real operating companies are called "X Holdings".
    Resolve to unknown rather than forcing a bucket."""
    status, _ = classify_trading_status(
        company_name="HAWKINS HOLDINGS LIMITED", company_status="active",
        sic_code_primary="45111", turnover=128644238, employees=249, has_financials=True,
    )
    assert status == UNKNOWN


def test_no_turnover_and_no_employees_is_dormant() -> None:
    status, _ = classify_trading_status(
        company_name="QUIET LTD", company_status="active", sic_code_primary="62020",
        turnover=None, employees=0, has_financials=True,
    )
    assert status == DORMANT


def test_ordinary_company_is_trading() -> None:
    status, _ = classify_trading_status(
        company_name="CAR GIANT LIMITED", company_status="active", sic_code_primary="45112",
        turnover=352538885, employees=483, has_financials=True,
    )
    assert status == TRADING


def test_missing_financials_resolve_to_unknown() -> None:
    status, reason = classify_trading_status(
        company_name="NEW LTD", company_status="active", sic_code_primary="62020",
        turnover=None, employees=None, has_financials=False,
    )
    assert status == UNKNOWN
    assert "no current-period financial data" in reason


def test_identical_turnover_marks_the_holding_entity_as_the_duplicate(conn: sqlite3.Connection) -> None:
    """LEMON PEPPER TOPCO and LEMON PEPPER HOLDINGS both report
    £125,026,523 -- one business, two leads. The operating company is kept
    as primary and the structure entity is marked as the duplicate."""
    _company(conn, "11111111", "LEMON PEPPER LIMITED")
    _company(conn, "22222222", "LEMON PEPPER TOPCO LIMITED")
    _financials(conn, "11111111", turnover=125026523, employees=200)
    _financials(conn, "22222222", turnover=125026523, employees=0)
    conn.commit()

    by_number = {r["company_number"]: r["signals"] for r in triage_rows(conn)}

    assert by_number["22222222"]["duplicate_of"] == "11111111"
    assert by_number["11111111"]["duplicate_of"] is None


def test_duplicate_detection_falls_back_to_financial_year(conn: sqlite3.Connection) -> None:
    """period_end_on arrived with a later migration and is populated on only
    a few hundred of the live current-period rows, so duplicate detection
    has to key on financial_year when it is missing -- otherwise it finds
    nothing at all. LEMON PEPPER TOPCO/HOLDINGS are a real pair with no
    period_end_on."""
    _company(conn, "11319703", "LEMON PEPPER TOPCO LIMITED")
    _company(conn, "10589672", "LEMON PEPPER HOLDINGS LIMITED")
    _company(conn, "33333333", "LEMON PEPPER TRADING LIMITED")
    for number in ("11319703", "10589672", "33333333"):
        _financials(conn, number, turnover=125026523, employees=0, period_end_on=None, financial_year=2024)
    conn.commit()

    by_number = {r["company_number"]: r["signals"] for r in triage_rows(conn)}

    # The one without a holding-style name is kept as the operating company.
    assert by_number["33333333"]["duplicate_of"] is None
    assert by_number["11319703"]["duplicate_of"] == "33333333"
    assert by_number["10589672"]["duplicate_of"] == "33333333"


def test_duplicate_detection_ignores_different_period_ends(conn: sqlite3.Connection) -> None:
    _company(conn, "11111111", "ALPHA LIMITED")
    _company(conn, "22222222", "BETA LIMITED")
    _financials(conn, "11111111", turnover=5000000, employees=10, period_end_on="2024-12-31")
    _financials(conn, "22222222", turnover=5000000, employees=10, period_end_on="2023-12-31")
    conn.commit()

    by_number = {r["company_number"]: r["signals"] for r in triage_rows(conn)}

    assert by_number["11111111"]["duplicate_of"] is None
    assert by_number["22222222"]["duplicate_of"] is None


def test_refresh_writes_signals_and_is_idempotent(conn: sqlite3.Connection) -> None:
    _company(conn, "11111111", "STONEBRIDGE CONTRACTING SOLUTIONS LTD", sic="41100")
    _financials(conn, "11111111", turnover=408107838, employees=24, gross_profit=40000000)
    conn.commit()

    first = refresh_all_company_signals(conn)
    second = refresh_all_company_signals(conn)
    assert first == second

    rows = dict(
        conn.execute(
            "select signal_key, coalesce(signal_text, signal_int, signal_real, signal_bool) "
            "from company_signals where company_number = '11111111'"
        ).fetchall()
    )
    assert rows["trading_status"] == "trading"
    assert rows["revenue_per_employee"] == 17004493
    assert rows["revenue_per_employee_flagged"] == 1
    assert conn.execute("select count(*) from company_signals where company_number='11111111'").fetchone()[0] == len(rows)


def test_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    _company(conn, "11111111", "ANY COMPANY LTD")
    _financials(conn, "11111111", turnover=1000000, employees=10)
    conn.commit()

    counts = refresh_all_company_signals(conn, dry_run=True)

    assert counts["trading"] == 1
    assert conn.execute("select count(*) from company_signals").fetchone()[0] == 0


def test_signal_value_type_switches_cleanly_when_a_value_changes_type(conn: sqlite3.Connection) -> None:
    """A re-run must not leave the previous typed column populated
    alongside the new one, or readers using coalesce() get a stale value."""
    from core.companies_house_sqlite import upsert_company_signals

    _company(conn, "11111111", "ANY COMPANY LTD")
    conn.commit()
    upsert_company_signals(conn, "11111111", {"probe": 42})
    upsert_company_signals(conn, "11111111", {"probe": "now text"})

    row = conn.execute(
        "select signal_value_type, signal_int, signal_text from company_signals "
        "where company_number='11111111' and signal_key='probe'"
    ).fetchone()
    assert row == ("text", None, "now text")


def test_none_valued_signal_is_cleared_not_stored(conn: sqlite3.Connection) -> None:
    from core.companies_house_sqlite import upsert_company_signals

    _company(conn, "11111111", "ANY COMPANY LTD")
    conn.commit()
    upsert_company_signals(conn, "11111111", {"probe": 42})
    upsert_company_signals(conn, "11111111", {"probe": None})

    assert conn.execute(
        "select count(*) from company_signals where company_number='11111111' and signal_key='probe'"
    ).fetchone()[0] == 0
