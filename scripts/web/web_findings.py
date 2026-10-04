#!/usr/bin/env python3
"""Findings: the talking points for each company (docs/WEB_STAGE_PLAN.md).

Free and re-runnable: rules over what the earlier steps stored (`web_sites`,
`web_technologies`, `web_pages`, `company_market`). Each finding is a **gap**
(a pitch point: something the company does not do or measure) or a **strength**
(something it already does well), stored in plain English for the lead sheet
together with the signals behind it. The setup level and the gap segment are
summaries for sorting; the findings are the substance.

Setup level, from the measurement and conversion tools the site shows:

  unknown          no usable crawl (blocked, unreachable, parked) or no site
  none             no analytics, no tag manager and no ad pixels at all
  basic            fewer than three of the ten points below
  partial          three to five points
  sophisticated    six or more points, with a Google Ads conversion event and a
                   CRM or call tracking

  points: analytics, tag manager, Google Ads conversion event, Google Ads
  remarketing, Consent Mode, another ad pixel, CRM or marketing automation,
  call tracking, a conversion-optimisation tool, a landing-style page.

Gap segment (docs/WEB_STAGE.md, with one added value):

  site_first          no website, an unreachable or parked one, or none with a way to enquire,
                      book or buy
  unknown             crawl blocked, or no advertising data yet
  advertising_well    advertising now, with a conversion event and a CRM, call tracking or an agency credit
  advertising_poorly  advertising now, without that setup
  greenfield          not advertising now, people search for what it sells (at least DEMAND_THRESHOLD a month)
  low_demand          not advertising now, and little search demand (added 2026-10-02: neither of the above)

"Advertising now" is an ad shown in the last 30 days (Ads Transparency).
"Not advertising" for `search_demand_not_advertising` means none in 90 days.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from core.companies_house_sqlite import utc_now

FINDING_VERSION = "findings-v1"
DEMAND_THRESHOLD = 500            # monthly searches over a company's seed phrases
NOT_ADVERTISING_DAYS = 90
USABLE_STATUSES = ("ok", "thin")

# key -> (kind, detail template). Templates use the keys of `signals`.
FINDINGS: dict[str, tuple[str, str]] = {
    "ads_without_conversion_tracking": (
        "gap", "Running Google ads (last shown {ads_last_shown}) but its website has no Google Ads conversion "
               "tracking, so it cannot tell which ads produce enquiries."),
    "ads_without_tag": (
        "gap", "Running Google ads (last shown {ads_last_shown}) but no Google Ads tag was found on its website."),
    "no_consent_mode": (
        "gap", "Has a Google Ads tag but no Consent Mode, which UK sites need for full Google Ads measurement."),
    "phone_leads_untracked": (
        "gap", "Shows a phone number for customers to ring but has no call tracking, so phone enquiries cannot be "
               "tied to marketing."),
    "no_remarketing": (
        "gap", "Advertising on Google with no remarketing tag, so visitors who leave are not followed up."),
    "no_landing_pages": (
        "gap", "Advertising on Google but the site shows no landing-style pages: ads probably point at the "
               "homepage or general pages."),
    "no_crm": (
        "gap", "Has an enquiry form but no CRM or marketing automation (such as HubSpot), so leads are probably "
               "not tracked after the click."),
    "search_demand_not_advertising": (
        "gap", "People search for what it sells ({phrase_search_volume:,} searches a month across its main "
               "phrases) but no Google ads have been shown in the last {not_advertising_days} days."),
    "no_analytics": (
        "gap", "No website analytics were found, so it cannot see where its visitors come from."),
    "multi_channel_advertiser": (
        "strength", "Already advertises beyond Google: {pixels}."),
    "call_tracking_in_place": (
        "strength", "Already tracks phone leads with {call_tracking_vendor}."),
    "crm_in_place": (
        "strength", "Already uses {crm_vendors} to manage leads."),
    "agency_managed": (
        "strength", "Its marketing appears to be managed by a specialist: {agency_reason}."),
}


def _load(value: Any) -> list[Any]:
    try:
        return json.loads(value) if value else []
    except (TypeError, ValueError):
        return []


def _days_since(date: str | None, now: datetime) -> int | None:
    if not date:
        return None
    try:
        return (now - datetime.strptime(date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
    except ValueError:
        return None


def evaluate(site: dict[str, Any] | None, market: dict[str, Any] | None, *, analytics: bool = False,
             phone_shown: bool = False, other_pixels: int | None = None, optimisation: bool = False,
             now: datetime | None = None) -> dict[str, Any]:
    """`{"findings": [...], "setup_level", "gap_segment", "gap_count", "strength_count"}` for one company.
    `site`: a `web_sites` row (None if the company has no website); `market`: a
    `company_market` row (None before W4); `analytics`: any analytics technology
    was found; `phone_shown`: a phone number appears on any crawled page."""
    now = now or datetime.now(timezone.utc)
    usable = site is not None and site.get("crawl_status") in USABLE_STATUSES
    market = market or {}
    ads_known = market.get("advertising_now") is not None
    advertising_now = bool(market.get("advertising_now"))
    last_shown = market.get("ads_last_shown")
    since = _days_since(last_shown, now)
    not_advertising_90 = ads_known and (since is None or since > NOT_ADVERTISING_DAYS)
    demand = market.get("phrase_search_volume") or 0
    social = _load(site.get("social_pixels")) if site else []
    pixels = [*social, *(["Microsoft Ads"] if site and site.get("has_microsoft_ads") else [])]
    crm = _load(site.get("crm_vendors")) if site else []
    calls = (site.get("call_tracking_vendor") or "") if site else ""
    signals = {"ads_last_shown": last_shown, "phrase_search_volume": demand, "not_advertising_days": NOT_ADVERTISING_DAYS,
               "pixels": ", ".join(pixels), "call_tracking_vendor": calls, "crm_vendors": ", ".join(crm)}
    found: dict[str, dict[str, Any]] = {}

    def add(key: str, evidence: dict[str, Any], **extra: Any) -> None:
        kind, template = FINDINGS[key]
        found[key] = {"finding": key, "kind": kind, "detail": template.format(**{**signals, **extra}),
                      "evidence": evidence}

    if usable:
        has_tag = bool(site.get("has_google_ads_tag"))
        if advertising_now and not has_tag:
            add("ads_without_tag", {"advertising_now": True, "has_google_ads_tag": False})
        if advertising_now and not site.get("has_ads_conversion_event"):
            add("ads_without_conversion_tracking", {"advertising_now": True, "has_ads_conversion_event": False})
        if has_tag and not site.get("has_consent_mode"):
            add("no_consent_mode", {"has_google_ads_tag": True, "has_consent_mode": False})
        if (site.get("has_click_to_call") or phone_shown) and not calls:
            add("phone_leads_untracked", {"has_click_to_call": bool(site.get("has_click_to_call")),
                                          "phone_shown": phone_shown, "call_tracking": False})
        if advertising_now and not site.get("has_ads_remarketing"):
            add("no_remarketing", {"advertising_now": True, "has_ads_remarketing": False})
        if advertising_now and not site.get("landing_page_count"):
            add("no_landing_pages", {"advertising_now": True, "landing_page_count": 0})
        if site.get("has_contact_form") and not crm:
            add("no_crm", {"has_contact_form": True, "crm_vendors": []})
        if not analytics and not site.get("gtm_ids") and not site.get("ga4_ids"):
            add("no_analytics", {"analytics": False, "gtm_ids": None})
        if len(pixels) >= 2:
            add("multi_channel_advertiser", {"pixels": pixels})
        if calls:
            add("call_tracking_in_place", {"call_tracking_vendor": calls})
        if crm:
            add("crm_in_place", {"crm_vendors": crm})
        reason = site.get("agency_credit") and f"credited on its site to {site['agency_credit']}" or (
            calls and site.get("server_side_tagging") and "call tracking with server-side tagging") or None
        if reason:
            add("agency_managed", {"agency_credit": site.get("agency_credit"), "call_tracking": bool(calls),
                                   "server_side_tagging": bool(site.get("server_side_tagging"))}, agency_reason=reason)
    if ads_known and not_advertising_90 and demand >= DEMAND_THRESHOLD:
        add("search_demand_not_advertising", {"phrase_search_volume": demand, "ads_last_shown": last_shown})

    level = _setup_level(site, usable, analytics, pixels, crm, calls, optimisation)
    segment = _gap_segment(site, usable, market, ads_known, advertising_now, demand, crm, calls)
    findings = list(found.values())
    return {"findings": findings, "setup_level": level, "gap_segment": segment,
            "gap_count": sum(f["kind"] == "gap" for f in findings),
            "strength_count": sum(f["kind"] == "strength" for f in findings)}


def _setup_level(site: dict[str, Any] | None, usable: bool, analytics: bool, pixels: list[str], crm: list[str],
                 calls: str, optimisation: bool) -> str:
    if not usable:
        return "unknown"
    social = [p for p in pixels if p != "Microsoft Ads"]
    tag_manager = bool(site.get("gtm_ids"))
    if not (analytics or tag_manager or pixels or site.get("has_google_ads_tag")):
        return "none"
    points = sum([bool(analytics or site.get("ga4_ids")), tag_manager, bool(site.get("has_ads_conversion_event")),
                  bool(site.get("has_ads_remarketing")), bool(site.get("has_consent_mode")), bool(social or pixels),
                  bool(crm), bool(calls), optimisation, bool(site.get("landing_page_count"))])
    if points >= 6 and site.get("has_ads_conversion_event") and (crm or calls):
        return "sophisticated"
    return "partial" if points >= 3 else "basic"


def _gap_segment(site: dict[str, Any] | None, usable: bool, market: dict[str, Any], ads_known: bool,
                 advertising_now: bool, demand: int, crm: list[str], calls: str) -> str:
    if site is None or site.get("crawl_status") in ("unreachable", "parked"):
        return "site_first"
    if site.get("crawl_status") == "blocked":
        return "unknown"
    can_convert = any([site.get("has_contact_form"), site.get("has_click_to_call"), site.get("has_booking"),
                       site.get("has_checkout"), site.get("has_live_chat")])
    if not can_convert:
        return "site_first"
    if not ads_known:
        return "unknown"
    if advertising_now:
        well = site.get("has_ads_conversion_event") and (crm or calls or site.get("agency_credit"))
        return "advertising_well" if well else "advertising_poorly"
    return "greenfield" if demand >= DEMAND_THRESHOLD else "low_demand"


# ---------------------------------------------------------------- storage

def _row(conn: sqlite3.Connection, sql: str, params: tuple) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.row_factory = None
    return dict(row) if row else None


def load_inputs(conn: sqlite3.Connection, number: str, *, resolver_version: str | None, market_version: str,
                crawl_version: str, rule_version: str) -> dict[str, Any]:
    # resolver_version None: the company's newest identity, whichever version wrote it
    ident = conn.execute(
        "select domain from " + ("company_web_identity" if resolver_version else "company_web_identity_current")
        + " where company_number = ? " + ("and resolver_version = ? " if resolver_version else "")
        + "and role = 'main' and domain is not null order by id desc limit 1",
        (number, resolver_version) if resolver_version else (number,)).fetchone()
    domain = ident[0] if ident else None
    site = _row(conn, "select * from web_sites where domain = ? and crawl_version = ? and rule_version = ?",
                (domain, crawl_version, rule_version)) if domain else None
    market = _row(conn, "select * from company_market where company_number = ? and market_version = ?",
                  (number, market_version))
    categories: set[str] = set()
    phone_shown = False
    if domain:
        categories = {c for (c,) in conn.execute(
            "select category from web_technologies where domain = ? and rule_version = ?", (domain, rule_version))}
        phone_shown = conn.execute(
            "select 1 from web_pages where domain = ? and crawl_version = ? and phone_numbers is not null "
            "and phone_numbers != '[]' limit 1", (domain, crawl_version)).fetchone() is not None
    return {"domain": domain, "site": site, "market": market, "analytics": "analytics" in categories,
            "optimisation": "optimisation" in categories, "phone_shown": phone_shown}


def assess_companies(conn: sqlite3.Connection, numbers: Iterable[str], *, resolver_version: str | None, market_version: str,
                     crawl_version: str, rule_version: str, finding_version: str = FINDING_VERSION,
                     now: datetime | None = None) -> dict[str, int]:
    """Write each company's findings and the rollup columns of `company_market`.
    Re-running replaces the findings of this version."""
    counts = {"companies": 0, "findings": 0}
    segments: dict[str, int] = {}
    for number in numbers:
        data = load_inputs(conn, number, resolver_version=resolver_version, market_version=market_version,
                           crawl_version=crawl_version, rule_version=rule_version)
        result = evaluate(data["site"], data["market"], analytics=data["analytics"], phone_shown=data["phone_shown"],
                          optimisation=data["optimisation"], now=now)
        conn.execute("delete from company_setup_findings where company_number = ? and finding_version = ?",
                     (number, finding_version))
        for f in result["findings"]:
            conn.execute(
                "insert into company_setup_findings (company_number, domain, finding_version, finding, kind, detail, "
                "evidence) values (?, ?, ?, ?, ?, ?, ?)",
                (number, data["domain"], finding_version, f["finding"], f["kind"], f["detail"], json.dumps(f["evidence"])))
        conn.execute(
            "insert into company_market (company_number, market_version, domain, assessed_at, gap_count, "
            "strength_count, setup_level, gap_segment) values (?, ?, ?, ?, ?, ?, ?, ?) "
            "on conflict(company_number, market_version) do update set gap_count=excluded.gap_count, "
            "strength_count=excluded.strength_count, setup_level=excluded.setup_level, "
            "gap_segment=excluded.gap_segment",
            (number, market_version, data["domain"], utc_now(), result["gap_count"], result["strength_count"],
             result["setup_level"], result["gap_segment"]))
        counts["companies"] += 1
        counts["findings"] += len(result["findings"])
        segments[result["gap_segment"]] = segments.get(result["gap_segment"], 0) + 1
    conn.commit()
    return {**counts, **{f"segment_{k}": v for k, v in sorted(segments.items())}}
