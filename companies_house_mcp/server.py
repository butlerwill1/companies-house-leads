from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .service import CompaniesHouseDataService


READ_ONLY_TOOL = ToolAnnotations(readOnlyHint=True)


def create_mcp_server(
    db_path: str | Path = "companies-house.db",
    *,
    service: CompaniesHouseDataService | None = None,
) -> FastMCP:
    data_service = service or CompaniesHouseDataService(db_path)
    server = FastMCP(
        name="companies-house-leads",
        instructions=(
            "Use these read-only tools to inspect Companies House lead, "
            "financial, PPC, narrative, and website investigation data."
        ),
    )

    @server.tool(annotations=READ_ONLY_TOOL)
    def search_leads(
        query: str | None = None,
        min_score: int | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search lead records by company name or company number."""
        return data_service.search_leads(
            query=query,
            min_score=min_score,
            statuses=statuses,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY_TOOL)
    def get_company_snapshot(company_number: str) -> dict[str, Any]:
        """Return joined context for one company."""
        return data_service.get_company_snapshot(company_number)

    @server.tool(annotations=READ_ONLY_TOOL)
    def get_top_ppc_candidates(
        min_monthly: float = 0.0,
        max_monthly: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return companies ranked by estimated monthly PPC spend."""
        return data_service.get_top_ppc_candidates(
            min_monthly=min_monthly,
            max_monthly=max_monthly,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY_TOOL)
    def search_narrative_sections(
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search extracted narrative report sections."""
        return data_service.search_narrative_sections(query=query, limit=limit)

    @server.tool(annotations=READ_ONLY_TOOL)
    def get_website_investigation(
        company_number: str,
        source_label: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest stored website investigation for one company."""
        return data_service.get_website_investigation(
            company_number,
            source_label=source_label,
        )

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Companies House leads MCP server.")
    parser.add_argument("--db", default="companies-house.db", help="SQLite database path.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport to run.",
    )
    args = parser.parse_args(argv)

    server = create_mcp_server(args.db)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
