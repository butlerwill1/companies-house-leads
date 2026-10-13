"""Prepare website search tasks and check URLs found by a person or assistant.

This file DOES NOT search the web from a company name. It has two commands:

``prepare`` starts from the database's screen-passing companies, excludes
duplicates and companies with an existing identity lookup, and writes an
editable JSON queue containing company names, numbers, places and search
phrases. ``candidate_urls`` starts empty. A person or an assistant must search
separately and fill that list; generating search phrases is not a website search.

``verify`` starts only once a candidate URL has been supplied. It scrapes that
site's homepage and legal/contact pages using the existing W1 checker, looking
for the registered number or matching name and postcode. Optional browser
rendering reads thin HTML, but never retries a refusal or challenge. It writes
one review/checkpoint record per company, not database identity results.

``verify`` accepts either the editable JSON array written by ``prepare`` or a
JSONL candidate-discovery checkpoint such as ``brave-api-discovery.jsonl``.
The latter contains only names and URLs, so the command reads the registered
postcode and any known trading names from the local database before checking
the sites. This is verification, not a second web search.

There are no Serper, DataForSEO or model calls, and neither command writes to
SQLite. The queue's 425 entries on 2026-10-12 were companies whose first website
discovery lookup had not been attempted; it did not mean 425 known websites
needed scraping. An unsuccessful supplied-URL check does not prove no site
exists. See docs/WEB_STAGE.md for commands, input format and review workflow.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from scripts.website_analysis.search_providers import registrable_domain
from scripts.website_analysis.web_fetch import Fetcher, THIN_WORDS, parse_html
from scripts.website_analysis.web_browser import BrowserFetcher
from scripts.website_analysis.web_identity import MAX_SITE_CHECKS, TIER_RANK, blocked, check_site, clean_name, tier_for
from scripts.website_analysis.web_population import append_checkpoint, company_inputs
from scripts.website_analysis.web_rank_order import build_queue

RESOLVER_VERSION = "identity-manual-candidates-v3"


class CandidateFetcher:
    """Optionally render thin HTML; refusals are never retried in a browser."""

    def __init__(self, fetcher: Fetcher, browser: BrowserFetcher | None = None):
        self.fetcher, self.browser = fetcher, browser
        self.problems = []

    def get(self, url: str, accept=("html",)):
        page = self.fetcher.get(url, accept=accept)
        if (self.browser is not None and not self.fetcher.cache_only and page.ok
                and page.fetched_with != "browser" and not page.blocked
                and "html" in (page.content_type or "html")
                and parse_html(page.html, page.final_url or url).word_count < THIN_WORDS):
            rendered = self.browser.fetch(page.final_url or url)
            if rendered.ok or rendered.blocked:
                return rendered
            self.problems.append(rendered.error or "browser did not return readable HTML")
        return page


def prepare_queue(conn: sqlite3.Connection) -> list[dict]:
    """Write-ready company search tasks; performs no search or other network call."""
    attempted = {r[0] for r in conn.execute("select distinct company_number from company_web_identity_current")}
    numbers = [r["company_number"] for r in build_queue(conn) if r["company_number"] not in attempted]
    inputs = company_inputs(conn, numbers)
    return [{**inputs[n], "search_query": " ".join(filter(None, (
                clean_name(inputs[n]["company_name"]), inputs[n].get("locality")))),
             "number_search_query": f'"{n}"', "candidate_urls": []}
            for n in numbers if n in inputs]


def verify_candidates(company: dict, fetcher: Fetcher) -> dict:
    """Read supplied URLs and return identity evidence; cannot discover new sites."""
    record = {"company_number": company["company_number"], "company_name": company["company_name"],
              "resolver_version": RESOLVER_VERSION, "resolved_at": datetime.now(timezone.utc).isoformat(),
              "searches": [], "listing": None, "candidates": [], "domain": None, "tier": "none",
              "problems": [], "new_trading_names": []}
    seen = set()
    for url in company.get("candidate_urls") or []:
        try:
            parsed = urlparse(url)
            domain = registrable_domain(url)
            valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
        except (TypeError, ValueError):
            valid, domain = False, None
        if not valid:
            record["problems"].append("invalid candidate URL")
            continue
        if blocked(domain) or domain in seen:
            continue
        if len(seen) >= MAX_SITE_CHECKS:
            record["problems"].append("candidate site limit reached")
            break
        seen.add(domain)
        evidence = check_site(domain, company, fetcher, start_url=url)
        final_domain = evidence.get("final_domain") or domain
        if blocked(final_domain):
            record["problems"].append("candidate redirected to an excluded domain")
            continue
        record["candidates"].append({"domain": final_domain, "tier": tier_for(evidence),
                                     "sources": ["manual_search"], "evidence": evidence})
    record["candidates"].sort(key=lambda r: TIER_RANK[r["tier"]])
    if record["candidates"] and record["candidates"][0]["tier"] != "none":
        best = record["candidates"][0]
        record.update(domain=best["domain"], tier=best["tier"])
    record["problems"].extend(getattr(fetcher, "problems", []))
    return record


def load_candidate_companies(input_path: Path, conn: sqlite3.Connection) -> list[dict]:
    """Load prepared JSON or discovery JSONL and add authoritative local inputs.

    Discovery records deliberately omit registered-address data. Hydration here
    lets the W1 checker apply its normal postcode and trading-name evidence.
    """
    text = input_path.read_text(encoding="utf-8")
    if input_path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError("candidate input must be a JSON array or JSONL records")
    inputs = company_inputs(conn, [str(row.get("company_number") or "") for row in rows])
    loaded = []
    for row in rows:
        number = str(row.get("company_number") or "")
        if not number or not row.get("candidate_urls") or number not in inputs:
            continue
        loaded.append({**row, **inputs[number], "candidate_urls": row["candidate_urls"]})
    return loaded


def completed_verifications(output_path: Path) -> set[str]:
    """Return checkpointed verification records for this checker version."""
    if not output_path.exists():
        return set()
    done = set()
    for line in output_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("resolver_version") == RESOLVER_VERSION and row.get("company_number"):
            done.add(str(row["company_number"]))
    return done


def verify_many(companies: list[dict], output_path: Path, *, workers: int, cache_only: bool) -> dict:
    """Verify independent sites concurrently and fsync each completed record.

    Each worker owns its Fetcher, retaining the one-second per-host pause and
    robots handling. Results are checkpointed by the coordinating thread, so
    interrupted batches resume without rechecking completed companies.
    """
    def check(company: dict) -> tuple[dict, int]:
        fetcher = Fetcher(cache_only=cache_only)
        record = verify_candidates(company, CandidateFetcher(fetcher))
        return record, fetcher.requests_made

    completed = 0
    requests_made = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check, company): company["company_number"] for company in companies}
        for future in as_completed(futures):
            record, requests = future.result()
            append_checkpoint(record, output_path)
            completed += 1
            requests_made += requests
            print(json.dumps({"company_number": record["company_number"], "tier": record["tier"],
                              "domain": record["domain"]}), flush=True)
    return {"checked": completed, "page_requests": requests_made, "database_updated": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Write an editable JSON queue; no network calls.")
    prepare.add_argument("--db", type=Path, default=Path("companies-house.db"))
    prepare.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify", help="Check supplied URLs and append review records; no database writes.")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--db", type=Path, default=Path("companies-house.db"),
                        help="Read company particulars when the input is discovery JSONL.")
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--limit", type=int, required=True, help="Maximum companies with supplied candidates to check.")
    verify.add_argument("--workers", type=int, default=8,
                        help="Concurrent site checks (default 8; each worker remains robots-aware).")
    verify.add_argument("--cache-only", action="store_true")
    verify.add_argument("--browser", action="store_true", help="Render thin HTML with the existing Playwright fallback.")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = prepare_queue(conn)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"pending_companies": len(rows), "output": str(args.output)}))
        return 0
    if args.limit < 1 or args.workers < 1:
        parser.error("--limit and --workers must be positive")
    if args.browser and args.workers != 1:
        parser.error("--browser requires --workers 1 because one Playwright session is shared")
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_candidate_companies(args.input, conn)
    done = completed_verifications(args.output)
    rows = [row for row in rows if row["company_number"] not in done][:args.limit]
    if args.browser:
        fetcher = Fetcher(cache_only=args.cache_only)
        browser = BrowserFetcher(fetcher)
        try:
            for company in rows:
                record = verify_candidates(company, CandidateFetcher(fetcher, browser))
                append_checkpoint(record, args.output)
                print(json.dumps({"company_number": record["company_number"], "tier": record["tier"],
                                  "domain": record["domain"]}), flush=True)
        finally:
            browser.close()
        summary = {"checked": len(rows), "page_requests": fetcher.requests_made, "database_updated": False}
    else:
        summary = verify_many(rows, args.output, workers=args.workers, cache_only=args.cache_only)
    print(json.dumps({**summary, "already_completed": len(done)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
