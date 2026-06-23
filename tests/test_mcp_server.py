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
            "get_top_ppc_candidates",
            "search_narrative_sections",
            "get_website_investigation",
        }

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
