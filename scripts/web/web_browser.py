#!/usr/bin/env python3
"""Playwright fallback for pages a plain download cannot read (W2).

Used only when a plain download is refused (403 or 429), comes back as a
bot-challenge page, or is "thin" (a shell that JavaScript fills in after
load). It loads the page once in headless Chromium, waits briefly for tags to
fire, and caches the rendered HTML in the same page cache as everything else,
together with the hosts the page actually requested (which shows tags that
only load through JavaScript).

The boundary, the same as the plain fetcher's:

- it identifies as this project (`USER_AGENT`), never as a person's browser;
- it honours robots.txt;
- if it meets a block, challenge or CAPTCHA, it stops and the page is
  recorded as `blocked`. Nothing is solved, retried under a disguised
  identity, or routed around.

Playwright is optional: without it `BrowserFetcher.available()` is False and
the crawl records such pages as blocked or thin. Install with
`pip install playwright` then `playwright install chromium` (about 150 MB, a
one-off).
"""
from __future__ import annotations

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urlparse

from scripts.web.search_providers import registrable_domain
from scripts.web.web_fetch import BLOCK_STATUSES, MAX_BYTES, USER_AGENT, Fetcher, Page, is_challenge

NAVIGATION_TIMEOUT_MS = 20_000
SETTLE_MS = 2_500
MAX_SCRIPT_HOSTS = 200


def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _launch_chromium() -> tuple[Any, Callable[[], None]]:  # pragma: no cover -- needs Playwright and Chromium installed
    from playwright.sync_api import sync_playwright

    manager = sync_playwright().start()
    browser = manager.chromium.launch(headless=True)

    def stop() -> None:
        browser.close()
        manager.stop()

    return browser, stop


class BrowserFetcher:
    """Renders one page at a time. `launcher` returns `(browser, stop)`; tests
    pass a stub, so nothing here needs a real browser to be tested."""

    def __init__(self, fetcher: Fetcher, *, launcher: Callable[[], tuple[Any, Callable[[], None]]] | None = None,
                 timeout_ms: int = NAVIGATION_TIMEOUT_MS, settle_ms: int = SETTLE_MS) -> None:
        self.fetcher = fetcher
        self._launcher = launcher
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self._browser: Any = None
        self._stop: Callable[[], None] | None = None
        self._lock = threading.Lock()
        # Playwright's sync API belongs to the thread that started it, and the crawl calls from many
        # worker threads: every browser operation (launch, render, close) runs on this one thread.
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
        self.pages_rendered = 0

    def available(self) -> bool:
        return self._launcher is not None or playwright_installed()

    def _ensure_browser(self) -> Any:
        if self._browser is None:
            launcher = self._launcher or _launch_chromium
            self._browser, self._stop = launcher()
        return self._browser

    def close(self) -> None:
        def stop() -> None:
            if self._stop is not None:
                self._stop()
            self._browser = self._stop = None

        with self._lock:
            self._thread.submit(stop).result()
            self._thread.shutdown(wait=True)

    def __enter__(self) -> "BrowserFetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, url: str) -> Page:
        """Render `url`, cache the result (replacing a blocked plain download) and return it."""
        if self.fetcher.cache_only:
            return Page(url=url, final_url=None, status=None, html="", error="not cached (cache-only run)",
                        fetched_with="browser")
        if not self.available():
            return Page(url=url, final_url=None, status=None, html="", error="browser unavailable",
                        fetched_with="browser")
        if not self.fetcher.allowed(url):
            return Page(url=url, final_url=None, status=None, html="", error="disallowed by robots.txt",
                        fetched_with="browser")
        with self._lock:
            page = self._thread.submit(self._render, url).result()
            self.pages_rendered += 1
        if page.status is not None or page.blocked:   # a timeout or crash is not a verdict on the site: not cached
            self.fetcher.put(page)
        return page

    def _render(self, url: str) -> Page:
        self.fetcher._pause(urlparse(url).netloc)
        site = registrable_domain(url)
        hosts: set[str] = set()
        context = None
        try:
            context = self._ensure_browser().new_context(user_agent=USER_AGENT)
            tab = context.new_page()
            tab.on("request", lambda request: hosts.add(urlparse(request.url).hostname or ""))
            response = tab.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            tab.wait_for_timeout(self.settle_ms)
            html = tab.content()[:MAX_BYTES]
            final_url, status = tab.url, (response.status if response is not None else None)
        except Exception as exc:  # noqa: BLE001 -- a timeout, a crash or a closed browser: record it, do not raise
            return Page(url=url, final_url=None, status=None, html="", error=f"browser: {type(exc).__name__}: {exc}"[:300],
                        fetched_with="browser")
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
        third_party = sorted(h for h in hosts if h and registrable_domain(h) != site)[:MAX_SCRIPT_HOSTS]
        if status in BLOCK_STATUSES:
            return Page(url=url, final_url=final_url, status=status, html="", error=f"blocked ({status})",
                        blocked=True, fetched_with="browser", script_hosts=third_party)
        if is_challenge(html):
            return Page(url=url, final_url=final_url, status=status, html="", error="challenge page", blocked=True,
                        fetched_with="browser", script_hosts=third_party)
        return Page(url=url, final_url=final_url, status=status, html=html, fetched_with="browser",
                    content_type="text/html", script_hosts=third_party,
                    error=None if status == 200 else f"HTTP {status}")
