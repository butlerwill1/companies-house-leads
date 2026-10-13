from __future__ import annotations

import anyio


def test_mcp_server_registers_expected_tools(seeded_db_path) -> None:
    from companies_house_mcp.server import create_mcp_server

    async def run() -> None:
        server = create_mcp_server(seeded_db_path)
        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert tool_names == {
            "search_leads",
            "get_company_snapshot",
            "search_narrative_sections",
            "get_lead_pipeline_summary",
            "find_unenriched_high_score_leads",
            "explain_lead_score",
            "compare_companies",
            "search_performance_statements",
            "get_enrichment_errors",
            "search_web_leads",
            "get_company_web_evidence",
            "get_website_pages",
            "get_website_page_text",
            "get_company_market_evidence",
            "get_company_financial_history",
        }

    anyio.run(run)


def test_registered_web_tools_match_contract_and_delegate(seeded_db_path):
    from companies_house_mcp.contract import TOOL_DEFINITIONS
    from companies_house_mcp.server import create_mcp_server

    async def run():
        server = create_mcp_server(seeded_db_path)
        contracts = {t["name"]: t for t in TOOL_DEFINITIONS}
        for tool in await server.list_tools():
            assert set(tool.inputSchema["properties"]) == set(contracts[tool.name]["inputSchema"]["properties"])
            assert tool.inputSchema.get("required", []) == contracts[tool.name]["inputSchema"]["required"]
            assert tool.annotations.readOnlyHint is True
        for name in ["get_company_web_evidence", "get_website_pages", "get_company_market_evidence",
                     "get_company_financial_history"]:
            content, structured = await server.call_tool(name, {"company_number": "13406761"})
            assert structured is not None
        _, structured = await server.call_tool("search_web_leads", {"technology": "google_ads"})
        assert structured["result"] == []

    anyio.run(run)


def test_mcp_server_pipeline_summary_tool_delegates_to_service(seeded_db_path) -> None:
    from companies_house_mcp.server import create_mcp_server

    async def run() -> None:
        server = create_mcp_server(seeded_db_path)

        _content, structured = await server.call_tool("get_lead_pipeline_summary", {})

        assert structured["lead_counts"]["total"] == 3
        assert structured["lead_counts"]["by_status"]["error"] == 1

    anyio.run(run)


def test_mcp_server_explain_lead_score_tool_delegates_to_service(seeded_db_path) -> None:
    from companies_house_mcp.server import create_mcp_server

    async def run() -> None:
        server = create_mcp_server(seeded_db_path)

        _content, structured = await server.call_tool(
            "explain_lead_score",
            {"company_number": "22222222"},
        )

        assert structured["company_number"] == "22222222"
        assert structured["score_reasons"] == ["tier-3 SIC", "established 7.3yr"]

    anyio.run(run)


def test_mcp_server_search_leads_tool_delegates_to_service(seeded_db_path) -> None:
    from companies_house_mcp.server import create_mcp_server

    async def run() -> None:
        server = create_mcp_server(seeded_db_path)

        _content, structured = await server.call_tool(
            "search_leads",
            {"query": "mesh", "min_score": 70, "limit": 10},
        )

        assert [row["company_number"] for row in structured["result"]] == [
            "13406761",
            "22222222",
        ]

    anyio.run(run)


def test_mcp_server_company_snapshot_tool_returns_joined_context(seeded_db_path) -> None:
    from companies_house_mcp.server import create_mcp_server

    async def run() -> None:
        server = create_mcp_server(seeded_db_path)

        _content, structured = await server.call_tool(
            "get_company_snapshot",
            {"company_number": "13406761"},
        )

        snapshot = structured
        assert snapshot["company"]["company_name"] == "MESH AI LTD"
        assert snapshot["financials"]["current"]["turnover"] == 1250000

    anyio.run(run)
