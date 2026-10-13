#!/usr/bin/env python3
"""W2 crawl: read up to 25 pages of each website (docs/WEB_STAGE_PLAN.md).

Fetching is separate from detecting. This step fetches pages into the page
cache (scripts/website_analysis/web_fetch.py) and records one `web_pages` row per page;
detection (scripts/website_analysis/web_detect.py) later reads the cache and can be re-run
for free.

Pages, in priority order: the homepage, the sitemap (and up to two child
sitemaps of an index), privacy and terms, contact, about, pricing, service
pages, location pages, landing-style pages, one product page, one blog page,
other menu pages, then each Google Tag Manager container the pages load
(`gtm.js`, up to three; containers do not count towards the 25).

A plain download comes first. The browser fallback (scripts/website_analysis/web_browser.py)
is tried once for any page that was refused or challenged, and for a homepage
that comes back thin (JavaScript-built). A site that still refuses is recorded
as blocked, not worked around.

Sites are crawled in parallel (one site per worker; the fetcher pauses between
requests to the same host). Each finished site is stored at once, so an
interrupted run resumes by skipping the domains that already have rows.
"""
from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable
from urllib.parse import urldefrag, urlparse

from scripts.website_analysis.search_providers import registrable_domain
from scripts.website_analysis.web_browser import BrowserFetcher
from scripts.website_analysis.web_detect import page_facts, store_pages
from scripts.website_analysis.web_fetch import Fetcher, Page, is_thin, parse_html
from scripts.website_analysis.web_identity import PARKED

CRAWL_VERSION = "crawl-v1"
MAX_PAGES = 25
MAX_GTM_CONTAINERS = 3
MAX_CHILD_SITEMAPS = 2
GTM_URL = "https://www.googletagmanager.com/gtm.js?id={id}"
GTM_ID = re.compile(r"\b(GTM-[A-Z0-9]{5,8})\b")

# Fetched in this order, each kind up to its cap.
KIND_ORDER = ("privacy", "terms", "contact", "about", "pricing", "service", "location", "landing", "product",
              "blog", "other")
KIND_CAPS = {"privacy": 1, "terms": 1, "contact": 1, "about": 1, "pricing": 1, "service": 6, "location": 4,
             "landing": 4, "product": 1, "blog": 1, "other": 4}
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".zip", ".doc", ".docx",
                   ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".mov", ".xml", ".json", ".txt", ".woff", ".woff2")
SKIP_PATHS = re.compile(r"/(?:wp-admin|wp-login|login|log-in|signin|sign-in|account|my-account|basket|cart|"
                        r"checkout|feed|tag|author|page/\d+|wp-json|cdn-cgi)(?:/|$)", re.I)
KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("privacy", re.compile(r"privacy|cookie|gdpr|data-protection", re.I)),
    ("terms", re.compile(r"terms|conditions|legal|disclaimer|t-cs|tcs", re.I)),
    ("landing", re.compile(r"(?:^|/)(?:lp|landing|landing-page|offer|offers|promo|campaign|ppc)(?:/|-|$)|"
                           r"get-a-quote|get-quote|request-a-|book-now|free-quote|free-consultation|thank-you|"
                           r"enquire-now", re.I)),
    ("contact", re.compile(r"contact|get-in-touch|enquir|find-us|reach-us", re.I)),
    ("pricing", re.compile(r"pricing|prices|fees|costs|rates|packages|plans", re.I)),
    ("location", re.compile(r"locations?|branches|areas?|our-offices|offices|coverage|near-me|find-a-", re.I)),
    ("about", re.compile(r"about|who-we-are|our-story|our-team|meet-the-team|company", re.I)),
    ("service", re.compile(r"services?|what-we-do|solutions|treatments|specialis|practice-areas|our-work|"
                           r"offerings|what-we-offer|expertise", re.I)),
    ("blog", re.compile(r"blog|news|insights?|articles?|guides?|posts?", re.I)),
    ("product", re.compile(r"products?|shop|collections?|store|category", re.I)),
)


def classify_url(url: str, anchor: str = "") -> str:
    """`privacy`, `terms`, `landing`, `contact`, `pricing`, `location`, `about`,
    `service`, `blog`, `product` or `other`, from the path and the link text."""
    path = urlparse(url).path.lower()
    haystack = f"{path} {anchor.lower()}"
    for kind, pattern in KIND_PATTERNS:
        if pattern.search(haystack):
            return kind
    return "other"


def _clean(url: str) -> str:
    return urldefrag(url)[0].split("?")[0].rstrip("/") or url


def select_pages(domain: str, links: Iterable[tuple[str, str]], nav_links: Iterable[str],
                 sitemap_urls: Iterable[str], *, budget: int, home_url: str) -> list[tuple[str, str, bool]]:
    """Up to `budget` pages as `(url, kind, in_navigation)`, by kind priority and per-kind cap.
    Menu links come before other links, which come before sitemap urls."""
    nav = {_clean(u) for u in nav_links}
    seen = {_clean(home_url)}
    ordered: list[tuple[int, str, str, bool]] = []  # (sort key, url, kind, in navigation)
    candidates: list[tuple[str, str, int]] = [(u, a, 0 if _clean(u) in nav else 1) for u, a in links]
    candidates += [(u, "", 2) for u in sitemap_urls]
    for url, anchor, source in candidates:
        cleaned = _clean(url)
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https") or registrable_domain(cleaned) != domain or cleaned in seen:
            continue
        kind = classify_url(cleaned, anchor)
        path = parsed.path.lower()
        if SKIP_PATHS.search(path) or (path.endswith(SKIP_EXTENSIONS)) or (
                path.endswith(".pdf") and kind not in ("privacy", "terms")):
            continue
        seen.add(cleaned)
        # kind priority first, then menu links before other links before sitemap urls
        ordered.append((KIND_ORDER.index(kind) * 10 + source, cleaned, kind, cleaned in nav))
    ordered.sort(key=lambda item: item[0])
    taken: dict[str, int] = {}
    chosen: list[tuple[str, str, bool]] = []
    for _, url, kind, in_nav in ordered:
        if len(chosen) >= budget:
            break
        if taken.get(kind, 0) >= KIND_CAPS[kind]:
            continue
        taken[kind] = taken.get(kind, 0) + 1
        chosen.append((url, kind, in_nav))
    return chosen


def sitemap_locs(xml_text: str) -> tuple[list[str], list[str]]:
    """`(page urls, child sitemap urls)` of a sitemap or sitemap index."""
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text or "", re.S | re.I)
    if re.search(r"<sitemapindex", xml_text or "", re.I):
        return [], locs
    return locs, []


def gtm_ids(html_texts: Iterable[str]) -> list[str]:
    found: list[str] = []
    for text in html_texts:
        for gid in GTM_ID.findall(text or ""):
            if gid not in found:
                found.append(gid)
    return found


# ---------------------------------------------------------------- one site

class _Crawl:
    """State of one site's crawl."""

    def __init__(self, domain: str, fetcher: Fetcher, browser: BrowserFetcher | None, company_numbers: list[str],
                 crawl_version: str) -> None:
        self.domain, self.fetcher, self.browser = domain, fetcher, browser
        self.company_numbers, self.crawl_version = company_numbers, crawl_version
        self.rows: list[dict[str, Any]] = []
        self.html: list[str] = []

    def _can_browse(self) -> bool:
        return self.browser is not None and self.browser.available() and not self.fetcher.cache_only

    def fetch(self, url: str, kind: str, *, in_navigation: bool | None = None,
              accept: tuple[str, ...] = ("html",)) -> Page:
        """A plain download; if it was refused, one browser attempt (which may itself record `blocked`)."""
        page = self.fetcher.get(url, accept=accept)
        if page.blocked and self._can_browse():
            rendered = self.browser.fetch(url)
            if rendered.ok or rendered.blocked:
                page = rendered
        self.record(page, kind, in_navigation)
        return page

    def record(self, page: Page, kind: str, in_navigation: bool | None = None) -> None:
        facts = page_facts(page, domain=self.domain, page_kind=kind, crawl_version=self.crawl_version,
                           cache_key=self.fetcher.cache_key(page.url), company_numbers=self.company_numbers,
                           in_navigation=in_navigation)
        self.rows.append(facts)
        if page.ok and kind not in ("sitemap", "gtm_container"):
            self.html.append(page.html)


def crawl_site(domain: str, fetcher: Fetcher, browser: BrowserFetcher | None = None, *,
               company_numbers: Iterable[str] = (), start_url: str | None = None, max_pages: int = MAX_PAGES,
               crawl_version: str = CRAWL_VERSION) -> list[dict[str, Any]]:
    """Fetch one site and return its `web_pages` rows (nothing is stored here)."""
    crawl = _Crawl(domain, fetcher, browser, list(company_numbers), crawl_version)
    starts = [u for u in (start_url, f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/") if u]
    home: Page | None = None
    last: Page | None = None
    for url in dict.fromkeys(starts):
        page = fetcher.get(url)
        if not page.ok and page.blocked and crawl._can_browse():
            rendered = browser.fetch(url)
            if rendered.ok or rendered.blocked:
                page = rendered
        elif page.ok and is_thin(page.html) and crawl._can_browse():
            rendered = browser.fetch(url)
            if rendered.ok and len(rendered.html) > len(page.html):
                page = rendered
        if page.ok:
            home = page
            break
        last = page
    if home is None:
        crawl.record(last, "home")          # the failed attempt, so the site is recorded as blocked or unreachable
        return crawl.rows
    crawl.record(home, "home")
    parsed = parse_html(home.html, home.final_url or home.url)
    if PARKED.search(parsed.text[:3000]):
        crawl.rows[-1]["fetch_error"] = "parked"
        return crawl.rows
    site_domain = registrable_domain(home.final_url or home.url) or domain
    origin = "{0.scheme}://{0.netloc}".format(urlparse(home.final_url or home.url))

    fetched = 1
    sitemap_urls: list[str] = []
    sitemap_page = crawl.fetch(f"{origin}/sitemap.xml", "sitemap")
    fetched += 1
    if sitemap_page.ok:
        urls, children = sitemap_locs(sitemap_page.html)
        sitemap_urls += urls
        for child in children[:MAX_CHILD_SITEMAPS]:
            child_page = crawl.fetch(child, "sitemap")
            fetched += 1
            if child_page.ok:
                sitemap_urls += sitemap_locs(child_page.html)[0]
    for url, kind, in_nav in select_pages(site_domain, parsed.links, parsed.nav_links, sitemap_urls,
                                          budget=max(0, max_pages - fetched), home_url=home.final_url or home.url):
        crawl.fetch(url, kind, in_navigation=in_nav, accept=("html", "pdf") if kind in ("privacy", "terms") else ("html",))

    for gid in gtm_ids(crawl.html)[:MAX_GTM_CONTAINERS]:
        container = fetcher.get(GTM_URL.format(id=gid), accept=("script",), check_robots=False)
        crawl.record(container, "gtm_container")
    return crawl.rows


# ---------------------------------------------------------------- many sites

def domains_to_crawl(conn: sqlite3.Connection, resolver_version: str | None = None, *,
                     tiers: tuple[str, ...] = ("verified", "probable")) -> list[dict[str, Any]]:
    """Each chosen website once, with the companies that use it and the url the
    identity check landed on: `{"domain", "company_numbers", "start_url"}`.
    `resolver_version` None reads each company's newest identity, whichever
    version wrote it."""
    marks = ",".join("?" for _ in tiers)
    source = "company_web_identity where resolver_version = ? and" if resolver_version else "company_web_identity_current where"
    rows = conn.execute(
        f"select domain, company_number, final_url from {source} "
        f"role = 'main' and tier in ({marks}) and domain is not null order by domain, company_number",
        ((resolver_version, *tiers) if resolver_version else tiers)).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for domain, number, final_url in rows:
        site = out.setdefault(domain, {"domain": domain, "company_numbers": [], "start_url": final_url})
        site["company_numbers"].append(number)
    return list(out.values())


def crawled_domains(conn: sqlite3.Connection, crawl_version: str) -> set[str]:
    return {d for (d,) in conn.execute("select distinct domain from web_pages where crawl_version = ?",
                                       (crawl_version,))}


def crawl_many(conn: sqlite3.Connection, sites: list[dict[str, Any]], fetcher: Fetcher,
               browser: BrowserFetcher | None = None, *, workers: int = 8, max_pages: int = MAX_PAGES,
               crawl_version: str = CRAWL_VERSION, log: Callable[[str], None] = print) -> dict[str, int]:
    """Crawl `sites` in parallel and store each as it finishes. Sites that already
    have rows for this crawl version are skipped, so an interrupted run resumes."""
    done = crawled_domains(conn, crawl_version)
    todo = [s for s in sites if s["domain"] not in done]
    counts = {"asked": len(sites), "skipped": len(sites) - len(todo), "crawled": 0, "pages": 0, "failed": 0}
    if not todo:
        return counts

    def work(site: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | Exception]:
        try:
            return site, crawl_site(site["domain"], fetcher, browser, company_numbers=site.get("company_numbers", ()),
                                    start_url=site.get("start_url"), max_pages=max_pages, crawl_version=crawl_version)
        except Exception as exc:  # noqa: BLE001 -- one site must not sink the run; it is left uncrawled and retried next run
            return site, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, site) for site in todo]
        for index, future in enumerate(as_completed(futures), 1):
            site, result = future.result()
            if isinstance(result, Exception):
                counts["failed"] += 1
                log(f"  {index}/{len(todo)} {site['domain']}: failed ({type(result).__name__}: {result})")
                continue
            counts["pages"] += store_pages(conn, result)
            counts["crawled"] += 1
            log(f"  {index}/{len(todo)} {site['domain']}: {len(result)} pages")
    return counts
