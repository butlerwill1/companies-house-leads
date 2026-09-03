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

# v2: Phase 2's taxonomy prune (36 -> 33 classes) and Phase 3a/3c's rewrite
# of the uncertainty instruction and per-value field definitions -- a
# different prompt and a different response schema (unclear now means "no
# signal at all" instead of "the default safe answer"), so runs against v1
# should not be compared against v2 as if they measured the same thing.
PROMPT_VERSION = "business-profile-v2"

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
    "b2b_relationship",
    "platform_intermediated",
    "not_customer_facing",
    "unclear",
)

# considered_b2b, tender_framework, and relationship_repeat were originally
# separate values (docs/BUSINESS_PROFILE_EXTRACTION.md once posed exactly
# this as an open question: "is relationship_repeat reliably distinguishable
# from considered_b2b in filed text?"). A 57-case gold-set run showed the
# answer is no -- narrative text rarely states whether repeat B2B trade was
# won by tender, referral, or research, so the model guessed among them
# about as often as it guessed right, and demand_model's accuracy (37-40%)
# was the worst of every field by a wide margin. Merged into one value; the
# distinction that actually matters for this field's purpose (is demand
# search-driven, i.e. can paid search work) is search vs not, not which
# non-search channel.
DEMAND_MODEL_DEFINITIONS: dict[str, str] = {
    "consumer_search": "individuals search online and buy directly (e.g. e-commerce retail)",
    "local_service": "individuals search for a nearby provider (e.g. opticians, restaurants)",
    "b2b_relationship": (
        "business customers arrive via research-then-enquire, a formal tender/framework/"
        "procurement process, or ongoing accounts, referrals and repeat trade -- i.e. any "
        "B2B channel that is not open competitive search"
    ),
    "platform_intermediated": "demand arrives via a marketplace, OTA, or aggregator (e.g. hotels booked through platforms)",
    "not_customer_facing": "a holding vehicle, SPV, or investment company with no customer-facing trade of its own",
    "unclear": "the text does not support a confident call",
}



# b2b2c dropped: in 57 hand-labelled cases no human ever chose it and the
# model never predicted it once. An option nobody uses is not free -- it is
# one more near-synonym for the model to hedge between, and this field's
# dominant error is already hedging (9 of 14 errors were `mixed` chosen over
# a clean b2c or b2b).
CUSTOMER_TYPE_VALUES = ("b2c", "b2b", "public_sector", "mixed", "unclear")

# saas merged into product_digital: SaaS is a digital product, the split had
# one gold example each, and nothing downstream treats them differently.
# rental_leasing and property are deliberately KEPT despite thin support --
# equipment and vehicle hire are among the most paid-search-driven categories
# there are, so the distinction changes the decision this stage exists to
# make. They get targeted labels instead of being merged away.
DELIVERY_MODEL_VALUES = (
    "product_physical",
    "product_digital",
    "professional_service",
    "trade_service",
    "contracting",
    "distribution_resale",
    "rental_leasing",
    "property",
    "unclear",
)

GEOGRAPHY_SERVED_VALUES = ("local", "regional", "national_uk", "international", "unclear")

# dormant dropped: Gate A already decides it deterministically and for free
# from structured data (core/company_triage.py, "no turnover and no
# employees"), and only 1 of the 2,960 companies that reach this stage with a
# filed narrative is dormant at all. Asking an LLM to re-derive a decision the
# free deterministic gate already made is pure waste. investment_holding is
# kept despite having no gold examples yet -- separating it from
# trading_group_parent is the entire reason this field exists (the 369
# turnover-without-employees companies Gate A explicitly refuses to guess
# about), so it gets targeted labels instead.
TRADING_STATUS_VALUES = (
    "trading",
    "investment_holding",
    "trading_group_parent",
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

# A one-line meaning for every value of every field. This exists because a
# bare list of enum names ("Allowed values: consumer_search, local_service,
# ...") gives the model nothing to reason from: these are our coinages, not
# terms it was trained to define the way we mean them. The evidence that this
# matters is direct -- the two fields whose prompts already carried per-value
# glosses (trading_status_confirmed, sic_agreement) ranked first and second on
# accuracy, the one field with none ranked last, and adding definitions to it
# moved it about +10 points.
#
# Several definitions below are written to counter a specific observed error
# rather than merely to describe the value; those carry a note saying so.
FIELD_DEFINITIONS: dict[str, dict[str, str]] = {
    "demand_model": DEMAND_MODEL_DEFINITIONS,
    "customer_type": {
        "b2c": "sells to individual consumers",
        "b2b": "sells to other businesses",
        "public_sector": "sells to government, councils, NHS, schools or similar public bodies",
        # Counter-error: 9 of 14 customer_type mistakes were `mixed` chosen
        # over a clean b2c or b2b. The model was using it as a hedge, so the
        # bar for it is stated explicitly rather than left to inference.
        "mixed": (
            "genuinely serves both consumers and businesses in significant proportion, and the "
            "text evidences BOTH. Do not choose this because you are unsure which one dominates "
            "-- if the text points mainly at one, choose that one"
        ),
        "unclear": "the text says nothing about who the customers are",
    },
    "delivery_model": {
        "product_physical": "makes or sells physical goods",
        "product_digital": "sells software, digital products, or software-as-a-service",
        "professional_service": "advisory or expert services delivered by people (consultancy, legal, accountancy, agency work)",
        "trade_service": "hands-on skilled work at a customer's site (plumbing, electrical, installation, repair)",
        "contracting": "delivers projects under contract, typically construction or engineering",
        "distribution_resale": "buys and resells others' goods (wholesale, distribution, dealership)",
        "rental_leasing": "rents or leases assets to customers rather than selling them (equipment, vehicles, plant hire)",
        "property": "owns, develops, or lets property as its business",
        "unclear": "the text does not say what is actually delivered",
    },
    "geography_served": {
        "local": "serves one town, city, or immediate area",
        "regional": "serves a region of the UK",
        "national_uk": "serves the UK broadly",
        # Counter-error: 6 of 14 geography mistakes were national_uk answered
        # as international. The model treated any foreign mention -- an
        # overseas parent, a subsidiary, an incidental export line -- as
        # evidence of international customers.
        "international": (
            "sells to CUSTOMERS outside the UK. A foreign parent company, an overseas subsidiary, "
            "a foreign shareholder, or an incidental export line is NOT enough on its own -- the "
            "text must indicate customers or markets abroad"
        ),
        "unclear": "the text does not indicate geographic reach",
    },
    "trading_status_confirmed": {
        "trading": "operates its own business with its own staff",
        "trading_group_parent": "a real trade filed through the top-of-group entity; the subsidiaries do the work and the narrative names an actual trade",
        "investment_holding": "owns shares or property and names no trade of its own",
        "spv": "a special-purpose financing, concession, or securitisation vehicle rather than a trading business",
        "unclear": "the narrative does not say enough to place it",
    },
    "sic_agreement": {
        "agrees": "the business described is consistent with the registered SIC classification",
        "disagrees": "the business described does not match the registered SIC classification",
        "unclear": "there is not enough description to judge against the SIC code",
    },
}


def format_field_options(field: str) -> str:
    """The allowed values for one field, each with its one-line meaning."""
    definitions = FIELD_DEFINITIONS[field]
    values = FIELD_VALUES.get(field) or SIC_AGREEMENT_VALUES
    return "\n".join(f"  {value} -- {definitions[value]}" for value in values)

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

For each field below, choose the single best-supported value and support it with a short \
quote copied EXACTLY (character for character) from the section text above.

Express uncertainty through the confidence number, not by withholding an answer. A call \
the text states outright gets high confidence; a reasonable inference from indirect \
evidence gets low confidence. Both are more useful than "unclear", because a low-confidence \
answer can be filtered later while a missing one cannot be recovered. Reserve "unclear" for \
when the text genuinely says nothing bearing on the question -- not for when the answer is \
merely implicit, or when you had to reason to reach it. When you do answer "unclear", leave \
the quote empty.

confidence -- a number from 0.0 to 1.0. Use the range honestly: it is what decides whether \
your answer is relied on, so a confident-sounding number on a weak inference is worse than \
a low one.

demand_model -- how customers actually arrive:
{demand_model_options}
customer_type -- who the customers are:
{customer_type_options}
delivery_model -- what is delivered and how:
{delivery_model_options}
geography_served -- geographic reach:
{geography_served_options}

Also provide:
business_description -- one plain sentence describing what the company actually does, in \
your own words based on the text.

trading_status_confirmed -- is this the entity that actually trades, or does the real \
business sit elsewhere in the group:
{trading_status_confirmed_options}

The company's registered SIC classification is: {sic_label} ({sic_code}).
sic_agreement -- does the text you read describe a business consistent with that \
classification. Give a one-sentence reason either way.
{sic_agreement_options}

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
        **prompt_option_blocks(),
        sic_label=sic_label or "(none declared)",
        sic_code=sic_code or "(none)",
    )


def prompt_option_blocks() -> dict[str, str]:
    """The `{<field>_options}` substitutions PROMPT_TEMPLATE expects.

    Kept separate from build_prompt so the whole-document comparison harness,
    which formats PROMPT_TEMPLATE itself against a different sections_block,
    cannot drift out of sync with the field definitions here."""
    return {f"{field}_options": format_field_options(field) for field in FIELD_DEFINITIONS}


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


_QUOTE_WHITESPACE_RE = re.compile(r"\s+")
# Punctuation not directly between two digits -- so "22,557,801" and "12.5%"
# stay intact (a comma/period there changes which number is being claimed),
# while a sentence's trailing full stop, a stray semicolon, or a straight
# vs curly quote does not (that's formatting, not a different claim).
_QUOTE_SOFT_PUNCT_RE = re.compile(r"(?<!\d)[.,;:!?\"'‘’“”()\[\]{}\-–—]+(?!\d)")


def normalize_quote_text(text: str) -> str:
    """Normalize a quote (or the source text it's checked against) before
    the verbatim-match check. Filed HTML tables collapse to markdown with
    inconsistent line breaks and spacing, and a model occasionally drops a
    trailing period, reformats a quotation mark, or re-cases the first
    letter of a sentence it is quoting mid-JSON-string ("The..." -> "the...",
    "DoBeDo..." -> "DOBEDO...") without changing what it is actually
    claiming -- that should not fail the hallucination check that this
    exists to run, which cares whether the words came from the source, not
    whether their capitalization survived being embedded in a JSON value.
    Confirmed live in the 2026-09-02 Phase 3d smoke test: two rejections
    were exactly this. See docs/BUSINESS_PROFILE_EXTRACTION.md for the real
    rejected quotes that motivated the rest of this normalization.

    Punctuation is replaced with a space, not deleted outright, and
    whitespace is re-collapsed afterward -- deleting it outright means
    whether a hyphen originally had spaces around it changes the result:
    "long-term" -> "longterm" but "long - term" -> "long term", two
    different strings for the same two words. Also confirmed live the same
    day: a model's quote used spaced-out hyphens where the source had none,
    for otherwise identical, correctly-quoted text."""
    text = text.casefold()
    text = _QUOTE_WHITESPACE_RE.sub(" ", text)
    text = _QUOTE_SOFT_PUNCT_RE.sub(" ", text)
    return _QUOTE_WHITESPACE_RE.sub(" ", text).strip()


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
        # Confidence is requested and returned but was, until now, never
        # checked -- a response with confidence 1.5, "high", or missing
        # entirely passed validation exactly like a real one. Checked for
        # every value including "unclear": the prompt asks for it
        # regardless (Phase 3a made confidence the way uncertainty gets
        # expressed at all), and every real response on hand already
        # includes 0.0 there, so this tightens nothing that was actually
        # in use. bool is excluded explicitly because Python's bool is an
        # int subclass -- True would otherwise silently pass as 1.0.
        confidence = entry.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            errors.append(f"{field}.confidence {confidence!r} must be a number between 0.0 and 1.0")
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
        elif normalize_quote_text(quote) not in normalize_quote_text(section_text):
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
