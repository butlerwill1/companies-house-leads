from __future__ import annotations

import sqlite3

from core.companies_house_sqlite import init_db
from scripts.web import web_crawl as C
from scripts.web.web_fetch import Page


# ---------------------------------------------------------------- classification and selection

def test_classify_url_by_path_and_anchor():
    assert C.classify_url("https://x.co.uk/privacy-policy") == "privacy"
    assert C.classify_url("https://x.co.uk/terms-and-conditions") == "terms"
    assert C.classify_url("https://x.co.uk/contact-us") == "contact"
    assert C.classify_url("https://x.co.uk/about-us") == "about"
    assert C.classify_url("https://x.co.uk/our-services/boiler-repair") == "service"
    assert C.classify_url("https://x.co.uk/pricing") == "pricing"
    assert C.classify_url("https://x.co.uk/locations/leeds") == "location"
    assert C.classify_url("https://x.co.uk/lp/free-boiler-grant") == "landing"
    assert C.classify_url("https://x.co.uk/get-a-quote") == "landing"
    assert C.classify_url("https://x.co.uk/blog/post") == "blog"
    assert C.classify_url("https://x.co.uk/shop/shoes") == "product"
    assert C.classify_url("https://x.co.uk/x", "Contact us") == "contact"
    assert C.classify_url("https://x.co.uk/zebra") == "other"


def test_select_pages_prefers_core_kinds_applies_caps_and_skips_junk():
    links = [("https://x.co.uk/services/a", "A"), ("https://x.co.uk/services/b", "B"),
             ("https://x.co.uk/contact", "Contact"), ("https://x.co.uk/about", "About"),
             ("https://x.co.uk/privacy", "Privacy"), ("https://x.co.uk/files/brochure.pdf", "Brochure"),
             ("https://x.co.uk/privacy.pdf", "Privacy PDF"), ("https://x.co.uk/wp-admin/", "Admin"),
             ("https://other.com/contact", "Elsewhere"), ("https://x.co.uk/#top", "Top"),
             ("https://x.co.uk/contact?utm=1", "Contact again"), ("https://x.co.uk/img.png", "Image")]
    chosen = C.select_pages("x.co.uk", links, ["https://x.co.uk/services/a"], [], budget=20,
                            home_url="https://x.co.uk/")
    urls = [u for u, _, _ in chosen]
    assert urls[:3] == ["https://x.co.uk/privacy", "https://x.co.uk/contact", "https://x.co.uk/about"]
    assert "https://x.co.uk/privacy.pdf" not in urls            # privacy cap is 1: the html page wins
    assert not any(u.endswith((".png", "brochure.pdf")) or "wp-admin" in u or "other.com" in u for u in urls)
    assert len([u for u in urls if u == "https://x.co.uk/contact"]) == 1        # the query variant is the same page
    assert ("https://x.co.uk/services/a", "service", True) in chosen
    assert ("https://x.co.uk/services/b", "service", False) in chosen


def test_select_pages_caps_each_kind_and_the_budget():
    links = [(f"https://x.co.uk/services/s{i}", "") for i in range(20)]
    sitemap = [f"https://x.co.uk/locations/l{i}" for i in range(10)] + ["https://x.co.uk/lp/offer-1"]
    chosen = C.select_pages("x.co.uk", links, [], sitemap, budget=100, home_url="https://x.co.uk/")
    kinds = [k for _, k, _ in chosen]
    assert kinds.count("service") == 6 and kinds.count("location") == 4 and kinds.count("landing") == 1
    assert len(C.select_pages("x.co.uk", links, [], sitemap, budget=5, home_url="https://x.co.uk/")) == 5


def test_sitemap_locs_and_gtm_ids():
    assert C.sitemap_locs("<urlset><url><loc> https://x.co.uk/a </loc></url></urlset>") == (["https://x.co.uk/a"], [])
    index = "<sitemapindex><sitemap><loc>https://x.co.uk/s1.xml</loc></sitemap></sitemapindex>"
    assert C.sitemap_locs(index) == ([], ["https://x.co.uk/s1.xml"])
    assert C.gtm_ids(["x GTM-ABC1234 y GTM-ABC1234", "z GTM-XYZ9876", "GTM-12"]) == ["GTM-ABC1234", "GTM-XYZ9876"]


# ---------------------------------------------------------------- crawling one site

WORDS = "word " * 200


def _page(url, body, **kw):
    return Page(url=url, final_url=kw.pop("final", url), status=200, html=f"<html><body>{body}{WORDS}</body></html>", **kw)


class FakeFetcher:
    cache_only = False

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, *, accept=("html",), check_robots=True):
        self.asked.append((url, tuple(accept), check_robots))
        page = self.pages.get(url)
        if page is None:
            return Page(url=url, final_url=None, status=404, html="", error="HTTP 404")
        return page

    def cache_key(self, url):
        return "k-" + url[-12:]


class FakeBrowser:
    def __init__(self, pages, available=True):
        self.pages, self._available, self.fetched = pages, available, []

    def available(self):
        return self._available

    def fetch(self, url):
        self.fetched.append(url)
        return self.pages.get(url) or Page(url=url, final_url=None, status=None, html="", error="browser: x",
                                          fetched_with="browser")


SITE = {
    "https://x.co.uk/": _page("https://x.co.uk/", '<nav><a href="/services/boilers">Boilers</a><a href="/contact">'
                                                   'Contact</a></nav><a href="/privacy">Privacy</a><a href="/lp/grant">'
                                                   'Grant</a><script src="https://www.googletagmanager.com/gtm.js?'
                                                   'id=GTM-ABC1234"></script>'),
    "https://x.co.uk/sitemap.xml": Page(url="https://x.co.uk/sitemap.xml", final_url="https://x.co.uk/sitemap.xml",
                                        status=200, html="<urlset><url><loc>https://x.co.uk/locations/leeds</loc>"
                                                         "</url></urlset>"),
    "https://x.co.uk/services/boilers": _page("https://x.co.uk/services/boilers", "Boilers"),
    "https://x.co.uk/contact": _page("https://x.co.uk/contact", "Company number 01234567 <form action='/c'>"
                                                                "<input name='a'></form>"),
    "https://x.co.uk/privacy": _page("https://x.co.uk/privacy", "Privacy"),
    "https://x.co.uk/lp/grant": _page("https://x.co.uk/lp/grant", "Grant"),
    "https://x.co.uk/locations/leeds": _page("https://x.co.uk/locations/leeds", "Leeds"),
    "https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234": Page(
        url="https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234", final_url=None, status=200,
        html='{"function":"__awct"}'),
}


def test_crawl_site_fetches_pages_sitemap_and_the_tag_manager_container():
    fetcher = FakeFetcher(dict(SITE))
    rows = C.crawl_site("x.co.uk", fetcher, company_numbers=["01234567"])
    kinds = {r["url"]: r["page_kind"] for r in rows}
    assert kinds["https://x.co.uk/"] == "home" and kinds["https://x.co.uk/sitemap.xml"] == "sitemap"
    assert kinds["https://x.co.uk/privacy"] == "privacy" and kinds["https://x.co.uk/lp/grant"] == "landing"
    assert kinds["https://x.co.uk/locations/leeds"] == "location" and kinds["https://x.co.uk/services/boilers"] == "service"
    assert kinds["https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"] == "gtm_container"
    by_url = {r["url"]: r for r in rows}
    assert by_url["https://x.co.uk/services/boilers"]["in_navigation"] == 1
    assert by_url["https://x.co.uk/lp/grant"]["in_navigation"] == 0
    assert by_url["https://x.co.uk/contact"]["company_number_found"] == 1 and by_url["https://x.co.uk/contact"]["form_count"] == 1
    assert by_url["https://x.co.uk/"]["fetched_with"] == "http"
    gtm_calls = [c for c in fetcher.asked if "googletagmanager" in c[0]]
    assert gtm_calls == [("https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234", ("script",), False)]
    assert dict(((u, a) for u, a, _ in fetcher.asked)).get("https://x.co.uk/privacy") == ("html", "pdf")


def test_crawl_site_never_exceeds_the_page_budget():
    pages = dict(SITE)
    for i in range(30):
        pages[f"https://x.co.uk/services/s{i}"] = _page(f"https://x.co.uk/services/s{i}", "s")
    links = "".join(f'<a href="/services/s{i}">S{i}</a><a href="/about-{i}">A{i}</a><a href="/zz{i}">Z{i}</a>'
                    for i in range(30))
    pages["https://x.co.uk/"] = _page("https://x.co.uk/", links)
    rows = C.crawl_site("x.co.uk", FakeFetcher(pages), max_pages=10)
    html_rows = [r for r in rows if r["page_kind"] != "gtm_container"]
    assert len(html_rows) <= 10


def test_a_blocked_site_is_recorded_not_worked_around_without_a_browser():
    blocked = Page(url="https://x.co.uk/", final_url=None, status=403, html="", error="blocked (403)", blocked=True)
    fetcher = FakeFetcher({"https://x.co.uk/": blocked, "https://www.x.co.uk/": blocked, "http://x.co.uk/": blocked})
    rows = C.crawl_site("x.co.uk", fetcher)
    assert len(rows) == 1 and rows[0]["page_kind"] == "home" and rows[0]["fetch_error"] == "blocked (403)"


def test_browser_is_tried_once_for_a_blocked_homepage_and_inner_pages():
    blocked = Page(url="https://x.co.uk/", final_url=None, status=403, html="", error="blocked (403)", blocked=True)
    rendered_home = _page("https://x.co.uk/", '<a href="/contact">Contact</a>', fetched_with="browser",
                          script_hosts=["www.googletagmanager.com"])
    blocked_contact = Page(url="https://x.co.uk/contact", final_url=None, status=403, html="",
                           error="blocked (403)", blocked=True)
    fetcher = FakeFetcher({"https://x.co.uk/": blocked, "https://x.co.uk/contact": blocked_contact})
    browser = FakeBrowser({"https://x.co.uk/": rendered_home,
                           "https://x.co.uk/contact": _page("https://x.co.uk/contact", "Contact",
                                                            fetched_with="browser")})
    rows = C.crawl_site("x.co.uk", fetcher, browser)
    by_url = {r["url"]: r for r in rows}
    assert by_url["https://x.co.uk/"]["fetched_with"] == "browser"
    assert by_url["https://x.co.uk/"]["script_hosts"] == '["www.googletagmanager.com"]'
    assert by_url["https://x.co.uk/contact"]["fetched_with"] == "browser"
    assert browser.fetched.count("https://x.co.uk/") == 1


def test_browser_that_meets_a_challenge_leaves_the_site_blocked():
    blocked = Page(url="https://x.co.uk/", final_url=None, status=403, html="", error="blocked (403)", blocked=True)
    challenged = Page(url="https://x.co.uk/", final_url=None, status=200, html="", error="challenge page",
                      blocked=True, fetched_with="browser")
    fetcher = FakeFetcher({"https://x.co.uk/": blocked, "https://www.x.co.uk/": blocked, "http://x.co.uk/": blocked})
    browser = FakeBrowser({"https://x.co.uk/": challenged, "https://www.x.co.uk/": challenged,
                           "http://x.co.uk/": challenged})
    rows = C.crawl_site("x.co.uk", fetcher, browser)
    assert rows[-1]["fetch_error"] == "challenge page" and len(rows) == 1


def test_a_thin_homepage_is_rerendered_when_the_browser_finds_more():
    thin = Page(url="https://x.co.uk/", final_url="https://x.co.uk/", status=200,
                html="<html><body><div id='root'></div></body></html>")
    full = _page("https://x.co.uk/", "Welcome", fetched_with="browser")
    rows = C.crawl_site("x.co.uk", FakeFetcher({"https://x.co.uk/": thin}), FakeBrowser({"https://x.co.uk/": full}))
    assert rows[0]["fetched_with"] == "browser" and rows[0]["word_count"] > 150


def test_a_parked_domain_stops_after_the_homepage():
    parked = Page(url="https://x.co.uk/", final_url="https://x.co.uk/", status=200,
                  html="<html><body>This domain is for sale! Buy this domain today.</body></html>")
    fetcher = FakeFetcher({"https://x.co.uk/": parked})
    rows = C.crawl_site("x.co.uk", fetcher)
    assert len(rows) == 1 and rows[0]["fetch_error"] == "parked" and len(fetcher.asked) == 1


def test_a_browser_that_is_unavailable_is_never_asked():
    blocked = Page(url="https://x.co.uk/", final_url=None, status=403, html="", error="blocked (403)", blocked=True)
    browser = FakeBrowser({}, available=False)
    C.crawl_site("x.co.uk", FakeFetcher({"https://x.co.uk/": blocked}), browser)
    assert browser.fetched == []


# ---------------------------------------------------------------- many sites

def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_domains_to_crawl_groups_companies_by_site(tmp_path):
    conn = _db(tmp_path)
    for number, name in (("1", "PAY STORE"), ("2", "STORE FIRST")):
        conn.execute("insert into companies (company_number, company_name, company_status, source_mode, "
                     "profile_payload, updated_at) values (?, ?, 'active', 'api', '{}', '2026')", (number, name))
    rows = [("1", "storefirst.com", "main", "verified", "https://www.storefirst.com/"),
            ("2", "storefirst.com", "main", "probable", "https://www.storefirst.com/"),
            ("1", "other.com", "candidate", "ambiguous", None), ("2", None, "none", "none", None)]
    for number, domain, role, tier, final in rows:
        conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, "
                     "final_url, resolved_at) values (?, 'v', ?, ?, ?, ?, '2026')", (number, domain, role, tier, final))
    assert C.domains_to_crawl(conn, "v") == [{"domain": "storefirst.com", "company_numbers": ["1", "2"],
                                              "start_url": "https://www.storefirst.com/"}]
    assert C.domains_to_crawl(conn, "v", tiers=("verified",))[0]["company_numbers"] == ["1"]


def test_crawl_many_stores_each_site_resumes_and_survives_a_failure(tmp_path):
    conn = _db(tmp_path)

    class Fetcher2(FakeFetcher):
        def get(self, url, **kw):
            if "boom.co.uk" in url:
                raise RuntimeError("connection pool exploded")
            return super().get(url, **kw)

    fetcher = Fetcher2({"https://x.co.uk/": _page("https://x.co.uk/", "home"),
                        "https://y.co.uk/": _page("https://y.co.uk/", "home")})
    sites = [{"domain": "x.co.uk", "company_numbers": ["1"]}, {"domain": "y.co.uk", "company_numbers": ["2"]},
             {"domain": "boom.co.uk", "company_numbers": ["3"]}]
    logs: list[str] = []
    counts = C.crawl_many(conn, sites, fetcher, workers=2, log=logs.append)
    assert counts == {"asked": 3, "skipped": 0, "crawled": 2, "pages": counts["pages"], "failed": 1}
    assert {d for (d,) in conn.execute("select distinct domain from web_pages")} == {"x.co.uk", "y.co.uk"}
    assert any("boom.co.uk: failed" in line for line in logs)
    again = C.crawl_many(conn, sites, fetcher, workers=2, log=logs.append)       # resumes: only the failed one is retried
    assert again["skipped"] == 2 and again["failed"] == 1 and again["crawled"] == 0
