#!/usr/bin/env python3
"""W4: search demand and the paid-search position of each company (docs/WEB_STAGE_PLAN.md).

For each company with a website from W1:

- **Ads Transparency** (`SearchClient.ads_search`): the Google search ads
  shown in the UK for an advertiser linked to the company's domain, with first
  and last shown dates. `advertising_now` means an ad was last shown in the
  past 30 days. This is the advertising signal: on the 23-company test it
  found 11 advertisers where DataForSEO Labs' paid estimate found 3 of them,
  and one live search found none.
- **Keyword market** (`SearchClient.keyword_volume`): monthly UK searches and
  cost per click for the company's seed phrases (from W3), every company's
  phrases in one call. `monthly_click_value_usd` (searches x cost per click,
  summed) is what all those clicks would cost to buy: a ceiling on the
  paid-search market, not a spend estimate. Google Ads prices are US dollars.
- **Organic traffic** (`SearchClient.bulk_traffic`): DataForSEO Labs' modelled
  monthly organic visits, one call for every domain. Labs' paid figures are
  not used.
- **Live results check** (`SearchClient.web_search_with_ads`), optional and
  for a short list only: the highest-volume phrase searched from the
  company's location, for its organic position and map presence. One snapshot
  misses advertisers with small daily budgets, so "not seen in the ads" is
  never evidence that a company does not advertise.

Every response is cached by `SearchClient`, so an interrupted run costs
nothing to repeat; each finished company is also appended to a checkpoint.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from companies_house_core.companies_house_sqlite import utc_now
from scripts.website_analysis.search_providers import UK_LOCATION_CODE, SearchClient, registrable_domain
from scripts.website_analysis.web_identity import names_similar

MARKET_VERSION = "market-v1"
ADVERTISING_NOW_DAYS = 30
CHECKPOINT = Path("logs/web/market-checkpoint.jsonl")
BATCH = 1000


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(str(value), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def ads_summary(result: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    creatives = result.get("creatives") or []
    first = [t for t in (_parse_time(c.get("first_shown")) for c in creatives) if t]
    last = [t for t in (_parse_time(c.get("last_shown")) for c in creatives) if t]
    latest = max(last) if last else None
    return {
        "ads_seen": len(creatives),
        "advertising_now": latest is not None and now - latest <= timedelta(days=ADVERTISING_NOW_DAYS),
        "first_shown": min(first).date().isoformat() if first else None,
        "last_shown": latest.date().isoformat() if latest else None,
        "advertisers": sorted({c.get("title") for c in creatives if c.get("title")}),
        "verified": any(c.get("verified") for c in creatives),
        "formats": sorted({c.get("format") for c in creatives if c.get("format")}),
    }


def seed_map(seeds: dict[str, Any]) -> dict[str, list[str]]:
    """Accept `{number: [phrases]}` or `{number: {"keywords": [phrases], ...}}`."""
    out: dict[str, list[str]] = {}
    for number, value in seeds.items():
        phrases = value.get("keywords") if isinstance(value, dict) else value
        if not isinstance(phrases, list):
            continue  # a note or other metadata, not a company's phrases
        clean = [" ".join(str(p).lower().split()) for p in phrases or []]
        out[number] = [p for p in dict.fromkeys(clean) if p]
    return out


def keyword_market(rows: list[dict[str, Any]], phrases: list[str]) -> dict[str, Any]:
    by_keyword = {row["keyword"]: row for row in rows if row.get("keyword")}
    keywords = []
    for phrase in phrases:
        row = by_keyword.get(phrase) or {}
        keywords.append({"keyword": phrase, "search_volume": row.get("search_volume"), "cpc": row.get("cpc"),
                         "competition": row.get("competition"), "competition_index": row.get("competition_index"),
                         "top_of_page_bid_high": row.get("high_top_of_page_bid")})
    priced = [k for k in keywords if k["search_volume"] and k["cpc"]]
    volume = sum(k["search_volume"] or 0 for k in keywords)
    value = sum(k["search_volume"] * k["cpc"] for k in priced)
    weighted = value / sum(k["search_volume"] for k in priced) if priced else None
    return {"keywords": keywords, "total_volume": volume,
            "weighted_cpc_usd": round(weighted, 2) if weighted is not None else None,
            "monthly_click_value_usd": round(value, 2)}


def serp_observation(serp: dict[str, Any], domain: str | None) -> dict[str, Any]:
    domain = registrable_domain(domain) if domain else None
    paid = [row for row in serp.get("paid") or [] if row.get("domain")]
    advertisers = list(dict.fromkeys(row["domain"] for row in paid))
    organic_rank = next((i for i, row in enumerate(serp.get("organic") or [], 1) if row.get("domain") == domain), None)
    return {
        "in_ads": domain in advertisers if domain else False,
        "organic_position": organic_rank,
        "in_local_pack": any(row.get("domain") == domain for row in serp.get("local_pack") or []) if domain else False,
        "advertisers": advertisers,
        "advertiser_count": len(advertisers),
    }


def best_listing(listings: list[dict[str, Any]], *, domain: str | None, brand: str | None,
                 registered_name: str | None) -> dict[str, Any] | None:
    """The Maps listing that is this company: one linking to its website first,
    then one named like its trading name, then like its registered name."""
    if domain:
        for listing in listings:
            if listing.get("domain") == domain:
                return {**listing, "match": "domain"}
    for name, how in ((brand, "brand"), (registered_name, "registered_name")):
        if not name:
            continue
        for listing in listings:
            if names_similar(listing.get("title") or "", name):
                return {**listing, "match": how}
    return None


def _chunks(items: list[Any], size: int = BATCH) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ---------------------------------------------------------------- the run

def load_checkpoint(path: Path = CHECKPOINT, version: str = MARKET_VERSION) -> dict[str, dict[str, Any]]:
    done: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        if record.get("market_version") == version:
            done[record["company_number"]] = record
    return done


def append_checkpoint(record: dict[str, Any], path: Path = CHECKPOINT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _location(company: dict[str, Any], client: SearchClient, listing: dict[str, Any] | None) -> dict[str, Any] | None:
    if listing and listing.get("latitude") is not None and listing.get("longitude") is not None:
        return {"latitude": listing["latitude"], "longitude": listing["longitude"], "source": "listing"}
    if company.get("latitude") is not None and company.get("longitude") is not None:
        return {"latitude": company["latitude"], "longitude": company["longitude"], "source": "company"}
    if company.get("postcode"):
        found = client.postcode_location(company["postcode"])
        if found and found.get("latitude") is not None:
            return {"latitude": found["latitude"], "longitude": found["longitude"], "source": "registered postcode"}
    return None


def assess_market(companies: list[dict[str, Any]], seeds: dict[str, list[str]], client: SearchClient, *,
                  serp_checks: int = 0, lookup_listings: bool = False, checkpoint: Path | None = CHECKPOINT,
                  log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """The market record of each company: `{company_number, domain, ads, traffic, market, serp_checks, ...}`.
    `companies`: company_number, company_name, domain (or None), and optionally
    brand, postcode and a Maps `listing` (from W1). The batch calls (organic
    traffic, keyword volumes) cover every company that still needs work.
    `serp_checks` live searches per company are made only when asked for (the
    short list); `lookup_listings` makes a Maps search per company that has no
    listing yet (the hand-supplied research harness)."""
    done = load_checkpoint(checkpoint) if checkpoint else {}
    todo = [c for c in companies if c["company_number"] not in done]
    results: dict[str, dict[str, Any]] = dict(done)
    if todo:
        provider = client.dataforseo_provider
        spent_before = client.billed.get(provider, 0.0)
        domains = sorted({c["domain"] for c in todo if c.get("domain")})
        traffic: dict[str, dict[str, Any]] = {}
        for batch in _chunks(domains):
            for row in client.bulk_traffic(batch):
                key = registrable_domain(row.get("target") or "")
                if key:
                    traffic[key] = row
        phrases = list(dict.fromkeys(p for c in todo for p in seeds.get(c["company_number"], [])))
        volumes: list[dict[str, Any]] = []
        for batch in _chunks(phrases):
            volumes += client.keyword_volume(batch)
        log(f"batch calls done: {len(domains)} domains, {len(phrases)} phrases "
            f"(${round(client.billed.get(provider, 0.0) - spent_before, 4)})")
        for company in todo:
            record = _assess_company(company, seeds, client, traffic, volumes, serp_checks, lookup_listings)
            results[company["company_number"]] = record
            if checkpoint:
                append_checkpoint(record, checkpoint)
            log(f"  {company['company_number']} {company.get('brand') or company.get('company_name')}: done "
                f"(spent so far ${round(client.billed.get(provider, 0.0) - spent_before, 4)})")
    return [results[c["company_number"]] for c in companies if c["company_number"] in results]


def _assess_company(company: dict[str, Any], seeds: dict[str, list[str]], client: SearchClient,
                    traffic: dict[str, dict[str, Any]], volumes: list[dict[str, Any]], serp_checks: int,
                    lookup_listings: bool) -> dict[str, Any]:
    number, domain = company["company_number"], company.get("domain")
    listing = company.get("listing")
    place = None
    if lookup_listings and listing is None and company.get("postcode"):
        place = client.postcode_location(company["postcode"])
        if place and place.get("latitude") is not None:
            found = client.maps_search(company.get("brand") or company.get("company_name") or "",
                                       lat=place["latitude"], lng=place["longitude"], zoom=12)
            listing = best_listing(found, domain=domain, brand=company.get("brand"),
                                   registered_name=company.get("company_name"))
    market = keyword_market(volumes, seeds.get(number, []))
    row = traffic.get(domain) if domain else None
    out: dict[str, Any] = {
        "company_number": number, "company_name": company.get("company_name"), "brand": company.get("brand"),
        "domain": domain, "market_version": MARKET_VERSION, "assessed_at": utc_now(), "listing": listing,
        "ads": ads_summary(client.ads_search(domain)) if domain else None,
        "organic": None if row is None else {k: row.get(k) for k in ("organic_etv", "organic_count", "local_pack_etv")},
        "market": market, "serp_checks": [], "location_source": None}
    if serp_checks and domain:
        location = _location(company, client, listing)
        out["location_source"] = location["source"] if location else None
        ranked = sorted((k for k in market["keywords"] if k["search_volume"]), key=lambda k: -k["search_volume"])
        for keyword in ranked[:serp_checks] if location else []:
            serp = client.web_search_with_ads(keyword["keyword"], lat=location["latitude"], lng=location["longitude"])
            out["serp_checks"].append({"keyword": keyword["keyword"], "latitude": location["latitude"],
                                       "longitude": location["longitude"], **serp_observation(serp, domain)})
    return out


# ---------------------------------------------------------------- storage

def store_market(conn: sqlite3.Connection, records: Iterable[dict[str, Any]],
                 market_version: str = MARKET_VERSION) -> dict[str, int]:
    """Write keyword rows, the company row and live checks. Re-storing replaces
    them; the gap rollup columns of `company_market` (written by the findings
    step) are left alone."""
    counts = {"companies": 0, "keywords": 0, "serp_observations": 0}
    for rec in records:
        number, now = rec["company_number"], rec.get("assessed_at") or utc_now()
        ads, organic, market = rec.get("ads") or {}, rec.get("organic") or {}, rec.get("market") or {}
        conn.execute(
            "insert into company_market (company_number, market_version, domain, ads_seen, advertising_now, "
            "ads_first_shown, ads_last_shown, advertiser_verified, advertiser_name, phrase_search_volume, "
            "weighted_cpc_usd, monthly_click_value_usd, organic_etv, organic_keywords, local_pack_etv, assessed_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "on conflict(company_number, market_version) do update set domain=excluded.domain, "
            "ads_seen=excluded.ads_seen, advertising_now=excluded.advertising_now, "
            "ads_first_shown=excluded.ads_first_shown, ads_last_shown=excluded.ads_last_shown, "
            "advertiser_verified=excluded.advertiser_verified, advertiser_name=excluded.advertiser_name, "
            "phrase_search_volume=excluded.phrase_search_volume, weighted_cpc_usd=excluded.weighted_cpc_usd, "
            "monthly_click_value_usd=excluded.monthly_click_value_usd, organic_etv=excluded.organic_etv, "
            "organic_keywords=excluded.organic_keywords, local_pack_etv=excluded.local_pack_etv, "
            "assessed_at=excluded.assessed_at",
            (number, market_version, rec.get("domain"), ads.get("ads_seen"),
             None if rec.get("ads") is None else int(bool(ads.get("advertising_now"))), ads.get("first_shown"),
             ads.get("last_shown"), None if rec.get("ads") is None else int(bool(ads.get("verified"))),
             (ads.get("advertisers") or [None])[0], market.get("total_volume"), market.get("weighted_cpc_usd"),
             market.get("monthly_click_value_usd"), organic.get("organic_etv"), organic.get("organic_count"),
             organic.get("local_pack_etv"), now))
        counts["companies"] += 1
        for k in market.get("keywords") or []:
            conn.execute(
                "insert or replace into company_keyword_market (company_number, market_version, keyword, location_code, "
                "search_volume, cpc_usd, competition, competition_index, top_of_page_bid_high_usd, fetched_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (number, market_version, k["keyword"], UK_LOCATION_CODE, k.get("search_volume"), k.get("cpc"),
                 k.get("competition"), k.get("competition_index"), k.get("top_of_page_bid_high"), now))
            counts["keywords"] += 1
        for check in rec.get("serp_checks") or []:
            conn.execute(
                "insert or replace into serp_observations (company_number, market_version, keyword, latitude, "
                "longitude, observed_at, in_ads, organic_position, in_local_pack, advertisers) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (number, market_version, check["keyword"], check.get("latitude"), check.get("longitude"), now,
                 int(bool(check.get("in_ads"))), check.get("organic_position"), int(bool(check.get("in_local_pack"))),
                 json.dumps(check.get("advertisers") or [])))
            counts["serp_observations"] += 1
    conn.commit()
    return counts


# ---------------------------------------------------------------- the hand-supplied research harness

SUMMARY_HEADER = [
    "company number", "company", "trading as", "website", "Google category", "rating", "reviews", "listing claimed",
    "Google ads seen (Ads Transparency)", "advertising now (last 30 days)", "ads last shown",
    "est. organic visits/month (Labs)", "searches/month for its phrases", "avg cpc USD", "click value USD/month",
    "live check phrase", "in ads", "organic position", "in map pack", "advertisers on that page",
]


def summary_rows(records: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = [SUMMARY_HEADER]
    for r in records:
        listing, ads, organic = r.get("listing") or {}, r.get("ads") or {}, r.get("organic") or {}
        check = (r.get("serp_checks") or [{}])[0] if r.get("serp_checks") else {}
        market = r.get("market") or {}
        rows.append([
            r["company_number"], r.get("company_name"), r.get("brand"), r.get("domain") or "",
            listing.get("category") or "", listing.get("rating") or "", listing.get("rating_count") or "",
            "" if listing.get("is_claimed") is None else ("yes" if listing.get("is_claimed") else "no"),
            ads.get("ads_seen", "") if r.get("ads") is not None else "",
            "" if r.get("ads") is None else ("yes" if ads.get("advertising_now") else "no"),
            ads.get("last_shown") or "",
            round(organic["organic_etv"]) if organic.get("organic_etv") is not None else "",
            market.get("total_volume", ""), market.get("weighted_cpc_usd") or "",
            market.get("monthly_click_value_usd", ""),
            check.get("keyword", ""), "" if not check else ("yes" if check.get("in_ads") else "no"),
            check.get("organic_position") or "", "" if not check else ("yes" if check.get("in_local_pack") else "no"),
            "; ".join(check.get("advertisers") or []),
        ])
    return rows


def write_report(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")
