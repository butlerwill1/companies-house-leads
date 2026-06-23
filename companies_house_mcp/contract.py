from __future__ import annotations

READ_ONLY = {"readOnlyHint": True}


TOOL_DEFINITIONS = [
    {
        "name": "search_leads",
        "description": "Search lead records by company name or company number.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "min_score": {"type": "integer", "minimum": 0, "maximum": 100},
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    },
    {
        "name": "get_company_snapshot",
        "description": "Return joined lead, filing, document, financial, PPC, and website context for one company.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_number": {"type": "string"},
            },
            "required": ["company_number"],
        },
    },
    {
        "name": "get_top_ppc_candidates",
        "description": "Return companies ranked by estimated monthly PPC spend.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_monthly": {"type": "number", "minimum": 0},
                "max_monthly": {"type": "number", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    },
    {
        "name": "search_narrative_sections",
        "description": "Search extracted narrative report sections.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_website_investigation",
        "description": "Return the latest stored website investigation for one company.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_number": {"type": "string"},
                "source_label": {"type": "string"},
            },
            "required": ["company_number"],
        },
    },
]
