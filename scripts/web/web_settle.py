#!/usr/bin/env python3
"""Settle W1's ambiguous companies with whole-site evidence (docs/WEB_STAGE_PLAN.md).

W1 checks a candidate website cheaply: its home page and up to three legal
pages. A company ends `ambiguous` when a candidate shows the company's name (or
its domain spells it) but nothing that ties the site to this company in those
few pages. The W2 crawl reads up to 25 pages of a site, including privacy
policies, terms and contact pages, which is where UK companies must show their
registered number, so the same candidates are checked again against the whole
crawled site, with no new searches and no paid calls.

Steps:

  plan   List the sites to crawl: each ambiguous company's two best candidates
         (`--out` writes them for `web_population crawl --sites-file`).
  run    Re-judge each ambiguous company from its crawled candidates. A company
         that reaches `verified` or `probable` gets a settled record (resolver
         version `identity-v4-settled`), appended to the identity checkpoint;
         `web_population store` then writes it. Companies that stay ambiguous
         are left as they are.

Rules, strongest first. A candidate's tier is the first that applies:

  verified   the company number appears on any crawled page
  probable   the full registered name (suffix included) appears on a page AND
             one of: the registered postcode appears, the Maps listing's phone
             number is shown, the Maps listing matched on name and postcode
             and links here
  probable   the full registered name appears as a disclosure (a privacy,
             terms, contact or about page, or next to "(c)", "registered",
             "data controller", "trading as" or a postcode) on a site that
             looks UK (a .uk-type domain or a UK phone number). A name in a
             news story or a client case study is not a disclosure.
  probable   the cleaned name appears AND the registered postcode appears
  probable   the cleaned name appears AND the Maps listing's phone is shown,
             but only when the listing matched the registered postcode: a
             listing that matches by name alone may be a namesake, and its
             phone then only ties the listing to its own site
  otherwise  ambiguous

When several candidates reach a tier the better rule wins, then a Maps listing
link, then the number of sources that named it.

Usage:
    python -m scripts.web.web_settle plan --out logs/web/settle-sites.json
    python -m scripts.web.web_settle run
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_sqlite import init_db, utc_now  # noqa: E402
from scripts.web.search_providers import normalise_postcode  # noqa: E402
from scripts.web.web_crawl import CRAWL_VERSION  # noqa: E402
from scripts.web.web_detect import load_pages, phone_numbers  # noqa: E402
from scripts.web.web_fetch import Fetcher, parse_html  # noqa: E402
from scripts.web.web_identity import (  # noqa: E402
    TIER_RANK, _legal_norm, blocked, company_names, distinctive_tokens, legal_name_found, name_found, number_found, postcode_found)

SETTLED_VERSION = "identity-v4-settled"
MAX_CANDIDATES = 2
UK_TLDS = (".uk", ".london", ".scot", ".wales", ".cymru")
NOT_PAGES = ("sitemap", "gtm_container")
RULE_RANK = {"number": 0, "legal_name+corroboration": 1, "legal_name+disclosure": 2, "name+postcode": 3,
             "name+phone": 4}
DISCLOSURE_KINDS = ("privacy", "terms", "contact", "about")
# What sits just before a company's own name in a legal footer or policy, or just after it.
_BEFORE = re.compile(r"(copyright|all rights reserved|registered|data controller|controller|operated by|owned by|"
                     r"trading as|t a|trading styles of|provided by|issued by|who we are|we are|company name)\s*"
                     r"(?:[a-z0-9]+\s+){0,3}$")
_AFTER = re.compile(r"^(?:\s*[a-z0-9]+){0,12}?\s*(?:registered office|registered in|company number|company no|"
                    r"(?:is|are) (?:the )?(?:data )?controllers?|data controllers?|collects? and|processes|"
                    r"(?:is|are) committed to|trading as|t a |[a-z]{1,2}\d{1,2}[a-z]?\s*\d[a-z]{2})")
CHECKPOINT = Path("logs/web/identity-checkpoint.jsonl")
SITES_FILE = Path("logs/web/settle-sites.json")


# ---------------------------------------------------------------- which sites to crawl

def candidate_score(candidate: dict[str, Any]) -> int:
    """How plausible an ambiguous candidate is, before reading it again."""
    evidence = candidate.get("evidence") or {}
    return (3 * ("maps" in candidate["sources"]) + 2 * bool(evidence.get("name_found"))
            + 2 * bool(evidence.get("domain_spells_name")) + len(candidate["sources"]) - 1)


def top_candidates(record: dict[str, Any], limit: int = MAX_CANDIDATES,
                   tiers: tuple[str, ...] = ("ambiguous", "blocked")) -> list[dict[str, Any]]:
    # the blocklist has grown since W1 ran, so a candidate it now names is skipped
    ranked = sorted((c for c in record.get("candidates") or [] if c["tier"] in tiers and not blocked(c["domain"])),
                    key=candidate_score, reverse=True)
    return ranked[:limit]


def sites_to_crawl(records: list[dict[str, Any]], limit: int = MAX_CANDIDATES,
                   tier: str = "ambiguous") -> list[dict[str, Any]]:
    """Each unsettled company's best candidates (`tier`: ambiguous, or blocked for
    sites that refused a plain download), one entry per domain with the companies
    that nominated it, so the crawl can look for their numbers."""
    sites: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("tier") != tier:
            continue
        for candidate in top_candidates(record, limit, (tier,)):
            site = sites.setdefault(candidate["domain"], {
                "domain": candidate["domain"], "company_numbers": [],
                "start_url": (candidate.get("evidence") or {}).get("final_url")})
            if record["company_number"] not in site["company_numbers"]:
                site["company_numbers"].append(record["company_number"])
    return list(sites.values())


# ---------------------------------------------------------------- whole-site evidence

def disclosure_context(text: str, company_name: str) -> bool:
    """The full registered name appears the way a company discloses itself: after
    copyright, "registered", "data controller" and the like, or before its
    registered office or a postcode. A news story or a case study naming the
    company does neither."""
    phrase = _legal_norm(company_name)
    norm = _legal_norm((text or "").replace("©", " copyright ").replace("(c)", " copyright "))
    start = 0
    while True:
        at = norm.find(phrase, start)
        if at < 0:
            return False
        before, after = norm[max(0, at - 70):at], norm[at + len(phrase):at + len(phrase) + 120]
        if _BEFORE.search(before) or _AFTER.match(after):
            return True
        start = at + len(phrase)


# A registered number printed after "company", "registration" or "registered": the site's own legal footer.
_OTHER_NUMBER = re.compile(r"(?:company|registration|registered)[^0-9]{0,60}?((?:SC|NI|OC|SO|NC|R0)?\d{6,8})\b", re.I)
_SNIPPET_KEY = re.compile(r"(registered (?:in|office)|company (?:registration )?(?:no|number)|trading (?:style|as|name)|"
                          r"t/a\b|all rights reserved|©|copyright)", re.I)


def other_numbers(texts: list[tuple[str, str]], own: str) -> list[str]:
    """Registered numbers the site prints that are not this company's: a sister
    company's, a parent's, or the real owner's."""
    own = own.strip().upper()
    found: dict[str, None] = {}
    for _, text in texts:
        for match in _OTHER_NUMBER.finditer(text):
            number = match.group(1).upper()
            if number != own and number.lstrip("0") != own.lstrip("0"):
                found[number] = None
    return list(found)[:5]


def disclosure_snippets(texts: list[tuple[str, str]], limit: int = 4) -> list[str]:
    """Short passages from the legal and footer text: registered office, trading style, copyright."""
    out: list[str] = []
    for kind in (*DISCLOSURE_KINDS, "home"):
        for page_kind, text in texts:
            if page_kind != kind:
                continue
            flat = " ".join(text.split())
            for match in _SNIPPET_KEY.finditer(flat):
                snippet = flat[max(0, match.start() - 90):match.end() + 150].strip()
                if all(snippet[:60] not in other for other in out):
                    out.append(snippet)
                if len(out) >= limit:
                    return out
    return out


def brand_found(texts: list[tuple[str, str]], company_name: str) -> bool:
    """The company's distinctive name words ("Heaton" for Heaton Group Developments Limited) appear as
    a phrase on at least two pages: the brand, where the full registered name is not shown."""
    phrase = " ".join(distinctive_tokens(company_name))
    if len(phrase.replace(" ", "")) < 5:
        return False
    return sum(1 for _, text in texts if f" {phrase} " in f" {_legal_norm(text)} ") >= 2


def _site_texts(pages: list[dict[str, Any]], fetcher: Fetcher) -> list[tuple[str, str]]:
    """(page kind, text) of each crawled page that has any."""
    texts = []
    for row in pages:
        if row["page_kind"] in NOT_PAGES or row.get("status_code") != 200 or row.get("fetch_error"):
            continue
        page = fetcher.get(row["url"], accept=("html", "pdf"))
        if page.html:
            parsed = parse_html(page.html, page.final_url or row["url"])
            texts.append((row["page_kind"], parsed.title + " " + parsed.text))
    return texts


def _digits(phone: str | None) -> str | None:
    found = phone_numbers("", [phone or ""])
    return found[0] if found else None


def site_evidence(domain: str, company: dict[str, Any], listing: dict[str, Any] | None, pages: list[dict[str, Any]],
                  fetcher: Fetcher, *, digest: bool = False) -> dict[str, Any]:
    """What the whole crawled site shows about this company. `digest` adds the
    title, a home-page excerpt and footer snippets, for the model check."""
    texts = _site_texts(pages, fetcher)
    names = company_names(company)
    shown: set[str] = set()
    for row in pages:
        try:
            shown.update(json.loads(row.get("phone_numbers") or "[]"))
        except ValueError:
            continue
    listing_phone = _digits((listing or {}).get("phone"))
    extra: dict[str, Any] = {}
    if digest:
        home = next((text for kind, text in texts if kind == "home"), texts[0][1] if texts else "")
        extra = {"title": next((row.get("title") for row in pages if row["page_kind"] == "home"), None),
                 "home_excerpt": " ".join(home.split())[:1800], "snippets": disclosure_snippets(texts)}
    return {
        **extra,
        "domain": domain, "pages_read": len(texts),
        "brand_found": brand_found(texts, company["company_name"]),
        "other_numbers": other_numbers(texts, company["company_number"]),
        "number_found": any(number_found(text, company["company_number"]) for _, text in texts)
        or any(row.get("company_number_found") for row in pages),
        "legal_name_found": any(legal_name_found(text, company["company_name"]) for _, text in texts),
        "legal_name_disclosed": any(
            legal_name_found(text, company["company_name"])
            and (kind in DISCLOSURE_KINDS or disclosure_context(text, company["company_name"]))
            for kind, text in texts),
        "name_found": any(name_found(text, name) for _, text in texts for name in names),
        "postcode_found": any(postcode_found(text, company.get("postcode")) for _, text in texts),
        "phone_match": bool(listing_phone and listing_phone in shown),
        "uk_site": domain.endswith(UK_TLDS) or bool(shown),
    }


def settle_tier(evidence: dict[str, Any], *, listing_links_here: bool = False,
                listing_at_registered_postcode: bool = False) -> tuple[str, str | None]:
    """(tier, rule) for one candidate from its whole-site evidence."""
    if evidence["number_found"]:
        return "verified", "number"
    if evidence["legal_name_found"]:
        if evidence["postcode_found"] or evidence["phone_match"] or listing_links_here:
            return "probable", "legal_name+corroboration"
        if evidence["legal_name_disclosed"] and evidence["uk_site"]:
            return "probable", "legal_name+disclosure"
    if evidence["name_found"] and evidence["postcode_found"]:
        return "probable", "name+postcode"
    if evidence["name_found"] and evidence["phone_match"] and listing_at_registered_postcode:
        return "probable", "name+phone"
    return "ambiguous", None


def settle_record(record: dict[str, Any], company: dict[str, Any], conn: sqlite3.Connection, fetcher: Fetcher, *,
                  crawl_version: str = CRAWL_VERSION) -> dict[str, Any] | None:
    """A settled copy of an ambiguous record, or None when no candidate reaches
    verified or probable on the crawled evidence."""
    listing = record.get("listing")
    best: tuple[tuple, dict[str, Any], str, str, dict[str, Any]] | None = None
    judged = []
    for candidate in top_candidates(record):
        pages = load_pages(conn, candidate["domain"], crawl_version)
        if not pages:
            continue
        at_registered = bool(listing and listing.get("match") == "name+postcode")
        links_here = at_registered and listing.get("domain") == candidate["domain"]
        evidence = site_evidence(candidate["domain"], company, listing, pages, fetcher)
        tier, rule = settle_tier(evidence, listing_links_here=links_here, listing_at_registered_postcode=at_registered)
        judged.append({"domain": candidate["domain"], "tier": tier, "rule": rule, "evidence": evidence})
        if tier == "ambiguous":
            continue
        key = (TIER_RANK[tier], RULE_RANK[rule], not links_here, -len(candidate["sources"]))
        if best is None or key < best[0]:
            best = (key, candidate, tier, rule, evidence)
    if best is None:
        return None
    _, candidate, tier, rule, evidence = best
    settled = json.loads(json.dumps(record))      # a copy
    settled.update(resolver_version=SETTLED_VERSION, settled_from=record["resolver_version"], tier=tier,
                   domain=candidate["domain"], resolved_at=utc_now(), settled_rule=rule, settled_judged=judged)
    for item in settled["candidates"]:
        if item["domain"] == candidate["domain"]:
            item["tier"] = tier
            item["evidence"] = {**item["evidence"], "settled_rule": rule, "settled": evidence}
    return settled


# ---------------------------------------------------------------- CLI

def _unsettled(path: Path = CHECKPOINT, tiers: tuple[str, ...] = ("ambiguous", "blocked")) -> list[dict[str, Any]]:
    from scripts.web.web_population import load_checkpoint
    return [r for r in load_checkpoint(path, version=None).values() if r.get("tier") in tiers]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="The candidate sites to crawl for the ambiguous companies.")
    plan.add_argument("--out", type=Path, default=SITES_FILE)
    plan.add_argument("--blocked", action="store_true",
                      help="Plan the best candidate of each company whose site refused a plain download instead.")
    sub.add_parser("run", help="Re-judge the ambiguous companies from the crawled sites (no network).")
    args = parser.parse_args(argv)

    records = _unsettled(args.checkpoint)
    if args.command == "plan":
        sites = sites_to_crawl(records, 1, "blocked") if args.blocked else sites_to_crawl(records)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(sites, indent=1), encoding="utf-8")
        print(json.dumps({"companies": len([r for r in records if r["tier"] == ("blocked" if args.blocked else "ambiguous")]),
                          "sites": len(sites), "file": str(args.out)}, indent=2))
        return 0

    from collections import Counter

    from scripts.web.web_population import append_checkpoint, company_inputs
    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        init_db(conn)
        companies = company_inputs(conn, [r["company_number"] for r in records])
        fetcher = Fetcher(cache_only=True)
        settled, rules = [], Counter()
        for record in records:
            company = companies.get(record["company_number"])
            result = settle_record(record, company, conn, fetcher) if company else None
            if result is not None:
                settled.append(result)
                rules[f"{result['tier']} by {result['settled_rule']}"] += 1
        for result in settled:
            append_checkpoint(result, args.checkpoint)
        print(json.dumps({"unsettled": len(records), "settled": len(settled), "still_unsettled": len(records) - len(settled),
                          "by_rule": dict(rules)}, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
