"""Discover website candidates through Brave Search's public results page.

This is a bounded, checkpointed candidate finder. Given a screen-passing
company name and locality, it can either open one Brave public-results page in
Playwright or call the Brave Search API and record up to six non-directory
candidate URLs. Neither backend scrapes candidate websites, verifies a
company's identity, or writes SQLite.

The output is deliberately compatible with ``web_manual_lookup verify``:
review the recorded candidates, then pass the JSON to that checker. A result
from a public search page is only a candidate; it must not be imported as a
company website without the registered-number/name/postcode evidence collected
by the existing W1 checker.

One browser context is used serially and each completed company is fsynced to
the JSONL checkpoint, so an interrupted run resumes safely. The run stops at
the first Brave challenge, CAPTCHA, access block or consent page. It never
tries another browser, alters its identity, solves a challenge, or routes
around a block. See docs/WEB_STAGE.md.

Example (the 425 companies awaiting their first lookup on 2026-10-12):
    python -m scripts.website_analysis.web_brave_browser_search run \
        --output logs/web/brave-browser-discovery.jsonl --limit 425

Once BRAVE_SEARCH_API_KEY is set in `.env`, the API backend avoids the public
page's CAPTCHA and requires an explicit monthly request cap:
    python -m scripts.website_analysis.web_brave_browser_search run --backend api \
        --output logs/web/brave-api-discovery.jsonl --limit 425 --brave-allowance 425
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from scripts.website_analysis import search_providers as sp
from scripts.website_analysis.search_providers import registrable_domain
from scripts.website_analysis.web_fetch import USER_AGENT
from scripts.website_analysis.web_identity import blocked
from scripts.website_analysis.web_manual_lookup import prepare_queue
from scripts.website_analysis.web_population import append_checkpoint


RESOLVER_VERSION = "brave-browser-discovery-v1"
MAX_CANDIDATES = 6
DEFAULT_PAUSE_SECONDS = 2.0
CHALLENGE_PHRASES = (
    "captcha", "unusual traffic", "verify you are human", "not a robot",
    "access denied", "security verification", "solve the challenge",
)


def query_for(company: dict[str, Any]) -> str:
    """Return the short public-search phrase used for one company."""
    return str(company.get("search_query") or company["company_name"]).strip() + " official website"


def is_blocked_page(url: str, text: str, status: int | None) -> bool:
    """Recognise a refusal or a page asking the browser to prove it is human."""
    lowered = (text or "").lower()
    return status in (401, 403, 429) or "/sorry/" in (url or "") or any(phrase in lowered for phrase in CHALLENGE_PHRASES)


def candidate_urls(links: list[dict[str, str]]) -> list[str]:
    """Keep unique http(s) destinations and reject directories/search pages."""
    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        href = str(link.get("url") or "")
        try:
            parsed = urlparse(href)
        except ValueError:
            continue
        domain = registrable_domain(href)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not domain:
            continue
        if domain in seen or blocked(domain):
            continue
        seen.add(domain)
        out.append(href)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def completed(path: Path) -> set[str]:
    """Return company numbers with a usable (not blocked) discovery record."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (row.get("resolver_version") == RESOLVER_VERSION and row.get("company_number")
                and not (row.get("search") or {}).get("blocked")):
            out.add(str(row["company_number"]))
    return out


class BraveBrowserSearch:
    """One identifiable, serial Playwright session for Brave public search."""

    def __init__(self, *, launcher: Callable[[], Any] | None = None, pause_seconds: float = DEFAULT_PAUSE_SECONDS) -> None:
        self.launcher = launcher
        self.pause_seconds = pause_seconds
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> "BraveBrowserSearch":
        if self.launcher is not None:
            self._browser = self.launcher()
        else:  # pragma: no cover - requires local Playwright and browser
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=USER_AGENT, locale="en-GB")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def search(self, query: str) -> dict[str, Any]:
        """Open exactly one results page and return its direct candidate links."""
        page = self._context.new_page()
        try:
            response = page.goto("https://search.brave.com/search?" + urlencode({"q": query}),
                                 wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_500)
            text = page.locator("body").inner_text(timeout=5_000)
            result_links = page.locator("a").evaluate_all(
                "(els) => els.map(e => ({title: e.innerText, url: e.href}))"
                ".filter(e => e.url.startsWith('http') && e.title.trim())")
            status = response.status if response is not None else None
            return {"status": status, "final_url": page.url, "title": page.title(),
                    "blocked": is_blocked_page(page.url, text, status),
                    "candidate_urls": candidate_urls(result_links),
                    "result_count": len(result_links)}
        finally:
            page.close()


class BraveApiSearch:
    """Brave's supported API, wrapped to share the browser backend's output shape."""

    def __init__(self, *, client: sp.SearchClient, pause_seconds: float = 0) -> None:
        self.client = client
        self.pause_seconds = pause_seconds

    def __enter__(self) -> "BraveApiSearch":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def search(self, query: str) -> dict[str, Any]:
        result = self.client.brave_web_search(query)
        links = [{"url": row.get("url") or ""} for row in result["organic"]]
        return {"status": 200, "final_url": sp.BRAVE_SEARCH_URL, "title": None, "blocked": False,
                "candidate_urls": candidate_urls(links), "result_count": len(result["organic"])}


def discovery_record(company: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Make a standalone review record, preserving only public search metadata."""
    return {"company_number": company["company_number"], "company_name": company["company_name"],
            "search_query": query_for(company),
            # A provider help/challenge link is not a candidate business site.
            "candidate_urls": [] if result.get("blocked") else result.get("candidate_urls", []),
            "resolver_version": RESOLVER_VERSION,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "search": {key: result.get(key) for key in ("status", "final_url", "title", "blocked", "result_count")}}


def run(companies: list[dict[str, Any]], output: Path, *, pause_seconds: float = DEFAULT_PAUSE_SECONDS,
        searcher_factory: Callable[..., Any] = BraveBrowserSearch) -> dict[str, Any]:
    """Search companies serially, checkpointing each; stop safely on provider block."""
    done = completed(output)
    selected = [company for company in companies if company["company_number"] not in done]
    counts = {"already_completed": len(companies) - len(selected), "searched": 0, "with_candidates": 0,
              "blocked": 0}
    with searcher_factory(pause_seconds=pause_seconds) as searcher:
        for company in selected:
            try:
                result = searcher.search(query_for(company))
            except (sp.ProviderError, sp.AllowanceExhausted, sp.CacheMiss) as exc:
                counts["stopped"] = str(exc)
                break
            record = discovery_record(company, result)
            append_checkpoint(record, output)
            counts["searched"] += 1
            counts["with_candidates"] += bool(record["candidate_urls"])
            print(json.dumps({"company_number": company["company_number"], "candidates": len(record["candidate_urls"]),
                              "blocked": result["blocked"]}), flush=True)
            if result["blocked"]:
                counts["blocked"] += 1
                break
            if pause_seconds:
                time.sleep(pause_seconds)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Discover candidate URLs in Brave public search; leaves SQLite unchanged.")
    run_parser.add_argument("--db", type=Path, default=Path("companies-house.db"))
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--limit", type=int, required=True, help="Maximum pending companies to search.")
    run_parser.add_argument("--backend", choices=("browser", "api"), default="browser",
                            help="browser: public Brave page; api: Brave Search API with cached, capped requests.")
    run_parser.add_argument("--brave-allowance", type=int,
                            help="Hard cap on Brave API requests for this run; required with --backend api.")
    run_parser.add_argument("--cache-only", action="store_true",
                            help="API only: read saved Brave responses and stop at the first cache miss.")
    run_parser.add_argument("--pause-seconds", type=float, default=DEFAULT_PAUSE_SECONDS)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.pause_seconds < 0:
        parser.error("--limit must be positive and --pause-seconds cannot be negative")
    if args.backend == "api" and (args.brave_allowance is None or args.brave_allowance < 1):
        parser.error("--backend api requires --brave-allowance N as the hard request cap for this run")
    if args.backend == "browser" and args.cache_only:
        parser.error("--cache-only is available only with --backend api")
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        companies = prepare_queue(conn)[:args.limit]
    if args.backend == "api":
        client = sp.SearchClient(cache_only=args.cache_only, limits={"brave": args.brave_allowance})
        factory = lambda **kwargs: BraveApiSearch(client=client, **kwargs)
    else:
        factory = BraveBrowserSearch
    counts = run(companies, args.output, pause_seconds=args.pause_seconds, searcher_factory=factory)
    print(json.dumps({**counts, "selected": len(companies), "backend": args.backend,
                      "billed": {} if args.backend == "browser" else client.billed,
                      "database_updated": False, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
