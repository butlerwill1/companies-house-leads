#!/usr/bin/env python3
"""Gold set for W1 (website identity): draw, review sheets, verdicts, score.

Protocol (docs/WEB_STAGE.md):

- `draw`: a seeded random 100 of the screen-passing companies, recorded in
  `evals/web_identity/selection.json`, one case file each. The first 25 are
  blind.
- `export --sheet blind`: the blind cases with no resolver output. The
  reviewer finds each company's website by hand (or writes `none`) before
  the resolver's answer is visible, so agreement is a fair measurement.
- `export --sheet review`: every case with the resolver's answer (blind cases
  only once their blind verdict is in). The reviewer writes `agree`, the
  correct domain, or `none`, and whether the matched Google listing is the
  company (`y`/`n`).
- `import-verdicts`: validates every row, then writes the verdicts into the
  case files. Google Sheets drops leading zeros, so numbers fall back to
  `zfill(8)`.
- `score`: precision per tier with a Wilson 95% interval, coverage, and
  listing precision, against the pre-registered criteria.

Nothing here calls a provider or a model.

Usage:
    python -m scripts.web.web_review draw
    python -m scripts.web.web_review export --sheet blind --out logs/web/identity-blind.csv
    python -m scripts.web.web_review import-verdicts --csv blind.csv --kind blind --reviewer will
    python -m scripts.web.web_review export --sheet review --out logs/web/identity-review.csv
    python -m scripts.web.web_review import-verdicts --csv review.csv --kind review --reviewer will
    python -m scripts.web.web_review score
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.profile.business_profile_eval import case_files, load_case, save_case, utc_now  # noqa: E402
from scripts.web.search_providers import registrable_domain  # noqa: E402
from scripts.web.web_identity import RESOLVER_VERSION  # noqa: E402
from scripts.web.web_population import DB_DEFAULT, GOLD_DIR, company_inputs, load_checkpoint  # noqa: E402
from scripts.web.web_rank_order import duplicates, passing_companies  # noqa: E402

SELECTION = GOLD_DIR.parent / "selection.json"
SEED = 20261001
COUNT = 100
BLIND = 25

# Pre-registered in docs/WEB_STAGE.md before any labelling or search run.
CRITERIA = {"verified_precision": 0.98, "verified_probable_precision": 0.95, "coverage": 0.70}


# ---------------------------------------------------------------- draw

def draw(conn: sqlite3.Connection, *, count: int = COUNT, blind: int = BLIND, seed: int = SEED,
         cases_dir: Path = GOLD_DIR, selection: Path = SELECTION) -> list[str]:
    if selection.exists():
        raise SystemExit(f"{selection} exists; the draw is made once (delete it deliberately to redraw)")
    population = sorted(set(passing_companies(conn)) - duplicates(conn))  # the queue's population
    drawn = random.Random(seed).sample(population, min(count, len(population)))
    inputs = company_inputs(conn, drawn)
    for position, number in enumerate(drawn):
        company = inputs[number]
        row = conn.execute("select sic_code_primary from companies where company_number = ?", (number,)).fetchone()
        save_case(cases_dir / f"{number}.json", {
            "schema_version": 1, "company_number": number, "company_name": company["company_name"],
            "postcode": company.get("postcode"), "locality": company.get("locality"),
            "sic_code": row[0] if row else None, "blind": position < blind,
            "blind_review": None, "expected": None, "review": None})
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text(json.dumps({"seed": seed, "count": count, "blind": blind, "population": len(population),
                                     "population_rule": "company_search_screen passes = 1 (current screen version), "
                                                        "minus Gate A duplicate_of",
                                     "drawn": drawn, "drawn_at": utc_now()}, indent=1) + "\n", encoding="utf-8")
    return drawn


# ---------------------------------------------------------------- sheets

def _search_link(case: dict[str, Any]) -> str:
    return "https://www.google.com/search?q=" + quote_plus(f"{case['company_name']} {case.get('locality') or ''}")


def blind_rows(cases_dir: Path = GOLD_DIR) -> list[list[Any]]:
    rows: list[list[Any]] = [["company number", "company name", "registered postcode", "locality", "SIC",
                              "search link", "verdict (website domain, or none)", "notes"]]
    for path in case_files(cases_dir):
        case = load_case(path)
        if case.get("blind") and not case.get("blind_review"):
            rows.append([case["company_number"], case["company_name"], case.get("postcode"), case.get("locality"),
                         case.get("sic_code"), _search_link(case), "", ""])
    return rows


def review_rows(cases_dir: Path = GOLD_DIR, checkpoint: dict[str, dict[str, Any]] | None = None) -> list[list[Any]]:
    checkpoint = checkpoint if checkpoint is not None else load_checkpoint()
    rows: list[list[Any]] = [["company number", "company name", "registered postcode", "locality", "SIC",
                              "search link", "resolver domain", "resolver tier", "other candidates",
                              "listing title", "listing category", "listing address",
                              "verdict (agree, the correct domain, or none)", "listing ok (y/n)", "notes"]]
    for path in case_files(cases_dir):
        case = load_case(path)
        if case.get("blind") and not case.get("blind_review"):
            continue  # not labelled blind yet: showing the resolver would anchor the reviewer
        record = checkpoint.get(case["company_number"])
        if record is None:
            continue
        others = [f"{row['domain']} ({row['tier']})" for row in record.get("candidates") or []
                  if row["domain"] != record.get("domain") and row["tier"] != "none"]
        listing = record.get("listing") or {}
        rows.append([case["company_number"], case["company_name"], case.get("postcode"), case.get("locality"),
                     case.get("sic_code"), _search_link(case), record.get("domain") or "none", record.get("tier"),
                     "; ".join(others), listing.get("title"), listing.get("category"), listing.get("address"),
                     "", "", ""])
    return rows


def write_csv(rows: list[list[Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


# ---------------------------------------------------------------- verdicts

def _resolve(number: str, cases_dir: Path) -> Path:
    number = number.strip()
    for candidate in (number, number.zfill(8)):
        path = cases_dir / f"{candidate}.json"
        if path.exists():
            return path
    raise ValueError(f"no case file for company number {number!r} (also tried {number.zfill(8)!r})")


def _column(fieldnames: list[str], prefix: str) -> str | None:
    return next((name for name in fieldnames if name.lower().replace("_", " ").startswith(prefix)), None)


def parse_verdict(verdict: str, *, kind: str, resolver_domain: str | None) -> tuple[bool, str | None]:
    """(has a site, its domain). `agree` takes the resolver's answer."""
    verdict = verdict.strip().lower()
    if verdict == "none":
        return False, None
    if verdict == "agree":
        if kind != "review":
            raise ValueError("'agree' is only for the review sheet")
        return (resolver_domain is not None), resolver_domain
    domain = registrable_domain(verdict)
    if domain is None:
        raise ValueError(f"verdict {verdict!r} is not a domain, 'none' or 'agree'")
    return True, domain


def import_verdicts(csv_path: Path, *, kind: str, reviewer: str, cases_dir: Path = GOLD_DIR,
                    checkpoint: dict[str, dict[str, Any]] | None = None) -> int:
    """Every row is validated before any case file is written, so a typo changes nothing."""
    if kind not in ("blind", "review"):
        raise ValueError(f"kind must be 'blind' or 'review', not {kind!r}")
    checkpoint = checkpoint if checkpoint is not None else load_checkpoint()
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        number_key, verdict_key = _column(fields, "company number"), _column(fields, "verdict")
        listing_key = _column(fields, "listing ok")
        if not number_key or not verdict_key:
            raise ValueError("the CSV needs 'company number' and 'verdict' columns")
        raw = [(row[number_key], (row.get(verdict_key) or "").strip(),
                (row.get(listing_key) or "").strip().lower() if listing_key else "",
                (row.get("notes") or "").strip()) for row in reader]
    updates = []
    for number, verdict, listing_ok, notes in raw:
        if not verdict:
            continue
        path = _resolve(number, cases_dir)
        resolver_domain = (checkpoint.get(path.stem) or {}).get("domain")
        has_site, domain = parse_verdict(verdict, kind=kind, resolver_domain=resolver_domain)
        if listing_ok not in ("", "y", "n"):
            raise ValueError(f"{number}: listing ok must be y, n or blank, not {listing_ok!r}")
        updates.append((path, has_site, domain, listing_ok or None, notes or None))
    now = utc_now()
    for path, has_site, domain, listing_ok, notes in updates:
        case = load_case(path)
        label = {"has_site": has_site, "domain": domain, "notes": notes}
        if kind == "blind":
            case["blind_review"] = {**label, "reviewer": reviewer, "reviewed_at": now}
        else:
            case["expected"] = {**label, "listing_ok": listing_ok}
            case["review"] = {"status": "verified", "reviewer": reviewer, "reviewed_at": now,
                              "resolver_version": RESOLVER_VERSION}
        save_case(path, case)
    return len(updates)


# ---------------------------------------------------------------- score

def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(centre - half, 3), round(centre + half, 3)


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and registrable_domain(a) == registrable_domain(b)


def score(cases_dir: Path = GOLD_DIR, checkpoint: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    checkpoint = checkpoint if checkpoint is not None else load_checkpoint()
    by_tier: dict[str, list[bool]] = {}
    with_site = covered = 0
    listing_judged = listing_right = 0
    blind_pairs = blind_agree = 0
    unscored = 0
    for path in case_files(cases_dir):
        case = load_case(path)
        expected = case.get("expected")
        record = checkpoint.get(case["company_number"])
        if expected is None or record is None:
            unscored += 1
            continue
        tier, domain = record.get("tier") or "none", record.get("domain")
        if tier != "none":
            by_tier.setdefault(tier, []).append(_same(domain, expected.get("domain")))
        if expected.get("has_site"):
            with_site += 1
            covered += tier in ("verified", "probable") and _same(domain, expected.get("domain"))
        if expected.get("listing_ok") in ("y", "n"):
            listing_judged += 1
            listing_right += expected["listing_ok"] == "y"
        blind = case.get("blind_review")
        if blind is not None:
            blind_pairs += 1
            blind_agree += (blind.get("domain") == expected.get("domain"))
    result: dict[str, Any] = {"resolver_version": RESOLVER_VERSION, "unscored": unscored, "tiers": {}}
    for tier in ("verified", "probable", "ambiguous"):
        hits = by_tier.get(tier, [])
        result["tiers"][tier] = {"n": len(hits), "correct": sum(hits),
                                 "precision": round(sum(hits) / len(hits), 3) if hits else None,
                                 "interval": wilson(sum(hits), len(hits))}
    vp = by_tier.get("verified", []) + by_tier.get("probable", [])
    result["verified_probable"] = {"n": len(vp), "correct": sum(vp),
                                   "precision": round(sum(vp) / len(vp), 3) if vp else None,
                                   "interval": wilson(sum(vp), len(vp))}
    result["coverage"] = {"with_site": with_site, "covered": covered,
                          "rate": round(covered / with_site, 3) if with_site else None,
                          "interval": wilson(covered, with_site)}
    result["listing"] = {"judged": listing_judged, "right": listing_right, "interval": wilson(listing_right,
                                                                                               listing_judged)}
    result["blind_agreement"] = {"n": blind_pairs, "agree": blind_agree}
    v, cov = result["tiers"]["verified"]["precision"], result["coverage"]["rate"]
    result["criteria"] = {
        "verified_precision": None if v is None else v >= CRITERIA["verified_precision"],
        "verified_probable_precision": None if result["verified_probable"]["precision"] is None
        else result["verified_probable"]["precision"] >= CRITERIA["verified_probable_precision"],
        "coverage": None if cov is None else cov >= CRITERIA["coverage"],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_DEFAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("draw")
    export = sub.add_parser("export")
    export.add_argument("--sheet", choices=("blind", "review"), required=True)
    export.add_argument("--out", type=Path, required=True)
    verdicts = sub.add_parser("import-verdicts")
    verdicts.add_argument("--csv", type=Path, required=True)
    verdicts.add_argument("--kind", choices=("blind", "review"), required=True)
    verdicts.add_argument("--reviewer", required=True)
    sub.add_parser("score")
    args = parser.parse_args(argv)
    if args.command == "draw":
        conn = sqlite3.connect(args.db)
        try:
            drawn = draw(conn)
        finally:
            conn.close()
        print(f"drew {len(drawn)} cases into {GOLD_DIR} ({BLIND} blind)")
    elif args.command == "export":
        rows = blind_rows() if args.sheet == "blind" else review_rows()
        write_csv(rows, args.out)
        print(f"{len(rows) - 1} rows -> {args.out}")
    elif args.command == "import-verdicts":
        print(f"{import_verdicts(args.csv, kind=args.kind, reviewer=args.reviewer)} verdicts written")
    elif args.command == "score":
        print(json.dumps(score(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
