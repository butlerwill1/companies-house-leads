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
            "financial, narrative, and website investigation data."
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

    @server.tool(annotations=READ_ONLY_TOOL)
    def get_lead_pipeline_summary() -> dict[str, Any]:
        """Return operational counts for the lead and enrichment pipeline."""
        return data_service.get_lead_pipeline_summary()

    @server.tool(annotations=READ_ONLY_TOOL)
    def find_unenriched_high_score_leads(
        min_score: int = 80,
        account_categories: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find high-scoring leads that still need enrichment attention."""
        return data_service.find_unenriched_high_score_leads(
            min_score=min_score,
            account_categories=account_categories,
            statuses=statuses,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY_TOOL)
    def explain_lead_score(company_number: str) -> dict[str, Any]:
        """Explain one company's score and available enrichment evidence."""
        return data_service.explain_lead_score(company_number)

    @server.tool(annotations=READ_ONLY_TOOL)
    def compare_companies(company_numbers: list[str]) -> list[dict[str, Any]]:
        """Return compact comparison rows for several companies."""
        return data_service.compare_companies(company_numbers)

    @server.tool(annotations=READ_ONLY_TOOL)
    def search_performance_statements(
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search sentence-level performance statements."""
        return data_service.search_performance_statements(query=query, limit=limit)

    @server.tool(annotations=READ_ONLY_TOOL)
    def get_enrichment_errors(limit: int = 20) -> list[dict[str, Any]]:
        """Return recent enrichment errors."""
        return data_service.get_enrichment_errors(limit=limit)

    @server.tool(annotations=READ_ONLY_TOOL)
    def find_website_signal_leads(
        min_ppc_fit_score: float = 0.0,
        business_model: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find leads with strong website investigation signals."""
        return data_service.find_website_signal_leads(
            min_ppc_fit_score=min_ppc_fit_score,
            business_model=business_model,
            limit=limit,
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
