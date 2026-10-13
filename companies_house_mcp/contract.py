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
        "description": "Return joined lead, filing, document, and financial context for one company.",
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
        "name": "get_lead_pipeline_summary",
        "description": "Return operational counts for leads, enrichment outputs, and text extraction.",
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
]

LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
OFFSET = {"type": "integer", "minimum": 0}
COMPANY = {"type": "string"}

TOOL_DEFINITIONS.extend([
    {
        "name": "search_web_leads",
        "description": "Shortlist stored chosen websites with business, technology and latest financial filters.",
        "annotations": READ_ONLY,
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "customer_type": {"type": "string"},
            "geography": {"type": "string"}, "technology": {"type": "string"},
            "location": {"type": "string"},
            "has_google_ads_tag": {"type": "boolean"},
            "min_turnover": {"type": "number"}, "max_turnover": {"type": "number"},
            "limit": LIMIT, "offset": OFFSET,
        }, "required": []},
    },
    {
        "name": "get_website_page_text",
        "description": "Read the visible text of one page from the selected website's stored snapshot.",
        "annotations": READ_ONLY,
        "inputSchema": {"type": "object", "properties": {
            "company_number": COMPANY, "url": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000}, "offset": OFFSET,
        }, "required": ["company_number", "url"]},
    },
    *[{
        "name": name, "description": description, "annotations": READ_ONLY,
        "inputSchema": {"type": "object", "properties": {
            "company_number": COMPANY, "limit": LIMIT,
            **({"offset": OFFSET} if name == "get_website_pages" else {}),
        }, "required": ["company_number"]},
    } for name, description in [
        ("get_company_web_evidence", "Read website identity, Maps, profile quotes, crawl and technology evidence."),
        ("get_website_pages", "Read page metadata and URLs from the selected website's latest stored crawl."),
        ("get_company_market_evidence", "Read saved advertising, keyword demand and gap/strength evidence."),
        ("get_company_financial_history", "Read canonical financial history with provenance, one row per year."),
    ]],
])
