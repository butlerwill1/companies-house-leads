#!/usr/bin/env python3
"""W3 site profile: prompt, input shaping and response checks (docs/WEB_STAGE_PLAN.md).

One text-only model call per company reads the text of its website (from the
W2 crawl), the principal activity from its filing and, when W1 found one, its
Google Maps category. It answers what the business sells, to whom, how a
customer converts, how far it serves, how urgent and how big a purchase is,
whether search or social suits it, and which phrases a customer would type.

The same rules as the other model stages in this repository:

- every label except the summary carries a quote from what the model was shown
  (site text, principal activity, Maps category), checked here with the
  business-profile stage's bounded tolerance and the kind of match recorded
  (`*_quote_match`); a quote that still does not validate gets one retry
  (web_profile_eval.run_case), and if it fails again it is flagged
  (`*_quote_valid = 0`) and the value kept, never silently dropped;
- a value outside its enumeration becomes `unclear`;
- the Google category is the Maps listing's own when there is one
  (`google_listing`); otherwise the model picks from a shortlist taken from
  Google's business-category list (`model_assigned`), and a pick that is not on
  the list is discarded;
- seed phrases that contain the company's own name are removed: a search for
  the brand measures the brand, not the market.

Nothing here calls a model; scripts/web/web_profile_eval.py does.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from core.llm_validation import StrictResponseModel, JsonResponseError, parse_json_object, validate_object, validation_message
from scripts.business_profile_classifier import business_profile_policy as business_policy

PROMPT_VERSION = "web-profile-v3-enquiry-tender"

CUSTOMER_TYPES = ("consumer", "business", "mixed", "unclear")
CONVERSIONS = ("buy_online", "book", "call", "enquiry_form", "visit", "unclear")
# Values earlier prompt versions used: quote_form was renamed (a callback request is
# an enquiry form too), and tender became its own flag, wins_by_tender.
RETIRED_CONVERSIONS = {"quote_form": "enquiry_form"}
TENDER_VALUES = ("yes", "no", "unclear")
GEOGRAPHIES = ("local", "regional", "national", "international", "unclear")
URGENCIES = ("emergency", "planned", "considered", "unclear")
TICKET_BANDS = ("under_100", "100_to_1000", "1000_to_10000", "over_10000", "unclear")
CHANNEL_FITS = ("search", "social", "both", "unclear")
QUOTED_FIELDS = ("customer_type", "conversion_action", "geography", "wins_by_tender")
MIN_SEED_KEYWORDS = 5
MAX_SEED_KEYWORDS = 20
MAX_KEYWORD_WORDS = 8
CATEGORY_SHORTLIST = 25
MAX_TEXT_CHARS = 12_000


class _QuotedResponse(StrictResponseModel):
    value: str | bool
    quote: str | None = None
    main_town: str | None = None


class _CategoryResponse(StrictResponseModel):
    value: str | None = None


class _WebProfileResponse(StrictResponseModel):
    summary: str | None = None
    products_services: list[str] | None = None
    customer_type: _QuotedResponse | str | None = None
    conversion_action: _QuotedResponse | str | None = None
    geography: _QuotedResponse | str | None = None
    wins_by_tender: _QuotedResponse | str | None = None
    urgency: str | None = None
    ticket_band: str | None = None
    channel_fit: str | None = None
    google_category: _CategoryResponse | str | None = None
    seed_keywords: list[str] | None = None

TEMPLATE = """You profile a UK business from the text of its website, for someone deciding whether it is a good \
prospect for paid search advertising (Google Ads). Use only the text below.

Company: {company_name}
Principal activity in its latest filed accounts: {principal_activity}
Google Maps category: {listing_category}

Answer with one JSON object and nothing else:
{{
  "summary": "one or two plain sentences: what it sells and to whom",
  "products_services": ["up to 8 short items"],
  "customer_type": {{"value": "consumer | business | mixed | unclear", "quote": "<exact words from the text>"}},
  "conversion_action": {{"value": "buy_online | book | call | enquiry_form | visit | unclear", "quote": "<exact words>"}},
  "geography": {{"value": "local | regional | national | international | unclear", "main_town": "<town or null>", "quote": "<exact words>"}},
  "wins_by_tender": {{"value": "yes | no | unclear", "quote": "<exact words, needed for yes>"}},
  "urgency": "emergency | planned | considered | unclear",
  "ticket_band": "under_100 | 100_to_1000 | 1000_to_10000 | over_10000 | unclear  (typical value of one sale)",
  "channel_fit": "search | social | both | unclear  (search suits people who already know what they need; social suits visual, impulse or lifestyle purchases)",
  "google_category": {category_instruction},
  "seed_keywords": ["{min_keywords} to {max_keywords} phrases a customer would type into Google to find this kind of business; no brand names"]
}}

Rules:
- Each "quote" must be copied exactly, a sentence or phrase that justifies the value. It may come from the website text or from the principal activity line above. Copy one continuous passage: do not shorten words, reword, or join separate passages with "...". If nothing shows it, use "unclear" and an empty quote.
- conversion_action is the main route the website offers a new customer, judged by what the site pushes hardest: "buy_online" (basket or checkout), "book" (appointment, table, room, viewing), "call" (phone is the main route and there is no form), "enquiry_form" (any form the customer fills in: quote request, enquiry, claim form, callback request), "visit" (walk-in shop or site). If a form and a phone number are offered equally, answer "enquiry_form".
- wins_by_tender is "yes" when much of the business comes through public tenders, procurement frameworks or contracted public programmes, which advertising does not reach; quote the text that shows it. "no" when the site shows customers coming to it directly; "unclear" if it cannot be told.
- Do not guess from the company name. If the website text is thin, answer "unclear" rather than invent.

Website text:
{text}
"""

CATEGORY_WITH_LISTING = '"{listing}" (already known; copy it)'
CATEGORY_FROM_SHORTLIST = 'one of: {categories}, or null if none fits'


def build_prompt(*, company_name: str, principal_activity: str | None, listing_category: str | None, text: str,
                 categories: Iterable[str] = ()) -> str:
    categories = list(categories)
    if listing_category:
        instruction = CATEGORY_WITH_LISTING.format(listing=listing_category)
    else:
        instruction = CATEGORY_FROM_SHORTLIST.format(categories=", ".join(f'"{c}"' for c in categories) or "null")
    return TEMPLATE.format(
        company_name=company_name, principal_activity=principal_activity or "not available",
        listing_category=listing_category or "none found", category_instruction=instruction,
        min_keywords=MIN_SEED_KEYWORDS + 5, max_keywords=MAX_SEED_KEYWORDS, text=text[:MAX_TEXT_CHARS])


# ---------------------------------------------------------------- input shaping

def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", (text or "").lower())}


def shortlist_categories(categories: list[dict[str, Any]], text: str, *, limit: int = CATEGORY_SHORTLIST) -> list[str]:
    """The categories whose words overlap most with the text (a business's
    filing activity plus the start of its site), the more-used ones first among
    ties. `categories`: `{"category_name", "business_count"}` from DataForSEO's
    free list. A cheap keyword filter so the model chooses from 25, not 5,000."""
    words = _tokens(text)
    scored: list[tuple[float, str]] = []
    for item in categories:
        name = item.get("category_name") or ""
        parts = _tokens(name.replace("_", " "))
        overlap = len(parts & words)
        if not overlap:
            continue
        score = overlap + overlap / len(parts) + math.log10((item.get("business_count") or 0) + 1) / 100
        scored.append((score, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:limit]]


# ---------------------------------------------------------------- response checks

def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


QUOTE_MATCH_JOINED = "joined"
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.+|…)\s*")
_MATCH_STRICTNESS = (business_policy.QUOTE_MATCH_EXACT, business_policy.QUOTE_MATCH_TABLE_ROW,
                     business_policy.QUOTE_MATCH_FUZZY)


def quote_match(quote: str | None, sources: Iterable[str]) -> str | None:
    """How the quote was found in what the model was shown, or None if it was not.

    `sources` is everything in the prompt: the site text, the filing's
    principal-activity line and the Maps category. The first run checked the
    site text only, and five of its nine failed quotes were word-for-word
    copies of the principal activity, which the prompt shows the model.

    The match is the business-profile stage's (`quote_match_kind`): exact,
    ignoring case, whitespace and soft punctuation; one column of a table
    row; or the bounded fuzzy match (one differing ordinary word per eight,
    none of them a number, a negation or a word a label turns on). A quote
    that joins passages with "..." is `joined` when every piece matches on
    its own: the model is citing two places, not inventing a sentence."""
    text = (quote or "").strip().strip(" .\"'“”‘’")
    if not text:
        return None
    sources = [s for s in sources if s]
    pieces = [p for p in _ELLIPSIS_RE.split(text) if p.strip(" .,;:")]
    if len(pieces) > 1:
        return QUOTE_MATCH_JOINED if all(quote_match(p, sources) for p in pieces) else None
    kinds = {business_policy.quote_match_kind(text, source) for source in sources}
    return next((kind for kind in _MATCH_STRICTNESS if kind in kinds), None)


def quote_in_text(quote: str | None, text: str) -> bool:
    """The quote is found in the text (see `quote_match`). An empty quote is
    not valid evidence."""
    return quote_match(quote, [text]) is not None


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    value = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if allowed is CONVERSIONS:
        value = RETIRED_CONVERSIONS.get(value, value)
    if allowed is TENDER_VALUES and value in ("true", "false"):
        value = "yes" if value == "true" else "no"
    return value if value in allowed else "unclear"


def _clean_json(raw: str | None) -> dict[str, Any] | None:
    try:
        return parse_json_object(raw).payload
    except JsonResponseError:
        return None


def seed_keywords(values: Any, brand_terms: Iterable[str] = ()) -> list[str]:
    """Lower-cased, de-duplicated phrases of 1 to 8 words, minus any that contain a brand term."""
    brands = [b for b in (_norm(t) for t in brand_terms) if len(b) >= 3]
    out: list[str] = []
    for value in values if isinstance(values, list) else []:
        phrase = re.sub(r"[^a-z0-9£&' \-]", " ", str(value).lower())
        phrase = " ".join(phrase.split())
        words = phrase.split()
        if not words or len(words) > MAX_KEYWORD_WORDS or phrase in out:
            continue
        if any(brand in phrase for brand in brands):
            continue
        out.append(phrase)
    return out[:MAX_SEED_KEYWORDS]


def _quoted(data: dict[str, Any], field: str, allowed: tuple[str, ...], sources: list[str]) -> dict[str, Any]:
    block = data.get(field)
    value = block.get("value") if isinstance(block, dict) else block
    quote = (block.get("quote") if isinstance(block, dict) else None) or None
    value = _enum(value, allowed)
    if value == "unclear" or (field == "wins_by_tender" and value == "no"):
        # a "no" is the absence of tender evidence: there is nothing to quote
        return {"value": value, "quote": quote if value != "unclear" else None, "quote_valid": None, "quote_match": None}
    kind = quote_match(quote, sources)
    return {"value": value, "quote": quote, "quote_valid": int(kind is not None), "quote_match": kind}


def quote_failures(profile: dict[str, Any]) -> dict[str, str | None]:
    """The quoted fields whose quote was not found: `{field: quote}`."""
    return {f: profile.get(f"{f}_quote") for f in QUOTED_FIELDS if profile.get(f"{f}_quote_valid") == 0}


RETRY_TEMPLATE = """{prompt}

Your previous answer was:
{raw}

These quotes were not found word for word in the text above:
{failures}

Answer again with the complete JSON object. For each field listed, copy one continuous passage exactly as it \
appears in the website text or the principal activity line, or set its value to "unclear" with an empty quote. \
Keep every other answer unless you find it was wrong."""


def build_retry_prompt(prompt: str, raw: str, failures: dict[str, str | None]) -> str:
    """The one follow-up a company gets when a quote is not found: the first
    prompt, the model's answer, and which quotes failed."""
    listed = "\n".join(f'- {field}: "{quote or ""}"' for field, quote in failures.items())
    return RETRY_TEMPLATE.format(prompt=prompt, raw=raw, failures=listed)


def parse_profile(raw: str | None, text: str, *, listing_category: str | None = None,
                  allowed_categories: Iterable[str] = (), brand_terms: Iterable[str] = (),
                  principal_activity: str | None = None) -> dict[str, Any]:
    """The columns of `company_web_profile` from a model response. Never raises:
    an unusable response gives empty fields and a `problem`. Quotes are checked
    against everything the prompt showed: the site text, the principal
    activity and the Maps category."""
    try:
        parsed = parse_json_object(raw)
    except JsonResponseError as exc:
        parsed = None
        validation = exc.validation
    else:
        _, validation = validate_object(parsed, _WebProfileResponse)
    data = parsed.payload if parsed else None
    sources = [text, principal_activity or "", listing_category or ""]
    empty: dict[str, Any] = {
        "summary": None, "products_services": [], "main_town": None, "urgency": "unclear", "ticket_band": "unclear",
        "channel_fit": "unclear", "google_category": listing_category, "seed_keywords": [],
        "category_source": "google_listing" if listing_category else None,
        **{f: None for f in QUOTED_FIELDS},
        **{f"{f}_quote": None for f in QUOTED_FIELDS}, **{f"{f}_quote_valid": None for f in QUOTED_FIELDS},
        **{f"{f}_quote_match": None for f in QUOTED_FIELDS},
        "problem": None, "validation": validation}
    if data is None:
        return {**empty, "problem": "empty response" if not raw else "unparseable response"}
    conversion = data.get("conversion_action")
    conversion_value = conversion.get("value") if isinstance(conversion, dict) else conversion
    if conversion_value in RETIRED_CONVERSIONS:
        validation["normalisations"].append({"path": "conversion_action.value", "code": "retired_label",
                                               "message": "mapped retired conversion label"})
    tender = data.get("wins_by_tender")
    tender_value = tender.get("value") if isinstance(tender, dict) else tender
    if isinstance(tender_value, bool):
        validation["normalisations"].append({"path": "wins_by_tender.value", "code": "boolean_alias",
                                               "message": "mapped boolean verdict to yes or no"})
    problems: list[str] = []
    if validation["errors"]:
        problems.append(validation_message(validation))
    out = dict(empty)
    out["summary"] = (str(data.get("summary") or "").strip() or None)
    items = data.get("products_services")
    out["products_services"] = [str(i).strip() for i in items if str(i).strip()][:8] if isinstance(items, list) else []
    for field, allowed in (("customer_type", CUSTOMER_TYPES), ("conversion_action", CONVERSIONS),
                           ("geography", GEOGRAPHIES), ("wins_by_tender", TENDER_VALUES)):
        checked = _quoted(data, field, allowed, sources)
        out[field], out[f"{field}_quote"], out[f"{field}_quote_valid"], out[f"{field}_quote_match"] = (
            checked["value"], checked["quote"], checked["quote_valid"], checked["quote_match"])
        if checked["quote_valid"] == 0:
            problems.append(f"{field} quote not found in the text")
    geography = data.get("geography")
    town = geography.get("main_town") if isinstance(geography, dict) else None
    out["main_town"] = str(town).strip() if town and str(town).strip().lower() not in ("null", "none", "unclear") else None
    out["urgency"] = _enum(data.get("urgency"), URGENCIES)
    out["ticket_band"] = _enum(data.get("ticket_band"), TICKET_BANDS)
    out["channel_fit"] = _enum(data.get("channel_fit"), CHANNEL_FITS)
    if listing_category:
        out["google_category"], out["category_source"] = listing_category, "google_listing"
    else:
        allowed = {c.lower(): c for c in allowed_categories}
        pick = data.get("google_category")
        pick = pick.get("value") if isinstance(pick, dict) else pick
        if pick and str(pick).strip().lower() in allowed:
            out["google_category"], out["category_source"] = allowed[str(pick).strip().lower()], "model_assigned"
        else:
            out["google_category"], out["category_source"] = None, None
            if pick:
                problems.append("google_category is not on the shortlist")
    out["seed_keywords"] = seed_keywords(data.get("seed_keywords"), brand_terms)
    if len(out["seed_keywords"]) < MIN_SEED_KEYWORDS:
        problems.append(f"only {len(out['seed_keywords'])} usable seed keywords")
    out["problem"] = "; ".join(problems) or None
    return out
