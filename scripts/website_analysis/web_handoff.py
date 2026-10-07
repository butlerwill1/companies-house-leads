#!/usr/bin/env python3
"""W5: the lead sheet for the friend, and the outcome log (docs/WEB_STAGE_PLAN.md).

`build` joins everything the earlier steps stored into one row per company:
financials, what the business does (Google category and description), how it
sells, advertising status, search demand, the gap segment, the gap and strength
findings as sentences, and links to the website, its Maps listing and Companies
House. The sheet is a CSV; publishing it to the "Projects /
companies-house-leads" Drive folder as a native Google Sheet is a separate
step (AGENTS.md).

The order of the sheet is a documented sort, not a ranking (ranking is its own
stage, scored by precision at k on the outcomes below): gap segment first
(greenfield, advertising_poorly, advertising_well, low_demand, unknown), then
more gaps before fewer, then the financial queue order of
scripts/website_analysis/web_rank_order.py. Companies whose segment is `site_first` (no
website, or none that can take an enquiry) are left out unless
`--site-first lead` is given: whether they are leads depends on whether the
friend also builds websites.

Outcomes: `register` records the leads handed over (`lead_outcomes`), the
friend's feedback comes back as a CSV, `import-outcomes` writes it in, and
`outcomes` reports contacted, replied, meeting and won. Those outcomes become
the gold set for the ranking stage and the precision at k claim.

Usage:
    python -m scripts.website_analysis.web_handoff build --limit 50 --out logs/web/lead-sheet.csv
    python -m scripts.website_analysis.web_handoff register --sheet-version leads-2026-10-02 --csv logs/web/lead-sheet.csv
    python -m scripts.website_analysis.web_handoff import-outcomes --csv feedback.csv --sheet-version leads-2026-10-02
    python -m scripts.website_analysis.web_handoff outcomes --sheet-version leads-2026-10-02
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from companies_house_core.companies_house_sqlite import init_db, utc_now  # noqa: E402
from scripts.website_analysis.web_findings import FINDING_VERSION  # noqa: E402
from scripts.website_analysis.web_market import MARKET_VERSION  # noqa: E402

DB_DEFAULT = "companies-house.db"
SEGMENT_ORDER = ("greenfield", "advertising_poorly", "advertising_well", "low_demand", "unknown", "site_first")
LEAD_HEADER = [
    "rank", "company number", "company", "trading as", "turnover GBP", "profit after tax GBP", "financial year",
    "employees", "Google category", "description", "customer type", "how customers convert", "area served",
    "urgency", "typical sale", "website", "Google Maps listing", "rating", "reviews", "Companies House",
    "gap segment", "setup level", "advertising now", "ads last shown", "searches a month (its phrases)",
    "avg cost per click USD", "gaps (what to pitch)", "already doing", "financial queue position"]
OUTCOME_COLUMNS = ("contacted", "replied", "meeting", "won")
YES = {"y", "yes", "true", "1", "x"}
NO = {"n", "no", "false", "0"}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = None


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any]:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else {}


def candidate_numbers(conn: sqlite3.Connection, market_version: str) -> list[str]:
    return [r["company_number"] for r in _rows(
        conn, "select company_number from company_market where market_version = ? and gap_segment is not null "
              "order by company_number", (market_version,))]


def lead_row(conn: sqlite3.Connection, number: str, *, market_version: str, finding_version: str,
             queue_position: int | None = None) -> dict[str, Any] | None:
    """Everything the sheet shows for one company, or None if it has no market row."""
    market = _one(conn, "select * from company_market where company_number = ? and market_version = ?",
                  (number, market_version))
    if not market:
        return None
    company = _one(conn, "select company_name from companies where company_number = ?", (number,))
    fin = _one(conn, "select financial_year, turnover, profit_after_tax, employees from company_financial_history "
                     "where company_number = ? and turnover is not null order by financial_year desc limit 1", (number,))
    listing = _one(conn, "select * from company_google_listing where company_number = ? order by id desc limit 1",
                   (number,))
    profile = _one(conn, "select * from company_web_profile where company_number = ? order by profiled_at desc limit 1",
                   (number,))
    names = [r["name"] for r in _rows(conn, "select name from company_trading_names where company_number = ? "
                                            "order by id", (number,))]
    findings = _rows(conn, "select kind, detail from company_setup_findings where company_number = ? "
                           "and finding_version = ? order by id", (number, finding_version))
    domain = market.get("domain")
    place_link = None
    if listing.get("cid"):
        place_link = f"https://www.google.com/maps?cid={listing['cid']}"
    elif listing.get("place_id"):
        place_link = f"https://www.google.com/maps/place/?q=place_id:{listing['place_id']}"
    elif listing.get("title"):
        place_link = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
            f"{listing['title']} {listing.get('address') or ''}")
    return {
        "company_number": number, "company": company.get("company_name"), "trading_as": "; ".join(names),
        "turnover": fin.get("turnover"), "profit": fin.get("profit_after_tax"), "financial_year": fin.get("financial_year"),
        "employees": fin.get("employees"),
        "category": listing.get("category") or profile.get("google_category"),
        "description": profile.get("summary"), "customer_type": profile.get("customer_type"),
        "conversion": profile.get("conversion_action"),
        "area": " ".join(x for x in (profile.get("geography"), profile.get("main_town")) if x) or None,
        "urgency": profile.get("urgency"), "ticket": profile.get("ticket_band"),
        "website": f"https://{domain}/" if domain else None, "place_link": place_link,
        "rating": listing.get("rating"), "reviews": listing.get("rating_count"),
        "companies_house": f"https://find-and-update.company-information.service.gov.uk/company/{number}",
        "segment": market.get("gap_segment"), "setup_level": market.get("setup_level"),
        "advertising_now": market.get("advertising_now"), "ads_last_shown": market.get("ads_last_shown"),
        "volume": market.get("phrase_search_volume"), "cpc": market.get("weighted_cpc_usd"),
        "gaps": [f["detail"] for f in findings if f["kind"] == "gap"],
        "strengths": [f["detail"] for f in findings if f["kind"] == "strength"],
        "gap_count": market.get("gap_count") or 0, "queue_position": queue_position}


def sort_key(row: dict[str, Any]) -> tuple:
    segment = row["segment"] if row["segment"] in SEGMENT_ORDER else "unknown"
    return (SEGMENT_ORDER.index(segment), -(row.get("gap_count") or 0),
            row["queue_position"] if row.get("queue_position") is not None else 10**9, row["company_number"])


def select_leads(rows: list[dict[str, Any]], *, limit: int | None = None, site_first: str = "reject") -> list[dict[str, Any]]:
    if site_first not in ("reject", "lead"):
        raise ValueError("site_first must be 'reject' or 'lead'")
    kept = [r for r in rows if site_first == "lead" or r["segment"] != "site_first"]
    kept.sort(key=sort_key)
    return kept[:limit] if limit is not None else kept


def sheet_rows(leads: list[dict[str, Any]]) -> list[list[Any]]:
    def money(value: Any) -> Any:
        return "" if value is None else round(value)

    out: list[list[Any]] = [LEAD_HEADER]
    for rank, r in enumerate(leads, 1):
        out.append([
            rank, r["company_number"], r["company"], r["trading_as"], money(r["turnover"]), money(r["profit"]),
            r["financial_year"] or "", r["employees"] if r["employees"] is not None else "", r["category"] or "",
            r["description"] or "", r["customer_type"] or "", (r["conversion"] or "").replace("_", " "),
            r["area"] or "", r["urgency"] or "", (r["ticket"] or "").replace("_", " "), r["website"] or "",
            r["place_link"] or "", r["rating"] if r["rating"] is not None else "",
            r["reviews"] if r["reviews"] is not None else "", r["companies_house"], r["segment"] or "",
            r["setup_level"] or "", "" if r["advertising_now"] is None else ("yes" if r["advertising_now"] else "no"),
            r["ads_last_shown"] or "", r["volume"] if r["volume"] is not None else "",
            r["cpc"] if r["cpc"] is not None else "", "\n".join(f"- {g}" for g in r["gaps"]),
            "\n".join(f"- {s}" for s in r["strengths"]),
            r["queue_position"] if r["queue_position"] is not None else ""])
    return out


def build_leads(conn: sqlite3.Connection, numbers: list[str], *, market_version: str = MARKET_VERSION,
                finding_version: str = FINDING_VERSION, queue: dict[str, int] | None = None, limit: int | None = None,
                site_first: str = "reject") -> list[dict[str, Any]]:
    queue = queue or {}
    rows = [r for n in numbers if (r := lead_row(conn, n, market_version=market_version,
                                                 finding_version=finding_version, queue_position=queue.get(n)))]
    return select_leads(rows, limit=limit, site_first=site_first)


def write_csv(rows: list[list[Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


# ---------------------------------------------------------------- outcomes

def register_handover(conn: sqlite3.Connection, numbers: Iterable[str], sheet_version: str, *,
                      handed_over_at: str | None = None) -> int:
    """Record the leads handed over. Registering the same sheet twice adds nothing."""
    at = handed_over_at or utc_now()
    added = 0
    for number in numbers:
        cursor = conn.execute("insert or ignore into lead_outcomes (company_number, sheet_version, handed_over_at) "
                              "values (?, ?, ?)", (number, sheet_version, at))
        added += cursor.rowcount
    conn.commit()
    return added


def _flag(value: str, field: str, number: str) -> int | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if value in YES:
        return 1
    if value in NO:
        return 0
    raise ValueError(f"{number}: {field} must be yes or no, not {value!r}")


def import_outcomes(conn: sqlite3.Connection, csv_path: Path, sheet_version: str) -> int:
    """Apply the friend's feedback. Columns: `company number`, `contacted` (a date
    or yes), `replied`, `meeting`, `won` (yes or no) and `notes`. Every row is
    validated before anything is written; a company that was never handed over
    on this sheet is an error."""
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {f.lower().strip(): f for f in reader.fieldnames or []}
        if "company number" not in fields:
            raise ValueError("the CSV needs a 'company number' column")
        rows = list(reader)
    handed = {n for (n,) in conn.execute("select company_number from lead_outcomes where sheet_version = ?",
                                         (sheet_version,))}
    updates: list[tuple[Any, ...]] = []
    for row in rows:
        raw = row[fields["company number"]].strip()
        number = raw.zfill(8) if raw.isdigit() else raw.upper()
        if number not in handed:
            raise ValueError(f"{number}: not handed over on sheet {sheet_version!r}")
        contacted = (row.get(fields.get("contacted", ""), "") or "").strip()
        contacted_at = None
        if contacted.lower() in YES:
            contacted_at = utc_now()
        elif contacted and contacted.lower() not in NO:
            try:
                datetime.strptime(contacted[:10], "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{number}: contacted must be a date (YYYY-MM-DD) or yes, not {contacted!r}") from None
            contacted_at = contacted[:10]
        flags = {f: _flag(row.get(fields.get(f, ""), "") or "", f, number) for f in ("replied", "meeting", "won")}
        already = conn.execute("select contacted_at from lead_outcomes where company_number = ? and sheet_version = ?",
                               (number, sheet_version)).fetchone()[0]
        if any(flags.values()) and contacted_at is None and already is None:
            raise ValueError(f"{number}: a reply, meeting or win needs a contacted date")
        notes = (row.get(fields.get("notes", ""), "") or "").strip() or None
        updates.append((contacted_at, flags["replied"], flags["meeting"], flags["won"], notes, number))
    for contacted_at, replied, meeting, won, notes, number in updates:
        conn.execute("update lead_outcomes set contacted_at = coalesce(?, contacted_at), replied = coalesce(?, replied), "
                     "meeting = coalesce(?, meeting), won = coalesce(?, won), notes = coalesce(?, notes) "
                     "where company_number = ? and sheet_version = ?",
                     (contacted_at, replied, meeting, won, notes, number, sheet_version))
    conn.commit()
    return len(updates)


def outcome_summary(conn: sqlite3.Connection, sheet_version: str) -> dict[str, Any]:
    row = _one(conn, "select count(*) as handed_over, sum(contacted_at is not null) as contacted, "
                     "sum(coalesce(replied, 0)) as replied, sum(coalesce(meeting, 0)) as meetings, "
                     "sum(coalesce(won, 0)) as won from lead_outcomes where sheet_version = ?", (sheet_version,))
    contacted = row.get("contacted") or 0
    summary = {k: int(row.get(k) or 0) for k in ("handed_over", "contacted", "replied", "meetings", "won")}
    summary["reply_rate_of_contacted"] = round(summary["replied"] / contacted, 3) if contacted else None
    summary["meeting_rate_of_contacted"] = round(summary["meetings"] / contacted, 3) if contacted else None
    by_segment = _rows(conn, "select m.gap_segment as segment, count(*) as handed_over, "
                             "sum(o.contacted_at is not null) as contacted, sum(coalesce(o.meeting, 0)) as meetings "
                             "from lead_outcomes o left join company_market m on m.company_number = o.company_number "
                             "where o.sheet_version = ? group by m.gap_segment order by m.gap_segment", (sheet_version,))
    summary["by_segment"] = by_segment
    return summary


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_DEFAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build the lead sheet CSV (free).")
    build.add_argument("--limit", type=int, help="At most this many leads (the friend's weekly capacity).")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--market-version", default=MARKET_VERSION)
    build.add_argument("--site-first", choices=("reject", "lead"), default="reject")
    register = sub.add_parser("register", help="Record the leads in a sheet as handed over.")
    register.add_argument("--sheet-version", required=True)
    register.add_argument("--csv", type=Path, required=True)
    imp = sub.add_parser("import-outcomes", help="Write the friend's feedback CSV into lead_outcomes.")
    imp.add_argument("--csv", type=Path, required=True)
    imp.add_argument("--sheet-version", required=True)
    summary = sub.add_parser("outcomes", help="Contacted, replied, meeting and won for a sheet.")
    summary.add_argument("--sheet-version", required=True)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        init_db(conn)
        if args.command == "build":
            from scripts.website_analysis.web_rank_order import load_queue
            queue = {row["company_number"]: row["position"] for row in load_queue(conn)}
            leads = build_leads(conn, candidate_numbers(conn, args.market_version), market_version=args.market_version,
                                queue=queue, limit=args.limit, site_first=args.site_first)
            write_csv(sheet_rows(leads), args.out)
            by_segment: dict[str, int] = {}
            for lead in leads:
                by_segment[lead["segment"]] = by_segment.get(lead["segment"], 0) + 1
            print(json.dumps({"leads": len(leads), "by_segment": by_segment, "out": str(args.out)}, indent=2))
        elif args.command == "register":
            with args.csv.open(encoding="utf-8", newline="") as handle:
                numbers = [row["company number"] for row in csv.DictReader(handle)]
            print(f"{register_handover(conn, numbers, args.sheet_version)} leads registered on {args.sheet_version}")
        elif args.command == "import-outcomes":
            print(f"{import_outcomes(conn, args.csv, args.sheet_version)} outcomes written")
        elif args.command == "outcomes":
            print(json.dumps(outcome_summary(conn, args.sheet_version), indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
