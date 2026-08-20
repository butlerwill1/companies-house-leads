"""Taxonomy, prompt, and response validation for the business-profile stage
(Gate A2). See docs/BUSINESS_PROFILE_EXTRACTION.md for the design rationale.

The policy deliberately makes hallucination checkable rather than trusted:
every classification must carry a verbatim quote from the source text, and
a quote that does not appear in the section it claims to come from is
rejected here, before anything is persisted. No field is ever accepted on
the model's self-reported confidence alone.
"""

from __future__ import annotations

import json
import re
from typing import Any

PROMPT_VERSION = "business-profile-v1"

# Sections read in priority order. Sections flagged is_auditor_text by
# core/companies_house_pdf_text.py are excluded by the caller before this
# module ever sees them -- that text is the auditor describing its audit,
# not the company describing itself.
NARRATIVE_SECTION_PRIORITY = (
    # Financial notes, not qualitative narrative -- but the decisive evidence
    # for geography_served and customer_type in practice: the turnover note's
    # geographic/class-of-business split settles calls the prose sections
    # often leave unclear (14 of 47 gold-set geography_served labels turned
    # on this note alone). Ranked first for exactly that reason.
    "turnover_note",
    "employee_note",
    "principal_activity",
    "business_review",
    "strategic_report",
    "directors_report",
    "principal_risks",
    "future_developments",
)

DEMAND_MODEL_VALUES = (
    "consumer_search",
    "local_service",
    "considered_b2b",
    "tender_framework",
    "relationship_repeat",
    "platform_intermediated",
    "wholesale_contract",
    "not_customer_facing",
    "unclear",
)

CUSTOMER_TYPE_VALUES = ("b2c", "b2b", "b2b2c", "public_sector", "mixed", "unclear")

DELIVERY_MODEL_VALUES = (
    "product_physical",
    "product_digital",
    "saas",
    "professional_service",
    "trade_service",
    "contracting",
    "distribution_resale",
    "rental_leasing",
    "property",
    "unclear",
)

GEOGRAPHY_SERVED_VALUES = ("local", "regional", "national_uk", "international", "unclear")

TRADING_STATUS_VALUES = (
    "trading",
    "investment_holding",
    "trading_group_parent",
    "dormant",
    "spv",
    "unclear",
)

SIC_AGREEMENT_VALUES = ("agrees", "disagrees", "unclear")

FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "demand_model": DEMAND_MODEL_VALUES,
    "customer_type": CUSTOMER_TYPE_VALUES,
    "delivery_model": DELIVERY_MODEL_VALUES,
    "geography_served": GEOGRAPHY_SERVED_VALUES,
    "trading_status_confirmed": TRADING_STATUS_VALUES,
}

# The classification fields precede sic_agreement in every prompt and
# response ordering in this module. In an autoregressive model, tokens
# generated earlier condition tokens generated later: if the SIC label were
# shown or asked about before the model describes the business in its own
# words, the description anchors on it and the independent read -- the
# entire purpose of this stage -- is lost. sic_label is deliberately the
# last thing given to the model, after it has already made its calls.
PROMPT_TEMPLATE = """You are reading a UK company's filed annual report to record how it \
acquires customers. Base every answer only on the text given below. Do not use outside \
knowledge of the company or the industry.

Company name: {company_name}

Filed narrative sections:
{sections_block}

For each of the four fields below, choose exactly one value from its allowed list, and \
support it with a short quote copied EXACTLY (character for character) from the section \
text above. If no part of the text supports a confident choice, use "unclear" and leave \
the quote empty. Never guess to avoid saying unclear -- unclear is a correct answer, not \
a failure.

demand_model -- how customers actually arrive. Allowed values: {demand_model_values}
customer_type -- who the customers are. Allowed values: {customer_type_values}
delivery_model -- what is delivered and how. Allowed values: {delivery_model_values}
geography_served -- geographic reach. Allowed values: {geography_served_values}

Also provide:
business_description -- one plain sentence describing what the company actually does, in \
your own words based on the text.

trading_status_confirmed -- is this an operating trading business, an investment holding \
company with no trade of its own, a parent filing through a trading group, dormant, or a \
special-purpose vehicle. Allowed values: {trading_status_values}

The company's registered SIC classification is: {sic_label} ({sic_code}).
sic_agreement -- does the text you read describe a business consistent with that \
classification. Allowed values: {sic_agreement_values}. Give a one-sentence reason either way.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{
  "business_description": "...",
  "demand_model": {{"value": "...", "confidence": 0.0, "quote": "...", "section": "..."}},
  "customer_type": {{"value": "...", "confidence": 0.0, "quote": "...", "section": "..."}},
  "delivery_model": {{"value": "...", "confidence": 0.0, "quote": "...", "section": "..."}},
  "geography_served": {{"value": "...", "confidence": 0.0, "quote": "...", "section": "..."}},
  "trading_status_confirmed": {{"value": "...", "confidence": 0.0, "quote": "...", "section": "..."}},
  "sic_agreement": {{"value": "...", "reason": "..."}}
}}

"section" must be one of the section names shown above (e.g. "principal_activity"). \
"quote" must be a substring you could find with Ctrl-F in that section's text -- do not \
paraphrase, summarise, or add ellipses."""


def build_sections_block(sections: dict[str, str]) -> str:
    parts = []
    for key in NARRATIVE_SECTION_PRIORITY:
        text = sections.get(key)
        if text:
            parts.append(f"[{key}]\n{text}")
    return "\n\n".join(parts) if parts else "(no usable narrative text available)"


def build_prompt(*, company_name: str, sections: dict[str, str], sic_label: str | None, sic_code: str | None) -> str:
    return PROMPT_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        sections_block=build_sections_block(sections),
        demand_model_values=", ".join(DEMAND_MODEL_VALUES),
        customer_type_values=", ".join(CUSTOMER_TYPE_VALUES),
        delivery_model_values=", ".join(DELIVERY_MODEL_VALUES),
        geography_served_values=", ".join(GEOGRAPHY_SERVED_VALUES),
        trading_status_values=", ".join(TRADING_STATUS_VALUES),
        sic_agreement_values=", ".join(SIC_AGREEMENT_VALUES),
        sic_label=sic_label or "(none declared)",
        sic_code=sic_code or "(none)",
    )


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse a model's JSON response, tolerating a markdown code fence --
    the one repair that cannot invent a value. Anything else that fails to
    parse is a hard error, not silently patched."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", cleaned)
    if fenced:
        cleaned = fenced.group(1)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("model response must be a JSON object")
    return payload


def validate_response(payload: dict[str, Any], sections: dict[str, str]) -> list[str]:
    """Return a list of problems (empty means valid). Every problem here
    means the extraction is rejected outright -- there is no partial-credit
    persistence of a response that fails validation."""
    errors: list[str] = []

    description = payload.get("business_description")
    if not isinstance(description, str) or not description.strip():
        errors.append("business_description is missing or empty")

    for field, allowed in FIELD_VALUES.items():
        entry = payload.get(field)
        if not isinstance(entry, dict):
            errors.append(f"{field} is missing or not an object")
            continue
        value = entry.get("value")
        if value not in allowed:
            errors.append(f"{field}.value {value!r} is not one of {allowed}")
            continue
        quote = entry.get("quote") or ""
        if value == "unclear":
            continue
        if not quote:
            errors.append(f"{field} has value {value!r} but no supporting quote")
            continue
        section_name = entry.get("section")
        section_text = sections.get(section_name) if section_name else None
        if section_text is None:
            errors.append(f"{field}.section {section_name!r} is not one of the sections given to the model")
        elif quote not in section_text:
            errors.append(f"{field}.quote does not appear verbatim in section {section_name!r}: {quote!r}")

    sic = payload.get("sic_agreement")
    if not isinstance(sic, dict) or sic.get("value") not in SIC_AGREEMENT_VALUES:
        errors.append(f"sic_agreement.value must be one of {SIC_AGREEMENT_VALUES}")

    return errors


def select_narrative_sections(all_sections: dict[str, dict[str, Any]]) -> dict[str, str]:
    """From the full stored section payload (section_key -> {text,
    is_auditor_text, ...}), keep only company-authored text in the priority
    list this stage reads."""
    return {
        key: entry["text"]
        for key, entry in all_sections.items()
        if key in NARRATIVE_SECTION_PRIORITY and entry.get("text") and not entry.get("is_auditor_text")
    }
