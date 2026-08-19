from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.companies_house_extractor import CompaniesHouseExtractor


def _filing(*, date: str, made_up_date: str, filing_type: str = "AA", transaction_id: str) -> dict:
    return {
        "transaction_id": transaction_id,
        "type": filing_type,
        "date": date,
        "category": "accounts",
        "description_values": {"made_up_date": made_up_date},
        "action_date": made_up_date,
    }


def _recent(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()


def _extractor_with_filings(filings: list[dict]) -> CompaniesHouseExtractor:
    extractor = CompaniesHouseExtractor(api_key=None)
    extractor.get_accounts_filings = lambda company_number: filings  # type: ignore[method-assign]
    return extractor


def test_get_accounts_history_returns_newest_first_up_to_max_filings() -> None:
    filings = [
        _filing(date=_recent(0), made_up_date=_recent(30), transaction_id="tx1"),
        _filing(date=_recent(370), made_up_date=_recent(395), transaction_id="tx2"),
        _filing(date=_recent(740), made_up_date=_recent(760), transaction_id="tx3"),
        _filing(date=_recent(1110), made_up_date=_recent(1125), transaction_id="tx4"),
        _filing(date=_recent(1480), made_up_date=_recent(1490), transaction_id="tx5"),
    ]
    extractor = _extractor_with_filings(filings)

    result = extractor.get_accounts_history("00000000", years=10, max_filings=3)

    assert [f["transaction_id"] for f in result] == ["tx1", "tx2", "tx3"]


def test_get_accounts_history_excludes_non_accounts_filing_types() -> None:
    filings = [
        _filing(date=_recent(0), made_up_date=_recent(30), transaction_id="tx-aa"),
        _filing(date=_recent(60), made_up_date=_recent(90), filing_type="AA01", transaction_id="tx-aa01"),
    ]
    extractor = _extractor_with_filings(filings)

    result = extractor.get_accounts_history("00000000", years=5, max_filings=4)

    assert [f["transaction_id"] for f in result] == ["tx-aa"]


def test_get_accounts_history_prefers_the_amendment_for_the_same_period() -> None:
    """An AAMD amendment filed after an earlier AA for the same accounting
    period supersedes it — both cover the same real period, and the
    amendment is the corrected figure."""
    filings = [
        _filing(date=_recent(60), made_up_date=_recent(400), filing_type="AA", transaction_id="tx-aa"),
        _filing(date=_recent(10), made_up_date=_recent(400), filing_type="AAMD", transaction_id="tx-aamd"),
    ]
    extractor = _extractor_with_filings(filings)

    result = extractor.get_accounts_history("00000000", years=5, max_filings=4)

    assert [f["transaction_id"] for f in result] == ["tx-aamd"]


def test_get_accounts_history_respects_the_years_cutoff() -> None:
    filings = [
        _filing(date=_recent(0), made_up_date=_recent(30), transaction_id="tx-recent"),
        _filing(date=_recent(2000), made_up_date=_recent(2020), transaction_id="tx-old"),
    ]
    extractor = _extractor_with_filings(filings)

    result = extractor.get_accounts_history("00000000", years=3, max_filings=10)

    assert [f["transaction_id"] for f in result] == ["tx-recent"]


def test_get_accounts_history_does_not_conflate_two_filings_in_the_same_calendar_year() -> None:
    """A company can file twice within one calendar year for two distinct
    accounting periods (e.g. catching up after a late filing) — both are
    real, separate periods, not duplicates to collapse."""
    filings = [
        _filing(date=_recent(30), made_up_date=_recent(60), transaction_id="tx-a"),
        _filing(date=_recent(200), made_up_date=_recent(420), transaction_id="tx-b"),
    ]
    extractor = _extractor_with_filings(filings)

    result = extractor.get_accounts_history("00000000", years=5, max_filings=10)

    assert {f["transaction_id"] for f in result} == {"tx-a", "tx-b"}
