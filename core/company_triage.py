#!/usr/bin/env python3
"""Gate A: entity triage from data already in SQLite.

Answers "is this a real, customer-facing trading company?" before anything
expensive runs against it. Every rule here is deterministic and reads only
stored data -- no API calls, no model calls -- so the whole pass is free and
can be re-run at any time after enrichment or a history backfill changes its
inputs.

The rules exist because turnover alone is a bad filter. Three failure modes
seen in the live data:

- Holding vehicles. MTALX GLOBAL HOLDINGS reports £423m turnover with zero
  employees; it is a group holding entity, not a business anyone can sell
  advertising to.
- Double counting. LEMON PEPPER TOPCO and LEMON PEPPER HOLDINGS both report
  £125,026,523 -- one business consolidated twice, appearing as two leads.
- Passthrough contracting. STONEBRIDGE CONTRACTING reports £408m turnover
  against 24 employees (£17m per employee): revenue is largely subcontractor
  cost flowing through, not a marketing-driven trading base.

Deliberately conservative: a flag means "do not treat this like an ordinary
trading company without looking", not "this is definitely a shell". Ambiguous
cases resolve to `unknown` rather than being forced into a bucket.

What `holding` does and does not mean
-------------------------------------
It means the legal entity employs nobody directly. It does NOT mean there is
no business here. Many group parents consolidate a real trading operation:
RICHARDSONS (HOLDINGS) reports zero employees but its filed narrative reads
"The group is a car dealership with two locations in East Yorkshire", and
BELL TRUCKS (HOLDINGS) is "commercial vehicle sales and servicing". Those are
perfectly good leads.

Only the filed narrative separates a true investment vehicle ("the principal
activity of the company continued to be that of an investment holding
company") from a trading group filing through its parent ("the principal
activity of the company and group continued to be that of ..."). Gate A
cannot make that call from structured fields alone, so `holding` here should
be read as "route to narrative confirmation", never as "discard".
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from core.companies_house_sqlite import upsert_company_signals

TRADING = "trading"
HOLDING = "holding"
DORMANT = "dormant"
NON_TRADING = "non_trading"
UNKNOWN = "unknown"

# Matched against the company name as whole words. TOPCO/BIDCO/MIDCO are
# private-equity acquisition-structure names and are close to conclusive;
# "HOLDINGS" alone is weaker, so it is only decisive alongside no employees.
HOLDING_NAME_PATTERN = re.compile(r"\b(HOLDINGS?|HOLDCO|TOPCO|BIDCO|MIDCO|NEWCO)\b", re.I)

# SIC codes that positively describe a holding or head-office entity.
HOLDING_SIC_CODES = frozenset({"64209", "70100"})

# Residual "not elsewhere classified" buckets. These carry no usable signal
# about what the business does, so downstream stages must not treat the SIC
# group as informative -- the old PPC ratio table mapped 96090 ("other
# service activities n.e.c.") onto personal care and produced the single
# largest, and most wrong, spend estimate in the database.
CATCH_ALL_SIC_CODES = frozenset(
    {"82990", "96090", "64999", "47990", "74909", "46909", "43999", "81299", "70229"}
)

# Live distribution of turnover/employees is median £225k, p90 £4.1m. £2m
# flags roughly the top eighth -- enough to catch passthrough vehicles
# (STONEBRIDGE sits at £17m) without flagging every capital-intensive but
# genuine operator.
REVENUE_PER_EMPLOYEE_REVIEW_THRESHOLD = 2_000_000

# Turnover below this is too small for an exact match between two companies
# to mean anything (many companies report round numbers or nil).
DUPLICATE_TURNOVER_FLOOR = 10_000


def name_suggests_holding(company_name: str | None) -> bool:
    return bool(company_name and HOLDING_NAME_PATTERN.search(company_name))


def sic_is_catch_all(sic_code_primary: str | None) -> bool:
    return bool(sic_code_primary and sic_code_primary in CATCH_ALL_SIC_CODES)


def revenue_per_employee(turnover: int | None, employees: float | None) -> int | None:
    if not turnover or turnover <= 0 or not employees or employees <= 0:
        return None
    return int(turnover / employees)


def gross_margin_pct(turnover: int | None, gross_profit: int | None) -> float | None:
    """Bounded to 0-100. Values outside that range are arithmetically
    impossible and indicate an extraction error or a passthrough artefact
    (32 current-period rows in the live data exceed 100%), so they are
    dropped rather than passed downstream."""
    if not turnover or turnover <= 0 or gross_profit is None:
        return None
    pct = 100.0 * gross_profit / turnover
    if pct < 0 or pct > 100:
        return None
    return round(pct, 2)


def classify_trading_status(
    *,
    company_name: str | None,
    company_status: str | None,
    sic_code_primary: str | None,
    turnover: int | None,
    employees: float | None,
    has_financials: bool,
    is_duplicate: bool = False,
) -> tuple[str, str]:
    """Return (trading_status, reason). Ordered most conclusive first.

    Turnover against zero employees is suggestive of a group vehicle but is
    NOT on its own sufficient: checked against the filed narrative, roughly
    half of such companies are genuine traders whose staff sit in
    subsidiaries (HEDIN AUTOMOTIVE, zero employees on £412m, states its
    activity as "motor car retailers and repairers"; CONSTELLIA PUBLIC,
    "managed service provision for procurement"). It therefore needs
    corroboration from the name, the SIC code, or duplicate turnover before
    it decides anything; on its own it only sets the
    `turnover_without_employees` flag for the narrative stage to resolve."""
    if company_status and company_status != "active":
        return NON_TRADING, f"company_status is {company_status}"

    if sic_code_primary in HOLDING_SIC_CODES:
        return HOLDING, f"SIC {sic_code_primary} is a holding or head-office code"

    holding_name = name_suggests_holding(company_name)
    no_staff = employees is not None and employees == 0
    has_turnover = bool(turnover and turnover > 0)

    if holding_name and no_staff:
        return HOLDING, "holding-style name with zero reported employees"

    if is_duplicate and no_staff:
        return HOLDING, "duplicates another company's turnover and employs nobody"

    if not has_financials:
        return UNKNOWN, "no current-period financial data"

    if not has_turnover and no_staff:
        return DORMANT, "no turnover and no employees"

    if has_turnover and no_staff:
        return UNKNOWN, "turnover reported against zero employees; needs narrative confirmation"

    if holding_name:
        return UNKNOWN, "holding-style name but active trading indicators"

    return TRADING, "active with trading indicators"


def _duplicate_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Companies whose current-period turnover exactly matches another
    company's for the same period. Maps the secondary copy -> the company it
    duplicates. The entity WITHOUT a holding-style name is preferred as
    primary, since that is the operating company; ties break on
    company_number so the result is stable across runs.

    Periods are keyed on period_end_on where available and financial_year
    otherwise: period_end_on arrived with a later migration and is populated
    on only a few hundred of the current-period rows, so keying on it alone
    would miss almost every real duplicate."""
    rows = conn.execute(
        """
        select f.turnover,
               coalesce(f.period_end_on, 'FY' || f.financial_year) as period_key,
               f.company_number, c.company_name
        from financial_period_summaries f
        join companies c on c.company_number = f.company_number
        where f.period_type = 'current'
          and f.turnover is not null and f.turnover > ?
          and (f.period_end_on is not null or f.financial_year is not null)
        order by f.company_number
        """,
        (DUPLICATE_TURNOVER_FLOOR,),
    ).fetchall()

    groups: dict[tuple[int, str], list[tuple[str, str | None]]] = {}
    for turnover, period_key, company_number, company_name in rows:
        groups.setdefault((turnover, period_key), []).append((company_number, company_name))

    duplicates: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        operating = [m for m in members if not name_suggests_holding(m[1])]
        primary = (operating or members)[0][0]
        for company_number, _ in members:
            if company_number != primary:
                duplicates[company_number] = primary
    return duplicates


def triage_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One triage result per enriched company."""
    duplicates = _duplicate_map(conn)
    rows = conn.execute(
        """
        select
            c.company_number, c.company_name, c.company_status, c.sic_code_primary,
            f.turnover, f.employees, f.gross_profit
        from companies c
        left join financial_period_summaries f
          on f.company_number = c.company_number
         and f.period_type = 'current'
         and f.id = (
             select id from financial_period_summaries
             where company_number = c.company_number and period_type = 'current'
             order by financial_year desc, id desc limit 1
         )
        order by c.company_number
        """
    ).fetchall()

    results: list[dict[str, Any]] = []
    for company_number, company_name, company_status, sic_primary, turnover, employees, gross_profit in rows:
        status, reason = classify_trading_status(
            company_name=company_name,
            company_status=company_status,
            sic_code_primary=sic_primary,
            turnover=turnover,
            employees=employees,
            has_financials=turnover is not None or employees is not None,
            is_duplicate=company_number in duplicates,
        )
        rpe = revenue_per_employee(turnover, employees)
        results.append(
            {
                "company_number": company_number,
                "signals": {
                    "trading_status": status,
                    "trading_status_reason": reason,
                    "name_suggests_holding": name_suggests_holding(company_name),
                    "sic_is_catch_all": sic_is_catch_all(sic_primary),
                    "revenue_per_employee": rpe,
                    "revenue_per_employee_flagged": (
                        rpe is not None and rpe >= REVENUE_PER_EMPLOYEE_REVIEW_THRESHOLD
                    ),
                    "turnover_without_employees": bool(
                        turnover and turnover > 0 and employees is not None and employees == 0
                    ),
                    "gross_margin_pct": gross_margin_pct(turnover, gross_profit),
                    "duplicate_of": duplicates.get(company_number),
                },
            }
        )
    return results


def refresh_all_company_signals(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, int]:
    """Recompute Gate A signals for every enriched company. Idempotent."""
    counts: dict[str, int] = {}
    for result in triage_rows(conn):
        signals = result["signals"]
        counts[signals["trading_status"]] = counts.get(signals["trading_status"], 0) + 1
        if signals["duplicate_of"]:
            counts["_duplicate"] = counts.get("_duplicate", 0) + 1
        if signals["revenue_per_employee_flagged"]:
            counts["_revenue_per_employee_flagged"] = counts.get("_revenue_per_employee_flagged", 0) + 1
        if signals["turnover_without_employees"]:
            counts["_turnover_without_employees"] = counts.get("_turnover_without_employees", 0) + 1
        if signals["sic_is_catch_all"]:
            counts["_sic_catch_all"] = counts.get("_sic_catch_all", 0) + 1
        if not dry_run:
            upsert_company_signals(conn, result["company_number"], signals, source_scope="triage")
    if not dry_run:
        conn.commit()
    return counts
