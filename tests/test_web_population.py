from __future__ import annotations

import json
import sqlite3

from core.companies_house_sqlite import init_db
from scripts.web import search_providers as sp
from scripts.web import web_population as P
from scripts.web import web_rank_order as R


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _company(conn, number, name, *, postcode="LS1 4AP", passes=1, years=()):
    payload = {"registered_office_address": {"postal_code": postcode, "locality": "Leeds"}}
    conn.execute("insert into companies (company_number, company_name, company_status, source_mode, profile_payload, "
                 "updated_at) values (?, ?, 'active', 'api', ?, '2026-01-01')", (number, name, json.dumps(payload)))
    conn.execute("insert into company_search_screen (company_number, prompt_version, model, input_kind, answer, "
                 "passes, screened_at) values (?, ?, 'm', 'short', 'likely', ?, '2026-09-30')",
                 (number, R.SCREEN_VERSION, passes))
    for year, turnover, profit in years:
        conn.execute("insert into financial_period_summaries (company_number, period_type, financial_year, turnover, "
                     "profit_after_tax, raw_payload, data_source, currency_validation_status) "
                     "values (?, 'current', ?, ?, ?, '{}', 'xhtml', 'unknown')", (number, year, turnover, profit))


def test_queue_order_bands_profit_growth_then_size(tmp_path):
    conn = _db(tmp_path)
    _company(conn, "00000001", "BIG LTD", years=[(2025, 90e6, 5e6)])                    # above the core band
    _company(conn, "00000002", "CORE LOSS LTD", years=[(2025, 20e6, -1e5)])
    _company(conn, "00000003", "CORE FLAT LTD", years=[(2024, 10e6, 1e5), (2025, 9e6, 1e5)])
    _company(conn, "00000004", "CORE GROWING LTD", years=[(2024, 4e6, 1e5), (2025, 5e6, 1e5)])
    _company(conn, "00000005", "TINY LTD", years=[(2025, 2e5, 1e4)])
    _company(conn, "00000006", "REJECTED LTD", passes=0, years=[(2025, 5e6, 1e5)])
    queue = R.build_queue(conn)
    assert [row["company_number"] for row in queue] == ["00000004", "00000003", "00000002", "00000001", "00000005"]
    assert queue[0]["band"] == "core" and queue[3]["band"] == "above" and queue[4]["band"] == "below"


def test_queue_leaves_out_gate_a_duplicates(tmp_path):
    conn = _db(tmp_path)
    _company(conn, "00000001", "HOMES LTD", years=[(2025, 5e6, 1e5)])
    _company(conn, "00000002", "DEVELOPMENTS LTD", years=[(2025, 5e6, 1e5)])
    conn.execute("insert into company_signals (company_number, signal_key, signal_value_type, signal_text, "
                 "created_at, updated_at) values ('00000002', 'duplicate_of', 'text', '00000001', '2026', '2026')")
    assert [row["company_number"] for row in R.build_queue(conn)] == ["00000001"]


def test_queue_is_frozen_until_refresh(tmp_path):
    conn = _db(tmp_path)
    _company(conn, "00000001", "A LTD", years=[(2025, 5e6, 1e5)])
    path = tmp_path / "queue.json"
    assert len(R.load_queue(conn, path=path)) == 1
    _company(conn, "00000002", "B LTD", years=[(2025, 6e6, 1e5)])
    assert len(R.load_queue(conn, path=path)) == 1
    assert len(R.load_queue(conn, path=path, refresh=True)) == 2


def _record(number, tier="verified", domain="piperfinn.com", listing=True):
    candidates = [] if domain is None else [
        {"domain": domain, "tier": tier, "sources": ["maps"], "evidence": {"final_url": f"https://{domain}/"}},
        {"domain": "other.co.uk", "tier": "ambiguous", "sources": ["organic"], "evidence": {"final_url": None}},
        {"domain": "nope.co.uk", "tier": "none", "sources": ["organic"], "evidence": {}}]
    return {"company_number": number, "resolver_version": P.RESOLVER_VERSION, "domain": domain,
            "tier": tier if domain else "none", "candidates": candidates, "resolved_at": "2026-10-01T00:00:00+00:00",
            "listing": {"title": "Piperfinn", "category": "Shoe store", "categories": ["Shoe store"], "rating": 4.8,
                        "rating_count": 10, "domain": domain, "match": "name+postcode", "source": "serper_maps"}
            if listing else None}


def test_store_identity_writes_main_candidates_and_listing_and_replaces(tmp_path):
    conn = _db(tmp_path)
    _company(conn, "00000001", "PIPERFINN LTD")
    _company(conn, "00000002", "NOWHERE LTD")
    counts = P.store_identity(conn, [_record("00000001"), _record("00000002", domain=None, listing=False)])
    assert counts == {"verified": 1, "listing": 1, "none": 1}
    rows = conn.execute("select company_number, domain, role, tier from company_web_identity order by id").fetchall()
    assert rows == [("00000001", "piperfinn.com", "main", "verified"),
                    ("00000001", "other.co.uk", "candidate", "ambiguous"),
                    ("00000002", None, "none", "none")]
    assert conn.execute("select category, match from company_google_listing").fetchall() == [("Shoe store",
                                                                                             "name+postcode")]
    P.store_identity(conn, [_record("00000001", tier="probable")])          # a re-store replaces, never duplicates
    assert conn.execute("select count(*), max(tier) from company_web_identity where company_number = '00000001'"
                        ).fetchone() == (2, "probable")


def test_checkpoint_replays_and_tolerates_a_torn_line(tmp_path):
    path = tmp_path / "cp.jsonl"
    P.append_checkpoint(_record("00000001"), path)
    P.append_checkpoint({**_record("00000002"), "resolver_version": "identity-v0"}, path)
    with path.open("a") as handle:
        handle.write('{"company_number": "0000')
    done = P.load_checkpoint(path)
    assert set(done) == {"00000001"}


def test_run_identity_stops_cleanly_when_the_allowance_runs_out(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _company(conn, "00000001", "A LTD")
    _company(conn, "00000002", "B LTD")
    calls = []

    def fake_resolve(company, client, fetcher, **_):
        calls.append(company["company_number"])
        if len(calls) == 2:
            raise sp.AllowanceExhausted("serper: 0 credits left")
        client.billed["serper"] = client.billed.get("serper", 0) + 2
        return _record(company["company_number"])

    monkeypatch.setattr(P, "resolve", fake_resolve)

    class Client:
        billed: dict = {}

    path = tmp_path / "cp.jsonl"
    summary = P.run_identity(conn, ["00000001", "00000002"], Client(), fetcher=None, checkpoint=path)
    assert summary["finished"] == 1 and summary["stopped"].startswith("AllowanceExhausted")
    done = P.load_checkpoint(path)
    assert set(done) == {"00000001"} and done["00000001"]["search_used"] == {"serper": 2}
    calls.clear()
    P.run_identity(conn, ["00000001"], Client(), fetcher=None, checkpoint=path)     # replayed: no resolve call
    assert calls == []


def test_pick_skips_done_and_gold(tmp_path):
    queue = [{"company_number": n} for n in ("1", "2", "3", "4")]
    assert P.pick(queue, {"1"}, limit=2, exclude={"2"}) == ["3", "4"]


# ---------------------------------------------------------------- the later steps' inputs and commands

import argparse

import pytest


def _identity_row(conn, number, domain, *, role="main", tier="verified", version=None):
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, final_url, "
                 "resolved_at) values (?, ?, ?, ?, ?, ?, '2026')",
                 (number, version or P.RESOLVER_VERSION, domain, role, tier, f"https://{domain}/" if domain else None))


def test_store_identity_also_stores_new_trading_names(tmp_path):
    conn = _db(tmp_path)
    _company(conn, "00000001", "ARA FITNESS LTD")
    record = _record("00000001")
    record["new_trading_names"] = [{"name": "Free Soul", "evidence": "x is a trading name of y", "source": "website"},
                                   {"name": "Free Soul Sistas", "evidence": "listing", "source": "maps_listing"}]
    record["listing"]["is_claimed"] = True
    record["listing"]["category_ids"] = ["supplement_shop"]
    counts = P.store_identity(conn, [record])
    assert counts["trading_names"] == 2
    assert conn.execute("select name, source from company_trading_names order by id").fetchall() == [
        ("Free Soul", "website"), ("Free Soul Sistas", "maps_listing")]
    assert conn.execute("select is_claimed, category_ids from company_google_listing").fetchone() == (
        1, '["supplement_shop"]')
    assert P.company_inputs(conn, ["00000001"])["00000001"]["trading_names"] == ["Free Soul", "Free Soul Sistas"]


def test_the_friends_account_needs_an_explicit_cap():
    args = argparse.Namespace(dataforseo_account="friend", dataforseo_allowance=None, cache_only=False)
    with pytest.raises(SystemExit, match="no default cap"):
        P._client(args)
    ok = P._client(argparse.Namespace(dataforseo_account="friend", dataforseo_allowance=0.5, cache_only=True))
    assert ok.dataforseo_provider == "dataforseo:friend" and ok.remaining("dataforseo:friend") == 0.5
    mine = P._client(argparse.Namespace(dataforseo_account="mine", dataforseo_allowance=None, cache_only=True))
    assert mine.dataforseo_provider == "dataforseo" and mine.remaining("dataforseo") <= 1.0


def test_market_inputs_come_from_identity_listing_and_profile(tmp_path):
    conn = _db(tmp_path)
    for number, name in (("00000001", "ALPHA LTD"), ("00000002", "BETA LTD"), ("00000003", "GAMMA LTD")):
        _company(conn, number, name, years=[(2025, 5e6, 1e5)])
    _identity_row(conn, "00000001", "alpha.co.uk")
    _identity_row(conn, "00000002", "beta.co.uk")
    _identity_row(conn, "00000003", None, role="none", tier="none")
    conn.execute("insert into company_google_listing (company_number, resolver_version, title, category, rating, "
                 "rating_count, latitude, longitude, domain, is_claimed, match, source, found_at) values "
                 "('00000001', 'rv', 'Alpha', 'Law firm', 4.5, 10, 53.8, -1.5, 'alpha.co.uk', 1, 'name+postcode', "
                 "'serper_maps', '2026')")
    for number, phrases in (("00000001", '["a one", "a two"]'), ("00000002", "[]")):
        conn.execute("insert into company_web_profile (company_number, profile_version, model, seed_keywords, "
                     "profiled_at) values (?, 'pv', 'm', ?, '2026')", (number, phrases))
    companies = P.market_companies(conn, ["00000001", "00000003"])
    assert companies[0]["domain"] == "alpha.co.uk" and companies[0]["postcode"] == "LS1 4AP"
    assert companies[0]["listing"]["category"] == "Law firm" and companies[0]["listing"]["latitude"] == 53.8
    assert companies[1]["domain"] is None and companies[1]["listing"] is None
    assert P.profile_seeds(conn, ["00000001", "00000002", "00000003"]) == {"00000001": ["a one", "a two"]}
    queue = [{"company_number": n} for n in ("00000003", "00000002", "00000001")]
    assert P.pick_market_numbers(conn, queue, "mv", limit=5) == ["00000001"]     # site and seeds both needed
    conn.execute("insert into company_market (company_number, market_version, ads_seen, assessed_at) "
                 "values ('00000001', 'mv', 0, '2026')")
    assert P.pick_market_numbers(conn, queue, "mv", limit=5) == []               # already assessed


def test_detect_and_findings_commands_run_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    init_db(conn)
    _company(conn, "00000001", "ALPHA LTD")
    _identity_row(conn, "00000001", "alpha.co.uk")
    conn.execute("insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, status_code, "
                 "fetch_error, word_count, tel_link_count) values ('alpha.co.uk', 'https://alpha.co.uk/', 'home', "
                 "'crawl-v1', 'http', 200, null, 400, 1)")
    conn.commit()
    conn.close()
    from scripts.web.web_fetch import Fetcher, Page
    Fetcher(cache_dir=tmp_path / "data" / "raw" / "web-pages", respect_robots=False).put(Page(
        url="https://alpha.co.uk/", final_url="https://alpha.co.uk/", status=200,
        html="<html><body><a href='tel:01625415800'>Call</a> <script src='https://js.hs-scripts.com/1234567.js'>"
             "</script>" + "word " * 200 + "</body></html>"))
    assert P.main(["--db", str(db), "detect"]) == 0
    assert '"domains": 1' in capsys.readouterr().out
    check = sqlite3.connect(db)
    assert check.execute("select technology from web_technologies").fetchall() == [("HubSpot",)]
    assert check.execute("select crawl_status, has_click_to_call from web_sites").fetchone() == ("ok", 1)
    check.execute("insert into company_market (company_number, market_version, domain, advertising_now, "
                  "phrase_search_volume, assessed_at) values ('00000001', 'market-v1', 'alpha.co.uk', 0, 900, '2026')")
    check.commit()
    check.close()
    assert P.main(["--db", str(db), "findings", "--resolver-version", P.RESOLVER_VERSION]) == 0
    out = capsys.readouterr().out
    assert '"companies": 1' in out and "segment_greenfield" in out
    final = sqlite3.connect(db)
    assert final.execute("select gap_segment from company_market").fetchone() == ("greenfield",)


def test_crawl_dry_run_reports_without_fetching(tmp_path, capsys):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    init_db(conn)
    _company(conn, "00000001", "ALPHA LTD")
    _identity_row(conn, "00000001", "alpha.co.uk")
    _identity_row(conn, "00000001", "beta.co.uk")
    conn.execute("insert into web_pages (domain, url, page_kind, crawl_version, fetched_with) "
                 "values ('beta.co.uk', 'https://beta.co.uk/', 'home', 'crawl-v1', 'http')")
    conn.commit()
    conn.close()
    assert P.main(["--db", str(db), "crawl", "--cache-only"]) == 0
    assert json.loads(capsys.readouterr().out) == {"would_crawl": 1, "already_crawled": 1}


def test_market_requires_a_cap_and_a_company_selection(tmp_path):
    db = tmp_path / "t.db"
    sqlite3.connect(db).close()
    with pytest.raises(SystemExit):
        P.main(["--db", str(db), "market", "--limit", "5"])                     # --dataforseo-allowance is required
    with pytest.raises(SystemExit, match="give --limit N or --numbers"):
        P.main(["--db", str(db), "market", "--dataforseo-allowance", "0.2", "--cache-only"])


def test_parallel_identity_run_checkpoints_every_company(tmp_path, monkeypatch):
    import threading
    conn = sqlite3.connect(tmp_path / "t.db")
    from core.companies_house_sqlite import init_db
    init_db(conn)
    numbers = [f"{i:08d}" for i in range(1, 7)]
    monkeypatch.setattr(P, "company_inputs", lambda c, ns: {n: {"company_number": n, "company_name": n} for n in ns})
    seen = set()

    def fake_resolve(company, client, fetcher, **_):
        seen.add(threading.get_ident())
        return {"company_number": company["company_number"], "resolver_version": "identity-v3-places-first",
                "tier": "verified", "domain": "x.co.uk"}

    monkeypatch.setattr(P, "resolve", fake_resolve)

    class Client:
        billed = {}
        identity_provider = "serper"

        def thread_billed(self):
            return {}

    cp = tmp_path / "cp.jsonl"
    summary = P.run_identity(conn, numbers, Client(), None, checkpoint=cp, workers=3)
    assert summary["finished"] == 6 and summary["stopped"] is None
    assert set(P.load_checkpoint(cp, version=None)) == set(numbers)
    again = P.run_identity(conn, numbers, Client(), None, checkpoint=cp, workers=3)
    assert again["replayed"] == 6 and again["finished"] == 0


def test_store_keeps_a_hand_found_website(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    from core.companies_house_sqlite import init_db
    init_db(conn)
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, sources, "
                 "resolved_at) values ('00000001', 'identity-v2-trading-names', 'real.co.uk', 'main', 'probable', "
                 "'[\"hand_found\"]', '2026')")
    record = {"company_number": "00000001", "resolver_version": "identity-v2-trading-names", "tier": "ambiguous",
              "domain": None, "candidates": [], "listing": None}
    assert P.store_identity(conn, [record]) == {"kept_hand_found": 1}
    assert conn.execute("select domain from company_web_identity").fetchall() == [("real.co.uk",)]


def _versioned_identity_row(conn, number, version, domain, tier, role="main"):
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, sources, "
                 "resolved_at) values (?, ?, ?, ?, ?, '[]', '2026')", (number, version, domain, role, tier))


def test_later_steps_read_each_companys_newest_identity_whatever_its_version(tmp_path):
    from scripts.web import web_crawl
    conn = sqlite3.connect(tmp_path / "t.db")
    from core.companies_house_sqlite import init_db
    init_db(conn)
    _versioned_identity_row(conn, "00000001", "identity-v2-trading-names", "old.co.uk", "probable")
    _versioned_identity_row(conn, "00000002", "identity-v3-places-first", "places.co.uk", "verified")
    _versioned_identity_row(conn, "00000003", "identity-v3-places-first", "maybe.co.uk", "ambiguous", role="candidate")
    _versioned_identity_row(conn, "00000003", "identity-v4-settled", "maybe.co.uk", "probable")   # settled later
    assert P.main_domains(conn) == {"00000001": "old.co.uk", "00000002": "places.co.uk", "00000003": "maybe.co.uk"}
    assert P.main_domains(conn, "identity-v3-places-first") == {"00000002": "places.co.uk"}
    sites = {s["domain"]: s["company_numbers"] for s in web_crawl.domains_to_crawl(conn)}
    assert sites == {"old.co.uk": ["00000001"], "places.co.uk": ["00000002"], "maybe.co.uk": ["00000003"]}
