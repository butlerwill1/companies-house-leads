#!/usr/bin/env python3
"""W1 of the web stage: find each company's website and Google Maps listing.

Definitions and acceptance criteria are pre-registered in docs/WEB_STAGE.md.

For one company:

1. Candidates. A Google Maps search on the cleaned name at the registered
   postcode (UK-wide if nothing there matches the name); if no listing's
   website already verifies, an organic search on the cleaned name and up to
   three guessed domains; as a last resort, a search for the company number
   in quotes.
2. Checks. Each candidate site's homepage and up to six privacy, terms,
   contact or about pages are fetched (free, cached, robots.txt honoured) and
   searched for the company number, the registered name and the registered
   postcode. UK companies must show their registered number on their website,
   so a number match is close to certain.
3. Tier. `verified` (number found), `probable` (name and postcode found, or a
   Maps listing matched on name and postcode links to the site), `ambiguous`
   (only the name, or a domain that spells the name), `none`.

Search calls go through `SearchClient` (cached, allowance-capped); running out
of allowance raises and stops the stage at the last checkpoint.
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Any, Callable

from scripts.website_analysis.search_providers import SearchClient, normalise_postcode, registrable_domain
from scripts.website_analysis.web_fetch import Fetcher, parse_html
from scripts.website_analysis.web_trading_names import name_from_listing, names_from_site_text

RESOLVER_VERSION = "identity-v2-trading-names"
# Places-first: Serper's places search (1 credit) near the company's district
# instead of its Maps search (3 credits). Thinner listings: one category, no
# secondary categories, place id or opening hours, and rating less often.
PLACES_FIRST_VERSION = "identity-v3-places-first"
FIRST_LOOKUPS = ("maps", "places")
# `blocked`: the site refused automated requests (403, 429 or a bot challenge), so
# nothing could be checked. Recorded as such, not as "nothing found".
TIERS = ("verified", "probable", "ambiguous", "blocked", "none")
TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}
SOURCE_RANK = {"maps": 0, "places": 0, "knowledge_graph": 1, "organic": 2, "guess": 3, "number_search": 4}
MAX_ORGANIC_CANDIDATES = 6
MAX_TRADING_NAME_SEARCHES = 2
MAX_GUESSES = 3
MAX_SITE_CHECKS = 8
MAX_EXTRA_PAGES = 6

LEGAL_WORDS = r"limited|ltd|plc|llp|l\.l\.p|company|co|holdings?|group|uk|u\.k|the|international|intl"
LEGAL_TOKEN = re.compile(rf"\b(?:{LEGAL_WORDS})\b\.?", re.I)
PARENTHETICAL = re.compile(r"\([^)]*\)")
GENERIC_TOKENS = {
    "and", "of", "services", "service", "solutions", "management", "consulting", "consultants", "trading",
    "enterprises", "industries", "systems", "associates", "partners", "partnership", "ventures", "global",
    "europe", "british", "britain", "england", "scotland", "wales", "northern", "southern", "national",
    "investments", "properties", "property", "developments", "products", "supplies", "technologies",
}
LEGAL_LINK = re.compile(r"privacy|terms|legal|contact|about|cookie|imprint|company-info|disclaimer|policy", re.I)
LINK_PRIORITY = ("privacy", "terms", "legal", "contact", "about", "cookie")
PARKED = re.compile(r"domain (?:name )?(?:is|may be) for sale|buy this domain|this domain is parked|"
                    r"parked free|domain has expired|future home of", re.I)
NUMBER_CONTEXT = re.compile(r"compan|regist|reg\.?\s*no|number|no\.", re.I)

BLOCKED_DOMAINS = frozenset({
    # seen as a "candidate" for many unrelated companies in the 2026-10-03 population run:
    # company-data and registry mirrors, academic and reference sites, marketplaces, travel and listing sites
    "formationdata.co.uk", "preqin.com", "ceoemail.com", "globaldatabase.com", "tracxn.com", "jars.lt",
    "pomanda.com", "zaubacorp.com", "instafinancials.com", "ukgovscan.com", "checkfree.co.uk", "housemetric.co.uk",
    "plainsite.org", "britishlei.co.uk", "companydatashop.com", "bymetric.com", "identeco.co.uk",
    "hal.science", "ieee.org", "nih.gov", "fca.org.uk", "ico.org.uk", "reddit.com", "imdb.com", "fandom.com",
    "ebay.com", "apple.com", "partsgeek.com", "vecteezy.com", "mauritius-images.com", "zillow.com", "mapquest.com",
    "booking.com", "trivago.com", "expedia.com", "daynurseries.co.uk",
    # company-data mirrors and registries
    "endole.co.uk", "opencorporates.com", "companycheck.co.uk", "bizdb.co.uk", "companiesintheuk.co.uk",
    "duedil.com", "northdata.com", "northdata.de", "dnb.com", "zoominfo.com", "rocketreach.co", "apollo.io",
    "crunchbase.com", "bloomberg.com", "thegazette.co.uk", "companieslist.co.uk", "192.com",
    "find-and-update.company-information.service.gov.uk", "service.gov.uk", "gov.uk",
    # directories and review sites
    "yell.com", "checkatrade.com", "cylex-uk.co.uk", "thomsonlocal.com", "scoot.co.uk", "freeindex.co.uk",
    "yelp.co.uk", "yelp.com", "trustpilot.com", "tripadvisor.co.uk", "tripadvisor.com", "bark.com",
    "ratedpeople.com", "mybuilder.com", "trustatrader.com", "which.co.uk", "houzz.co.uk", "google.com",
    "google.co.uk",
    # social, jobs, marketplaces, reference
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com", "youtube.com",
    "pinterest.com", "wikipedia.org", "glassdoor.co.uk", "glassdoor.com", "indeed.com", "indeed.co.uk",
    "reed.co.uk", "totaljobs.com", "amazon.co.uk", "amazon.com", "ebay.co.uk", "etsy.com",
})


# ---------------------------------------------------------------- names, numbers, postcodes

def clean_name(name: str) -> str:
    """`J F ASHTON (NORTHERN) LIMITED` -> `J F Ashton`. Falls back to the name
    without its legal suffix when everything else is a legal word."""
    stripped = PARENTHETICAL.sub(" ", name or "")
    words = " ".join(LEGAL_TOKEN.sub(" ", stripped).split())
    words = words.strip(" &-,.")
    if not re.search(r"[A-Za-z0-9]", words):
        words = " ".join(re.sub(r"\b(?:limited|ltd|plc|llp)\b\.?", " ", name or "", flags=re.I).split())
    return " ".join(word if any(c.islower() for c in word) else word.capitalize() for word in words.split())


def name_qualifier(name: str) -> str:
    """What the parentheses held, minus legal words and bare numbers, usually a
    place: `CITY HOTELS (DUNFERMLINE) LIMITED` -> `Dunfermline`. Dropped from
    the Maps query (already local) but added to the organic one, where
    `City Hotels` alone is too generic."""
    inside = " ".join(PARENTHETICAL.findall(name or "")).strip("() ")
    words = [w for w in LEGAL_TOKEN.sub(" ", inside.replace("(", " ").replace(")", " ")).split() if not w.isdigit()]
    return " ".join(word.capitalize() for word in words)


def _norm(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def name_tokens(name: str) -> list[str]:
    return _norm(clean_name(name)).split()


def distinctive_tokens(name: str) -> list[str]:
    tokens = name_tokens(name)
    distinctive = [token for token in tokens if token not in GENERIC_TOKENS]
    return distinctive or tokens


def name_found(text: str, company_name: str) -> bool:
    """The cleaned name appears as a phrase. A phrase made only of generic
    words, or shorter than four characters, is not evidence."""
    phrase = _norm(clean_name(company_name))
    if len(phrase.replace(" ", "")) < 4 or all(token in GENERIC_TOKENS for token in phrase.split()):
        return False
    return f" {phrase} " in f" {_norm(text)} "


def _legal_norm(text: str) -> str:
    """Normalised so that `Ltd` and `Limited`, `&` and `and`, `(UK)` and `UK` read the same."""
    return " ".join("limited" if word == "ltd" else word for word in _norm(text).split())


def legal_name_found(text: str, company_name: str) -> bool:
    """The full registered name, legal suffix included, appears as a phrase
    ("Lotus Homes (UK) Limited" or "Lotus Homes UK Ltd"). UK company names are
    unique, so this is close to the company number as evidence. The cleaned
    name must itself be distinctive (see `name_found`)."""
    if not name_found(company_name, company_name):
        return False
    phrase = _legal_norm(company_name)
    return len(phrase.replace(" ", "")) >= 8 and f" {phrase} " in f" {_legal_norm(text)} "


def names_similar(a: str, b: str) -> bool:
    """Listing title vs registered name: most distinctive tokens shared, or one
    cleaned name contained in the other."""
    ta, tb = set(distinctive_tokens(a)), set(distinctive_tokens(b))
    if not ta or not tb:
        return False
    na, nb = _norm(clean_name(a)), _norm(clean_name(b))
    if na and nb and (f" {na} " in f" {nb} " or f" {nb} " in f" {na} "):
        return True
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def domain_spells_name(domain: str | None, company_name: str) -> bool:
    if not domain:
        return False
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    tokens = distinctive_tokens(company_name)
    joined = "".join(tokens)
    if len(joined) < 4:
        return False
    return label == joined or label.startswith(joined) or (len(tokens) > 1 and all(t in label for t in tokens))


def number_found(text: str, company_number: str) -> bool:
    """The registered number on a page: exactly, with spaces or hyphens inside
    it, or (next to the words company or registered) without its leading zeros."""
    number = company_number.strip().upper()
    match = re.fullmatch(r"([A-Z]{2})?(\d+)", number)
    if not match or not text:
        return False
    prefix, digits = match.group(1) or "", match.group(2)
    start = r"(?<![0-9A-Za-z])"
    if re.search(start + re.escape(number) + r"(?![0-9])", text, re.I):
        return True
    spaced = (rf"{prefix}[ -]?" if prefix else "") + r"[ -]?".join(digits)
    if re.search(start + spaced + r"(?![ -]?\d)", text, re.I):
        return True
    stripped = digits.lstrip("0")
    if not prefix and stripped != digits and len(stripped) >= 5:
        for hit in re.finditer(start + stripped + r"(?![0-9])", text):
            if NUMBER_CONTEXT.search(text[max(0, hit.start() - 60):hit.start()]):
                return True
    return False


def postcode_found(text: str, postcode: str | None) -> bool:
    postcode = normalise_postcode(postcode)
    if not postcode or not text:
        return False
    outward, inward = postcode.split()
    return re.search(rf"\b{outward}\s*{inward}\b", text, re.I) is not None


def blocked(domain: str | None) -> bool:
    if not domain:
        return True
    return domain in BLOCKED_DOMAINS or any(domain.endswith("." + blocked) for blocked in BLOCKED_DOMAINS)


def domain_guesses(company_name: str) -> list[str]:
    tokens = distinctive_tokens(company_name)
    joined = "".join(tokens)
    if len(joined) < 4:
        return []
    guesses = [f"{joined}.co.uk", f"{joined}.com"]
    if len(tokens) > 1:
        guesses.append(f"{'-'.join(tokens)}.co.uk")
    return guesses[:MAX_GUESSES]


def resolves(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, 443)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


# ---------------------------------------------------------------- listing match and site checks

def company_names(company: dict[str, Any]) -> list[str]:
    """The registered name, then any trading names."""
    return [company["company_name"], *[n for n in company.get("trading_names") or [] if n]]


def match_listing(listing: dict[str, Any], company: dict[str, Any]) -> str | None:
    """`name+postcode`, `name_only`, or None when the title matches neither the
    registered name nor a trading name."""
    if not any(names_similar(listing.get("title") or "", name) for name in company_names(company)):
        return None
    registered = normalise_postcode(company.get("postcode"))
    if registered and listing.get("postcode") == registered:
        return "name+postcode"
    return "name_only"


def _legal_links(links: list[tuple[str, str]], domain: str) -> list[str]:
    picked: dict[str, int] = {}
    for url, anchor in links:
        if registrable_domain(url) != domain or url in picked:
            continue
        haystack = f"{url} {anchor}"
        if LEGAL_LINK.search(haystack):
            rank = next((i for i, word in enumerate(LINK_PRIORITY) if word in haystack.lower()), len(LINK_PRIORITY))
            picked[url.split("#")[0]] = rank
    return sorted(picked, key=lambda url: picked[url])[:MAX_EXTRA_PAGES]


def check_site(domain: str, company: dict[str, Any], fetcher: Fetcher, start_url: str | None = None) -> dict[str, Any]:
    """Fetch a candidate site and record what it shows. Stops as soon as the
    company number is found."""
    starts = [start_url] if start_url else []
    starts += [f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"]
    names = company_names(company)
    home = None
    refused = False
    for url in dict.fromkeys(starts):
        page = fetcher.get(url)
        if page.ok:
            home = page
            break
        refused = refused or page.blocked
    evidence: dict[str, Any] = {"domain": domain, "final_domain": None, "final_url": None, "pages_checked": 0,
                                "number_found": False, "number_page": None, "name_found": False,
                                "postcode_found": False,
                                "domain_spells_name": any(domain_spells_name(domain, n) for n in names),
                                "parked": False, "blocked": False, "title": None, "error": None,
                                "trading_names_found": []}
    if home is None:
        evidence["error"] = "no page fetched"
        evidence["blocked"] = refused
        return evidence
    final_domain = registrable_domain(home.final_url) or domain
    evidence.update(final_domain=final_domain, final_url=home.final_url)
    parsed = parse_html(home.html, home.final_url or home.url)
    evidence["title"] = parsed.title[:200]
    pages = [(home.final_url or home.url, parsed.title + " " + parsed.text)]
    if PARKED.search(parsed.text[:3000]):
        evidence["parked"] = True
        evidence["pages_checked"] = 1
        return evidence
    for url in _legal_links(parsed.links, final_domain):
        if any(number_found(text, company["company_number"]) for _, text in pages):
            break
        page = fetcher.get(url, accept=("html", "pdf"))   # a privacy policy is often a PDF
        if page.ok:
            extra = parse_html(page.html, page.final_url or url)
            pages.append((page.final_url or url, extra.title + " " + extra.text))
    evidence["pages_checked"] = len(pages)
    for url, text in pages:
        if not evidence["number_found"] and number_found(text, company["company_number"]):
            evidence["number_found"], evidence["number_page"] = True, url
        evidence["name_found"] = evidence["name_found"] or any(name_found(text, n) for n in names)
        evidence["postcode_found"] = evidence["postcode_found"] or postcode_found(text, company.get("postcode"))
    if evidence["number_found"]:
        # The site is this company's, so "X is a trading name of <this company>" can be trusted.
        seen: dict[str, dict[str, str]] = {}
        for _, text in pages:
            for item in names_from_site_text(text, company["company_name"]):
                seen.setdefault(item["name"].lower(), item)
        evidence["trading_names_found"] = list(seen.values())
    return evidence


def tier_for(evidence: dict[str, Any], *, listing_links_here: bool = False) -> str:
    """`listing_links_here`: a Maps listing matched on name and postcode has this site as its website."""
    if evidence.get("parked"):
        return "none"
    if evidence.get("blocked"):
        # Nothing could be read, but a listing matched on name and postcode that links here is still evidence.
        return "probable" if listing_links_here else "blocked"
    if evidence.get("error"):
        return "none"
    if evidence.get("number_found"):
        return "verified"
    if (evidence.get("name_found") and evidence.get("postcode_found")) or listing_links_here:
        return "probable"
    if evidence.get("name_found") or evidence.get("domain_spells_name"):
        return "ambiguous"
    return "none"


# ---------------------------------------------------------------- one company

@dataclass
class _Candidate:
    domain: str
    sources: list[str]
    start_url: str | None = None


def resolve(company: dict[str, Any], client: SearchClient, fetcher: Fetcher, *,
            dns_ok: Callable[[str], bool] = resolves, first_lookup: str = "maps") -> dict[str, Any]:
    """`company`: company_number, company_name, postcode. Returns the record
    that is checkpointed and later stored."""
    number, name = company["company_number"], company["company_name"]
    query = clean_name(name)
    trading = [t for t in dict.fromkeys(company.get("trading_names") or []) if t][:MAX_TRADING_NAME_SEARCHES]
    if first_lookup not in FIRST_LOOKUPS:
        raise ValueError(f"first_lookup must be one of {FIRST_LOOKUPS}")
    version = PLACES_FIRST_VERSION if first_lookup == "places" else RESOLVER_VERSION
    record: dict[str, Any] = {"company_number": number, "company_name": name, "resolver_version": version,
                              "query": query, "trading_names_used": trading, "searches": [], "listings": [],
                              "listing": None, "candidates": [], "domain": None, "tier": "none", "problems": [],
                              "new_trading_names": []}
    candidates: dict[str, _Candidate] = {}
    checked: dict[str, dict[str, Any]] = {}

    def add(domain: str | None, source: str, start_url: str | None = None) -> None:
        if blocked(domain):
            return
        if domain in candidates:
            if source not in candidates[domain].sources:
                candidates[domain].sources.append(source)
        else:
            candidates[domain] = _Candidate(domain, [source], start_url)

    def check_pending() -> None:
        for domain, candidate in list(candidates.items()):
            if domain in checked or len(checked) >= MAX_SITE_CHECKS:
                continue
            evidence = check_site(domain, company, fetcher, candidate.start_url)
            checked[domain] = evidence
            final = evidence.get("final_domain")
            if final and final != domain and not blocked(final) and final not in candidates:
                candidates[final] = _Candidate(final, [*candidate.sources, "redirect"], evidence.get("final_url"))
                checked[final] = evidence

    def best_tier() -> str:
        tiers = [_tier(domain) for domain in checked]
        return min(tiers, key=TIER_RANK.__getitem__) if tiers else "none"

    matched: list[tuple[dict[str, Any], str]] = []

    def _tier(domain: str) -> str:
        # The listing may link to a domain that redirects here, so both names count.
        names = {domain, checked[domain].get("domain")}
        links_here = any(listing.get("domain") in names and how == "name+postcode" for listing, how in matched)
        return tier_for(checked[domain], listing_links_here=links_here)

    # 1. Google Maps listings
    location = None
    if company.get("postcode"):
        try:
            location = client.postcode_location(company["postcode"])
        except Exception as exc:  # noqa: BLE001 -- a free lookup failing only loses the local search
            record["problems"].append(f"postcode lookup failed: {exc}"[:200])
    listings: list[dict[str, Any]] = []
    located = bool(location and location.get("latitude") is not None)
    district = (location or {}).get("district")

    def local_search(text: str) -> list[dict[str, Any]]:
        if first_lookup == "places":
            near = f"{text} {district}" if district else text
            record["searches"].append({"kind": "places_local", "query": near, "near": location.get("postcode")})
            return client.places_search(near)
        record["searches"].append({"kind": "maps", "query": text, "near": location.get("postcode")})
        return client.maps_search(text, lat=location["latitude"], lng=location["longitude"])

    if located:
        listings = local_search(query)
    matched = [(listing, how) for listing in listings if (how := match_listing(listing, company))]
    if not matched and located:
        for trading_name in trading:   # the company may be listed under the name it trades as
            found = local_search(trading_name)
            listings += found
            matched = [(listing, how) for listing in found if (how := match_listing(listing, company))]
            if matched:
                break
    if not matched:
        for places_query in [query, *trading[:1]]:
            uk_wide = client.places_search(places_query)
            record["searches"].append({"kind": "places", "query": places_query, "near": "United Kingdom"})
            listings += uk_wide
            matched = [(listing, how) for listing in uk_wide if (how := match_listing(listing, company))]
            if matched:
                break
    record["listings"] = listings
    matched.sort(key=lambda pair: pair[1] != "name+postcode")
    for listing, _ in matched:
        add(listing.get("domain"), "maps", listing.get("website"))
    check_pending()

    # 2. Organic search and guesses, only if no listing's site is verified or probable
    if best_tier() not in ("verified", "probable"):
        organic_queries = [" ".join(filter(None, (query, name_qualifier(name))))]
        organic_queries += [" ".join(filter(None, (t, name_qualifier(name)))) for t in trading]
        for index, organic_query in enumerate(organic_queries):
            if index and best_tier() in ("verified", "probable"):
                break
            results = client.web_search(organic_query)
            record["searches"].append({"kind": "organic", "query": organic_query})
            graph = results.get("knowledge_graph") or {}
            add(graph.get("domain"), "knowledge_graph", graph.get("website"))
            for result in results.get("organic", [])[:MAX_ORGANIC_CANDIDATES]:
                add(result.get("domain"), "organic")
            guesses = [g for n in (name, *trading) for g in domain_guesses(n)]
            for guess in dict.fromkeys(guesses):
                if guess not in candidates and dns_ok(guess):
                    add(guess, "guess")
            check_pending()

    # 3. Last resort: the company number in quotes
    if best_tier() not in ("verified", "probable"):
        results = client.web_search(f'"{number}"')
        record["searches"].append({"kind": "number_search", "query": f'"{number}"'})
        for result in results.get("organic", [])[:MAX_ORGANIC_CANDIDATES]:
            add(result.get("domain"), "number_search")
        check_pending()

    rows = []
    for domain, evidence in checked.items():
        if evidence.get("final_domain") not in (None, domain) and evidence["final_domain"] in checked:
            continue  # a redirect: recorded once, under the domain it lands on
        rows.append({"domain": domain, "tier": _tier(domain), "sources": candidates[domain].sources,
                     "evidence": evidence})
    rows.sort(key=lambda row: (TIER_RANK[row["tier"]],
                               min(SOURCE_RANK.get(s, 9) for s in row["sources"])))
    record["candidates"] = rows
    if rows and rows[0]["tier"] != "none":
        record["domain"], record["tier"] = rows[0]["domain"], rows[0]["tier"]

    if matched:
        listing, how = matched[0]
        if record["domain"] and listing.get("domain") == record["domain"] and how == "name_only":
            how = "name+domain"
        record["listing"] = {**listing, "match": how}

    # New trading names, only from a verified site (its own pages and its matching Maps listing).
    if record["tier"] == "verified" and rows:
        for item in rows[0]["evidence"].get("trading_names_found") or []:
            record["new_trading_names"].append({**item, "source": "website"})
        if record["listing"] and record["listing"].get("domain") == record["domain"]:
            item = name_from_listing(record["listing"], name, verified_site=True)
            if item:
                record["new_trading_names"].append({**item, "source": "maps_listing"})
    return record
