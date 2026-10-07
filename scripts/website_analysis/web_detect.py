#!/usr/bin/env python3
"""W2 detection: page facts, technologies and the site summary (docs/WEB_STAGE_PLAN.md).

Detection reads pages from the page cache and never makes a request, so
changing a rule or a definition means re-running this step (free) under a new
`rule_version`; the crawl is not repeated.

Three outputs per website (a registrable domain, not a company):

- `web_pages`: one row of extracted facts per fetched page (`page_facts`);
- `web_technologies`: every technology found, with where it was found, the
  evidence and the account ids (`detect_technologies`);
- `web_sites`: one summary row derived from the two (`summarise_site`).

Usage (through scripts/website_analysis/web_population.py):
    python -m scripts.website_analysis.web_population detect --domains example.co.uk
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from companies_house_core.companies_house_sqlite import json_text, utc_now
from scripts.website_analysis.search_providers import registrable_domain
from scripts.website_analysis.tech_rules import RULE_VERSION, RULES, Rule
from scripts.website_analysis.web_fetch import Fetcher, Page, ParsedPage, parse_html
from scripts.website_analysis.web_identity import number_found

FORM_PROVIDERS = {
    "hubspot": r"hs-form|hbspt\.forms|js\.hsforms\.net",
    "gravity_forms": r"gform_wrapper|gravityforms",
    "contact_form_7": r"wpcf7",
    "wpforms": r"wpforms",
    "ninja_forms": r"\bnf-form",
    "elementor_forms": r"elementor-form",
    "typeform": r"typeform\.com",
    "jotform": r"jotform\.com",
    "formstack": r"formstack",
    "mailchimp": r"mc-embedded-subscribe-form|list-manage\.com",
    "zoho": r"zohopublic",
    "pardot": r"pardot\.com",
}
CTA_PHRASES = (
    "get a quote", "request a quote", "free quote", "book now", "book online", "book an appointment",
    "book a consultation", "free consultation", "call us", "call now", "contact us", "enquire now", "get in touch",
    "request a callback", "arrange a call", "add to basket", "add to cart", "buy now", "checkout", "get started",
    "sign up", "subscribe", "apply now", "check availability", "reserve a table", "book a table",
)
UK_PHONE_CANDIDATE = re.compile(r"(?<![\w.])(?:\+44|0)[\d\s\-().]{8,17}\d(?![\d])")
PRICE = re.compile(r"£\s?\d[\d,]*(?:\.\d{2})?")
COPYRIGHT = re.compile(r"(?:©|&copy;|copyright|\(c\))\s*(?:\(c\)\s*)?(?:(?:19|20)\d{2}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
AGENCY_KEYWORD = (r"(?:website|site|web\s*design|web\s*development|design|designed|built|developed|created|"
                  r"powered|marketing|seo)\s*(?:and\s+\w+\s+)?(?:by|:)\s*")
NOT_AN_AGENCY_HOST = re.compile(
    r"(facebook|instagram|linkedin|twitter|x|tiktok|youtube|pinterest|google|goo|wordpress|w3|gov\.uk|"
    r"companieshouse|trustpilot|feefo|apple|microsoft|cloudflare|wa\.me|whatsapp)\.", re.I)
BLOG_PATH = re.compile(r"/(?:blog|news|insights?|articles?|posts?)(?:/|$)", re.I)
PRODUCT_PATH = re.compile(r"/(?:products?|shop|collections?|item|store)(?:/|$)", re.I)
LOCATION_PATH = re.compile(r"/(?:locations?|areas?|branches?|our-locations|coverage|towns?)(?:/|$)", re.I)
THIN_HOME_WORDS = 150


# ---------------------------------------------------------------- page facts

def schema_types(blocks: Iterable[str]) -> list[str]:
    """Distinct schema.org `@type` values across JSON-LD blocks, in first-seen order."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("@type")
            for value in ([kind] if isinstance(kind, str) else kind or []):
                if isinstance(value, str) and value not in found:
                    found.append(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return found


def phone_numbers(text: str, tel_links: Iterable[str] = ()) -> list[str]:
    """Distinct UK numbers shown or linked, normalised to digits starting 0 (07..., 01..., 02..., 03..., 08...)."""
    found: list[str] = []
    for raw in [*tel_links, *UK_PHONE_CANDIDATE.findall(text or "")]:
        digits = re.sub(r"\D", "", raw)
        if digits.startswith("0044"):
            digits = digits[2:]
        if digits.startswith("44"):          # +44 20 ..., or +44 (0)20 ... with the redundant 0
            rest = digits[2:]
            digits = "0" + (rest[1:] if rest.startswith("0") else rest)
        if len(digits) not in (10, 11) or not digits.startswith("0") or digits.startswith("00"):
            continue
        if digits not in found:
            found.append(digits)
    return found[:10]


def copyright_year(text: str, *, now: datetime | None = None) -> int | None:
    limit = (now or datetime.now(timezone.utc)).year + 1
    years = [int(y) for y in COPYRIGHT.findall(text or "") if int(y) <= limit]
    return max(years) if years else None


def page_facts(page: Page, *, domain: str, page_kind: str, crawl_version: str, cache_key: str | None,
               company_numbers: Iterable[str] = (), in_navigation: bool | None = None) -> dict[str, Any]:
    """The `web_pages` row for a fetched page. Sitemaps, containers and failed
    fetches get the fetch facts only."""
    facts: dict[str, Any] = {
        "domain": domain, "url": page.url, "final_url": page.final_url, "page_kind": page_kind,
        "crawl_version": crawl_version, "fetched_with": page.fetched_with, "status_code": page.status,
        "fetch_error": page.error, "cache_key": cache_key, "fetched_at": None, "html_bytes": len(page.html or ""),
        "in_navigation": None if in_navigation is None else int(in_navigation),
        "script_hosts": json_text(page.script_hosts) if page.script_hosts else None,
        "title": None, "meta_description": None, "h1": None, "word_count": None, "lang": None,
        "canonical_url": None, "noindex": None, "has_viewport": None, "schema_types": None, "form_count": None,
        "form_providers": None, "tel_link_count": None, "phone_numbers": None, "cta_phrases": None,
        "price_mentions": None, "company_number_found": None, "copyright_year": None,
    }
    if not page.ok or page_kind in ("sitemap", "gtm_container"):
        return facts
    parsed = parse_html(page.html, page.final_url or page.url)
    html, text = page.html, parsed.text
    lowered = text.lower()
    facts.update({
        "title": parsed.title[:300] or None,
        "meta_description": (parsed.meta.get("description") or "")[:500] or None,
        "h1": parsed.h1[:300] or None, "word_count": parsed.word_count, "lang": parsed.lang or None,
        "canonical_url": parsed.canonical or None,
        "noindex": int("noindex" in (parsed.meta.get("robots") or "").lower()),
        "has_viewport": int("viewport" in parsed.meta),
        "schema_types": json_text(schema_types(parsed.jsonld)) if parsed.jsonld else None,
        "form_count": len(parsed.forms),
        "form_providers": json_text([n for n, rx in FORM_PROVIDERS.items() if re.search(rx, html, re.I)]) or None,
        "tel_link_count": len(parsed.tel_links),
        "phone_numbers": json_text(phone_numbers(text, parsed.tel_links)),
        "cta_phrases": json_text([p for p in CTA_PHRASES if p in lowered]),
        "price_mentions": len(PRICE.findall(text)),
        "company_number_found": int(any(number_found(parsed.title + " " + text, n) for n in company_numbers)),
        "copyright_year": copyright_year(text),
    })
    return facts


def agency_credit(parsed: ParsedPage, domain: str) -> tuple[str | None, str | None]:
    """The agency named in a footer credit ("Website by X"), only when X is the
    text of a link to another site: the first preview's looser pattern returned
    fragments such as "Media Group" and "or your agents"."""
    tail = parsed.text[-3000:]
    for url, anchor in parsed.links:
        host = urlparse(url).hostname or ""
        if not anchor or len(anchor) < 3 or len(anchor) > 50 or registrable_domain(url) in (None, domain):
            continue
        if NOT_AN_AGENCY_HOST.search(host + "."):
            continue
        if re.search(AGENCY_KEYWORD + re.escape(anchor), tail, re.I):
            return anchor, url
    return None, None


# ---------------------------------------------------------------- technologies

def _evidence(match: re.Match[str]) -> str:
    return " ".join(match.group(0).split())[:200]


def detect_technologies(pages: dict[str, str], gtm: dict[str, str], rules: Iterable[Rule] = RULES) -> list[dict[str, Any]]:
    """`pages` maps a page url to its raw HTML, `gtm` maps a container url to its text.
    One result per technology: where it was found (`page`, `gtm` or `both`), the
    first evidence on each surface and every account id."""
    results = []
    for rule in rules:
        hits: dict[str, tuple[str, str]] = {}
        ids: list[str] = []
        for surface, texts in (("page", pages), ("gtm", gtm)):
            if surface not in rule.where:
                continue
            for url, text in texts.items():
                if not text:
                    continue
                match = rule.search(text, surface)
                if match is None:
                    continue
                hits.setdefault(surface, (_evidence(match), url))
                ids += [i for i in rule.ids(text) if i not in ids]
        if not hits:
            continue
        found_in = "both" if len(hits) == 2 else next(iter(hits))
        evidence, evidence_url = hits.get("page") or hits["gtm"]
        results.append({"technology": rule.name, "category": rule.category, "found_in": found_in,
                        "account_ids": ids, "evidence": evidence, "evidence_url": evidence_url})
    return results


# ---------------------------------------------------------------- sitemap and site summary

def sitemap_info(xml_texts: Iterable[str]) -> dict[str, Any]:
    locs: list[tuple[str, str | None]] = []
    for text in xml_texts:
        for block in re.findall(r"<url>(.*?)</url>", text or "", re.S | re.I):
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S | re.I)
            mod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.S | re.I)
            if loc:
                locs.append((loc.group(1), mod.group(1)[:10] if mod else None))
    dated = sorted(m for _, m in locs if m)
    blog_dates = sorted(m for u, m in locs if m and BLOG_PATH.search(urlparse(u).path))
    return {"url_count": len(locs), "newest_lastmod": dated[-1] if dated else None,
            "product_url_count": sum(bool(PRODUCT_PATH.search(urlparse(u).path)) for u, _ in locs),
            "location_page_count": sum(bool(LOCATION_PATH.search(urlparse(u).path)) for u, _ in locs),
            "blog_latest_date": blog_dates[-1] if blog_dates else None}


def _names(techs: list[dict[str, Any]], category: str) -> list[str]:
    return [t["technology"] for t in techs if t["category"] == category]


def _has(techs: list[dict[str, Any]], name: str) -> bool:
    return any(t["technology"] == name for t in techs)


def _ids(techs: list[dict[str, Any]], name: str) -> list[str]:
    return next((t["account_ids"] for t in techs if t["technology"] == name), [])


ECOMMERCE_PLATFORMS = {"Shopify", "WooCommerce", "Magento", "BigCommerce", "PrestaShop"}
CHECKOUT_PHRASES = {"add to basket", "add to cart", "checkout", "buy now"}


def _json_list(value: str | None) -> list[Any]:
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []


def summarise_site(domain: str, pages: list[dict[str, Any]], techs: list[dict[str, Any]], *,
                   crawl_version: str, rule_version: str, sitemap: dict[str, Any] | None = None,
                   agency: tuple[str | None, str | None] = (None, None), parked: bool = False) -> dict[str, Any]:
    """The `web_sites` row. `pages` are `web_pages` rows (dicts)."""
    content = [p for p in pages if p["page_kind"] not in ("sitemap", "gtm_container")]
    fetched = [p for p in content if p.get("status_code") == 200 and not p.get("fetch_error") and p.get("word_count") is not None]
    home = next((p for p in content if p["page_kind"] == "home"), None)
    sitemap = sitemap or {}
    if parked:
        status = "parked"
    elif home is not None and home.get("fetch_error") and (
            str(home["fetch_error"]).startswith(("blocked", "challenge"))):
        status = "blocked"
    elif not fetched:
        status = "unreachable"
    elif home is not None and (home.get("word_count") or 0) < THIN_HOME_WORDS:
        status = "thin"
    else:
        status = "ok"
    union_types: list[str] = []
    for p in fetched:
        for t in _json_list(p.get("schema_types")):
            if t not in union_types:
                union_types.append(t)
    ads_ids = _ids(techs, "Google Ads")
    cms = _names(techs, "cms")
    shop = [n for n in _names(techs, "ecommerce") if n in ECOMMERCE_PLATFORMS]
    phrases = {ph for p in fetched for ph in _json_list(p.get("cta_phrases"))}
    consent_vendors = [n for n in _names(techs, "consent") if n != "Consent Mode"]
    return {
        "domain": domain, "crawl_version": crawl_version, "rule_version": rule_version, "crawl_status": status,
        "pages_fetched": len(fetched), "pages_by_browser": sum(p.get("fetched_with") == "browser" for p in fetched),
        "https": None if home is None else int(str(home.get("final_url") or home["url"]).startswith("https://")),
        "mobile_ready": None if home is None or home.get("has_viewport") is None else int(home["has_viewport"]),
        "platform": (cms[0] if cms else (shop[0] if shop else None)),
        "copyright_year": max((p["copyright_year"] for p in fetched if p.get("copyright_year")), default=None),
        "sitemap_url_count": sitemap.get("url_count"), "sitemap_newest_lastmod": sitemap.get("newest_lastmod"),
        "product_url_count": sitemap.get("product_url_count"),
        "service_page_count": sum(p["page_kind"] == "service" for p in fetched),
        "location_page_count": max(sum(p["page_kind"] == "location" for p in fetched),
                                   sitemap.get("location_page_count") or 0),
        "landing_page_count": sum(p["page_kind"] == "landing" or (
            p.get("noindex") == 1 and p.get("in_navigation") == 0 and p["page_kind"] not in ("privacy", "terms"))
            for p in fetched),
        "has_blog": int(any(p["page_kind"] == "blog" for p in fetched) or bool(sitemap.get("blog_latest_date"))),
        "blog_latest_date": sitemap.get("blog_latest_date"),
        "schema_types": json_text(union_types) if union_types else None,
        "gtm_ids": json_text(_ids(techs, "Google Tag Manager")) if _has(techs, "Google Tag Manager") else None,
        "ga4_ids": json_text(_ids(techs, "Google Analytics 4")) if _has(techs, "Google Analytics 4") else None,
        "google_ads_ids": json_text(ads_ids) if ads_ids else None,
        "has_google_ads_tag": int(_has(techs, "Google Ads")),
        "has_ads_conversion_event": int(_has(techs, "Google Ads conversion event")),
        "has_ads_remarketing": int(_has(techs, "Google Ads remarketing")),
        "has_consent_mode": int(_has(techs, "Consent Mode")),
        "consent_vendor": consent_vendors[0] if consent_vendors else None,
        "server_side_tagging": int(_has(techs, "Server-side tagging")),
        "social_pixels": json_text(_names(techs, "social_ads")) if _names(techs, "social_ads") else None,
        "has_microsoft_ads": int(_has(techs, "Microsoft Ads (UET)")),
        "crm_vendors": json_text(_names(techs, "crm_automation")) if _names(techs, "crm_automation") else None,
        "email_vendors": json_text(_names(techs, "email")) if _names(techs, "email") else None,
        "call_tracking_vendor": ", ".join(_names(techs, "call_tracking")) or None,
        "optimisation_vendors": json_text(_names(techs, "optimisation")) if _names(techs, "optimisation") else None,
        "landing_page_builder": (_names(techs, "landing_pages") or [None])[0],
        "has_contact_form": int(any((p.get("form_count") or 0) > 0 for p in fetched)),
        "has_click_to_call": int(any((p.get("tel_link_count") or 0) > 0 for p in fetched)),
        "has_booking": (_names(techs, "booking") or [None])[0],
        "has_checkout": int(bool(shop) or bool(phrases & CHECKOUT_PHRASES)),
        "has_live_chat": (_names(techs, "chat") or [None])[0],
        "review_widget": (_names(techs, "reviews") or [None])[0],
        "agency_credit": agency[0], "agency_credit_url": agency[1],
        "summarised_at": utc_now(),
    }


# ---------------------------------------------------------------- storage and the detect step

WEB_PAGE_COLUMNS = (
    "domain", "url", "final_url", "page_kind", "crawl_version", "fetched_with", "status_code", "fetch_error",
    "cache_key", "fetched_at", "html_bytes", "in_navigation", "script_hosts", "title", "meta_description", "h1",
    "word_count", "lang", "canonical_url", "noindex", "has_viewport", "schema_types", "form_count", "form_providers",
    "tel_link_count", "phone_numbers", "cta_phrases", "price_mentions", "company_number_found", "copyright_year")


def store_pages(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    sql = (f"insert or replace into web_pages ({', '.join(WEB_PAGE_COLUMNS)}) "
           f"values ({', '.join('?' for _ in WEB_PAGE_COLUMNS)})")
    for row in rows:
        row = {**row, "fetched_at": row.get("fetched_at") or utc_now()}
        conn.execute(sql, [row.get(column) for column in WEB_PAGE_COLUMNS])
    conn.commit()
    return len(rows)


def load_pages(conn: sqlite3.Connection, domain: str, crawl_version: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("select * from web_pages where domain = ? and crawl_version = ? order by id",
                            (domain, crawl_version)).fetchall()
    finally:
        conn.row_factory = None
    return [dict(row) for row in rows]


def store_site(conn: sqlite3.Connection, summary: dict[str, Any], techs: list[dict[str, Any]]) -> None:
    """Replace this domain's technologies and summary for the rule version."""
    domain, rule_version = summary["domain"], summary["rule_version"]
    conn.execute("delete from web_technologies where domain = ? and rule_version = ?", (domain, rule_version))
    now = utc_now()
    for t in techs:
        conn.execute(
            "insert into web_technologies (domain, rule_version, technology, category, found_in, account_ids, "
            "evidence, evidence_url, detected_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (domain, rule_version, t["technology"], t["category"], t["found_in"],
             json_text(t["account_ids"]) if t["account_ids"] else None, t["evidence"], t["evidence_url"], now))
    columns = list(summary)
    conn.execute(
        f"insert or replace into web_sites ({', '.join(columns)}) values ({', '.join('?' for _ in columns)})",
        [summary[c] for c in columns])
    conn.commit()


def detect_domain(conn: sqlite3.Connection, domain: str, fetcher: Fetcher, *, crawl_version: str,
                  rule_version: str = RULE_VERSION) -> dict[str, Any] | None:
    """Re-detect one domain from its cached pages. Returns the site summary, or
    None if the domain has no crawl rows. Makes no requests: `fetcher` must be
    in cache-only mode."""
    rows = load_pages(conn, domain, crawl_version)
    if not rows:
        return None
    pages: dict[str, str] = {}
    gtm: dict[str, str] = {}
    sitemaps: list[str] = []
    agency: tuple[str | None, str | None] = (None, None)
    parked = False
    for row in rows:
        if row.get("fetch_error") and row["fetch_error"] == "parked":
            parked = True
        cached = fetcher.get(row["url"], accept=("html", "pdf", "script"))
        if not cached.html:
            continue
        if row["page_kind"] == "gtm_container":
            gtm[row["url"]] = cached.html
        elif row["page_kind"] == "sitemap":
            sitemaps.append(cached.html)
        else:
            pages[row["url"]] = cached.html
            if row["page_kind"] == "home" and agency == (None, None):
                agency = agency_credit(parse_html(cached.html, cached.final_url or cached.url), domain)
    techs = detect_technologies(pages, gtm, [r for r in RULES])
    summary = summarise_site(domain, rows, techs, crawl_version=crawl_version, rule_version=rule_version,
                             sitemap=sitemap_info(sitemaps), agency=agency, parked=parked)
    store_site(conn, summary, techs)
    return summary


def run_detect(conn: sqlite3.Connection, domains: list[str], fetcher: Fetcher, *, crawl_version: str,
               rule_version: str = RULE_VERSION) -> dict[str, int]:
    counts = {"domains": 0, "no_crawl": 0}
    for domain in domains:
        summary = detect_domain(conn, domain, fetcher, crawl_version=crawl_version, rule_version=rule_version)
        if summary is None:
            counts["no_crawl"] += 1
            continue
        counts["domains"] += 1
        counts[summary["crawl_status"]] = counts.get(summary["crawl_status"], 0) + 1
    return counts
