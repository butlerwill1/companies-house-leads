#!/usr/bin/env python3
"""Polite page fetching and HTML parsing for the web stage.

Fetching a company's own website is free, so it is not ledgered, but it is
still done politely: robots.txt is honoured, every request names this
project in its user agent, each host gets a pause between requests, and
pages are cached under `data/raw/web-pages/<host>/<sha1>.json.gz` so a re-run
makes no requests at all. Older uncompressed `.json` cache entries are still
read.

What a page may be is set per call by `accept`: `html` (the default), `pdf`
(text is extracted with PyMuPDF and cached instead of the bytes) and `script`
(Google Tag Manager's `gtm.js` and other public JavaScript).

A refusal is recorded, never worked around: a 403 or 429, or a page that is
a bot-challenge ("Just a moment...", a CAPTCHA form), comes back as a `Page`
with `blocked=True`. The Playwright fallback (scripts/web/web_browser.py)
decides whether to try such a page again in a browser, under the same rules.

HTML is parsed with the standard library (`html.parser`): visible text, title,
links with anchor text and menu position, forms, `tel:` links, meta tags,
JSON-LD, script sources. That is all the identity check and the crawl need,
and it keeps the stage free of new dependencies.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

PAGE_CACHE = Path("data/raw/web-pages")
USER_AGENT = "Mozilla/5.0 (compatible; companies-house-leads-research/0.1)"
TIMEOUT = 15
MAX_BYTES = 2_000_000
MAX_PDF_PAGES = 15
HOST_PAUSE_SECONDS = 1.0
THIN_WORDS = 150
SKIP_TEXT_TAGS = {"script", "style", "noscript", "template", "svg"}
TRANSIENT_STATUSES = {408, 425, 500, 502, 503, 504}
BLOCK_STATUSES = {401, 403, 429}
CHARSET = re.compile(r"charset=[\"']?([\w-]+)", re.I)
CHALLENGE = re.compile(
    r"just a moment\.\.\.|checking your browser|attention required! \| cloudflare|cf-chl-|challenge-platform|"
    r"_incapsula_|incapsula incident|px-captcha|perimeterx|datadome|please verify you are (?:a )?human|"
    r"are you a robot|g-recaptcha|h-captcha|hcaptcha|access denied.{0,80}(?:reference|support id)|"
    r"request unsuccessful\. incapsula|enable javascript and cookies to continue", re.I | re.S)
ACCEPT_TYPES = {
    "html": ("html", "xml"),
    "pdf": ("pdf",),
    "script": ("javascript", "ecmascript", "text/plain", "octet-stream"),
}


@dataclass
class Page:
    url: str
    final_url: str | None
    status: int | None
    html: str
    error: str | None = None
    fetched_with: str = "http"        # http / browser
    blocked: bool = False             # refused or challenged: record, do not work around
    content_type: str | None = None
    script_hosts: list[str] = field(default_factory=list)   # browser: hosts the page actually requested

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and bool(self.html)


@dataclass
class ParsedPage:
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)       # (absolute url, anchor text)
    nav_links: list[str] = field(default_factory=list)               # urls of links inside <nav> or <header>
    tel_links: list[str] = field(default_factory=list)               # numbers from tel: links
    forms: list[dict[str, Any]] = field(default_factory=list)        # {"action", "inputs"}
    scripts: list[str] = field(default_factory=list)                 # absolute src urls
    meta: dict[str, str] = field(default_factory=dict)               # name/property (lower case) -> content
    jsonld: list[str] = field(default_factory=list)                  # raw JSON-LD blocks
    h1: str = ""
    lang: str = ""
    canonical: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class _Parser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.nav_depth = 0
        self.in_title = False
        self.in_h1 = False
        self.title: list[str] = []
        self.h1: list[str] = []
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.nav_links: list[str] = []
        self.tel_links: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.lang = ""
        self.canonical = ""
        self._href: str | None = None
        self._href_in_nav = False
        self._anchor: list[str] = []
        self._ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TEXT_TAGS:
            self.skip_depth += 1
            if tag == "script":
                if "ld+json" in a.get("type", "").lower():
                    self._ld = []
                if a.get("src"):
                    self.scripts.append(urljoin(self.base_url, a["src"]))
        elif tag == "html":
            self.lang = a.get("lang", "")[:12]
        elif tag == "title":
            self.in_title = True
        elif tag == "h1":
            self.in_h1 = True
        elif tag in ("nav", "header"):
            self.nav_depth += 1
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
        elif tag == "link":
            if "canonical" in a.get("rel", "").lower() and a.get("href"):
                self.canonical = urljoin(self.base_url, a["href"])
        elif tag == "form":
            self.forms.append({"action": a.get("action", ""), "id": a.get("id", ""), "class": a.get("class", ""),
                               "inputs": 0})
        elif tag in ("input", "textarea", "select"):
            if self.forms and a.get("type", "text").lower() not in ("hidden", "submit", "button", "image"):
                self.forms[-1]["inputs"] += 1
        elif tag == "a":
            href = a.get("href", "")
            if href.lower().startswith("tel:"):
                self.tel_links.append(href[4:].strip())
            elif href and not href.startswith(("mailto:", "javascript:", "#")):
                self._href = urljoin(self.base_url, href)
                self._href_in_nav = self.nav_depth > 0
                self._anchor = []
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "footer", "section"):
            self.text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TEXT_TAGS and self.skip_depth:
            self.skip_depth -= 1
            if tag == "script" and self._ld is not None:
                self.jsonld.append("".join(self._ld))
                self._ld = None
        elif tag == "title":
            self.in_title = False
        elif tag == "h1":
            self.in_h1 = False
        elif tag in ("nav", "header") and self.nav_depth:
            self.nav_depth -= 1
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._anchor).split())))
            if self._href_in_nav:
                self.nav_links.append(self._href)
            self._href = None

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            if self._ld is not None:
                self._ld.append(data)
            return
        if self.in_title:
            self.title.append(data)
            return
        if self.in_h1 and not self.h1:
            self.h1.append(data)
        self.text.append(data)
        if self._href is not None:
            self._anchor.append(data)


def parse_html(html: str, base_url: str) -> ParsedPage:
    parser = _Parser(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 -- malformed markup: keep whatever was parsed
        pass
    return ParsedPage(title=" ".join("".join(parser.title).split()),
                      text=" ".join("".join(parser.text).split()),
                      links=parser.links, nav_links=parser.nav_links, tel_links=parser.tel_links,
                      forms=parser.forms, scripts=parser.scripts, meta=parser.meta, jsonld=parser.jsonld,
                      h1=" ".join("".join(parser.h1).split()), lang=parser.lang, canonical=parser.canonical)


def is_challenge(html: str) -> bool:
    """A bot-challenge or access-denied page rather than the site's own."""
    return bool(html) and CHALLENGE.search(html[:60_000]) is not None


def is_thin(html: str, min_words: int = THIN_WORDS) -> bool:
    """A page that downloaded fine but says almost nothing: usually built by
    JavaScript after load, so a plain download sees an empty shell."""
    return parse_html(html, "http://x/").word_count < min_words


def pdf_text(data: bytes, *, max_pages: int = MAX_PDF_PAGES) -> str:
    """Text of the first pages of a PDF. Raises RuntimeError if PyMuPDF is missing."""
    try:
        import pymupdf as fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover -- the package is in requirements-eval.txt
        raise RuntimeError("PyMuPDF is not installed") from exc
    with fitz.open(stream=data, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in list(document)[:max_pages])


class Fetcher:
    """Cached, robots-respecting GET of whole pages."""

    def __init__(self, *, cache_dir: Path = PAGE_CACHE, session: Any = None, cache_only: bool = False,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 respect_robots: bool = True) -> None:
        self.cache_dir = cache_dir
        self.session = session or requests.Session()
        self.cache_only = cache_only
        self.sleep = sleep
        self.clock = clock
        self.respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self.requests_made = 0

    # -- cache

    def _cache_base(self, url: str) -> Path:
        host = (urlparse(url).hostname or "unknown").lower()
        return self.cache_dir / host / hashlib.sha1(url.encode()).hexdigest()

    def cache_key(self, url: str) -> str:
        return hashlib.sha1(url.encode()).hexdigest()

    def cache_path(self, url: str) -> Path | None:
        """The existing cache file for a url (compressed or legacy), if any."""
        base = self._cache_base(url)
        for candidate in (base.with_name(base.name + ".json.gz"), base.with_name(base.name + ".json")):
            if candidate.exists():
                return candidate
        return None

    def _read_cache(self, path: Path) -> Page:
        raw = path.read_bytes()
        data = json.loads((gzip.decompress(raw) if path.suffix == ".gz" else raw).decode("utf-8"))
        status, error, blocked = data.get("status"), data.get("error"), bool(data.get("blocked"))
        if status in BLOCK_STATUSES and not blocked:
            # an entry written before `blocked` existed ("HTTP 403"): read it as the refusal it was
            blocked, error = True, f"blocked ({status})"
        return Page(url=data["url"], final_url=data.get("final_url"), status=status,
                    html=data.get("html") or "", error=error,
                    fetched_with=data.get("fetched_with") or "http", blocked=blocked,
                    content_type=data.get("content_type"), script_hosts=data.get("script_hosts") or [])

    def put(self, page: Page) -> None:
        """Write a page to the cache, replacing any earlier entry for its url
        (the browser fallback replaces a blocked plain download this way)."""
        base = self._cache_base(page.url)
        base.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "url": page.url, "final_url": page.final_url, "status": page.status, "html": page.html,
            "error": page.error, "fetched_with": page.fetched_with, "blocked": page.blocked,
            "content_type": page.content_type, "script_hosts": page.script_hosts,
            "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}, ensure_ascii=False)
        target = base.with_name(base.name + ".json.gz")
        tmp = target.with_suffix(f".{threading.get_ident()}.tmp")   # unique per thread: parallel runs
        tmp.write_bytes(gzip.compress(payload.encode("utf-8"), compresslevel=6))
        os.replace(tmp, target)
        legacy = base.with_name(base.name + ".json")
        if legacy.exists():
            legacy.unlink()

    # -- politeness

    def _pause(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = HOST_PAUSE_SECONDS - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_hit[host] = self.clock()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots: RobotFileParser | None = RobotFileParser()
            try:
                self._pause(parsed.netloc)
                self.requests_made += 1
                response = self.session.get(f"{origin}/robots.txt", timeout=TIMEOUT,
                                            headers={"User-Agent": USER_AGENT})
                if response.status_code == 200:
                    robots.parse(response.text.splitlines())
                else:
                    robots = None  # no robots.txt (or an error page): nothing is disallowed
            except requests.RequestException:
                robots = None
            self._robots[origin] = robots
        robots = self._robots[origin]
        return robots is None or robots.can_fetch(USER_AGENT, url)

    # -- fetching

    def get(self, url: str, *, accept: Iterable[str] = ("html",), check_robots: bool = True) -> Page:
        """`check_robots=False` is for a script the page itself loads for every visitor (Tag Manager's
        gtm.js), which is an asset, not a page being crawled."""
        accept = tuple(accept)
        cached = self.cache_path(url)
        if cached is not None:
            page = self._read_cache(cached)
            refused_type = bool(page.error) and page.error.startswith("not html") and accept != ("html",)
            if not (refused_type and not self.cache_only):   # accepted more types since: ask again
                return page
        if self.cache_only:
            return Page(url=url, final_url=None, status=None, html="", error="not cached (cache-only run)")
        if check_robots and not self.allowed(url):
            page = Page(url=url, final_url=None, status=None, html="", error="disallowed by robots.txt")
        else:
            page = self._fetch(url, accept)
        if page.status in TRANSIENT_STATUSES or (page.status is None and page.error and "robots" not in page.error):
            return page  # a timeout or a 503 may pass: not cached, so the next run tries again
        if page.status == 429:
            return page  # asked to slow down: not a verdict on the site
        self.put(page)
        return page

    def _fetch(self, url: str, accept: tuple[str, ...]) -> Page:
        self._pause(urlparse(url).netloc)
        self.requests_made += 1
        try:
            response = self.session.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                                        allow_redirects=True)
            content_type = response.headers.get("Content-Type", "")
            status = response.status_code
            if status in BLOCK_STATUSES:
                return Page(url=url, final_url=response.url, status=status, html="", error=f"blocked ({status})",
                            blocked=True, content_type=content_type)
            allowed_types = tuple(t for kind in accept for t in ACCEPT_TYPES.get(kind, ()))
            lowered = content_type.lower()
            if content_type and not any(t in lowered for t in allowed_types):
                return Page(url=url, final_url=response.url, status=status, html="",
                            error=f"not html: {content_type[:60]}", content_type=content_type)
            if "pdf" in lowered:
                try:
                    text = pdf_text(response.content[:20_000_000])
                except Exception as exc:  # noqa: BLE001 -- a broken PDF is an unreadable page, not a crash
                    return Page(url=url, final_url=response.url, status=status, html="",
                                error=f"pdf unreadable: {exc}"[:200], content_type=content_type)
                return Page(url=url, final_url=response.url, status=status, html=text, content_type=content_type,
                            error=None if status == 200 else f"HTTP {status}")
            # Only an explicit charset is trusted: requests assumes ISO-8859-1 for
            # text/html without one, which garbles the many UTF-8 sites that omit it.
            charset = CHARSET.search(content_type)
            html = response.content[:MAX_BYTES].decode(charset.group(1) if charset else "utf-8", errors="replace")
            if "script" not in accept or "html" in lowered:
                if is_challenge(html):
                    return Page(url=url, final_url=response.url, status=status, html="", error="challenge page",
                                blocked=True, content_type=content_type)
            return Page(url=url, final_url=response.url, status=status, html=html, content_type=content_type,
                        error=None if status == 200 else f"HTTP {status}")
        except (requests.RequestException, LookupError) as exc:
            return Page(url=url, final_url=None, status=None, html="", error=f"{type(exc).__name__}: {exc}"[:300])
