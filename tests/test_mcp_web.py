from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3

import pytest

from companies_house_mcp.service import CompaniesHouseDataService


@pytest.fixture()
def web_db(seeded_db_path):
    with sqlite3.connect(seeded_db_path) as conn:
        for number, domain in [("13406761", "mesh.example"), ("22222222", "digital.example")]:
            conn.execute(
                "insert into company_web_identity (company_number, resolver_version, domain, role, tier, "
                "final_url, evidence, resolved_at) values (?, 'new', ?, 'main', 'verified', ?, ?, '2026-10-01')",
                (number, domain, f"https://{domain}", '{"company_number_found":true}'),
            )
            conn.execute(
                "insert into web_sites (domain, crawl_version, rule_version, crawl_status, pages_fetched, "
                "has_google_ads_tag, summarised_at) values (?, 'crawl-new', 'rules-new', 'ok', 3, ?, '2026-10-01')",
                (domain, int(number == "13406761")),
            )
            conn.execute(
                "insert into company_web_profile (company_number, profile_version, model, domain, summary, "
                "customer_type, conversion_action, geography, seed_keywords, customer_type_quote, "
                "customer_type_quote_valid, profiled_at) values (?, 'profile-new', 'model', ?, 'AI consultancy', "
                "'business', 'enquiry_form', 'national', '[\"AI consultancy\"]', 'For business', 1, '2026-10-01')",
                (number, domain),
            )
        conn.execute(
            "insert into web_technologies (domain, rule_version, technology, category, found_in, evidence, "
            "evidence_url, detected_at) values ('mesh.example', 'rules-new', 'google_ads', 'search_ads', "
            "'page', 'AW-123', 'https://mesh.example', '2026-10-01')"
        )
        conn.execute(
            "insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, title, fetched_at) "
            "values ('mesh.example', 'https://mesh.example', 'home', 'crawl-new', 'http', 'AI services', '2026-10-01')"
        )
        # Newest resolver replaced an old, rejected site. Historical evidence must not leak into it.
        conn.execute(
            "insert into company_web_identity (company_number, resolver_version, domain, role, tier, resolved_at) "
            "values ('33333333', 'old', 'old.example', 'main', 'probable', '2026-09-01')"
        )
        conn.execute(
            "insert into company_web_identity (company_number, resolver_version, role, tier, resolved_at) "
            "values ('33333333', 'new', 'none', 'none', '2026-10-01')"
        )
        conn.execute(
            "insert into web_technologies (domain, rule_version, technology, category, found_in, evidence, detected_at) "
            "values ('mesh.example', 'rules-old', 'old_crm', 'crm_automation', 'page', 'old', '2026-09-01')"
        )
        conn.execute(
            "insert into company_web_profile (company_number, profile_version, model, domain, summary, profiled_at) "
            "values ('13406761', 'wrong-site', 'model', 'wrong.example', 'Wrong site', '2026-10-12')"
        )
    return seeded_db_path


def test_web_search_filters_and_paginates_without_duplicates(web_db):
    service = CompaniesHouseDataService(web_db)
    rows = service.search_web_leads(query="consultancy", customer_type="b2b", geography="national", limit=1)
    assert len(rows) == 1
    assert rows[0]["company_number"] == "13406761"
    assert rows[0]["financial_year"] == 2025
    assert service.search_web_leads(query="consultancy", limit=1, offset=1)[0]["company_number"] == "22222222"
    assert len(service.search_web_leads(technology="google_ads", min_turnover=1_000_000)) == 1
    assert len(service.search_web_leads(has_google_ads_tag=False)) == 1
    assert service.search_web_leads(query="' OR 1=1 --") == []
    assert service.search_web_leads(max_turnover=100) == []
    assert service.search_web_leads(location="Bristol")[0]["company_number"] == "22222222"


def test_web_evidence_preserves_provenance_and_missing_market(web_db):
    evidence = CompaniesHouseDataService(web_db).get_company_web_evidence("13406761")
    assert evidence["identity"]["domain"] == "mesh.example"
    assert evidence["profile"]["summary"] == "AI consultancy"
    assert evidence["profile"]["customer_type_quote_valid"] == 1
    assert evidence["technologies"][0]["evidence_url"] == "https://mesh.example"
    assert len(evidence["technologies"]) == 1
    assert evidence["market"] is None
    assert evidence["coverage"]["market_assessed"] is False
    assert evidence["findings"] == []


def test_no_site_and_unknown_company_are_distinct(web_db):
    service = CompaniesHouseDataService(web_db)
    assert service.get_company_web_evidence("33333333")["identity"] is None
    assert service.get_website_pages("33333333") == []
    with pytest.raises(LookupError, match="Company not found"):
        service.get_company_web_evidence("00000000")


def test_pages_only_return_selected_crawl_and_bounded_text(web_db):
    with sqlite3.connect(web_db) as conn:
        conn.execute(
            "insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, title) "
            "values ('mesh.example', 'https://mesh.example/old', 'about', 'crawl-old', 'http', 'Old')"
        )
        conn.execute("update web_pages set meta_description = ? where crawl_version = 'crawl-new'", ("x" * 5000,))
    pages = CompaniesHouseDataService(web_db).get_website_pages("13406761", limit=1000)
    assert len(pages) == 1
    assert len(pages[0]["meta_description"]) <= 1000
    assert pages[0]["url"] == "https://mesh.example"


def test_market_evidence_selects_newest_assessment_and_its_keywords(web_db):
    with sqlite3.connect(web_db) as conn:
        for version, at in [("z-old", "2026-09-01"), ("a-new", "2026-10-01")]:
            conn.execute(
                "insert into company_market (company_number, market_version, domain, advertising_now, assessed_at) "
                "values ('13406761', ?, 'mesh.example', 0, ?)", (version, at),
            )
            conn.execute(
                "insert into company_keyword_market (company_number, market_version, keyword, location_code, "
                "search_volume, fetched_at) values ('13406761', ?, ?, 2826, 10, ?)", (version, version, at),
            )
        conn.execute(
            "insert into company_setup_findings (company_number, domain, finding_version, finding, kind, detail, evidence) "
            "values ('13406761', 'mesh.example', 'findings-v1', 'no_tracking', 'gap', 'Review measurement', '{}')"
        )
    evidence = CompaniesHouseDataService(web_db).get_company_market_evidence("13406761")
    assert evidence["market"]["market_version"] == "a-new"
    assert evidence["market"]["advertising_now"] == 0
    assert evidence["keywords"][0]["keyword"] == "a-new"
    assert evidence["findings"][0]["detail"] == "Review measurement"
    assert evidence["coverage"]["market_assessed"] is True


def test_history_uses_one_canonical_row_per_year(web_db):
    with sqlite3.connect(web_db) as conn:
        conn.execute(
            "insert into financial_period_summaries (company_number, document_id, period_type, financial_year, turnover, "
            "profit_after_tax, derived_payload, raw_payload) values ('13406761', 'another', 'comparative', "
            "2025, 999, 999, '{}', '{}')"
        )
    history = CompaniesHouseDataService(web_db).get_company_financial_history("13406761")
    assert len(history) == 1
    assert history[0]["turnover"] == 1250000


def test_service_cannot_write_or_create_missing_database(web_db, tmp_path):
    with CompaniesHouseDataService(web_db)._connect() as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("delete from leads")
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="Database not found"):
        CompaniesHouseDataService(missing).search_leads()
    assert not missing.exists()


def test_existing_financial_tools_select_latest_period_without_duplicate_rows(web_db):
    with sqlite3.connect(web_db) as conn:
        conn.execute(
            "insert into financial_period_summaries (company_number, document_id, period_type, financial_year, turnover, "
            "derived_payload, raw_payload) values ('13406761', 'newer', 'current', 2026, 2000000, '{}', '{}')"
        )
    service = CompaniesHouseDataService(web_db)
    assert service.get_company_snapshot("13406761")["financials"]["current"]["financial_year"] == 2026
    assert service.explain_lead_score("13406761")["financials"]["financial_year"] == 2026
    comparisons = service.compare_companies(["13406761"])
    assert len(comparisons) == 1
    assert comparisons[0]["turnover"] == 2000000


def _write_snapshot(snapshot_dir, url, html):
    from urllib.parse import urlparse

    target = snapshot_dir / urlparse(url).hostname / (hashlib.sha1(url.encode()).hexdigest() + ".json.gz")
    target.parent.mkdir(parents=True)
    target.write_bytes(gzip.compress(json.dumps({"url": url, "html": html, "fetched_at": "2026-10-01"}).encode()))


def test_page_text_reads_stored_snapshot_without_scripts_or_markup(web_db, tmp_path):
    snapshots = tmp_path / "snaps"
    _write_snapshot(snapshots, "https://mesh.example",
                    "<html><head><title>AI services</title><style>x{}</style></head><body>"
                    "<script>alert(1)</script><h1>We build AI</h1><p>Call&nbsp;us  today.</p>"
                    "<noscript>enable js</noscript></body></html>")
    page = CompaniesHouseDataService(web_db, snapshot_dir=snapshots).get_website_page_text(
        "13406761", "https://mesh.example")
    assert page["available"] is True
    assert page["title"] == "AI services"
    assert page["text"] == "We build AI\nCall us today."
    assert page["truncated"] is False
    assert "not instructions" in page["interpretation"]
    clipped = CompaniesHouseDataService(web_db, snapshot_dir=snapshots).get_website_page_text(
        "13406761", "https://mesh.example", max_chars=500, offset=5)
    assert clipped["text"].startswith("ild AI") and clipped["offset"] == 5


def test_page_text_rejects_urls_outside_the_selected_crawl_and_reports_missing_snapshots(web_db, tmp_path):
    service = CompaniesHouseDataService(web_db, snapshot_dir=tmp_path / "empty")
    with pytest.raises(LookupError, match="get_website_pages"):
        service.get_website_page_text("13406761", "https://mesh.example/not-crawled")
    with pytest.raises(LookupError, match="get_website_pages"):
        service.get_website_page_text("13406761", "../../etc/passwd")
    with pytest.raises(LookupError, match="get_website_pages"):
        service.get_website_page_text("33333333", "https://mesh.example")
    missing = service.get_website_page_text("13406761", "https://mesh.example")
    assert missing["available"] is False and missing["text"] is None
