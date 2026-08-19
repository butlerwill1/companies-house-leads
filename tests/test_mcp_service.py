from __future__ import annotations


def test_search_leads_returns_ranked_matches(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.search_leads(query="mesh", min_score=70, limit=10)

    assert [row["company_number"] for row in results] == ["13406761", "22222222"]
    assert results[0]["lead_score"] == 82
    assert results[1]["lead_score"] == 76


def test_get_company_snapshot_returns_joined_company_context(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    snapshot = service.get_company_snapshot("13406761")

    assert snapshot["company"]["company_name"] == "MESH AI LTD"
    assert snapshot["lead"]["lead_score"] == 82
    assert snapshot["latest_filing"]["transaction_id"] == "tx-13406761-aa"
    assert snapshot["latest_document"]["document_id"] == "doc-13406761-aa"
    assert snapshot["financials"]["current"]["turnover"] == 1250000
    assert snapshot["financials"]["current"]["financial_year"] == 2025
    assert snapshot["website_investigation"]["final_domain"] == "mesh.ai"


def test_get_company_snapshot_raises_for_unknown_company(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    try:
        service.get_company_snapshot("00000000")
    except LookupError as exc:
        assert "00000000" in str(exc)
    else:
        raise AssertionError("Expected LookupError for missing company number")


def test_search_narrative_sections_returns_matching_excerpt(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.search_narrative_sections(query="demand", limit=5)

    assert len(results) == 1
    assert results[0]["company_number"] == "13406761"
    assert results[0]["section_key"] == "strategic_report"
    assert "demand remained strong" in results[0]["section_text"].lower()


def test_get_lead_pipeline_summary_returns_operational_counts(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    summary = service.get_lead_pipeline_summary()

    assert summary["lead_counts"]["total"] == 3
    assert summary["lead_counts"]["by_status"] == {"done": 1, "error": 1, "pending": 1}
    assert summary["lead_counts"]["by_account_category"]["FULL"] == 2
    assert summary["enrichment_counts"]["companies"] == 3
    assert summary["enrichment_counts"]["website_investigations"] == 1
    assert summary["text_counts"]["performance_statements"] == 1


def test_find_unenriched_high_score_leads_returns_pending_or_error_leads(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.find_unenriched_high_score_leads(
        min_score=80,
        account_categories=["GROUP", "FULL"],
        statuses=["pending", "error"],
        limit=10,
    )

    assert [row["company_number"] for row in results] == ["33333333"]
    assert results[0]["status"] == "error"
    assert results[0]["error_message"] == "HTTP 500 while fetching filing history"


def test_explain_lead_score_returns_reasons_and_missing_data_flags(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    explanation = service.explain_lead_score("22222222")

    assert explanation["company_number"] == "22222222"
    assert explanation["lead_score"] == 76
    assert explanation["score_reasons"] == ["tier-3 SIC", "established 7.3yr"]
    assert explanation["data_flags"]["has_financials"] is False
    assert explanation["data_flags"]["has_website_investigation"] is False


def test_compare_companies_returns_compact_ranked_rows(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.compare_companies(["22222222", "13406761"])

    assert [row["company_number"] for row in results] == ["13406761", "22222222"]
    assert results[0]["turnover"] == 1250000
    assert results[0]["sic_label"] == "Software / IT consultancy"
    assert results[0]["sic_group"] == "software_it_consultancy"
    assert results[0]["final_domain"] == "mesh.ai"
    assert results[1]["turnover"] is None


def test_search_performance_statements_returns_matching_sentences(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.search_performance_statements(query="growth", limit=10)

    assert len(results) == 1
    assert results[0]["company_number"] == "13406761"
    assert results[0]["page_number"] == 3
    assert "Revenue growth" in results[0]["statement_text"]


def test_get_enrichment_errors_returns_recent_errors(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.get_enrichment_errors(limit=10)

    assert [row["company_number"] for row in results] == ["33333333"]
    assert results[0]["lead_score"] == 88
    assert "HTTP 500" in results[0]["error_message"]


def test_find_website_signal_leads_filters_by_ppc_fit_score(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.find_website_signal_leads(
        min_ppc_fit_score=70,
        business_model="B2B service",
        limit=10,
    )

    assert [row["company_number"] for row in results] == ["13406761"]
    assert results[0]["ppc_fit_score"] == 74.5
    assert results[0]["final_domain"] == "mesh.ai"
