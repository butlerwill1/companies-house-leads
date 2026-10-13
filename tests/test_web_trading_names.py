from __future__ import annotations

import sqlite3

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.website_analysis import web_trading_names as T


def test_filing_patterns_find_trading_names_with_their_sentence():
    text = ("The company's principal activity is the sale of nutrition products, trading as Free Soul. "
            "The company also operates under the brand name Sistas, which launched in 2023. "
            "Sales are made t/a Soul Kitchen. Revenue grew.")
    found = {item["name"]: item["evidence"] for item in T.names_from_filing(text, "ARA FITNESS LIMITED")}
    assert set(found) == {"Free Soul", "Sistas", "Soul Kitchen"}
    assert found["Free Soul"].startswith("The company's principal activity")


def test_filing_names_equal_to_the_registered_name_or_generic_words_are_dropped():
    text = "The company is trading as the Group. It is trading as Bott and Co Solicitors Limited."
    assert T.names_from_filing(text, "BOTT AND CO SOLICITORS LTD") == []


def test_filing_names_are_cut_at_connecting_words():
    text = "The company is trading as Millie's House Nursery and provides childcare in London."
    assert [i["name"] for i in T.names_from_filing(text, "SOUTH WEST LONDON NURSERY COMPANY LIMITED")] == [
        "Millie's House Nursery"]


def test_site_text_trading_name_of_this_company():
    text = "Bott and Co is a trading name of Bott and Co Solicitors Limited, authorised by the SRA."
    items = T.names_from_site_text(text, "BOTT AND CO SOLICITORS LTD")
    assert [i["name"] for i in items] == ["Bott and Co"]
    assert items[0]["evidence"].startswith("Bott and Co is a trading name of")
    text = "Free Soul is a trading name of ARA Fitness Limited. Registered in England 10150642."
    assert [i["name"] for i in T.names_from_site_text(text, "ARA FITNESS LIMITED")] == ["Free Soul"]


def test_site_text_names_belonging_to_another_company_are_ignored():
    text = "Store First is a trading name of Paystore Limited."
    assert T.names_from_site_text(text, "ARA FITNESS LIMITED") == []


def test_listing_title_is_a_trading_name_only_on_a_verified_site():
    listing = {"title": "Free Soul"}
    assert T.name_from_listing(listing, "ARA FITNESS LIMITED", verified_site=True)["name"] == "Free Soul"
    assert T.name_from_listing(listing, "ARA FITNESS LIMITED", verified_site=False) is None
    assert T.name_from_listing({"title": "Ara Fitness"}, "ARA FITNESS LIMITED", verified_site=True) is None


def _db():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute("insert into companies (company_number, company_name, company_status, source_mode, profile_payload, "
                 "updated_at) values ('1', 'ARA FITNESS LIMITED', 'active', 'api', '{}', '2026')")
    return conn


def test_store_names_keeps_one_row_per_name_and_validates_the_source():
    conn = _db()
    assert T.store_names(conn, "1", "filing", [{"name": "Free Soul", "evidence": "x"}]) == 1
    assert T.store_names(conn, "1", "website", [{"name": "Free Soul", "evidence": "y"}]) == 0
    assert T.trading_names(conn, "1") == ["Free Soul"]
    assert conn.execute("select source from company_trading_names").fetchone() == ("filing",)
    with pytest.raises(ValueError):
        T.store_names(conn, "1", "guess", [])


def test_extract_from_cached_filings_reads_only_cached_files(tmp_path):
    conn = _db()
    filing = tmp_path / "1.xhtml"
    filing.write_text("<html><body><p>The company is trading as Free Soul.</p></body></html>", encoding="utf-8")
    counts = T.extract_from_cached_filings(conn, ["1", "2"], xhtml_path=lambda n: filing if n == "1" else None)
    assert counts == {"filings_read": 1, "names_added": 1, "companies_with_names": 1}
    assert T.trading_names(conn, "1") == ["Free Soul"]
