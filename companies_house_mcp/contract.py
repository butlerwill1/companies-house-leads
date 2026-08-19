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
        "description": "Return joined lead, filing, document, financial, and website context for one company.",
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
    {
        "name": "get_lead_pipeline_summary",
        "description": "Return operational counts for leads, enrichment outputs, text extraction, and website investigations.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "find_unenriched_high_score_leads",
        "description": "Find high-scoring leads that still need enrichment attention.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_score": {"type": "integer", "minimum": 0, "maximum": 100},
                "account_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                },
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
        "name": "explain_lead_score",
        "description": "Explain one company's lead score and identify which enrichment data is present.",
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
        "name": "compare_companies",
        "description": "Return compact comparison rows for several companies.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "company_numbers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 100,
                },
            },
            "required": ["company_numbers"],
        },
    },
    {
        "name": "search_performance_statements",
        "description": "Search sentence-level performance statements extracted from reports.",
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
        "name": "get_enrichment_errors",
        "description": "Return recent lead enrichment errors.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    },
    {
        "name": "find_website_signal_leads",
        "description": "Find leads with strong website investigation signals.",
        "annotations": READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_ppc_fit_score": {"type": "number", "minimum": 0},
                "business_model": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    },
]
