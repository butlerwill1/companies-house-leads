from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.website_analysis import web_rank_order as R
from scripts.website_analysis import web_review as V


def _db(tmp_path, n=12):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    for i in range(1, n + 1):
        number = f"{i:08d}"
        conn.execute("insert into companies (company_number, company_name, company_status, source_mode, "
                     "profile_payload, updated_at, sic_code_primary) values (?, ?, 'active', 'api', ?, '2026', '47190')",
                     (number, f"COMPANY {i} LTD", json.dumps({"registered_office_address": {"postal_code": "LS1 4AP"}})))
        conn.execute("insert into company_search_screen (company_number, prompt_version, model, input_kind, passes, "
                     "screened_at) values (?, ?, 'm', 'short', 1, '2026')", (number, R.SCREEN_VERSION))
    return conn


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _checkpoint(domains):
    return {number: {"company_number": number, "domain": domain, "tier": tier,
                     "candidates": [], "listing": {"title": "X", "category": "Shop", "address": "Leeds"}}
            for number, (domain, tier) in domains.items()}


def test_draw_is_seeded_blind_first_and_made_once(tmp_path):
    conn = _db(tmp_path)
    cases, selection = tmp_path / "cases", tmp_path / "selection.json"
    drawn = V.draw(conn, count=6, blind=2, seed=7, cases_dir=cases, selection=selection)
    (tmp_path / "second").mkdir()
    again = V.draw(_db(tmp_path / "second"), count=6, blind=2, seed=7, cases_dir=tmp_path / "c2",
                   selection=tmp_path / "s2.json")
    assert drawn == again and len(list(cases.glob("*.json"))) == 6
    blind = [json.loads(p.read_text())["blind"] for p in cases.glob("*.json")]
    assert sum(blind) == 2
    with pytest.raises(SystemExit):
        V.draw(conn, count=6, blind=2, seed=7, cases_dir=cases, selection=selection)


def test_blind_then_review_round_trip_and_score(tmp_path):
    conn = _db(tmp_path, n=4)
    cases = tmp_path / "cases"
    drawn = V.draw(conn, count=4, blind=1, seed=1, cases_dir=cases, selection=tmp_path / "s.json")
    blind_number = drawn[0]
    checkpoint = _checkpoint({drawn[0]: ("right.co.uk", "verified"), drawn[1]: ("wrong.co.uk", "probable"),
                              drawn[2]: (None, "none"), drawn[3]: ("maybe.com", "ambiguous")})

    # The review sheet hides the blind case until its blind verdict is in.
    assert blind_number not in {row[0] for row in V.review_rows(cases, checkpoint)}
    blind_csv = tmp_path / "blind.csv"
    _write(blind_csv, ["company number", "verdict (website domain, or none)", "notes"],
           [[blind_number.lstrip("0"), "https://www.right.co.uk/", ""]])          # Sheets dropped the zeros
    assert V.import_verdicts(blind_csv, kind="blind", reviewer="will", cases_dir=cases, checkpoint=checkpoint) == 1
    assert blind_number in {row[0] for row in V.review_rows(cases, checkpoint)}

    review_csv = tmp_path / "review.csv"
    _write(review_csv, ["company number", "verdict (agree, the correct domain, or none)", "listing ok (y/n)", "notes"],
           [[drawn[0], "agree", "y", ""], [drawn[1], "real.co.uk", "n", ""], [drawn[2], "found.org.uk", "", ""],
            [drawn[3], "none", "", ""]])
    assert V.import_verdicts(review_csv, kind="review", reviewer="will", cases_dir=cases, checkpoint=checkpoint) == 4
    result = V.score(cases, checkpoint)
    assert result["tiers"]["verified"]["precision"] == 1.0
    assert result["verified_probable"] == {"n": 2, "correct": 1, "precision": 0.5,
                                           "interval": V.wilson(1, 2)}
    assert result["coverage"]["with_site"] == 3 and result["coverage"]["covered"] == 1
    assert result["listing"]["judged"] == 2 and result["listing"]["right"] == 1
    assert result["blind_agreement"] == {"n": 1, "agree": 1}


def test_bad_verdict_changes_nothing(tmp_path):
    conn = _db(tmp_path, n=2)
    cases = tmp_path / "cases"
    drawn = V.draw(conn, count=2, blind=0, seed=1, cases_dir=cases, selection=tmp_path / "s.json")
    before = {p.name: p.read_text() for p in cases.glob("*.json")}
    review_csv = tmp_path / "review.csv"
    _write(review_csv, ["company number", "verdict", "notes"], [[drawn[0], "none", ""], [drawn[1], "not a site", ""]])
    with pytest.raises(ValueError):
        V.import_verdicts(review_csv, kind="review", reviewer="will", cases_dir=cases, checkpoint={})
    assert {p.name: p.read_text() for p in cases.glob("*.json")} == before


def test_agree_is_review_only():
    with pytest.raises(ValueError):
        V.parse_verdict("agree", kind="blind", resolver_domain="x.com")
    assert V.parse_verdict("agree", kind="review", resolver_domain=None) == (False, None)


def test_wilson_interval():
    assert V.wilson(0, 0) is None
    low, high = V.wilson(98, 100)
    assert 0.92 < low < 0.94 and 0.99 < high <= 1.0
