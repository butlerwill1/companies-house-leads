from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core.companies_house_sqlite import init_db
from scripts.web import tech_rules as R
from scripts.web import web_detect as D
from scripts.web.web_fetch import Fetcher, Page, parse_html

HOME = """<html lang="en-GB"><head><title>Bott and Co Solicitors</title>
<meta name="description" content="Flight delay claims"><meta name="viewport" content="width=device-width">
<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"></script>
<script src="https://js.hs-scripts.com/1234567.js"></script>
<script>gtag('config', 'G-ABCDEFGHJK'); gtag('config', 'AW-123456789');
gtag('event','conversion',{'send_to':'AW-123456789/AbCdEf'}); fbq('init', '1234567890123');</script>
<script type="application/ld+json">{"@graph":[{"@type":"LegalService"},{"@type":["LocalBusiness","Organization"]}]}</script>
</head><body><nav><a href="/services">Services</a></nav>
<h1>Flight delay compensation</h1><p>Call 01625 415800 or 0333 880 3030. Fees from £99. Get a quote today.</p>
<form action="/enquiry" class="hs-form"><input name="email"></form><a href="tel:01625415800">Call</a>
<footer>&copy; 2025 Bott and Co. Website by <a href="https://www.pixelagency.co.uk/">Pixel Agency</a></footer>
</body></html>"""

# Tag Manager's own runtime mentions Wix, WooCommerce and Consent Mode strings whether or not a site uses them.
GTM = ('(function(){var a={"tags":[{"function":"__awct","vtp_conversionId":"123456789","vtp_conversionLabel":"AbCdEf"},'
       '{"function":"__sp","vtp_conversionId":"123456789"},{"function":"__baut","vtp_tagId":"5012345"}]};'
       'var storage=["ad_storage","analytics_storage","wait_for_update"];'
       'var platforms=["wix","woocommerce","Shopify"];'
       'var h="https://connect.facebook.net/en_US/fbevents.js";})()')


def _by_name(techs):
    return {t["technology"]: t for t in techs}


def test_rules_are_valid_unique_and_compile():
    assert len(R.RULES) >= 75 and {r.category for r in R.RULES} <= set(R.CATEGORIES)
    assert len({r.name for r in R.RULES}) == len(R.RULES)
    with pytest.raises(ValueError):
        R.Rule(name="x", category="nonsense", patterns=("x",))


def test_page_detection_with_ids_and_evidence():
    techs = _by_name(D.detect_technologies({"https://x.co.uk/": HOME}, {}))
    assert {"Google Tag Manager", "Google Analytics 4", "Google Ads", "Google Ads conversion event", "HubSpot",
            "Meta Pixel"} <= set(techs)
    assert techs["Google Tag Manager"]["account_ids"] == ["GTM-ABC1234"]
    assert techs["Google Analytics 4"]["account_ids"] == ["G-ABCDEFGHJK"]
    assert techs["Google Ads"]["account_ids"] == ["AW-123456789"]
    assert techs["HubSpot"]["account_ids"] == ["1234567"] and techs["HubSpot"]["found_in"] == "page"
    assert techs["Meta Pixel"]["account_ids"] == ["1234567890123"]
    assert techs["HubSpot"]["evidence_url"] == "https://x.co.uk/" and "hs-scripts" in techs["HubSpot"]["evidence"]
    assert "Shopify" not in techs and "WooCommerce" not in techs


def test_gtm_container_detection_and_found_in_both():
    techs = _by_name(D.detect_technologies({"https://x.co.uk/": HOME}, {"https://gtm/?id=GTM-ABC1234": GTM}))
    assert techs["Google Ads"]["found_in"] == "both"
    assert techs["Google Ads"]["account_ids"] == ["AW-123456789"]          # not duplicated across surfaces
    assert techs["Google Ads remarketing"]["found_in"] == "gtm" and techs["Microsoft Ads (UET)"]["found_in"] == "gtm"
    assert techs["Google Ads conversion event"]["found_in"] == "both"


def test_platform_rules_do_not_match_inside_tag_manager_code():
    techs = _by_name(D.detect_technologies({}, {"https://gtm/": GTM}))
    assert not {"Wix", "WooCommerce", "Shopify", "WordPress"} & set(techs)


def test_consent_mode_needs_an_explicit_default_not_runtime_strings():
    assert "Consent Mode" not in _by_name(D.detect_technologies({}, {"https://gtm/": GTM}))
    declared = 'var d={"ad_storage":"denied","analytics_storage":"denied"};'
    assert "Consent Mode" in _by_name(D.detect_technologies({}, {"https://gtm/": declared}))
    page = "<script>gtag('consent', 'default', {'ad_storage': 'denied'});</script>"
    assert "Consent Mode" in _by_name(D.detect_technologies({"https://x/": page}, {}))


def test_server_side_tagging_is_a_first_party_gtm_script():
    html = '<script src="https://data.example.co.uk/gtm.js?id=GTM-ABC1234"></script>'
    assert "Server-side tagging" in _by_name(D.detect_technologies({"https://x/": html}, {}))
    normal = '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"></script>'
    assert "Server-side tagging" not in _by_name(D.detect_technologies({"https://x/": normal}, {}))


@pytest.mark.parametrize("html,expected", [
    ('<script src="https://cdn.callrail.com/companies/1/abc/12/swap.js"></script>', "CallRail"),
    ('<script src="//static.hotjar.com/c/hotjar-123.js?sv=6"></script>', "Hotjar"),
    ('<script src="https://snap.licdn.com/li.lms-analytics/insight.min.js"></script>', "LinkedIn Insight Tag"),
    ('<iframe src="https://widget.fresha.com/x"></iframe> fresha.com', "Fresha"),
    ('<link href="https://cdn.shopify.com/s/files/1/x.css">', "Shopify"),
    ('<link href="/wp-content/themes/x/style.css">', "WordPress"),
    ('<script src="https://widget.trustpilot.com/bootstrap/v5/tp.widget.bootstrap.min.js"></script>', "Trustpilot"),
    ('<script src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"></script>', "OneTrust"),
    ('<script src="https://static.klaviyo.com/onsite/js/klaviyo.js"></script>', "Klaviyo"),
])
def test_individual_rules(html, expected):
    assert expected in _by_name(D.detect_technologies({"https://x/": html}, {}))


def test_plain_page_detects_nothing():
    plain = "<html><body><p>We sell shoes. Wix and WooCommerce are mentioned in passing, as is Shopify.</p></body></html>"
    assert D.detect_technologies({"https://x/": plain}, {}) == []


# ---------------------------------------------------------------- page facts

def _page(html, url="https://x.co.uk/"):
    return Page(url=url, final_url=url, status=200, html=html)


def test_page_facts_for_a_rich_page():
    facts = D.page_facts(_page(HOME), domain="x.co.uk", page_kind="home", crawl_version="crawl-v1",
                         cache_key="abc", company_numbers=["01234567"], in_navigation=None)
    assert facts["title"] == "Bott and Co Solicitors" and facts["h1"] == "Flight delay compensation"
    assert facts["lang"] == "en-GB" and facts["has_viewport"] == 1 and facts["noindex"] == 0
    assert json.loads(facts["schema_types"]) == ["LegalService", "LocalBusiness", "Organization"]
    assert facts["form_count"] == 1 and json.loads(facts["form_providers"]) == ["hubspot"]
    assert facts["tel_link_count"] == 1 and json.loads(facts["phone_numbers"]) == ["01625415800", "03338803030"]
    assert "get a quote" in json.loads(facts["cta_phrases"]) and facts["price_mentions"] == 1
    assert facts["copyright_year"] == 2025 and facts["company_number_found"] == 0 and facts["in_navigation"] is None


def test_page_facts_noindex_and_company_number():
    html = ('<html><head><meta name="robots" content="noindex,nofollow"></head><body>'
            'Registered in England, company number 01234567. ' + "word " * 5 + "</body></html>")
    facts = D.page_facts(_page(html), domain="x.co.uk", page_kind="landing", crawl_version="c", cache_key="k",
                         company_numbers=["01234567"], in_navigation=False)
    assert facts["noindex"] == 1 and facts["company_number_found"] == 1 and facts["in_navigation"] == 0


def test_failed_sitemap_and_container_pages_get_fetch_facts_only():
    failed = Page(url="https://x.co.uk/", final_url=None, status=403, html="", error="blocked (403)", blocked=True)
    facts = D.page_facts(failed, domain="x.co.uk", page_kind="home", crawl_version="c", cache_key="k")
    assert facts["fetch_error"] == "blocked (403)" and facts["title"] is None and facts["word_count"] is None
    sitemap = D.page_facts(_page("<urlset></urlset>", "https://x.co.uk/sitemap.xml"), domain="x.co.uk",
                           page_kind="sitemap", crawl_version="c", cache_key="k")
    assert sitemap["word_count"] is None


def test_phone_numbers_normalise_uk_formats_and_skip_other_numbers():
    text = "Call +44 (0)20 7946 0958, 07700 900123 or 0800 024 8505. Company 01234567. Ref 1234567890."
    assert D.phone_numbers(text) == ["02079460958", "07700900123", "08000248505"]
    assert D.phone_numbers("", ["+44 1625 415800"]) == ["01625415800"]


def test_copyright_year_ignores_years_far_in_the_future():
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    assert D.copyright_year("(c) 2019-2025 Acme", now=now) == 2025
    assert D.copyright_year("Copyright 2031", now=now) is None


def test_schema_types_survive_bad_json():
    assert D.schema_types(["{not json", '{"@type": "Product"}']) == ["Product"]


def test_agency_credit_requires_an_external_link_after_the_keyword():
    parsed = parse_html(HOME, "https://x.co.uk/")
    assert D.agency_credit(parsed, "x.co.uk") == ("Pixel Agency", "https://www.pixelagency.co.uk/")
    junk = parse_html('<footer>Website designed by experts. <a href="https://twitter.com/x">Twitter</a> '
                      '<a href="https://other.com/">Other</a> made by hand</footer>', "https://x.co.uk/")
    assert D.agency_credit(junk, "x.co.uk") == (None, None)


# ---------------------------------------------------------------- sitemap and site summary

SITEMAP = """<urlset>
<url><loc>https://x.co.uk/</loc><lastmod>2026-09-01</lastmod></url>
<url><loc>https://x.co.uk/blog/post-1</loc><lastmod>2026-08-20</lastmod></url>
<url><loc>https://x.co.uk/products/widget</loc></url>
<url><loc>https://x.co.uk/locations/leeds</loc><lastmod>2025-01-01</lastmod></url></urlset>"""


def test_sitemap_info():
    assert D.sitemap_info([SITEMAP]) == {"url_count": 4, "newest_lastmod": "2026-09-01", "product_url_count": 1,
                                         "location_page_count": 1, "blog_latest_date": "2026-08-20"}
    assert D.sitemap_info([])["url_count"] == 0


def _row(kind, url, **over):
    base = {"domain": "x.co.uk", "url": url, "final_url": url, "page_kind": kind, "crawl_version": "c",
            "fetched_with": "http", "status_code": 200, "fetch_error": None, "word_count": 400, "has_viewport": 1,
            "noindex": 0, "in_navigation": 1, "schema_types": None, "form_count": 0, "tel_link_count": 0,
            "cta_phrases": "[]", "copyright_year": None}
    return {**base, **over}


def _summary(rows, techs=(), **kw):
    return D.summarise_site("x.co.uk", rows, list(techs), crawl_version="c", rule_version="r", **kw)


def test_summary_of_a_sophisticated_site():
    techs = D.detect_technologies({"https://x.co.uk/": HOME}, {"g": GTM})
    rows = [_row("home", "https://x.co.uk/", form_count=1, tel_link_count=1, copyright_year=2025,
                 schema_types='["LocalBusiness"]', fetched_with="browser", cta_phrases='["get a quote"]'),
            _row("service", "https://x.co.uk/a"), _row("landing", "https://x.co.uk/lp/x", in_navigation=0, noindex=1),
            _row("blog", "https://x.co.uk/blog"), _row("privacy", "https://x.co.uk/p", noindex=1, in_navigation=0)]
    s = _summary(rows, techs, sitemap=D.sitemap_info([SITEMAP]), agency=("Pixel Agency", "https://pa.co.uk/"))
    assert s["crawl_status"] == "ok" and s["pages_fetched"] == 5 and s["pages_by_browser"] == 1
    assert s["https"] == 1 and s["mobile_ready"] == 1 and s["copyright_year"] == 2025
    assert s["has_google_ads_tag"] == 1 and s["has_ads_conversion_event"] == 1 and s["has_ads_remarketing"] == 1
    assert s["has_microsoft_ads"] == 1 and json.loads(s["social_pixels"]) == ["Meta Pixel"]
    assert json.loads(s["crm_vendors"]) == ["HubSpot"] and json.loads(s["gtm_ids"]) == ["GTM-ABC1234"]
    assert s["has_contact_form"] == 1 and s["has_click_to_call"] == 1 and s["service_page_count"] == 1
    assert s["landing_page_count"] == 1                    # the noindex privacy page is not a landing page
    assert s["has_blog"] == 1 and s["blog_latest_date"] == "2026-08-20" and s["location_page_count"] == 1
    assert s["agency_credit"] == "Pixel Agency" and s["sitemap_url_count"] == 4


def test_summary_statuses():
    ok = _row("home", "https://x.co.uk/")
    assert _summary([ok])["crawl_status"] == "ok"
    assert _summary([_row("home", "https://x.co.uk/", word_count=40)])["crawl_status"] == "thin"
    blocked = _row("home", "https://x.co.uk/", status_code=403, fetch_error="blocked (403)", word_count=None)
    assert _summary([blocked])["crawl_status"] == "blocked"
    assert _summary([_row("home", "https://x.co.uk/", status_code=None, fetch_error="timeout",
                          word_count=None)])["crawl_status"] == "unreachable"
    assert _summary([ok], parked=True)["crawl_status"] == "parked"


def test_summary_platform_checkout_and_consent_vendor():
    techs = [{"technology": "Shopify", "category": "ecommerce", "found_in": "page", "account_ids": [],
              "evidence": "", "evidence_url": ""},
             {"technology": "Cookiebot", "category": "consent", "found_in": "page", "account_ids": [],
              "evidence": "", "evidence_url": ""}]
    s = _summary([_row("home", "https://x.co.uk/")], techs)
    assert s["platform"] == "Shopify" and s["has_checkout"] == 1 and s["consent_vendor"] == "Cookiebot"
    assert s["has_consent_mode"] == 0 and s["has_google_ads_tag"] == 0 and s["agency_credit"] is None


# ---------------------------------------------------------------- the detect step over a cache

def test_detect_domain_reads_only_the_cache_and_stores_everything(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    cache = Fetcher(cache_dir=tmp_path / "pages", respect_robots=False)
    gtm_url = "https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"
    cache.put(Page(url="https://x.co.uk/", final_url="https://x.co.uk/", status=200, html=HOME))
    cache.put(Page(url=gtm_url, final_url=gtm_url, status=200, html=GTM))
    cache.put(Page(url="https://x.co.uk/sitemap.xml", final_url="https://x.co.uk/sitemap.xml", status=200,
                   html=SITEMAP))
    D.store_pages(conn, [
        D.page_facts(cache.get("https://x.co.uk/"), domain="x.co.uk", page_kind="home", crawl_version="c",
                     cache_key=cache.cache_key("https://x.co.uk/")),
        D.page_facts(cache.get(gtm_url, accept=("script",)), domain="x.co.uk", page_kind="gtm_container",
                     crawl_version="c", cache_key="g"),
        D.page_facts(cache.get("https://x.co.uk/sitemap.xml"), domain="x.co.uk", page_kind="sitemap",
                     crawl_version="c", cache_key="s")])
    offline = Fetcher(cache_dir=tmp_path / "pages", cache_only=True)
    counts = D.run_detect(conn, ["x.co.uk", "nocrawl.co.uk"], offline, crawl_version="c")
    assert counts == {"domains": 1, "no_crawl": 1, "thin": 1} and offline.requests_made == 0   # HOME is a tiny fixture
    assert conn.execute("select count(*) from web_pages").fetchone() == (3,)
    names = {r[0] for r in conn.execute("select technology from web_technologies")}
    assert {"HubSpot", "Google Ads remarketing", "Microsoft Ads (UET)"} <= names and "Wix" not in names
    site = conn.execute("select has_google_ads_tag, crm_vendors, sitemap_url_count, agency_credit from web_sites"
                        ).fetchone()
    assert site == (1, '["HubSpot"]', 4, "Pixel Agency")
    # re-running replaces rather than duplicates
    D.run_detect(conn, ["x.co.uk"], offline, crawl_version="c")
    assert conn.execute("select count(*) from web_sites").fetchone() == (1,)
    assert conn.execute("select count(*) from web_technologies where technology = 'HubSpot'").fetchone() == (1,)
    # a new rule version sits beside the old one
    D.run_detect(conn, ["x.co.uk"], offline, crawl_version="c", rule_version="rules-v2")
    assert conn.execute("select count(*) from web_sites").fetchone() == (2,)


def test_tag_manager_runtime_strings_are_not_tags():
    # Found on real containers: __lcl is the link-click trigger, and viewthroughconversion is in the runtime.
    runtime = ('{"function":"__lcl","vtp_waitForTags":false},{"function":"__cl"},'
               'var x="viewthroughconversion";var y=["__sp","__awct","__baut"];')
    techs = _by_name(D.detect_technologies({}, {"https://gtm/": runtime}))
    assert not {"LinkedIn Insight Tag", "Google Ads remarketing", "Google Ads", "Microsoft Ads (UET)"} & set(techs)
    real = '{"function":"__sp","vtp_conversionId":"123456789"}'
    assert "Google Ads remarketing" in _by_name(D.detect_technologies({}, {"https://gtm/": real}))
    on_page = "<script>var google_remarketing_only = true;</script>"
    assert "Google Ads remarketing" in _by_name(D.detect_technologies({"https://x/": on_page}, {}))
