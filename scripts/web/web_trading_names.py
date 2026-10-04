#!/usr/bin/env python3
"""Trading names: the names a company actually sells under (W1 fix).

About a third of the test companies trade under a name that is not their
registered one (Free Soul for ARA Fitness, Store First for Pay Store,
Millie's House for South West London Nursery Company). A Maps or web search on
the registered name misses them. Three free sources, each stored with the
sentence it came from:

- `filing`: "trading as ..." and similar phrases in the filed report
  (the text `core.companies_house_extractor.filed_report_text` produces);
- `website`: "X is a trading name of Y Limited" on a site that shows the
  company number, where Y is the registered company;
- `maps_listing`: the title of a Maps listing that links to a verified site.

Nothing here calls a provider. `extract-filings` reads cached filings only.

Usage:
    python -m scripts.web.web_trading_names extract-filings --limit 50
    python -m scripts.web.web_trading_names show 08615712
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_sqlite import init_db, utc_now  # noqa: E402

SOURCES = ("filing", "website", "maps_listing")
MAX_NAME_WORDS = 7
LEGAL_SUFFIX = re.compile(r"\b(?:limited|ltd|plc|llp)\b\.?", re.I)

# "trading as Free Soul", "trades under the name Free Soul", "t/a Free Soul", "known as ..."
FILING_PATTERNS = (
    re.compile(r"\b(?:trad(?:es|ing|ed)\s+(?:as|under\s+the\s+(?:trading\s+)?(?:name|style|brand)(?:\s+of)?))\s+"
               r"(?P<name>[A-Z0-9][^.;:,\n()]{2,80})", re.I),
    re.compile(r"\bt/a\s+(?P<name>[A-Z0-9][^.;:,\n()]{2,80})"),
    re.compile(r"\b(?:operates|operating|operated)\s+(?:under|as)\s+(?:the\s+)?"
               r"(?:(?:(?:trading|brand)\s+name|name|brand)\s+)?(?P<name>[A-Z][^.;:,\n()]{2,80})"),
    re.compile(r"\b(?:trading\s+name|brand\s+name|trade\s+name)\s+(?:of\s+the\s+company\s+)?(?:is|are)\s+"
               r"(?P<name>[A-Z0-9][^.;:,\n()]{2,80})", re.I),
)
# "Bott and Co is a trading name of Bott and Co Solicitors Limited"
SITE_PATTERN = re.compile(
    r"(?P<name>[A-Z0-9][\w&'’.\- ]{2,60}?)\s+(?:is|are)\s+(?:a\s+)?trading\s+(?:name|style)\s+of\s+"
    r"(?P<legal>[A-Z0-9][\w&'’.\-, ]{2,80}?(?:limited|ltd|plc|llp))", re.I)
# A connecting word ends a name only when a lower-case word follows it ("... Nursery and provides ..."),
# never inside one ("Bott and Co", "Crowther & Shaw").
TAIL_CUT = re.compile(r"(?i:\s+(?:and|which|that|where|whose|in|from|since|with|for|to|at|by|a|an|the|our|its))"
                      r"(?=\s+[a-z]).*$")
LEADING_ARTICLE = re.compile(r"^(?:the|its|a|an|our)\s+", re.I)
GENERIC_NAME = re.compile(r"(?:the|its|a|an|our|one|that|this|these|those|group|company|business|firm)", re.I)


def _clean(raw: str) -> str | None:
    """A candidate name: trimmed at the first connecting word, legal suffix and
    quotes dropped, rejected if it is long or a bare generic word."""
    name = raw.strip(" \t\"'“”‘’-–—")
    name = TAIL_CUT.sub("", name)
    name = LEGAL_SUFFIX.sub("", name).strip(" \t\"'“”‘’-–—&,")
    words = name.split()
    if not words or len(words) > MAX_NAME_WORDS or len(name) < 3:
        return None
    if GENERIC_NAME.fullmatch(LEADING_ARTICLE.sub("", name).strip()) or GENERIC_NAME.fullmatch(name):
        return None
    return name


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower().replace("&", " and ")).split())


def names_from_filing(text: str, company_name: str | None = None) -> list[dict[str, str]]:
    """Trading names stated in a filed report, each with its sentence. A name
    equal to the registered name is not a trading name."""
    found: dict[str, dict[str, str]] = {}
    registered = _norm(LEGAL_SUFFIX.sub("", company_name or ""))
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        sentence = sentence.strip()
        if not sentence or len(sentence) > 600:
            continue
        for pattern in FILING_PATTERNS:
            for match in pattern.finditer(sentence):
                name = _clean(match.group("name"))
                if name is None or _norm(name) == registered:
                    continue
                found.setdefault(_norm(name), {"name": name, "evidence": sentence[:300]})
    return list(found.values())


def names_from_site_text(text: str, company_name: str) -> list[dict[str, str]]:
    """"X is a trading name of Y Limited" where Y is this company. Only used on
    a site that already showed the company's number, so Y can be trusted
    to be it; Y is still checked against the registered name."""
    registered = _norm(LEGAL_SUFFIX.sub("", company_name))
    found: dict[str, dict[str, str]] = {}
    for match in SITE_PATTERN.finditer(text or ""):
        legal = _norm(LEGAL_SUFFIX.sub("", match.group("legal")))
        if not legal or not (legal in registered or registered in legal):
            continue
        name = _clean(match.group("name"))
        if name is None or _norm(name) == registered:
            continue
        found.setdefault(_norm(name), {"name": name, "evidence": " ".join(match.group(0).split())[:300]})
    return list(found.values())


def name_from_listing(listing: dict[str, Any] | None, company_name: str, *, verified_site: bool) -> dict[str, str] | None:
    """A Maps listing's title, when the listing links to a verified site and
    the title is not just the registered name."""
    if not listing or not verified_site or not listing.get("title"):
        return None
    name = _clean(listing["title"])
    if name is None or _norm(name) == _norm(LEGAL_SUFFIX.sub("", company_name)):
        return None
    return {"name": name, "evidence": f"Google Maps listing: {listing['title']}"[:300]}


def store_names(conn: sqlite3.Connection, company_number: str, source: str, names: Iterable[dict[str, str]]) -> int:
    """Insert new names; one row per (company, name), keeping the first source seen."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    added = 0
    now = utc_now()
    for item in names:
        cursor = conn.execute(
            "insert or ignore into company_trading_names (company_number, name, source, evidence, found_at) "
            "values (?, ?, ?, ?, ?)", (company_number, item["name"], source, item.get("evidence"), now))
        added += cursor.rowcount
    return added


def trading_names(conn: sqlite3.Connection, company_number: str) -> list[str]:
    rows = conn.execute("select name from company_trading_names where company_number = ? order by id",
                        (company_number,)).fetchall()
    return [name for (name,) in rows]


def extract_from_cached_filings(conn: sqlite3.Connection, numbers: list[str] | None = None, *,
                                limit: int | None = None, xhtml_path=None) -> dict[str, int]:
    """Run the filing patterns over every cached XHTML filing (free). `xhtml_path`
    maps a company number to its cached file; by default the search-screen
    population's lookup is used."""
    from core.companies_house_extractor import filed_report_text

    if xhtml_path is None:
        from scripts.screen.search_screen_population import xhtml_path as default_path
        xhtml_path = default_path
    if numbers is None:
        numbers = [n for (n,) in conn.execute("select company_number from companies order by company_number")]
    counts = {"filings_read": 0, "names_added": 0, "companies_with_names": 0}
    for number in numbers:
        if limit is not None and counts["filings_read"] >= limit:
            break
        path = xhtml_path(number)
        if path is None:
            continue
        row = conn.execute("select company_name from companies where company_number = ?", (number,)).fetchone()
        text = filed_report_text(Path(path).read_text(encoding="utf-8", errors="ignore"))
        counts["filings_read"] += 1
        added = store_names(conn, number, "filing", names_from_filing(text, row[0] if row else None))
        counts["names_added"] += added
        counts["companies_with_names"] += bool(added)
    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="companies-house.db")
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract-filings", help="Trading names from cached filings (free, no requests).")
    extract.add_argument("--limit", type=int)
    show = sub.add_parser("show", help="Stored trading names for companies.")
    show.add_argument("numbers", nargs="+")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        init_db(conn)
        if args.command == "extract-filings":
            print(extract_from_cached_filings(conn, limit=args.limit))
        else:
            for number in args.numbers:
                print(number, trading_names(conn, number))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
