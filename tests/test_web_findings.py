from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core.companies_house_sqlite import init_db
from scripts.web import web_findings as F

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _site(**over):
    base = {"crawl_status": "ok", "has_google_ads_tag": 0, "has_ads_conversion_event": 0, "has_ads_remarketing": 0,
            "has_consent_mode": 0, "social_pixels": None, "has_microsoft_ads": 0, "crm_vendors": None,
            "call_tracking_vendor": None, "landing_page_count": 0, "has_contact_form": 0, "has_click_to_call": 0,
            "has_booking": None, "has_checkout": 0, "has_live_chat": None, "agency_credit": None,
            "server_side_tagging": 0, "gtm_ids": None, "ga4_ids": None}
    return {**base, **over}


def _market(**over):
    return {**{"advertising_now": 0, "ads_last_shown": None, "phrase_search_volume": 0}, **over}


def _keys(result, kind=None):
    return {f["finding"] for f in result["findings"] if kind in (None, f["kind"])}


def test_advertiser_with_no_tag_or_tracking_gets_the_measurement_gaps():
    site = _site(has_contact_form=1, ga4_ids='["G-X"]')
    result = F.evaluate(site, _market(advertising_now=1, ads_last_shown="2026-09-30"), analytics=True, now=NOW)
    assert {"ads_without_tag", "ads_without_conversion_tracking", "no_remarketing", "no_landing_pages",
            "no_crm"} <= _keys(result, "gap")
    detail = next(f["detail"] for f in result["findings"] if f["finding"] == "ads_without_tag")
    assert "2026-09-30" in detail and result["gap_segment"] == "advertising_poorly"
    assert result["gap_count"] == len(_keys(result, "gap")) and result["strength_count"] == 0


def test_a_well_set_up_advertiser_has_strengths_and_few_gaps():
    site = _site(has_google_ads_tag=1, has_ads_conversion_event=1, has_ads_remarketing=1, has_consent_mode=1,
                 social_pixels='["Meta Pixel", "TikTok Pixel"]', has_microsoft_ads=1, crm_vendors='["HubSpot"]',
                 call_tracking_vendor="CallRail", landing_page_count=3, has_contact_form=1, has_click_to_call=1,
                 gtm_ids='["GTM-ABC1234"]', ga4_ids='["G-X"]', agency_credit="Pixel Agency")
    result = F.evaluate(site, _market(advertising_now=1, ads_last_shown="2026-10-01"), analytics=True,
                        optimisation=True, now=NOW)
    assert _keys(result, "gap") == set()
    assert _keys(result, "strength") == {"multi_channel_advertiser", "call_tracking_in_place", "crm_in_place",
                                         "agency_managed"}
    assert result["setup_level"] == "sophisticated" and result["gap_segment"] == "advertising_well"
    detail = {f["finding"]: f["detail"] for f in result["findings"]}
    assert "Meta Pixel, TikTok Pixel, Microsoft Ads" in detail["multi_channel_advertiser"]
    assert "CallRail" in detail["call_tracking_in_place"] and "Pixel Agency" in detail["agency_managed"]


def test_consent_mode_and_phone_tracking_gaps():
    result = F.evaluate(_site(has_google_ads_tag=1, has_click_to_call=1), _market(), analytics=True, now=NOW)
    assert {"no_consent_mode", "phone_leads_untracked"} <= _keys(result, "gap")
    shown = F.evaluate(_site(), _market(), analytics=True, phone_shown=True, now=NOW)
    assert "phone_leads_untracked" in _keys(shown)
    tracked = F.evaluate(_site(has_click_to_call=1, call_tracking_vendor="CallRail"), _market(), analytics=True, now=NOW)
    assert "phone_leads_untracked" not in _keys(tracked)


def test_search_demand_not_advertising_uses_ninety_days_and_a_threshold():
    quiet = F.evaluate(_site(), _market(phrase_search_volume=800, ads_last_shown="2026-03-01", advertising_now=0),
                       analytics=True, now=NOW)
    assert "search_demand_not_advertising" in _keys(quiet)
    assert "800 searches a month" in next(f["detail"] for f in quiet["findings"]
                                          if f["finding"] == "search_demand_not_advertising")
    recent = F.evaluate(_site(), _market(phrase_search_volume=800, ads_last_shown="2026-08-15", advertising_now=0),
                        analytics=True, now=NOW)
    assert "search_demand_not_advertising" not in _keys(recent)          # shown 48 days ago: not "not advertising"
    low = F.evaluate(_site(), _market(phrase_search_volume=100), analytics=True, now=NOW)
    assert "search_demand_not_advertising" not in _keys(low)
    unknown = F.evaluate(_site(), {"phrase_search_volume": 5000}, analytics=True, now=NOW)   # no ads data yet
    assert "search_demand_not_advertising" not in _keys(unknown)


def test_no_analytics_needs_no_analytics_tech_and_no_tag_manager_ids():
    assert "no_analytics" in _keys(F.evaluate(_site(), _market(), analytics=False, now=NOW))
    assert "no_analytics" not in _keys(F.evaluate(_site(gtm_ids='["GTM-ABC1234"]'), _market(), analytics=False, now=NOW))
    assert "no_analytics" not in _keys(F.evaluate(_site(), _market(), analytics=True, now=NOW))


def test_site_derived_findings_need_a_usable_crawl():
    blocked = F.evaluate(_site(crawl_status="blocked", has_contact_form=1), _market(advertising_now=1,
                                                                                  ads_last_shown="2026-10-01"), now=NOW)
    assert _keys(blocked) == set() and blocked["setup_level"] == "unknown" and blocked["gap_segment"] == "unknown"
    nosite = F.evaluate(None, _market(advertising_now=1), now=NOW)
    assert nosite["findings"] == [] and nosite["gap_segment"] == "site_first" and nosite["setup_level"] == "unknown"


@pytest.mark.parametrize("site,market,expected", [
    (None, None, "site_first"),
    (_site(crawl_status="unreachable"), _market(), "site_first"),
    (_site(crawl_status="parked"), _market(), "site_first"),
    (_site(crawl_status="blocked", has_contact_form=1), _market(), "unknown"),
    (_site(), _market(), "site_first"),                                         # nothing to enquire, book or buy with
    (_site(has_contact_form=1), None, "unknown"),                              # no advertising data yet
    (_site(has_contact_form=1), {"advertising_now": None}, "unknown"),
    (_site(has_contact_form=1), _market(phrase_search_volume=900), "greenfield"),
    (_site(has_checkout=1), _market(phrase_search_volume=900), "greenfield"),
    (_site(has_contact_form=1), _market(phrase_search_volume=100), "low_demand"),
    (_site(has_contact_form=1), _market(advertising_now=1, ads_last_shown="2026-10-01"), "advertising_poorly"),
    (_site(has_contact_form=1, has_ads_conversion_event=1), _market(advertising_now=1, ads_last_shown="2026-10-01"),
     "advertising_poorly"),                                                      # tracked, but no CRM, calls or agency
    (_site(has_contact_form=1, has_ads_conversion_event=1, agency_credit="X"),
     _market(advertising_now=1, ads_last_shown="2026-10-01"), "advertising_well"),
    (_site(has_contact_form=1, has_ads_conversion_event=1, call_tracking_vendor="CallRail"),
     _market(advertising_now=1, ads_last_shown="2026-10-01"), "advertising_well"),
])
def test_gap_segments(site, market, expected):
    assert F.evaluate(site, market, analytics=True, now=NOW)["gap_segment"] == expected


@pytest.mark.parametrize("site,analytics,expected", [
    (_site(), False, "none"),
    (_site(), True, "basic"),
    (_site(gtm_ids='["GTM-ABC1234"]', ga4_ids='["G-X"]', has_consent_mode=1), True, "partial"),
    (_site(crawl_status="thin"), True, "basic"),
    (_site(crawl_status="unreachable"), True, "unknown"),
])
def test_setup_levels(site, analytics, expected):
    assert F.evaluate(site, _market(), analytics=analytics, now=NOW)["setup_level"] == expected


def test_sophisticated_needs_conversion_tracking_and_crm_or_call_tracking():
    base = dict(has_google_ads_tag=1, has_ads_remarketing=1, has_consent_mode=1, gtm_ids='["GTM-ABC1234"]',
                ga4_ids='["G-X"]', social_pixels='["Meta Pixel"]', landing_page_count=2, has_contact_form=1)
    no_conversion = F.evaluate(_site(crm_vendors='["HubSpot"]', **base), _market(), analytics=True, optimisation=True,
                               now=NOW)
    assert no_conversion["setup_level"] == "partial"
    full = F.evaluate(_site(crm_vendors='["HubSpot"]', has_ads_conversion_event=1, **base), _market(), analytics=True,
                      optimisation=True, now=NOW)
    assert full["setup_level"] == "sophisticated"


# ---------------------------------------------------------------- storage

def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    conn.execute("insert into companies (company_number, company_name, company_status, source_mode, profile_payload, "
                 "updated_at) values ('1', 'X LTD', 'active', 'api', '{}', '2026')")
    conn.execute("insert into companies (company_number, company_name, company_status, source_mode, profile_payload, "
                 "updated_at) values ('2', 'NO SITE LTD', 'active', 'api', '{}', '2026')")
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, resolved_at) "
                 "values ('1', 'rv', 'x.co.uk', 'main', 'verified', '2026')")
    conn.execute("insert into web_sites (domain, crawl_version, rule_version, crawl_status, has_contact_form, "
                 "has_click_to_call, has_google_ads_tag, ga4_ids, summarised_at) "
                 "values ('x.co.uk', 'cv', 'rl', 'ok', 1, 1, 1, '[\"G-X\"]', '2026')")
    conn.execute("insert into web_technologies (domain, rule_version, technology, category, found_in, detected_at) "
                 "values ('x.co.uk', 'rl', 'Google Analytics 4', 'analytics', 'page', '2026')")
    conn.execute("insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, phone_numbers) "
                 "values ('x.co.uk', 'https://x.co.uk/', 'home', 'cv', 'http', '[\"01625415800\"]')")
    conn.execute("insert into company_market (company_number, market_version, domain, advertising_now, ads_last_shown, "
                 "phrase_search_volume, assessed_at) values ('1', 'mv', 'x.co.uk', 1, '2026-10-01', 900, '2026')")
    return conn


def test_assess_companies_writes_findings_and_the_rollup_and_replaces_on_rerun(tmp_path):
    conn = _db(tmp_path)
    kwargs = dict(resolver_version="rv", market_version="mv", crawl_version="cv", rule_version="rl", now=NOW)
    counts = F.assess_companies(conn, ["1", "2"], **kwargs)
    assert counts["companies"] == 2 and counts["segment_advertising_poorly"] == 1 and counts["segment_site_first"] == 1
    rows = {f: (k, e) for f, k, e in conn.execute("select finding, kind, evidence from company_setup_findings "
                                                  "where company_number = '1'")}
    assert {"ads_without_conversion_tracking", "no_consent_mode", "phone_leads_untracked", "no_remarketing",
            "no_landing_pages", "no_crm"} == set(rows) and all(k == "gap" for k, _ in rows.values())
    assert json.loads(rows["no_crm"][1]) == {"has_contact_form": True, "crm_vendors": []}
    assert "no_analytics" not in rows                                        # GA4 was detected
    rollup = conn.execute("select gap_count, strength_count, setup_level, gap_segment, advertising_now "
                          "from company_market where company_number = '1'").fetchone()
    assert rollup == (6, 0, "basic", "advertising_poorly", 1)                # the W4 columns are untouched
    created = conn.execute("select gap_segment, setup_level from company_market where company_number = '2'").fetchone()
    assert created == ("site_first", "unknown")                              # a row is created when W4 has not run
    F.assess_companies(conn, ["1", "2"], **kwargs)
    assert conn.execute("select count(*) from company_setup_findings where company_number = '1'").fetchone() == (6,)
