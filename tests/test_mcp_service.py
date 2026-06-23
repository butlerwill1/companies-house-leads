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
    assert snapshot["ppc_estimate"]["estimated_monthly_ppc_spend"] == 2083.33
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


def test_get_top_ppc_candidates_returns_ranked_financial_leads(seeded_db_path) -> None:
    from companies_house_mcp.service import CompaniesHouseDataService

    service = CompaniesHouseDataService(seeded_db_path)

    results = service.get_top_ppc_candidates(min_monthly=1000.0, limit=10)

    assert len(results) == 1
    assert results[0]["company_number"] == "13406761"
    assert results[0]["estimated_monthly_ppc_spend"] == 2083.33
