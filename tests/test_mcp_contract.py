from __future__ import annotations


def test_tool_contract_exposes_expected_read_only_tools() -> None:
    from companies_house_mcp.contract import TOOL_DEFINITIONS

    definitions = {tool["name"]: tool for tool in TOOL_DEFINITIONS}

    assert set(definitions) == {
        "search_leads",
        "get_company_snapshot",
        "search_narrative_sections",
        "get_website_investigation",
        "get_lead_pipeline_summary",
        "find_unenriched_high_score_leads",
        "explain_lead_score",
        "compare_companies",
        "search_performance_statements",
        "get_enrichment_errors",
        "find_website_signal_leads",
    }

    assert definitions["search_leads"]["annotations"]["readOnlyHint"] is True
    assert definitions["get_company_snapshot"]["inputSchema"]["required"] == ["company_number"]
    assert definitions["search_narrative_sections"]["inputSchema"]["required"] == ["query"]


def test_search_leads_contract_accepts_filters_and_limit() -> None:
    from companies_house_mcp.contract import TOOL_DEFINITIONS

    search_leads = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "search_leads")
    properties = search_leads["inputSchema"]["properties"]

    assert set(properties) >= {"query", "min_score", "statuses", "limit"}
    assert properties["min_score"]["type"] == "integer"
    assert properties["statuses"]["type"] == "array"
    assert properties["limit"]["maximum"] == 100


def test_new_tool_contracts_define_required_inputs() -> None:
    from companies_house_mcp.contract import TOOL_DEFINITIONS

    definitions = {tool["name"]: tool for tool in TOOL_DEFINITIONS}

    assert definitions["explain_lead_score"]["inputSchema"]["required"] == ["company_number"]
    assert definitions["compare_companies"]["inputSchema"]["required"] == ["company_numbers"]
    assert definitions["search_performance_statements"]["inputSchema"]["required"] == ["query"]
    assert definitions["find_website_signal_leads"]["inputSchema"]["properties"]["min_ppc_fit_score"]["type"] == "number"
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in TOOL_DEFINITIONS)
