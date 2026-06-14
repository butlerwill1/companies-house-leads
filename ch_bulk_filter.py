#!/usr/bin/env python3
"""
Filter Companies House bulk CSV data to find good PPC advertising prospects.

Filtering logic
---------------
HARD FILTERS (must pass all):
  1. CompanyStatus == "Active"
  2. CompanyCategory == "Private Limited Company"  (excludes PLCs, LLPs, charities, etc.)
  3. AccountCategory not in DORMANT / NO ACCOUNTS FILED / STRIKE OFF exclusions
  4. SIC code in the target sector list
  5. Incorporated >= 3 years ago  (established enough to have budget)
  6. LastMadeUpDate within last 3 years  (recently active)

SCORING (0-100, higher = better lead):
  - SIC sector tier (tier 1 sectors = highest PPC ROI) +30/20/10
  - Company age 3-10 years = sweet spot                 +15
  - Filed accounts recently (<= 18 months ago)          +10
  - Account type is FULL or TOTAL EXEMPTION FULL        +10  (bigger, more transparent)
  - England/Wales address                               +5
  - Name doesn't look like a holding/shell company      +5
  - No outstanding mortgages (not financially stressed) +5
  - Has a second SIC code (more complex business)       +5

Output: CSV with company number, name, SIC codes, location, score, and
        a human-readable reason string for the score.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# SIC code configuration
# ---------------------------------------------------------------------------

# Tier 1 - highest PPC ROI, strong intent-based search volume
TIER_1_SICS = {
    "47910", "47990",                              # e-commerce / online retail
    "64110", "64191", "64192", "64999",            # banking / lending / credit
    "65110", "65120", "65201", "65202",            # insurance
    "66110", "66120", "66190", "66210", "66220",   # financial services / brokers
    "69101", "69102",                              # legal services (solicitors)
    "86230",                                       # dental
    "86210",                                       # GP / general medical
    "86900",                                       # other health (physio, chiro, etc.)
    "85100", "85200", "85310", "85320",            # education / tutoring / schools
}

# Tier 2 - strong PPC users, competitive markets
TIER_2_SICS = {
    "41100", "41201", "41202",                     # property development
    "68100", "68201", "68209", "68310", "68320",   # estate agents / property management
    "45111", "45112", "45190",                     # car dealers
    "56101", "56102", "56103", "56210", "56290",   # restaurants / catering
    "55100", "55201", "55202", "55209",            # hotels / B&Bs
    "96010", "96020", "96030", "96040", "96090",   # personal care / beauty / wellness
    "43210", "43220", "43290", "43310", "43320",   # specialist construction / tradespeople
    "43341", "43342",                              # painters / decorators
    "43390",                                       # other finishing
}

# Tier 3 - occasional PPC, worth targeting but lower conversion
TIER_3_SICS = {
    "62011", "62012", "62020", "62090",            # software / IT consultancy
    "70210", "70221", "70229",                     # management consultancy / PR
    "73110", "73200",                              # advertising / market research
    "74100", "74201", "74202", "74209",            # design / photography
    "69201", "69202", "69203",                     # accountancy / bookkeeping / audit
    "71111", "71112", "71121", "71122",            # architecture / engineering
    "72110", "72190", "72200",                     # R&D / biotech
    "82110", "82190", "82990",                     # business support services
    "85600",                                       # educational support (tutors)
    "90010", "90020", "90030", "90040",            # arts / entertainment
    "93110", "93120", "93130", "93190",            # sport / fitness / gyms
    "93210", "93290",                              # theme parks / amusement
    "47411", "47710", "47730", "47740", "47750",   # specialist retail
    "49100", "49311", "49319", "49320",            # transport / taxi / logistics
}

ALL_TARGET_SICS = TIER_1_SICS | TIER_2_SICS | TIER_3_SICS

EXCLUDED_ACCOUNT_TYPES = {
    "DORMANT",
    "NO ACCOUNTS FILED",
    "INITIAL",
}

EXCLUDED_STATUS = {
    "Dissolved",
    "Liquidation",
    "Receivership",
    "Administration",
    "Active - Proposal to Strike off",
    "Voluntary Arrangement",
    "Converted / Closed",
}

SHELL_PATTERNS = re.compile(
    r"\b(holding|holdings|holdco|trustee|nominee|property fund|pension|spv)\b",
    re.I,
)

TODAY = date.today()


def parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def extract_sic_code(sic_text: str) -> str | None:
    """Extract 5-digit SIC code from strings like '62020 - Information technology...'"""
    sic_text = sic_text.strip()
    if not sic_text:
        return None
    match = re.match(r"(\d{5})", sic_text)
    return match.group(1) if match else None


def years_ago(d: date) -> float:
    return (TODAY - d).days / 365.25


def score_company(row: dict) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    # SIC tier
    sic1 = extract_sic_code(row.get("SICCode.SicText_1", ""))
    sic2 = extract_sic_code(row.get("SICCode.SicText_2", ""))
    sic3 = extract_sic_code(row.get("SICCode.SicText_3", ""))
    sic4 = extract_sic_code(row.get("SICCode.SicText_4", ""))
    all_sics = [s for s in [sic1, sic2, sic3, sic4] if s]

    if sic1 in TIER_1_SICS:
        score += 30
        reasons.append("tier-1 SIC")
    elif sic1 in TIER_2_SICS:
        score += 20
        reasons.append("tier-2 SIC")
    elif sic1 in TIER_3_SICS:
        score += 10
        reasons.append("tier-3 SIC")

    if len(all_sics) >= 2:
        score += 5
        reasons.append("multi-SIC")

    # Company age
    inc_date = parse_date(row.get("IncorporationDate", ""))
    if inc_date:
        age_years = years_ago(inc_date)
        if 3 <= age_years <= 10:
            score += 15
            reasons.append(f"sweet-spot age {age_years:.1f}yr")
        elif age_years > 10:
            score += 8
            reasons.append(f"established {age_years:.1f}yr")

    # Recency of accounts
    last_accounts = parse_date(row.get("Accounts.LastMadeUpDate", ""))
    if last_accounts:
        months_since = (TODAY - last_accounts).days / 30
        if months_since <= 18:
            score += 10
            reasons.append("accounts <18mo")
        elif months_since <= 36:
            score += 5
            reasons.append("accounts <36mo")

    # Account type quality
    acct_type = row.get("Accounts.AccountCategory", "").strip().upper()
    if acct_type in ("FULL", "TOTAL EXEMPTION FULL", "UNAUDITED ABRIDGED", "AUDITED ABRIDGED", "GROUP"):
        score += 10
        reasons.append(f"acct:{acct_type.lower()}")

    # England / Wales address (PPC agencies typically work domestically)
    country = row.get("RegAddress.Country", "").strip().upper()
    post_town = row.get("RegAddress.PostTown", "").strip()
    if country in ("ENGLAND", "WALES", "UNITED KINGDOM", "") and post_town:
        score += 5
        reasons.append("GB address")

    # Not a shell / holding company
    name = row.get("CompanyName", "")
    if not SHELL_PATTERNS.search(name):
        score += 5
        reasons.append("non-shell name")

    # No outstanding mortgage charges (proxy for financial stress)
    try:
        outstanding = int(row.get("Mortgages.NumMortOutstanding", "0") or 0)
        if outstanding == 0:
            score += 5
            reasons.append("no charges")
    except ValueError:
        pass

    return score, reasons


def passes_hard_filters(row: dict) -> tuple[bool, str]:
    # 1. Status
    status = row.get("CompanyStatus", "").strip()
    if status in EXCLUDED_STATUS or not status:
        return False, f"status:{status}"

    # 2. Category
    category = row.get("CompanyCategory", "").strip()
    if category != "Private Limited Company":
        return False, f"category:{category}"

    # 3. Account type
    acct_type = row.get("Accounts.AccountCategory", "").strip().upper()
    if acct_type in EXCLUDED_ACCOUNT_TYPES:
        return False, f"accounts:{acct_type}"

    # 4. SIC code in target list
    sic1 = extract_sic_code(row.get("SICCode.SicText_1", ""))
    if not sic1 or sic1 not in ALL_TARGET_SICS:
        return False, "sic:not_target"

    # 5. Incorporated >= 3 years ago
    inc_date = parse_date(row.get("IncorporationDate", ""))
    if not inc_date or years_ago(inc_date) < 3:
        return False, "too_young"

    # 6. Accounts filed within last 3 years
    last_accounts = parse_date(row.get("Accounts.LastMadeUpDate", ""))
    if not last_accounts or years_ago(last_accounts) > 3:
        return False, "stale_accounts"

    return True, "ok"


def process_files(input_paths: list[Path], output_path: Path, min_score: int) -> None:
    total = 0
    passed = 0
    written = 0
    filter_reasons: dict[str, int] = {}

    output_fields = [
        "company_number", "company_name", "company_status",
        "sic_1", "sic_2", "sic_3", "sic_4",
        "incorporation_date", "last_accounts_date", "account_category",
        "post_town", "post_code", "country",
        "score", "score_reasons",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=output_fields)
        writer.writeheader()

        for input_path in input_paths:
            print(f"Processing {input_path.name}...", file=sys.stderr)
            with input_path.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                # CH header has spaces around some field names — normalise keys
                reader.fieldnames = [k.strip() for k in (reader.fieldnames or [])]
                for row in reader:
                    row = {k.strip(): v for k, v in row.items()}
                    total += 1
                    ok, reason = passes_hard_filters(row)
                    if not ok:
                        filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
                        continue
                    passed += 1
                    score, reasons = score_company(row)
                    if score < min_score:
                        continue
                    written += 1
                    writer.writerow({
                        "company_number": row.get("CompanyNumber", "").strip(),
                        "company_name": row.get("CompanyName", "").strip(),
                        "company_status": row.get("CompanyStatus", "").strip(),
                        "sic_1": row.get("SICCode.SicText_1", "").strip(),
                        "sic_2": row.get("SICCode.SicText_2", "").strip(),
                        "sic_3": row.get("SICCode.SicText_3", "").strip(),
                        "sic_4": row.get("SICCode.SicText_4", "").strip(),
                        "incorporation_date": row.get("IncorporationDate", "").strip(),
                        "last_accounts_date": row.get("Accounts.LastMadeUpDate", "").strip(),
                        "account_category": row.get("Accounts.AccountCategory", "").strip(),
                        "post_town": row.get("RegAddress.PostTown", "").strip(),
                        "post_code": row.get("RegAddress.PostCode", "").strip(),
                        "country": row.get("RegAddress.Country", "").strip(),
                        "score": score,
                        "score_reasons": "|".join(reasons),
                    })

    print(f"\nResults:", file=sys.stderr)
    print(f"  Total rows processed : {total:,}", file=sys.stderr)
    print(f"  Passed hard filters  : {passed:,}", file=sys.stderr)
    print(f"  Written (score>={min_score:2d}) : {written:,}", file=sys.stderr)
    print(f"\nTop filter-out reasons:", file=sys.stderr)
    for reason, count in sorted(filter_reasons.items(), key=lambda x: -x[1])[:10]:
        print(f"  {reason:40s} {count:>8,}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Filter CH bulk CSV for PPC leads.")
    parser.add_argument("--input-dir", default="ch-data", help="Directory containing CH bulk CSVs.")
    parser.add_argument("--output", default="ch-leads.csv", help="Output CSV path.")
    parser.add_argument("--min-score", type=int, default=30, help="Minimum score to include (0-100).")
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    csv_files = sorted(input_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {input_dir}", file=sys.stderr)
        return 1

    print(f"Found {len(csv_files)} CSV file(s) in {input_dir}", file=sys.stderr)
    process_files(csv_files, Path(args.output), args.min_score)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
