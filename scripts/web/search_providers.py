#!/usr/bin/env python3
"""Search-results providers for the web stage, each used on its free allowance.

Stage code calls the methods of `SearchClient`, never a provider's HTTP API
directly, so moving a stage to another provider means changing one adapter.

Every billed request goes through `SearchClient._call`, which:

- returns the cached raw response when the same request was made before
  (`data/raw/search-providers/<provider>/<endpoint>/<sha1>.json`), so a re-run
  costs nothing;
- in cache-only mode raises `CacheMiss` instead of making a request (the free
  dry run);
- refuses a request that would go past the provider's allowance
  (`AllowanceExhausted`), so running out stops a stage cleanly and never falls
  through to a paid tier;
- appends one line per billed request to `logs/web/provider-usage.jsonl`.

Allowances, checked 2026-10-01 (docs/WEB_STAGE.md):

    serper       2,500 free credits, one-off
    dataforseo   $1.00 free credit, one-off; billed from each response's `cost`
    serpapi      250 free searches per calendar month
    postcodes.io free, unmetered (cached, not ledgered)

A second DataForSEO account (the friend's) has its own keys
(`DATAFORSEO_FRIEND_LOGIN` / `DATAFORSEO_FRIEND_PASSWORD`), its own ledger
entries under the provider name `dataforseo:friend`, and no default cap: a
run on it must state its allowance. Both accounts share one response cache,
so a result fetched on either never costs either account again.

The ledger only knows about calls made from this repository. After a top-up,
raise the limit with `limits={"serper": ...}` (or the stage's CLI flag) rather
than editing the ledger.

The response shapes parsed here were written from each provider's
documentation. The first real call of each kind is the check: its cached
response replaces the hand-written fixture in tests/.

Usage:
    python -m scripts.web.search_providers check        # keys set? both DataForSEO logins + live balances (free)
    python -m scripts.web.search_providers usage        # allowance used and left, from the ledger
    python -m scripts.web.search_providers categories   # Google's business categories (free)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

CACHE_DIR = Path("data/raw/search-providers")
LEDGER = Path("logs/web/provider-usage.jsonl")

SERPER_URL = "https://google.serper.dev/{endpoint}"
DATAFORSEO_URL = "https://api.dataforseo.com/v3/{endpoint}"
SERPAPI_URL = "https://serpapi.com/search.json"
POSTCODES_URL = "https://api.postcodes.io/postcodes/{postcode}"

COUNTRY = "uk"            # Google's gl code for the United Kingdom
IDENTITY_PROVIDERS = ("serper", "dataforseo")  # who answers maps_search, places_search and web_search
SERP_COST = 0.002         # DataForSEO live price of one results page (organic, Maps, Ads Transparency)
LABS_MAX_COST = 0.15      # pre-check bound for one Labs call; published prices disagree ($0.01 or $0.10 a task).
                          # Measured 2026-10-02: 20 domains cost $0.0144.
UK_LOCATION_CODE = 2826   # DataForSEO / Google Ads location code for the United Kingdom
TIMEOUT = 30
RETRIES = 3


@dataclass(frozen=True)
class Allowance:
    unit: str        # credits, usd or searches
    limit: float
    period: str      # once or month
    per_call: float  # the most one call can use; a call is refused if this would pass the limit


DATAFORSEO_ACCOUNTS = ("mine", "friend")
DATAFORSEO_PROVIDER = {"mine": "dataforseo", "friend": "dataforseo:friend"}
DATAFORSEO_KEYS = {"mine": ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
                   "friend": ("DATAFORSEO_FRIEND_LOGIN", "DATAFORSEO_FRIEND_PASSWORD")}

ALLOWANCES: dict[str, Allowance] = {
    "serper": Allowance("credits", 2500, "once", 1),
    "dataforseo": Allowance("usd", 1.00, "once", 0.09),
    # The friend's account has no default cap (0): every run states its allowance.
    "dataforseo:friend": Allowance("usd", 0.0, "once", 0.09),
    "serpapi": Allowance("searches", 250, "month", 1),
}


class ProviderError(RuntimeError):
    """A request failed after retries, or the provider reported an error.
    `cost` is what the provider charged for the failed call, if anything, so
    it still reaches the ledger."""

    def __init__(self, message: str, *, cost: float = 0.0) -> None:
        super().__init__(message)
        self.cost = cost


class CacheMiss(RuntimeError):
    """Cache-only mode met a request that would have been billed."""


class AllowanceExhausted(RuntimeError):
    """The next request would go past the provider's allowance."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- helpers

SECOND_LEVEL_SUFFIXES = {"co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "ac.uk", "gov.uk",
                         "nhs.uk", "com.au", "co.nz", "co.za", "com.br", "co.jp", "com.cn"}
UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


def registrable_domain(url: str | None) -> str | None:
    """`https://shop.example.co.uk/x` -> `example.co.uk`. No public-suffix
    library: the second-level suffixes that matter for UK companies are listed."""
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return None
    if not host or "." not in host or re.fullmatch(r"[\d.]+", host):
        return None
    labels = host.split(".")
    keep = 3 if ".".join(labels[-2:]) in SECOND_LEVEL_SUFFIXES and len(labels) >= 3 else 2
    return ".".join(labels[-keep:])


def normalise_postcode(text: str | None) -> str | None:
    """The first UK postcode in `text`, upper case with one space: `LS1 4AP`."""
    match = UK_POSTCODE.search(text or "")
    return f"{match.group(1).upper()} {match.group(2).upper()}" if match else None


def _request_key(endpoint: str, request: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps({"endpoint": endpoint, "request": request}, sort_keys=True).encode()).hexdigest()


def ledger_usage(provider: str, ledger: Path = LEDGER, *, now: datetime | None = None) -> float:
    """Allowance used so far: everything for a one-off allowance, the current
    calendar month for a monthly one."""
    allowance = ALLOWANCES.get(provider)
    if allowance is None or not ledger.exists():
        return 0.0
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    used = 0.0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        if record.get("provider") != provider:
            continue
        if allowance.period == "month" and not str(record.get("at", "")).startswith(month):
            continue
        used += float(record.get("used") or 0)
    return used


def normalise_listing(item: dict[str, Any], source: str) -> dict[str, Any]:
    """One Google Maps listing in a provider-neutral shape."""
    category = item.get("category") or item.get("type")  # Serper calls the category `type`
    categories = item.get("types") or item.get("additional_categories") or []
    if category and category not in categories:
        categories = [category, *categories]
    rating = item.get("rating")
    rating_count = item.get("ratingCount", item.get("rating_count"))
    if isinstance(rating, dict):  # DataForSEO shape: {"value": 4.6, "votes_count": 120}
        rating_count = rating.get("votes_count")
        rating = rating.get("value")
    website = item.get("website") or item.get("url")
    address = item.get("address")
    zip_code = (item.get("address_info") or {}).get("zip")
    return {
        "title": item.get("title"),
        "address": address,
        "postcode": normalise_postcode(address) or normalise_postcode(zip_code),
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
        "rating": rating,
        "rating_count": rating_count,
        "category": category,
        "categories": categories,
        "website": website,
        "domain": registrable_domain(website),
        "phone": item.get("phoneNumber") or item.get("phone"),
        "cid": str(item["cid"]) if item.get("cid") is not None else None,
        "place_id": item.get("placeId") or item.get("place_id"),
        "category_ids": item.get("category_ids"),
        "is_claimed": item.get("is_claimed"),
        "source": source,
    }


def _dataforseo_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for task in response.get("tasks") or [] for result in task.get("result") or []
            for item in result.get("items") or []]


def parse_dataforseo_maps(response: dict[str, Any]) -> list[dict[str, Any]]:
    # DataForSEO's `type` is the result kind (maps_search), not a category: drop it.
    return [normalise_listing({k: v for k, v in item.items() if k != "type"}, "dataforseo_maps")
            for item in _dataforseo_items(response) if item.get("type") == "maps_search"]


def parse_dataforseo_organic(response: dict[str, Any]) -> dict[str, Any]:
    """The same shape as `parse_serper_search`, so the resolver cannot tell the providers apart."""
    organic, graph = [], None
    for item in _dataforseo_items(response):
        if item.get("type") == "organic":
            organic.append({"position": item.get("rank_group"), "title": item.get("title"), "url": item.get("url"),
                            "domain": registrable_domain(item.get("url") or item.get("domain")),
                            "snippet": item.get("description")})
        elif item.get("type") == "knowledge_graph" and graph is None:
            website = item.get("url")
            graph = {"title": item.get("title"), "type": item.get("sub_title"), "website": website,
                     "domain": registrable_domain(website)}
    return {"organic": organic, "knowledge_graph": graph, "places": []}


def parse_dataforseo_ads_search(response: dict[str, Any]) -> dict[str, Any]:
    ads = [{key: item.get(key) for key in ("advertiser_id", "title", "verified", "format", "first_shown",
                                           "last_shown")}
           for item in _dataforseo_items(response) if item.get("type") == "ads_search"]
    return {"total_results": len(ads), "creatives": ads}


def parse_dataforseo_traffic(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Labs bulk traffic estimation: estimated monthly visits (etv) and the
    number of results the domain appears in, organic, paid and local pack."""
    rows = []
    for item in _dataforseo_items(response):
        metrics = item.get("metrics") or {}
        row: dict[str, Any] = {"target": item.get("target")}
        for kind in ("organic", "paid", "local_pack"):
            block = metrics.get(kind) or {}
            row[f"{kind}_etv"] = block.get("etv")
            row[f"{kind}_count"] = block.get("count")
        rows.append(row)
    return rows


def parse_serper_search(response: dict[str, Any]) -> dict[str, Any]:
    organic = [{"position": item.get("position"), "title": item.get("title"), "url": item.get("link"),
                "domain": registrable_domain(item.get("link")), "snippet": item.get("snippet")}
               for item in response.get("organic") or []]
    graph = response.get("knowledgeGraph") or {}
    knowledge_graph = {"title": graph.get("title"), "type": graph.get("type"), "website": graph.get("website"),
                       "domain": registrable_domain(graph.get("website"))} if graph else None
    places = [normalise_listing(item, "serper_search") for item in response.get("places") or []]
    return {"organic": organic, "knowledge_graph": knowledge_graph, "places": places}


def parse_dataforseo_keywords(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for task in response.get("tasks") or []:
        for item in task.get("result") or []:
            rows.append({key: item.get(key) for key in (
                "keyword", "search_volume", "cpc", "competition", "competition_index",
                "low_top_of_page_bid", "high_top_of_page_bid")})
    return rows


def parse_dataforseo_serp(response: dict[str, Any]) -> dict[str, Any]:
    paid: list[dict[str, Any]] = []
    organic: list[dict[str, Any]] = []
    local_pack: list[dict[str, Any]] = []
    for task in response.get("tasks") or []:
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                kind = item.get("type")
                row = {"rank": item.get("rank_absolute"), "title": item.get("title"), "url": item.get("url"),
                       "domain": registrable_domain(item.get("url") or item.get("domain"))}
                if kind == "paid":
                    paid.append(row)
                elif kind == "organic":
                    organic.append(row)
                elif kind == "local_pack":
                    local_pack.append({**row, "cid": item.get("cid")})
    return {"paid": paid, "organic": organic, "local_pack": local_pack}


def parse_serpapi_transparency(response: dict[str, Any]) -> dict[str, Any]:
    creatives = [{key: item.get(key) for key in (
        "advertiser_id", "advertiser", "target_domain", "format", "first_shown", "last_shown", "total_days_shown")}
        for item in response.get("ad_creatives") or []]
    info = response.get("search_information") or {}
    return {"total_results": info.get("total_results", len(creatives)), "creatives": creatives}


def parse_dataforseo_account(response: dict[str, Any]) -> dict[str, Any]:
    result = ((response.get("tasks") or [{}])[0].get("result") or [{}])[0]
    money = result.get("money") or {}
    return {"login": result.get("login"), "balance": money.get("balance"), "total_paid": money.get("total"),
            "day_limit": (money.get("limits") or {}).get("day")}


DATAFORSEO_OK = 20000
DATAFORSEO_NO_RESULTS = 40102  # "No Search Results": a real, empty answer (no ads, no listings), and it is billed


def _dataforseo_check(response: dict[str, Any]) -> None:
    for task in response.get("tasks") or []:
        if task.get("status_code") not in (DATAFORSEO_OK, DATAFORSEO_NO_RESULTS):
            raise ProviderError(f"dataforseo task failed: {task.get('status_code')} {task.get('status_message')}",
                                cost=float(response.get("cost") or 0))


# ---------------------------------------------------------------- client

class SearchClient:
    """One client for every provider. Keys are read from `.env` only when a
    request is actually sent, so a cache-only run needs none."""

    def __init__(self, *, cache_dir: Path = CACHE_DIR, ledger: Path = LEDGER, cache_only: bool = False,
                 limits: dict[str, float] | None = None, session: Any = None,
                 sleep: Callable[[float], None] = time.sleep, identity_provider: str = "serper",
                 dataforseo_account: str = "mine") -> None:
        if identity_provider not in IDENTITY_PROVIDERS:
            raise ValueError(f"identity_provider must be one of {IDENTITY_PROVIDERS}")
        if dataforseo_account not in DATAFORSEO_ACCOUNTS:
            raise ValueError(f"dataforseo_account must be one of {DATAFORSEO_ACCOUNTS}")
        self.identity_provider = identity_provider
        self.dataforseo_account_name = dataforseo_account
        # The provider name this client's DataForSEO spending is ledgered under.
        self.dataforseo_provider = DATAFORSEO_PROVIDER[dataforseo_account]
        self.cache_dir = cache_dir
        self.ledger = ledger
        self.cache_only = cache_only
        self.limits = {name: allowance.limit for name, allowance in ALLOWANCES.items()} | (limits or {})
        self.session = session or requests.Session()
        self.sleep = sleep
        self.billed: dict[str, float] = {}
        # Thread safety for parallel runs: a call reserves its worst-case cost
        # under the lock before it is sent and releases it once the ledger has
        # the real cost, so parallel workers cannot overshoot an allowance.
        self._lock = threading.Lock()
        self._ledger_lock = threading.Lock()
        self._reserved: dict[str, float] = {}
        self._thread = threading.local()
        load_dotenv(Path(".env"))

    # -- plumbing

    def _key(self, name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise ProviderError(f"{name} is not set in .env or the environment")
        return value

    def remaining(self, provider: str) -> float:
        return self.limits[provider] - ledger_usage(provider, self.ledger)

    def _call(self, provider: str, endpoint: str, request: dict[str, Any],
              send: Callable[[], tuple[dict[str, Any], float]], *, max_cost: float | None = None,
              cache_provider: str | None = None) -> dict[str, Any]:
        """`request` is what identifies the call for the cache; it never holds a key.
        `send` makes the HTTP request and returns (response JSON, allowance used).
        `max_cost` is the most this call can use, when it is less than the
        provider's worst case (a $0.002 search, not a $0.09 keyword task).
        `cache_provider` names the cache folder when it differs from the ledger
        name: both DataForSEO accounts read and write one shared cache."""
        key = _request_key(endpoint, request)
        path = self.cache_dir / (cache_provider or provider) / endpoint.replace("/", "_") / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        if self.cache_only:
            raise CacheMiss(f"{provider} {endpoint} is not cached: {json.dumps(request, sort_keys=True)[:200]}")
        allowance = ALLOWANCES.get(provider)
        worst = max_cost if max_cost is not None else (allowance.per_call if allowance else 0)
        if allowance is not None:
            with self._lock:
                left = self.remaining(provider) - self._reserved.get(provider, 0.0)
                if left < worst:
                    raise AllowanceExhausted(
                        f"{provider}: {left:g} {allowance.unit} left of {self.limits[provider]:g} (after calls in "
                        f"flight); the next call can use up to {worst:g}")
                self._reserved[provider] = self._reserved.get(provider, 0.0) + worst
        try:
            try:
                response, used = send()
            except ProviderError as exc:
                if exc.cost and allowance is not None:
                    self._ledger(provider, endpoint, key, exc.cost, allowance.unit, note="failed call, still billed")
                raise
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"provider": provider, "endpoint": endpoint, "request": request,
                                       "fetched_at": utc_now(), "used": used, "response": response},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            if allowance is not None:
                self._ledger(provider, endpoint, key, used, allowance.unit)
            return response
        finally:
            if allowance is not None:
                with self._lock:
                    self._reserved[provider] = self._reserved.get(provider, 0.0) - worst

    def _ledger(self, provider: str, endpoint: str, key: str, used: float, unit: str, *, note: str | None = None) -> None:
        record = {"at": utc_now(), "provider": provider, "endpoint": endpoint, "key": key, "used": used, "unit": unit}
        if note:
            record["note"] = note
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_lock:
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.billed[provider] = self.billed.get(provider, 0.0) + used
        mine = getattr(self._thread, "billed", None)
        if mine is None:
            mine = self._thread.billed = {}
        mine[provider] = mine.get(provider, 0.0) + used

    def thread_billed(self) -> dict[str, float]:
        """What this thread has spent: per-company accounting in a parallel run."""
        return dict(getattr(self._thread, "billed", None) or {})

    def _http(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                last = exc
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise ProviderError(f"{method} {urlparse(url).netloc}: HTTP {response.status_code} "
                                        f"{response.text[:200]}")
                last = ProviderError(f"HTTP {response.status_code}")
            self.sleep(2 ** attempt)
        raise ProviderError(f"{method} {urlparse(url).netloc} failed after {RETRIES} attempts: {last}")

    # -- Serper: Google Maps listings and organic results

    def _serper(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        def send() -> tuple[dict[str, Any], float]:
            response = self._http("POST", SERPER_URL.format(endpoint=endpoint), json=body,
                                  headers={"X-API-KEY": self._key("SERPER_API_KEY")})
            return response, float(response.get("credits") or 1)
        # A Maps search costs 3 credits; reserving 3 keeps parallel runs under the cap.
        return self._call("serper", endpoint, body, send, max_cost=3.0 if endpoint == "maps" else None)

    def maps_search(self, query: str, *, lat: float, lng: float, zoom: int = 14) -> list[dict[str, Any]]:
        """Google Maps listings near a point: category, website, rating, review count."""
        if self.identity_provider == "dataforseo":
            task = {"keyword": query, "location_coordinate": f"{lat:.7f},{lng:.7f},{zoom}z", "language_code": "en",
                    "depth": 20}
            return parse_dataforseo_maps(self._dataforseo("serp/google/maps/live/advanced", task, max_cost=SERP_COST))
        body = {"q": query, "ll": f"@{lat:.6f},{lng:.6f},{zoom}z", "gl": COUNTRY, "hl": "en"}
        return [normalise_listing(item, "serper_maps") for item in self._serper("maps", body).get("places") or []]

    def places_search(self, query: str, *, location: str = "United Kingdom") -> list[dict[str, Any]]:
        """Google listings for a query anywhere in the UK: the fallback when nothing local matches."""
        if self.identity_provider == "dataforseo":
            task = {"keyword": query, "location_code": UK_LOCATION_CODE, "language_code": "en", "depth": 20}
            return parse_dataforseo_maps(self._dataforseo("serp/google/maps/live/advanced", task, max_cost=SERP_COST))
        body = {"q": query, "location": location, "gl": COUNTRY, "hl": "en"}
        return [normalise_listing(item, "serper_places") for item in self._serper("places", body).get("places") or []]

    def web_search(self, query: str, *, num: int = 10) -> dict[str, Any]:
        """Organic results, the knowledge panel, any places block."""
        if self.identity_provider == "dataforseo":
            task = {"keyword": query, "location_code": UK_LOCATION_CODE, "language_code": "en", "device": "desktop",
                    "depth": num}
            return parse_dataforseo_organic(self._dataforseo("serp/google/organic/live/advanced", task,
                                                             max_cost=SERP_COST))
        body = {"q": query, "gl": COUNTRY, "hl": "en", "num": num}
        return parse_serper_search(self._serper("search", body))

    # -- DataForSEO: keyword volume and live results with the paid ads

    def _dataforseo_auth(self) -> tuple[str, str]:
        login, password = DATAFORSEO_KEYS[self.dataforseo_account_name]
        return self._key(login), self._key(password)

    def _dataforseo(self, endpoint: str, task: dict[str, Any], *, max_cost: float | None = None) -> dict[str, Any]:
        def send() -> tuple[dict[str, Any], float]:
            response = self._http("POST", DATAFORSEO_URL.format(endpoint=endpoint), json=[task],
                                  auth=self._dataforseo_auth())
            _dataforseo_check(response)
            return response, float(response.get("cost") or 0)
        return self._call(self.dataforseo_provider, endpoint, task, send, max_cost=max_cost,
                          cache_provider="dataforseo")

    def keyword_volume(self, keywords: list[str], *, location_code: int = UK_LOCATION_CODE) -> list[dict[str, Any]]:
        """Monthly searches, cost per click and competition, up to 1,000 keywords per call."""
        if len(keywords) > 1000:
            raise ValueError("keyword_volume takes at most 1,000 keywords per call")
        task = {"keywords": sorted(set(keywords)), "location_code": location_code, "language_code": "en"}
        return parse_dataforseo_keywords(self._dataforseo("keywords_data/google_ads/search_volume/live", task))

    def web_search_with_ads(self, keyword: str, *, lat: float, lng: float, radius: int = 10000) -> dict[str, Any]:
        """One live Google results page seen from a point, with the paid ads.
        `radius` is the location's accuracy in millimetres, as Google encodes it
        (DataForSEO accepts 199 to 199,999), not a search area."""
        task = {"keyword": keyword, "location_coordinate": f"{lat:.6f},{lng:.6f},{radius}",
                "language_code": "en", "device": "desktop", "depth": 10}
        return parse_dataforseo_serp(self._dataforseo("serp/google/organic/live/advanced", task, max_cost=SERP_COST))

    def ads_search(self, domain: str) -> dict[str, Any]:
        """Google Ads Transparency Center via DataForSEO: the search ads Google
        has shown in the UK for an advertiser linked to `domain`, with first and
        last shown dates. Up to 40 ads per call."""
        task = {"target": domain, "location_code": UK_LOCATION_CODE, "platform": "google_search", "depth": 40}
        return parse_dataforseo_ads_search(self._dataforseo("serp/google/ads_search/live/advanced", task,
                                                            max_cost=SERP_COST))

    def bulk_traffic(self, targets: list[str]) -> list[dict[str, Any]]:
        """DataForSEO Labs estimate of each domain's monthly UK organic search
        traffic and map-pack presence, up to 1,000 domains in one call. Modelled
        from the keywords each domain is seen ranking for: a rough guide, not
        analytics. Paid traffic is not requested: on the 23-company test Labs
        found paid traffic for 3 of 11 real advertisers (Ads Transparency is the
        advertising signal)."""
        if len(targets) > 1000:
            raise ValueError("bulk_traffic takes at most 1,000 domains per call")
        task = {"targets": sorted(set(targets)), "location_code": UK_LOCATION_CODE, "language_code": "en",
                "item_types": ["organic", "local_pack"]}
        return parse_dataforseo_traffic(self._dataforseo("dataforseo_labs/google/bulk_traffic_estimation/live", task,
                                                         max_cost=LABS_MAX_COST))

    def _dataforseo_free(self, endpoint: str) -> dict[str, Any]:
        """A GET to an endpoint DataForSEO does not charge for: never ledgered."""
        response = self._http("GET", DATAFORSEO_URL.format(endpoint=endpoint), auth=self._dataforseo_auth())
        _dataforseo_check(response)
        return response

    def dataforseo_account(self) -> dict[str, Any]:
        """Login and live balance (`appendix/user_data`, free). Never cached: the
        point is to see the balance now."""
        if self.cache_only:
            raise CacheMiss("dataforseo account check is never cached")
        return parse_dataforseo_account(self._dataforseo_free("appendix/user_data"))

    def business_categories(self) -> list[dict[str, Any]]:
        """Google's business categories (about 5,000, e.g. `dental_clinic`) with
        how many listings use each (`business_listings/categories`, free).
        Cached: the list changes rarely. W3 classifies companies onto it."""
        path = self.cache_dir / "dataforseo" / "business_listings_categories.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        if self.cache_only:
            raise CacheMiss("dataforseo business categories are not cached")
        response = self._dataforseo_free("business_data/business_listings/categories")
        categories = [{"category_name": item.get("category_name"), "business_count": item.get("business_count")}
                      for task in response.get("tasks") or [] for item in task.get("result") or []]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": utc_now(), "response": categories}), encoding="utf-8")
        return categories

    # -- SerpApi: Google Ads Transparency Center

    def ads_transparency(self, domain: str, *, region: str = str(UK_LOCATION_CODE)) -> dict[str, Any]:
        """Ads Google has shown for a domain in a region: count, advertiser, first and last shown."""
        params = {"engine": "google_ads_transparency_center", "text": domain, "region": region}

        def send() -> tuple[dict[str, Any], float]:
            response = self._http("GET", SERPAPI_URL, params={**params, "api_key": self._key("SERPAPI_API_KEY")})
            if response.get("error"):
                raise ProviderError(f"serpapi: {response['error']}")
            return response, 1.0
        return parse_serpapi_transparency(self._call("serpapi", "google_ads_transparency_center", params, send))

    # -- postcodes.io: free postcode -> coordinates

    def postcode_location(self, postcode: str) -> dict[str, Any] | None:
        """Latitude, longitude and district for a UK postcode; None if unknown.
        Free and unmetered, but cached like the rest so a re-run is offline."""
        postcode = normalise_postcode(postcode) or postcode.strip().upper()
        request = {"postcode": postcode}
        key = _request_key("postcodes", request)
        path = self.cache_dir / "postcodes_io" / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        if self.cache_only:
            raise CacheMiss(f"postcodes.io {postcode} is not cached")
        try:
            response = self.session.request("GET", POSTCODES_URL.format(postcode=postcode.replace(" ", "")),
                                            timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"postcodes.io: {exc}") from exc
        if response.status_code == 404:
            result = None
        elif response.status_code == 200:
            data = response.json().get("result") or {}
            result = {"postcode": data.get("postcode"), "latitude": data.get("latitude"),
                      "longitude": data.get("longitude"), "district": data.get("admin_district")}
        else:
            raise ProviderError(f"postcodes.io: HTTP {response.status_code}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"request": request, "fetched_at": utc_now(), "response": result}), encoding="utf-8")
        return result


def usage_report(ledger: Path = LEDGER, limits: dict[str, float] | None = None) -> list[dict[str, Any]]:
    limits = {name: allowance.limit for name, allowance in ALLOWANCES.items()} | (limits or {})
    return [{"provider": name, "unit": allowance.unit, "period": allowance.period,
             "used": round(ledger_usage(name, ledger), 4), "limit": limits[name],
             "left": round(limits[name] - ledger_usage(name, ledger), 4)}
            for name, allowance in ALLOWANCES.items()]


PROVIDER_KEYS = {"serper": ("SERPER_API_KEY",), "dataforseo": DATAFORSEO_KEYS["mine"],
                 "dataforseo:friend": DATAFORSEO_KEYS["friend"], "serpapi": ("SERPAPI_API_KEY",)}


def check(client: SearchClient, out: Callable[[str], None] = print,
          client_for: Callable[[str], SearchClient] | None = None) -> bool:
    """Which keys are set, and a free live check of each DataForSEO login and
    balance. Spends nothing. True when every key that is set works.
    `client_for(account)` builds the client for an account; by default the
    given client's own settings are reused for both."""
    ok = True
    for provider, names in PROVIDER_KEYS.items():
        missing = [name for name in names if not os.getenv(name)]
        out(f"{provider:<18} {'keys set' if not missing else 'missing ' + ', '.join(missing)}")
    for account in DATAFORSEO_ACCOUNTS:
        if not all(os.getenv(name) for name in DATAFORSEO_KEYS[account]):
            continue
        account_client = client_for(account) if client_for else SearchClient(
            cache_dir=client.cache_dir, ledger=client.ledger, session=client.session, sleep=client.sleep,
            dataforseo_account=account)
        provider = account_client.dataforseo_provider
        try:
            info = account_client.dataforseo_account()
        except ProviderError as exc:
            ok = False
            out(f"{provider:<18} login failed: {exc}")
            out("                   The password is the API password from the dashboard's API Access page, "
                "not the account password.")
            continue
        left = account_client.remaining(provider)
        out(f"{provider:<18} login ok ({info['login']}); live balance ${info['balance']}, "
            f"ledger allows ${left:g} more")
        if info["balance"] is not None and info["balance"] < left:
            out("                   the live balance is below the ledger's allowance: the API will refuse "
                "calls once the balance runs out")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("usage", help="Allowance used and left per provider, from the usage ledger.")
    sub.add_parser("check", help="Which keys are set; free live login and balance check for DataForSEO.")
    sub.add_parser("categories", help="Download Google's business-category list from DataForSEO (free).")
    args = parser.parse_args(argv)
    if args.command == "usage":
        for row in usage_report():
            print(f"{row['provider']:<18} {row['used']:>9g} of {row['limit']:g} {row['unit']} used "
                  f"({row['period']}), {row['left']:g} left")
    elif args.command == "check":
        return 0 if check(SearchClient()) else 1
    elif args.command == "categories":
        categories = SearchClient().business_categories()
        print(f"{len(categories)} categories; the most used:")
        for item in sorted(categories, key=lambda c: -(c.get("business_count") or 0))[:10]:
            print(f"  {item['category_name']:<30} {item.get('business_count') or 0:>12,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
