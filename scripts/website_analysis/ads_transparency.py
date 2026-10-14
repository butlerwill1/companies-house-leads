#!/usr/bin/env python3
"""On-demand Google Ads Transparency Centre check, without a paid provider.

This is the free alternative to W4's DataForSEO `ads_search`, which needs a
$50 account deposit before a single call. Google's own Ads Transparency
Centre at adstransparency.google.com publishes the same data, and the site is
a JavaScript app over a keyless internal RPC: the endpoints answer an
anonymous POST, no login and no API key. Two calls answer "does this company
run Google Ads?":

- `SearchService/SearchSuggestions` resolves a domain or brand to advertiser
  IDs (`AR...`) with name, region and a verified flag;
- `SearchService/SearchCreatives` lists the creatives: creative IDs (`CR...`),
  format, first/last shown dates, and a pagination cursor.

The payloads are protobuf rendered as JSON, so every key is a field number.
Request shapes originated in public reverse-engineering write-ups; creative
timestamps, verification and format now use saved live responses, checked
against the browser on 13-14 October 2026. Two traps matter:

- SearchCreatives refuses nothing when member `"7"` is missing: it answers
  `{}`, which is indistinguishable from "this advertiser has no ads". `"7"`
  is always set (see `creatives_payload`).
- Region filtering is disputed (one write-up measured Google ignoring it
  server-side), so it is optional and advisory. Without it every ad the
  advertiser has anywhere is returned, which checks nothing less.

The web stage's rules apply: this project is named in every request,
requests are paced within a client, robots.txt is honoured, and responses are cached under
`data/raw/ads-transparency-cache/` so a re-run makes no requests, and a block
(HTTP 429, a redirect to Google's `/sorry/` gate) is recorded as `blocked`
and never retried through a disguised identity or another route. Google
may impose quotas; a run paces itself and stops at its request budget.

An outbound proxy can be set for a run without changing the code:

- `TRANSPARENCY_PROXY_URL` (`http://user:pass@host:port`), or
- `OXYLABS_USERNAME` + `OXYLABS_PASSWORD` (Oxylabs, `pr.oxylabs.io:7777`
  unless `OXYLABS_PROXY_HOST` / `OXYLABS_PROXY_PORT` say otherwise).

Credentials are read when constructing the client and are never cached or logged.

Usage:
    python -m scripts.website_analysis.ads_transparency check --domain example.co.uk
    python -m scripts.website_analysis.ads_transparency check --company 12345678 \
        --db companies-house.db      # domain and name from the web-stage store
    python -m scripts.website_analysis.ads_transparency check --domain example.co.uk --cache-only
    python -m scripts.website_analysis.ads_transparency check --domain example.co.uk --refresh

Each `check` prints a JSON summary with complete/partial/failed/blocked status.
The W4 ads fields are retained, but unknown count/activity/verification is null.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.robotparser import RobotFileParser

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.website_analysis.search_providers import CacheMiss  # noqa: E402
from scripts.website_analysis.search_providers import registrable_domain, utc_now  # noqa: E402
from scripts.website_analysis.web_fetch import USER_AGENT  # noqa: E402
from scripts.website_analysis.web_identity import names_similar  # noqa: E402
from scripts.website_analysis.web_market import ads_summary  # noqa: E402  # W4's exact summary shape
from scripts.website_analysis.proxy_adapters import adapter_from_env  # noqa: E402

RPC_BASE = "https://adstransparency.google.com/anji/_/rpc/"
SEARCH_SUGGESTIONS = "SearchService/SearchSuggestions"
SEARCH_CREATIVES = "SearchService/SearchCreatives"
ADVERTISER_HOST = "adstransparency.google.com"
CACHE_DIR = Path("data/raw/ads-transparency-cache")
PAGE_SIZE = 40          # what the site itself asks for
DEFAULT_MAX_ADS = 120   # an on-demand single-company check does not need the whole library
REQUEST_BUDGET = 12     # calls one run may make; Google quota's per-IP burst is the reason to stay low
PAUSE_SECONDS = 0.5
TIMEOUT = 30
DATABASE_PATH = Path("companies-house.db")

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
ADVERTISER_ID_RE = re.compile(r"\bAR\d{10,}\b")
CREATIVE_ID_RE = re.compile(r"\bCR\d{10,}\b")
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
MIN_SECONDS = 946_684_800      # 2000-01-01: below this an integer is not a timestamp
MAX_SECONDS = 4_102_444_800    # 2100-01-01

_FORMAT_HINTS = (("youtube.com", "VIDEO"), ("videoplayback", "VIDEO"),
                 ("simgad", "IMAGE"), ("/pagead/", "IMAGE"))

ISO_NUMERIC = {
    "GB": 826, "IE": 372, "US": 840, "DE": 276, "FR": 250, "NL": 528, "ES": 724,
    "IT": 380, "AU": 36, "NZ": 554, "CA": 124, "SE": 752, "DK": 208, "NO": 578,
}


class Blocked(RuntimeError):
    """Google refused, rate-limited or challenged a request. Recorded by each
    check as `blocked=True`, never worked around."""


class ProviderDown(RuntimeError):
    """The surface failed outright (an HTTP error other than a block, or an
    unreachable network): a failure of the service, not a block."""


class BudgetReached(ProviderDown):
    """The local request cap ended a lookup; Google did not block it."""


# ---------------------------------------------------------------- payloads

def creatives_payload(*, query: str | None = None, advertiser_ids: list[str] | None = None,
                      region_codes: list[int] | None = None, cursor: str | int | None = None,
                      page_size: int = PAGE_SIZE) -> dict[str, Any]:
    """The SearchCreatives request. Member `"7"` is mandatory: with it absent
    the endpoint answers `{}` for any advertiser, which reads exactly like
    "no ads". The keys are protobuf field numbers: 2 page size, 4 pagination
    cursor, 3.12 free-text/domain query, 3.13 advertiser IDs, 3.8 regions."""
    member: dict[str, Any] = {}
    if query:
        member["12"] = {"1": query, "2": True}
    if advertiser_ids:
        member["13"] = {"1": sorted(set(advertiser_ids))}
    if region_codes:
        member["8"] = region_codes
    payload: dict[str, Any] = {"2": page_size, "3": member, "7": {"1": 1, "2": 0}}
    if cursor is not None:
        payload["4"] = cursor
    return payload


def suggestions_payload(query: str, *, limit: int = 10) -> dict[str, Any]:
    """The SearchSuggestions request. The counts are sent as integers: an
    object here is rejected with a BadRequestException (a public write-up's
    trap note, kept true as long as it costs nothing to keep)."""
    return {"1": query, "2": limit, "3": limit}


def region_codes(alpha2: list[str] | None) -> list[int] | None:
    """`["GB"]` -> `[2826]`: the site's enum is 2000 plus the ISO 3166-1
    numeric code. Advisory only (module docstring, "disputed")."""
    if not alpha2:
        return None
    out = []
    for code in alpha2:
        upper = code.upper()
        if upper not in ISO_NUMERIC:
            raise ValueError(f"no ISO numeric code for region {code!r}")
        out.append(2000 + ISO_NUMERIC[upper])
    return out


# ---------------------------------------------------------------- parsing

def _strings(node: Any) -> list[str]:
    """Every string in the numbered response, in document order."""
    out: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop(0)
        if isinstance(current, str):
            out.append(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return out


def _numbers(node: Any) -> list[int]:
    out: list[int] = []
    stack = [node]
    while stack:
        current = stack.pop(0)
        if isinstance(current, bool):
            continue
        if isinstance(current, int):
            out.append(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return out


def _first(pattern: re.Pattern[str], strings: list[str]) -> str | None:
    for value in strings:
        if match := pattern.search(value):
            return match.group(0)
    return None


def _dates(item: Any) -> tuple[str | None, str | None]:
    """Read live fields 6/7 as protobuf Timestamps. Retain legacy fixture
    support for ISO dates and integer timestamps outside the live row shape."""
    # Live protobuf Timestamp: field 1 is seconds (usually a string),
    # field 2 is fractional nanoseconds, never another Unix timestamp.
    if isinstance(item, dict) and ADVERTISER_ID_RE.fullmatch(str(item.get("1", ""))):
        def day(field: str) -> str | None:
            stamp = item.get(field)
            value = stamp.get("1") if isinstance(stamp, dict) else None
            try:
                seconds = int(value) if not isinstance(value, bool) else 0
            except (TypeError, ValueError):
                return None
            if MIN_SECONDS <= seconds <= MAX_SECONDS:
                return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
            return None
        return day("6"), day("7")
    day_strings = sorted(set(DATE_RE.findall(" ".join(_strings(item)))))
    if day_strings:
        return day_strings[0], day_strings[-1]
    values: list[int] = []
    for n in _numbers(item):
        if MIN_SECONDS <= n <= MAX_SECONDS:
            values.append(n)
        elif n >= MIN_SECONDS * 1000 and n // 1000 <= MAX_SECONDS:
            values.append(n // 1000)
    if values:
        stamps = [datetime.fromtimestamp(n, timezone.utc).date().isoformat() for n in sorted(set(values))]
        return stamps[0], stamps[-1]
    return None, None


def parse_suggestions(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Advertiser candidates from SearchSuggestions. Documented item shape:
    `{"1": {"1": name, "2": AR id, "3": region, "5": verified, ...}}`; the
    walk accepts these fields wherever they appear together (the write-ups
    disagree about the wrapper), and keeps an advertised domain in the item's
    own strings (`domain`), which is how a suggestion reads as the company."""
    advertisers: list[dict[str, Any]] = []
    stack, seen = [response], set()
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            name, ident, region = node.get("1"), node.get("2"), node.get("3")
            if isinstance(name, str) and isinstance(ident, str) and ADVERTISER_ID_RE.fullmatch(ident):
                if ident not in seen:
                    seen.add(ident)
                    outer = _strings(node)
                    domain = _first(DOMAIN_RE, [s for s in outer if s != name])
                    advertisers.append({"name": name, "advertiser_id": ident,
                                        "region": region if isinstance(region, str) else None,
                                        "verified": node.get("5") if isinstance(node.get("5"), bool) else None,
                                        "domain": domain})
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return advertisers


def parse_creatives(response: dict[str, Any]) -> dict[str, Any]:
    """Parse live creative rows and pagination. Unknown rows fail explicitly;
    recognised IDs with unreadable dates are retained as incomplete evidence."""
    # Confirmed against live zero-ad browser searches (14 October): protobuf
    # JSON omits all fields for an empty result. This is valid only for the
    # request shape above, including mandatory member 7. Other shapes fail.
    if response == {}:
        return {"creatives": [], "next_cursor": None, "total_range": (0, 0)}
    if not isinstance(response, dict) or not isinstance(response.get("1"), list):
        raise ProviderDown("unrecognised SearchCreatives response: missing creative list")
    creatives = []
    for row in response.get("1") or []:
        if not isinstance(row, dict):
            raise ProviderDown("unrecognised creative row")
        strings = _strings(row)
        live = bool(ADVERTISER_ID_RE.fullmatch(str(row.get("1", ""))))
        if not _first(CREATIVE_ID_RE, strings):
            raise ProviderDown("unrecognised creative row: missing creative ID")
        parsed = {"creative_id": _first(CREATIVE_ID_RE, strings),
                  "advertiser_id": _first(ADVERTISER_ID_RE, strings),
                  "format": {1: "TEXT", 2: "IMAGE", 3: "VIDEO"}.get(row.get("4")) if live else _format(strings),
                  "first_shown": None, "last_shown": None, "dates_read": False}
        first, last = _dates(row)
        parsed.update(first_shown=first, last_shown=last, dates_read=bool(last))
        if live:
            preview = row.get("3") or {}
            parsed.update(title=row.get("12"), domain=row.get("14"),
                          verified=preview.get("5") if isinstance(preview, dict) else None)
        creatives.append(parsed)
    cursor = response.get("2")
    try:
        total = (int(response["4"]), int(response["5"]))
    except (KeyError, TypeError, ValueError):
        total = None
    return {"creatives": creatives,
            "next_cursor": cursor if isinstance(cursor, (str, int)) else None,
            "total_range": total}


def _format(strings: list[str]) -> str | None:
    """Format read from what the row actually shows: a video preview URL or an
    image asset URL. Text ads show neither, and a row with neither preview
    nor an explicit format field stays None rather than guessed as text."""
    for value in strings:
        for hint, name in _FORMAT_HINTS:
            if hint in value:
                return name
    return None


# ---------------------------------------------------------------- client

def proxy_config(env: dict[str, str] | None = None) -> dict[str, str] | None:
    """The outbound proxy for a run, from the environment only: a
    `TRANSPARENCY_PROXY_URL`, or Oxylabs credentials. None when neither is
    set, which is the normal case (the requests leave from this IP)."""
    env = os.environ if env is None else env
    url = env.get("TRANSPARENCY_PROXY_URL")
    if url:
        return {"http": url, "https": url}
    provider = env.get("TRANSPARENCY_PROXY_PROVIDER")
    if provider:
        return adapter_from_env(provider, env).request_options()["proxies"] or None
    username, password = env.get("OXYLABS_USERNAME"), env.get("OXYLABS_PASSWORD")
    if username and password:
        return adapter_from_env("oxylabs", env).request_options()["proxies"]
    return None


@dataclass
class RequestBudget:
    """How many RPC calls one run may make. Google defends the surface with a
    per-IP burst quota and a run stops cleanly when the budget is spent."""
    limit: int
    used: int = 0


class TransparencyClient:
    """The polite RPC caller. Reads credentials when constructed and
    never carries them into a cache file."""

    def __init__(self, *, cache_dir: Path = CACHE_DIR, session: Any = None, cache_only: bool = False,
                 sleep: Callable[[float], None] = time.sleep, pause_seconds: float = PAUSE_SECONDS,
                 proxy: dict[str, str] | None = None, budget: RequestBudget | None = None,
                 respect_robots: bool = True, refresh: bool = False) -> None:
        self.cache_dir = cache_dir
        self.cache_only = cache_only
        self.refresh = refresh
        self.sleep = sleep
        self.pause_seconds = pause_seconds
        self.proxy = proxy if proxy is not None else proxy_config()
        provider = os.environ.get("TRANSPARENCY_PROXY_PROVIDER")
        self.verify = (adapter_from_env(provider).request_options()["verify"]
                       if proxy is None and provider and not os.environ.get("TRANSPARENCY_PROXY_URL") else True)
        self.budget = budget
        self.respect_robots = respect_robots
        self._session = session or requests.Session()
        self._last_hit: float | None = None
        self._robots: bool | None = None
        self.requests_made = 0
        self.partial_creatives: list[dict[str, Any]] = []
        self.creatives_complete = False
        self.stop_reason: str | None = None

    # -- politeness

    def _pause(self) -> None:
        if self._last_hit is not None:
            wait = self.pause_seconds - (time.monotonic() - self._last_hit)
            if wait > 0:
                self.sleep(wait)
        self._last_hit = time.monotonic()

    def _allowed_by_robots(self) -> bool:
        if not self.respect_robots:
            return True
        if self._robots is None:
            self._robots = self._robots_allows()
        return self._robots

    def _robots_allows(self) -> bool:
        """Honour robots rules. A missing file is allowed; a failed fetch
        is a failed lookup, and a block/challenge stops all live requests."""
        try:
            response = self._session.get(f"https://{ADVERTISER_HOST}/robots.txt", timeout=TIMEOUT,
                                         headers={"User-Agent": USER_AGENT}, proxies=self.proxy,
                                         verify=self.verify,
                                         allow_redirects=False)
        except requests.RequestException as exc:
            raise ProviderDown(f"robots lookup failed: {type(exc).__name__}") from exc
        self._check_block(response)
        if response.status_code == 404:
            return True
        if response.status_code != 200:
            raise ProviderDown(f"robots HTTP {response.status_code}")
        robots = RobotFileParser()
        robots.parse(response.text.splitlines())
        return all(robots.can_fetch("companies-house-leads-research", RPC_BASE + endpoint)
                   for endpoint in (SEARCH_SUGGESTIONS, SEARCH_CREATIVES))

    def _check_block(self, response: Any) -> None:
        text = (response.text or "")[:2000].lower()
        location = response.headers.get("Location", "")
        if response.status_code in (403, 429) or "/sorry/" in str(getattr(response, "url", "")) + location + text:
            self.stop_reason = f"Google block/challenge: HTTP {response.status_code} (sorry gate if present)"
        elif any(marker in text for marker in ("recaptcha", "unusual traffic")):
            self.stop_reason = "Google challenge response"
        if self.stop_reason:
            raise Blocked(self.stop_reason)

    # -- cache

    def _cache_path(self, endpoint: str, payload: dict[str, Any]) -> Path:
        key = json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True)
        name = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / endpoint.replace("/", "_") / f"{name}.json"

    def _write_cache(self, path: Path, endpoint: str, payload: dict[str, Any], body: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"endpoint": endpoint, "request": payload,
                                    "fetched_at": utc_now(), "response": body}, ensure_ascii=False),
                        encoding="utf-8")

    # -- the call

    def _rpc(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One RPC call, cached in whole. A cache-only run answers from files
        and never sends."""
        path = self._cache_path(endpoint, payload)
        if path.exists() and not self.refresh:
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        if self.cache_only:
            raise CacheMiss(f"ads transparency {endpoint} is not cached: "
                            f"{json.dumps(payload, sort_keys=True)[:200]}")
        if self.stop_reason:
            raise Blocked(self.stop_reason)
        if not self._allowed_by_robots():
            raise Blocked("disallowed by robots.txt")
        if self.budget is not None:
            if self.budget.used >= self.budget.limit:
                raise BudgetReached(f"request budget of {self.budget.limit} reached for this run")
            self.budget.used += 1
        self._pause()
        self.requests_made += 1
        try:
            response = self._send(endpoint, payload)
        except requests.RequestException as exc:
            raise ProviderDown(f"transport error: {type(exc).__name__}") from exc
        self._check_block(response)
        text_head = (response.text or "")[:500] if response.status_code != 200 else ""
        if response.status_code != 200:
            raise ProviderDown(f"HTTP {response.status_code} {text_head[:200]}".strip())
        ctype = response.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self.stop_reason = "unexpected non-JSON RPC response: possible challenge"
            raise Blocked(f"unexpected response of content type {ctype[:80] or 'none'}: the surface looks "
                          "challenged, not answering")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderDown("malformed JSON response") from exc
        if not isinstance(body, dict):
            raise ProviderDown("unrecognised RPC response type")
        if isinstance(body, dict) and body.get("error"):
            raise ProviderDown(f"service error: {json.dumps(body['error'])[:200]}")
        self._write_cache(path, endpoint, payload, body)
        return body

    def _send(self, endpoint: str, payload: dict[str, Any]) -> requests.Response:
        body = {"f.req": json.dumps(payload, separators=(",", ":"))}
        return self._session.post(f"{RPC_BASE}{endpoint}?authuser=", data=body,
                                  headers={"User-Agent": USER_AGENT,
                                           "Content-Type": "application/x-www-form-urlencoded"},
                                  timeout=TIMEOUT, proxies=self.proxy, verify=self.verify, allow_redirects=False)

    # -- the two stage calls

    def search_suggestions(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        body = self._rpc(SEARCH_SUGGESTIONS, suggestions_payload(query, limit=limit))
        # Protobuf JSON omits empty repeated fields. Suggestions are optional
        # discovery evidence: an empty suggestion is not a negative ads result.
        if body and not isinstance(body.get("1"), list):
            raise ProviderDown("unrecognised SearchSuggestions response")
        return parse_suggestions(body)

    def search_creatives(self, *, query: str | None = None, advertiser_ids: list[str] | None = None,
                         region_alpha2: list[str] | None = None,
                         max_ads: int = DEFAULT_MAX_ADS) -> list[dict[str, Any]]:
        """Every creative the surface returns, up to `max_ads`, paging on the
        cursor (each page is itself paused and cached like any other call)."""
        if max_ads < 1:
            raise ValueError("max_ads must be positive")
        found: dict[str, dict[str, Any]] = {}
        self.partial_creatives = []
        self.creatives_complete = False
        cursor: str | int | None = None
        seen_cursors: set[Any] = set()
        while len(found) < max_ads:
            batch = parse_creatives(self._rpc(
                SEARCH_CREATIVES, creatives_payload(query=query, advertiser_ids=advertiser_ids,
                                                    region_codes=region_codes(region_alpha2), cursor=cursor)))
            for row in batch["creatives"]:
                key = row.get("creative_id") or json.dumps(sorted(row.items()), sort_keys=True)
                found.setdefault(key, row)
            self.partial_creatives = list(found.values())[:max_ads]
            cursor = batch["next_cursor"]
            if cursor is None:
                self.creatives_complete = len(found) <= max_ads
                break
            if cursor in seen_cursors:
                raise ProviderDown("repeated pagination cursor; lookup incomplete")
            seen_cursors.add(cursor)
        return list(found.values())[:max_ads]


# ---------------------------------------------------------------- the check

def _match_suggestions(suggestions: list[dict[str, Any]], *, domain: str, name: str | None) -> bool:
    """Does any suggestion read as this company? The suggestion's own
    advertised domain first (Google shows it next to the name), the company's
    name through `names_similar` as the stand-in when the domain does not
    appear."""
    for suggestion in suggestions:
        if suggestion.get("domain") and registrable_domain(suggestion["domain"]) == domain:
            return True
    return bool(name) and any(names_similar(s.get("name") or "", name) for s in suggestions)


def check(client: TransparencyClient, *, domain: str, name: str | None = None,
          alpha2: list[str] | None = None, max_ads: int = DEFAULT_MAX_ADS,
          now: datetime | None = None, domain_only: bool = False) -> dict[str, Any]:
    """One company or domain, with W4 ads fields and explicit completeness.
    Unknown count/activity/verification is null rather than a negative finding."""
    now = now or datetime.now(timezone.utc)
    client.partial_creatives = []
    client.creatives_complete = False
    domain = registrable_domain(domain) or domain
    result: dict[str, Any] = {"domain": domain, "checked_at": now.isoformat(), "regions": alpha2,
                              "suggestions": [], "creatives": [], "blocked": False, "error": None,
                              "found": None, "status": "failed", "complete": False,
                              "incomplete_reason": None}
    try:
        suggestions = [] if domain_only else client.search_suggestions(domain)
        if not domain_only and not suggestions and name:
            suggestions = client.search_suggestions(name)
        matched = _match_suggestions(suggestions, domain=domain, name=name)
        result.update(suggestions=suggestions, found=matched)
        creatives = client.search_creatives(query=domain, region_alpha2=alpha2, max_ads=max_ads)
        matching = [s for s in suggestions if _match_suggestions([s], domain=domain, name=name)]
        if not creatives and matching:
            ids = [s["advertiser_id"] for s in matching[:5]]
            creatives = client.search_creatives(advertiser_ids=ids, region_alpha2=alpha2, max_ads=max_ads)
        for row in creatives:
            owner = next((s for s in suggestions if s.get("advertiser_id") == row.get("advertiser_id")), None)
            if owner:
                row.setdefault("title", owner["name"])
                if "verified" not in row:
                    row["verified"] = owner.get("verified")
        result["suggestions"], result["creatives"] = suggestions, creatives
        result["found"] = matched or any(registrable_domain(row.get("domain") or "") == domain
                                          for row in creatives)
        result["complete"] = client.creatives_complete
        result["status"] = "complete" if client.creatives_complete else "partial"
        if not client.creatives_complete:
            result["incomplete_reason"] = "ad_limit"
    except (Blocked, ProviderDown, CacheMiss) as exc:
        result["blocked"], result["error"] = isinstance(exc, Blocked), str(exc)
        result["creatives"] = client.partial_creatives
        result["status"] = "blocked" if result["blocked"] else ("partial" if result["creatives"] else "failed")
        result["incomplete_reason"] = type(exc).__name__
    result["ads"] = {**ads_summary({"creatives": result["creatives"]}, now=now),
                     "dates_read": any(c.get("dates_read") for c in result["creatives"])}
    if not result["complete"] and not result["creatives"]:
        result["ads"]["ads_seen"] = None
    if not result["ads"]["advertising_now"] and (
            not result["complete"] or any(not c.get("dates_read") for c in result["creatives"])):
        result["ads"]["advertising_now"] = None
    result["ads"]["verified"] = (True if any(c.get("verified") is True for c in result["creatives"])
                                     else False if result["creatives"] and all(c.get("verified") is False for c in result["creatives"])
                                     else None)
    return result


# ---------------------------------------------------------------- the CLI

def company_identity(db_path: Path, company_number: str) -> dict[str, str]:
    """The chosen website and name of one company, from the web-stage store
    (`company_web_identity_current`). Read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select c.company_name, w.domain from companies c "
            "join company_web_identity_current w on w.company_number = c.company_number "
            "and w.role = 'main' where c.company_number = ?", (company_number,)).fetchone()
    finally:
        conn.close()
    if not row or not row[1]:
        raise LookupError(f"no chosen website for company {company_number} in {db_path}; give --domain")
    return {"company_name": row[0] or "", "domain": row[1]}


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_command = sub.add_parser("check", help="Check one company or domain for Google ads, on demand.")
    check_command.add_argument("--domain", help="Registrable domain, e.g. example.co.uk")
    check_command.add_argument("--company", help="Company number: domain and name read from --db")
    check_command.add_argument("--name", help="A name for the advertiser lookup when the domain does not resolve")
    check_command.add_argument("--db", type=Path, default=DATABASE_PATH)
    check_command.add_argument("--regions", help="Comma-separated ISO alpha-2 codes, e.g. GB (advisory filter)")
    check_command.add_argument("--max-ads", type=int, default=DEFAULT_MAX_ADS)
    check_command.add_argument("--cache-only", action="store_true", help="Answer from the cache; never send")
    check_command.add_argument("--refresh", action="store_true", help="Fetch fresh evidence even when cached")
    args = parser.parse_args(argv)
    if args.max_ads < 1 or (args.refresh and args.cache_only):
        parser.error("--max-ads must be positive; --refresh cannot be combined with --cache-only")
    domain, name = args.domain, args.name
    if args.company:
        ident = company_identity(args.db, args.company)
        domain, name = domain or ident["domain"], name or ident["company_name"]
    if not domain:
        parser.error("give --domain, or --company with a chosen website in --db")
    alpha2 = [r.strip().upper() for r in args.regions.split(",")] if args.regions else None
    client = TransparencyClient(cache_only=args.cache_only, refresh=args.refresh,
                                budget=RequestBudget(limit=REQUEST_BUDGET))
    summary = check(client, domain=domain, name=name, alpha2=alpha2, max_ads=args.max_ads)
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    if summary["error"]:
        return 2
    print(f"checked {summary['domain']}: {summary['ads']['ads_seen']} ads, "
          f"advertising now: {summary['ads']['advertising_now']}, "
          f"demand part of run: {client.requests_made} requests, "
          f"blocked: {summary['blocked']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
