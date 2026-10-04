#!/usr/bin/env python3
"""Run the web stage over the screen-passing companies (docs/WEB_STAGE_PLAN.md).

Steps, each resumable. Steps marked (free) make no paid call; every step that
spends money or a free allowance needs the user's go for that run, a cap, and
a `--cache-only` dry run first.

  order           Freeze the queue order (free; scripts/web/web_rank_order.py).
  identity        W1: find each company's website and Google Maps listing
                  (Serper free credits, or DataForSEO with --provider).
  store           Write the identity checkpoint to company_web_identity,
                  company_google_listing and company_trading_names (free).
  trading-names   Trading names from cached filings (free).
  crawl           W2: fetch up to 25 pages per website into the page cache,
                  with the browser fallback (free; polite requests to the
                  companies' own sites).
  detect          W2: technologies and the site summary from the cached pages
                  (free, makes no request; re-runnable under a new rule version).
  market          W4: Ads Transparency, keyword volume, organic traffic and an
                  optional live check (DataForSEO; --dataforseo-account and
                  --dataforseo-allowance required).
  research        W4 on a hand-supplied list of companies and websites.
  findings        Talking points, setup level and gap segment (free).
  usage           Free allowance used and left per provider.

Profiles (W3) and the lead sheet (W5) have their own commands:
scripts/web/web_profile_eval.py and scripts/web/web_handoff.py.

Usage:
    python -m scripts.web.web_population order
    python -m scripts.web.web_population identity --limit 20 --cache-only
    python -m scripts.web.web_population identity --gold
    python -m scripts.web.web_population store
    python -m scripts.web.web_population crawl --limit 20
    python -m scripts.web.web_population detect
    python -m scripts.web.web_population market --limit 20 --dataforseo-account mine --dataforseo-allowance 0.30
    python -m scripts.web.web_population findings
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_sqlite import init_db, json_text, utc_now  # noqa: E402
from scripts.web import search_providers as sp  # noqa: E402
from scripts.web.web_fetch import Fetcher  # noqa: E402
from scripts.web.web_identity import RESOLVER_VERSION, resolve  # noqa: E402
from scripts.web import web_detect, web_findings, web_market  # noqa: E402
from scripts.web.tech_rules import RULE_VERSION  # noqa: E402
from scripts.web.web_browser import BrowserFetcher  # noqa: E402
from scripts.web.web_crawl import CRAWL_VERSION, MAX_PAGES, crawl_many, crawled_domains, domains_to_crawl  # noqa: E402
from scripts.web.web_rank_order import load_queue  # noqa: E402
from scripts.web.web_trading_names import store_names, trading_names  # noqa: E402

DB_DEFAULT = "companies-house.db"
IDENTITY_CHECKPOINT = Path("logs/web/identity-checkpoint.jsonl")
GOLD_DIR = Path("evals/web_identity/cases")


# ---------------------------------------------------------------- checkpoint

def load_checkpoint(path: Path = IDENTITY_CHECKPOINT, version: str | None = RESOLVER_VERSION) -> dict[str, dict[str, Any]]:
    """Finished records of one resolver version (None: any version), keyed by
    company number; the last record for a company wins."""
    done: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        if version is None or record.get("resolver_version") == version:
            done[record["company_number"]] = record
    return done


def append_checkpoint(record: dict[str, Any], path: Path = IDENTITY_CHECKPOINT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------- inputs

def company_inputs(conn: sqlite3.Connection, numbers: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for number in numbers:
        row = conn.execute("select company_name, profile_payload from companies where company_number = ?",
                           (number,)).fetchone()
        if row is None:
            continue
        try:
            address = (json.loads(row[1] or "{}") or {}).get("registered_office_address") or {}
        except ValueError:
            address = {}
        out[number] = {"company_number": number, "company_name": row[0], "postcode": address.get("postal_code"),
                       "locality": address.get("locality"), "trading_names": trading_names(conn, number)}
    return out


def gold_numbers(cases_dir: Path = GOLD_DIR) -> list[str]:
    return sorted(path.stem for path in cases_dir.glob("*.json")) if cases_dir.exists() else []


def pick(queue: list[dict[str, Any]], done: set[str], *, limit: int | None, exclude: set[str]) -> list[str]:
    numbers = [row["company_number"] for row in queue if row["company_number"] not in done | exclude]
    return numbers[:limit] if limit is not None else numbers


# ---------------------------------------------------------------- identity

def run_identity(conn: sqlite3.Connection, numbers: list[str], client: sp.SearchClient, fetcher: Fetcher,
                 checkpoint: Path = IDENTITY_CHECKPOINT, first_lookup: str = "maps", workers: int = 1) -> dict[str, Any]:
    """Resolve each company not yet in the checkpoint, `workers` at a time.

    Most of a company's time is spent waiting for its candidate websites, so
    several run in parallel. The search client reserves each call's cost before
    sending it, so parallel workers cannot overshoot the allowance; when a
    worker hits the limit, no new companies start and the ones in flight finish.
    Every finished company is appended to the checkpoint at once."""
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    # A company resolved by either first lookup is done: re-running it would spend credits twice.
    done = load_checkpoint(checkpoint, version=None)
    todo = [number for number in numbers if number not in done]
    inputs = company_inputs(conn, todo)
    tiers: Counter[str] = Counter()
    stopped: str | None = None
    finished = 0
    lock = threading.Lock()

    def work(number: str) -> dict[str, Any] | None:
        company = inputs.get(number)
        if company is None:
            return None
        before = client.thread_billed() if hasattr(client, "thread_billed") else dict(client.billed)
        record = resolve(company, client, fetcher, first_lookup=first_lookup)
        after = client.thread_billed() if hasattr(client, "thread_billed") else dict(client.billed)
        record["search_used"] = {name: round(after.get(name, 0) - before.get(name, 0), 4) for name in after}
        record["search_provider"] = getattr(client, "identity_provider", None)
        record["resolved_at"] = utc_now()
        return record

    pending = iter(todo)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        running = {}
        while True:
            while stopped is None and len(running) < max(1, workers):
                number = next(pending, None)
                if number is None:
                    break
                running[pool.submit(work, number)] = number
            if not running:
                break
            finished_now, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished_now:
                number = running.pop(future)
                try:
                    record = future.result()
                except (sp.AllowanceExhausted, sp.CacheMiss, sp.ProviderError) as exc:
                    stopped = stopped or f"{type(exc).__name__}: {exc}"
                    continue
                if record is None:
                    continue
                with lock:
                    append_checkpoint(record, checkpoint)
                    tiers[record["tier"]] += 1
                    finished += 1
                    print(f"  {finished}/{len(todo)} {number} {record['tier']:<9} {record['domain'] or '-'}",
                          file=sys.stderr)
    return {"asked": len(numbers), "replayed": len(numbers) - len(todo), "finished": finished,
            "tiers": dict(tiers), "billed": client.billed, "stopped": stopped}


def store_identity(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, int]:
    """Replace each company's rows for the record's resolver version."""
    counts: Counter[str] = Counter()
    # A website found by hand (the test companies) outranks the resolver: its row is kept and the
    # resolver's answer for that company is left in the checkpoint only, as an accuracy check.
    hand_found = {number for (number,) in conn.execute(
        "select distinct company_number from company_web_identity where sources like '%hand_found%'")}
    for record in records:
        number, version = record["company_number"], record["resolver_version"]
        if number in hand_found:
            counts["kept_hand_found"] += 1
            continue
        conn.execute("delete from company_web_identity where company_number = ? and resolver_version = ?",
                     (number, version))
        conn.execute("delete from company_google_listing where company_number = ? and resolver_version = ?",
                     (number, version))
        at = record.get("resolved_at") or utc_now()
        rows = [row for row in record.get("candidates") or [] if row["tier"] != "none"]
        if not rows:
            conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, "
                         "sources, final_url, evidence, resolved_at) values (?, ?, null, 'none', 'none', ?, null, ?, ?)",
                         (number, version, json_text([]), json_text({"checked": len(record.get("candidates") or [])}),
                          at))
        for row in rows:
            main = row["domain"] == record.get("domain") and row["tier"] in ("verified", "probable")
            conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, "
                         "sources, final_url, evidence, resolved_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (number, version, row["domain"], "main" if main else "candidate", row["tier"],
                          json_text(row["sources"]), row["evidence"].get("final_url"), json_text(row["evidence"]), at))
        counts[record["tier"]] += 1
        listing = record.get("listing")
        if listing:
            conn.execute(
                "insert into company_google_listing (company_number, resolver_version, title, category, categories, "
                "rating, rating_count, address, postcode, phone, website, domain, latitude, longitude, cid, place_id, "
                "match, source, found_at, is_claimed, category_ids) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (number, version, listing.get("title"), listing.get("category"), json_text(listing.get("categories")),
                 listing.get("rating"), listing.get("rating_count"), listing.get("address"), listing.get("postcode"),
                 listing.get("phone"), listing.get("website"), listing.get("domain"), listing.get("latitude"),
                 listing.get("longitude"), listing.get("cid"), listing.get("place_id"), listing["match"],
                 listing.get("source") or "unknown", at,
                 None if listing.get("is_claimed") is None else int(bool(listing["is_claimed"])),
                 json_text(listing.get("category_ids")) if listing.get("category_ids") else None))
            counts["listing"] += 1
        for source in ("website", "maps_listing"):
            fresh = [item for item in record.get("new_trading_names") or [] if item.get("source") == source]
            added = store_names(conn, number, source, fresh)
            if added:
                counts["trading_names"] += added
    conn.commit()
    return dict(counts)


# ---------------------------------------------------------------- inputs for the later steps

def main_domains(conn: sqlite3.Connection, resolver_version: str | None = None) -> dict[str, str]:
    """company number -> chosen website (verified or probable). `resolver_version` None: each
    company's newest identity, whichever resolver version wrote it."""
    if resolver_version:
        rows = conn.execute("select company_number, domain from company_web_identity where resolver_version = ? "
                            "and role = 'main' and domain is not null order by id", (resolver_version,)).fetchall()
    else:
        rows = conn.execute("select company_number, domain from company_web_identity_current "
                            "where role = 'main' and domain is not null order by id").fetchall()
    return {number: domain for number, domain in rows}


def profile_seeds(conn: sqlite3.Connection, numbers: list[str]) -> dict[str, list[str]]:
    """Each company's seed phrases from its latest W3 profile."""
    out: dict[str, list[str]] = {}
    for number in numbers:
        row = conn.execute("select seed_keywords from company_web_profile where company_number = ? "
                           "and seed_keywords is not null order by profiled_at desc limit 1", (number,)).fetchone()
        phrases = json.loads(row[0]) if row and row[0] else []
        if phrases:
            out[number] = phrases
    return out


def market_companies(conn: sqlite3.Connection, numbers: list[str], resolver_version: str | None = None
                     ) -> list[dict[str, Any]]:
    """The inputs of `assess_market`: name, website, registered postcode and the Maps listing W1 matched."""
    domains = main_domains(conn, resolver_version)
    inputs = company_inputs(conn, numbers)
    out = []
    for number in numbers:
        company = inputs.get(number)
        if company is None:
            continue
        listing = conn.execute(
            "select title, category, rating, rating_count, latitude, longitude, domain, is_claimed "
            "from company_google_listing where company_number = ? order by id desc limit 1", (number,)).fetchone()
        out.append({"company_number": number, "company_name": company["company_name"], "domain": domains.get(number),
                    "postcode": company.get("postcode"),
                    "listing": None if listing is None else dict(zip(
                        ("title", "category", "rating", "rating_count", "latitude", "longitude", "domain",
                         "is_claimed"), listing))})
    return out


def pick_market_numbers(conn: sqlite3.Connection, queue: list[dict[str, Any]], market_version: str, *,
                        limit: int | None, resolver_version: str | None = None) -> list[str]:
    """The next companies in queue order that have a chosen website and seed phrases but no market row yet."""
    have_site = set(main_domains(conn, resolver_version))
    done = {n for (n,) in conn.execute("select company_number from company_market where market_version = ? "
                                       "and ads_seen is not null", (market_version,))}
    seeded = {n for (n,) in conn.execute("select distinct company_number from company_web_profile "
                                         "where seed_keywords is not null and seed_keywords != '[]'")}
    numbers = [row["company_number"] for row in queue
               if row["company_number"] in have_site and row["company_number"] in seeded
               and row["company_number"] not in done]
    return numbers[:limit] if limit is not None else numbers


# ---------------------------------------------------------------- CLI

def _add_account_flags(parser: argparse.ArgumentParser, *, required_allowance: bool = False) -> None:
    parser.add_argument("--dataforseo-account", choices=sp.DATAFORSEO_ACCOUNTS, default="mine",
                        help="Whose DataForSEO account pays: yours (default) or the friend's (needs --dataforseo-allowance "
                             "and the friend's agreement to it).")
    parser.add_argument("--dataforseo-allowance", type=float, required=required_allowance,
                        help="Total DataForSEO dollars this account's ledger may reach: a hard cap. Defaults to $1 for "
                             "your account (the free credit) and has no default for the friend's.")


def _client(args: argparse.Namespace, *, identity_provider: str = "serper") -> sp.SearchClient:
    account = getattr(args, "dataforseo_account", "mine")
    allowance = getattr(args, "dataforseo_allowance", None)
    provider = sp.DATAFORSEO_PROVIDER[account]
    if account == "friend" and not allowance:
        raise SystemExit("the friend's DataForSEO account has no default cap: pass --dataforseo-allowance (dollars, "
                         "agreed with the friend for this run)")
    limits: dict[str, float] = {}
    if allowance:
        limits[provider] = allowance
    if getattr(args, "serper_allowance", None):
        limits["serper"] = args.serper_allowance
    return sp.SearchClient(cache_only=getattr(args, "cache_only", False), limits=limits,
                           identity_provider=identity_provider, dataforseo_account=account)


def _numbers(args: argparse.Namespace) -> list[str]:
    return [n.strip().zfill(8) if n.strip().isdigit() else n.strip().upper() for n in args.numbers]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_DEFAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    order = sub.add_parser("order", help="Freeze (or show) the queue order.")
    order.add_argument("--refresh", action="store_true")
    order.add_argument("--show", type=int, default=10)

    ident = sub.add_parser("identity", help="W1: website and Maps listing per company (spends search credits).")
    ident.add_argument("--limit", type=int, help="Next N companies in the queue order, gold cases excluded.")
    ident.add_argument("--gold", action="store_true", help="Run the identity gold cases instead of the queue.")
    ident.add_argument("--numbers", nargs="+", help="Specific company numbers.")
    ident.add_argument("--cache-only", action="store_true", help="Free dry run: stop at the first uncached search.")
    ident.add_argument("--serper-allowance", type=float, help="Override Serper's credit limit (after a top-up).")
    ident.add_argument("--first-lookup", choices=("maps", "places"), default="maps",
                       help="maps: Serper Maps near the postcode (3 credits, full listing); places: places search "
                            "near the district (1 credit, thinner listing).")
    ident.add_argument("--workers", type=int, default=1, help="Companies resolved in parallel.")
    ident.add_argument("--checkpoint", type=Path, default=IDENTITY_CHECKPOINT,
                       help="Checkpoint file (a trial can use its own).")
    ident.add_argument("--provider", choices=sp.IDENTITY_PROVIDERS, default="serper",
                       help="Who answers the Maps and organic searches (default serper, the free credits).")
    _add_account_flags(ident)
    sub.add_parser("store", help="Write the identity checkpoint to SQLite.")

    names = sub.add_parser("trading-names", help="Trading names from cached filings (free).")
    names.add_argument("--limit", type=int)

    crawl = sub.add_parser("crawl", help="W2: fetch up to 25 pages per website into the page cache (free).")
    crawl.add_argument("--limit", type=int, help="At most this many websites.")
    crawl.add_argument("--domains", nargs="+", help="Specific websites instead of the identity results.")
    crawl.add_argument("--sites-file", type=Path, help="JSON list of {domain, company_numbers, start_url}, e.g. "
                       "from web_settle plan; crawled in addition to the identity results.")
    crawl.add_argument("--workers", type=int, default=8)
    crawl.add_argument("--max-pages", type=int, default=MAX_PAGES)
    crawl.add_argument("--no-browser", action="store_true", help="Plain downloads only.")
    crawl.add_argument("--cache-only", action="store_true", help="Dry run: fetch nothing, report what would be crawled.")
    crawl.add_argument("--crawl-version", default=CRAWL_VERSION)
    crawl.add_argument("--resolver-version", default=None,
                       help="Default: each company's newest identity, whichever version wrote it.")

    detect = sub.add_parser("detect", help="W2: technologies and site summary from the cached pages (free).")
    detect.add_argument("--domains", nargs="+")
    detect.add_argument("--chosen", action="store_true",
                        help="Only the websites chosen as a company's own (not every crawled candidate).")
    detect.add_argument("--crawl-version", default=CRAWL_VERSION)
    detect.add_argument("--rule-version", default=RULE_VERSION)

    market = sub.add_parser("market", help="W4: ads, keyword volume, organic traffic (spends DataForSEO).")
    market.add_argument("--limit", type=int, help="Next N companies in queue order with a website and seed phrases.")
    market.add_argument("--numbers", nargs="+")
    market.add_argument("--seeds", type=Path, help='JSON {"<company number>": ["phrase", ...]} instead of W3 profiles.')
    market.add_argument("--serp-checks", type=int, default=0, help="Live searches per company (short list only).")
    market.add_argument("--cache-only", action="store_true")
    market.add_argument("--market-version", default=web_market.MARKET_VERSION)
    market.add_argument("--resolver-version", default=None,
                       help="Default: each company's newest identity, whichever version wrote it.")
    _add_account_flags(market, required_allowance=True)

    research = sub.add_parser("research", help="W4 on a hand-supplied list of companies and websites.")
    research.add_argument("--input", type=Path, required=True,
                          help='JSON list: [{"company_number", "brand", "domain"}, ...]')
    research.add_argument("--seeds", type=Path, required=True)
    research.add_argument("--out", type=Path, default=Path("logs/web/research-report.json"))
    research.add_argument("--csv", type=Path, default=Path("logs/web/research-summary.csv"))
    research.add_argument("--serp-checks", type=int, default=1)
    research.add_argument("--cache-only", action="store_true")
    _add_account_flags(research, required_allowance=True)

    findings = sub.add_parser("findings", help="Talking points, setup level and gap segment (free).")
    findings.add_argument("--numbers", nargs="+")
    findings.add_argument("--market-version", default=web_market.MARKET_VERSION)
    findings.add_argument("--resolver-version", default=None,
                       help="Default: each company's newest identity, whichever version wrote it.")
    findings.add_argument("--crawl-version", default=CRAWL_VERSION)
    findings.add_argument("--rule-version", default=RULE_VERSION)
    sub.add_parser("usage", help="Free allowance used and left per provider.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "usage":
        return sp.main(["usage"])

    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        init_db(conn)
        if args.command == "order":
            queue = load_queue(conn, refresh=args.refresh)
            bands = Counter(row["band"] for row in queue)
            print(f"{len(queue)} companies; bands {dict(bands)}")
            for row in queue[:args.show]:
                print(f"  {row['position']:>4} {row['company_number']} {row['band']:<5} "
                      f"turnover {row.get('turnover') or 0:>14,.0f} profit {row.get('profit_after_tax') or 0:>12,.0f}")
            return 0

        if args.command == "identity":
            if args.numbers:
                numbers = _numbers(args)
            elif args.gold:
                numbers = gold_numbers()
                if not numbers:
                    raise SystemExit(f"no gold cases in {GOLD_DIR}; draw them with scripts.web.web_review draw")
            else:
                if args.limit is None:
                    raise SystemExit("give --limit N (each run spends free credits and needs a go), --gold or --numbers")
                queue = load_queue(conn)
                numbers = pick(queue, set(load_checkpoint(args.checkpoint, version=None)), limit=args.limit,
                               exclude=set(gold_numbers()))
            client = _client(args, identity_provider=args.provider)
            fetcher = Fetcher(cache_only=args.cache_only)
            spend_provider = args.provider if args.provider == "serper" else client.dataforseo_provider
            print(f"{spend_provider} allowance left before the run: {client.remaining(spend_provider):g}",
                  file=sys.stderr)
            summary = run_identity(conn, numbers, client, fetcher, checkpoint=args.checkpoint,
                                   first_lookup=args.first_lookup, workers=args.workers)
            summary["left"] = {spend_provider: round(client.remaining(spend_provider), 4)}
            print(json.dumps(summary, indent=2))
            return 1 if summary["stopped"] and not args.cache_only else 0

        if args.command == "store":
            records = list(load_checkpoint(version=None).values())   # either first lookup
            print(json.dumps(store_identity(conn, records), indent=2))
            return 0

        if args.command == "trading-names":
            from scripts.web.web_trading_names import extract_from_cached_filings
            print(extract_from_cached_filings(conn, limit=args.limit))
            return 0

        if args.command == "crawl":
            if args.domains:
                sites = [{"domain": d, "company_numbers": [], "start_url": None} for d in args.domains]
            else:
                sites = domains_to_crawl(conn, args.resolver_version)
            if args.sites_file:
                known = {site["domain"] for site in sites}
                sites += [site for site in json.loads(args.sites_file.read_text(encoding="utf-8"))
                          if site["domain"] not in known]
            if args.limit is not None:
                done = crawled_domains(conn, args.crawl_version)
                fresh = [s for s in sites if s["domain"] not in done][:args.limit]
                sites = fresh
            if args.cache_only:
                done = crawled_domains(conn, args.crawl_version)
                print(json.dumps({"would_crawl": len([s for s in sites if s["domain"] not in done]),
                                  "already_crawled": len([s for s in sites if s["domain"] in done])}, indent=2))
                return 0
            fetcher = Fetcher()
            browser = None if args.no_browser else BrowserFetcher(fetcher)
            if browser is not None and not browser.available():
                print("Playwright is not installed: blocked and thin pages will be recorded as such "
                      "(pip install playwright; playwright install chromium)", file=sys.stderr)
            try:
                counts = crawl_many(conn, sites, fetcher, browser, workers=args.workers, max_pages=args.max_pages,
                                    crawl_version=args.crawl_version, log=lambda line: print(line, file=sys.stderr))
            finally:
                if browser is not None:
                    browser.close()
            counts["requests_made"] = fetcher.requests_made
            print(json.dumps(counts, indent=2))
            return 0

        if args.command == "detect":
            if args.chosen:
                domains = sorted(set(main_domains(conn).values()) & crawled_domains(conn, args.crawl_version))
            else:
                domains = args.domains or sorted(crawled_domains(conn, args.crawl_version))
            fetcher = Fetcher(cache_only=True)
            counts = web_detect.run_detect(conn, domains, fetcher, crawl_version=args.crawl_version,
                                           rule_version=args.rule_version)
            print(json.dumps(counts, indent=2))
            return 0

        if args.command == "market":
            client = _client(args, identity_provider="dataforseo")
            provider = client.dataforseo_provider
            if args.numbers:
                numbers = _numbers(args)
            else:
                if args.limit is None:
                    raise SystemExit("give --limit N or --numbers (each run spends DataForSEO money and needs a go)")
                numbers = pick_market_numbers(conn, load_queue(conn), args.market_version, limit=args.limit,
                                              resolver_version=args.resolver_version)
            seeds = web_market.seed_map(json.loads(args.seeds.read_text(encoding="utf-8"))) if args.seeds \
                else profile_seeds(conn, numbers)
            companies = market_companies(conn, numbers, args.resolver_version)
            print(f"{provider} allowance left before the run: {client.remaining(provider):g}; "
                  f"{len(companies)} companies", file=sys.stderr)
            try:
                records = web_market.assess_market(companies, seeds, client, serp_checks=args.serp_checks,
                                                   log=lambda line: print(line, file=sys.stderr))
            except (sp.AllowanceExhausted, sp.CacheMiss, sp.ProviderError) as exc:
                print(f"stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
                print(json.dumps({"billed": client.billed, "left": round(client.remaining(provider), 4)}))
                return 1
            stored = web_market.store_market(conn, records, args.market_version)
            print(json.dumps({**stored, "billed": client.billed, "left": round(client.remaining(provider), 4)}, indent=2))
            return 0

        if args.command == "research":
            wanted = json.loads(args.input.read_text(encoding="utf-8"))
            inputs = company_inputs(conn, [row["company_number"] for row in wanted])
            companies = [{**inputs[row["company_number"]], **row} for row in wanted if row["company_number"] in inputs]
            client = _client(args, identity_provider="dataforseo")
            provider = client.dataforseo_provider
            seeds = web_market.seed_map(json.loads(args.seeds.read_text(encoding="utf-8")))
            print(f"{provider} allowance left before the run: {client.remaining(provider):g}", file=sys.stderr)
            try:
                records = web_market.assess_market(companies, seeds, client, serp_checks=args.serp_checks,
                                                   lookup_listings=True, checkpoint=None,
                                                   log=lambda line: print(line, file=sys.stderr))
            except (sp.AllowanceExhausted, sp.CacheMiss, sp.ProviderError) as exc:
                print(f"stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
                print(json.dumps({"billed": client.billed, "left": round(client.remaining(provider), 4)}))
                return 1
            web_market.write_report(records, args.out)
            args.csv.parent.mkdir(parents=True, exist_ok=True)
            with args.csv.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(web_market.summary_rows(records))
            print(json.dumps({"companies": len(records), "billed": client.billed, "report": str(args.out),
                              "summary": str(args.csv), "left": round(client.remaining(provider), 4)}, indent=2))
            return 0

        if args.command == "findings":
            numbers = _numbers(args) if args.numbers else [n for (n,) in conn.execute(
                "select company_number from company_market where market_version = ? order by company_number",
                (args.market_version,))]
            counts = web_findings.assess_companies(
                conn, numbers, resolver_version=args.resolver_version, market_version=args.market_version,
                crawl_version=args.crawl_version, rule_version=args.rule_version)
            print(json.dumps(counts, indent=2))
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
