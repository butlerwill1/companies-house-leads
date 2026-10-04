from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from core.companies_house_sqlite import init_db
from scripts.web import web_market as M

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_ads_summary_marks_recent_advertisers():
    result = {"creatives": [
        {"title": "Bott & Co", "verified": True, "format": "text",
         "first_shown": "2023-01-05 00:00:00 +00:00", "last_shown": "2026-09-28 10:00:00 +00:00"},
        {"title": "Bott & Co", "format": "image", "first_shown": "2024-02-01 00:00:00 +00:00",
         "last_shown": "2025-01-01 00:00:00 +00:00"}]}
    summary = M.ads_summary(result, now=NOW)
    assert summary["ads_seen"] == 2 and summary["advertising_now"] is True
    assert summary["first_shown"] == "2023-01-05" and summary["last_shown"] == "2026-09-28"
    assert summary["advertisers"] == ["Bott & Co"] and summary["formats"] == ["image", "text"]


def test_ads_summary_old_or_no_ads_is_not_advertising_now():
    old = {"creatives": [{"last_shown": "2026-06-01 00:00:00 +00:00"}]}
    assert M.ads_summary(old, now=NOW)["advertising_now"] is False
    assert M.ads_summary({"creatives": []}, now=NOW) == {
        "ads_seen": 0, "advertising_now": False, "first_shown": None, "last_shown": None, "advertisers": [],
        "verified": False, "formats": []}


def test_seed_map_accepts_both_shapes_and_dedupes():
    assert M.seed_map({"_note": "drafted by hand", "1": ["Flight Delay Claims", "flight  delay claims"],
                       "2": {"keywords": ["self storage"]}}) == {"1": ["flight delay claims"], "2": ["self storage"]}


def test_keyword_market_totals_and_weighted_cpc():
    rows = [{"keyword": "self storage", "search_volume": 1000, "cpc": 2.0},
            {"keyword": "storage units", "search_volume": 3000, "cpc": 1.0},
            {"keyword": "rare phrase", "search_volume": None, "cpc": None}]
    market = M.keyword_market(rows, ["self storage", "storage units", "rare phrase", "unknown phrase"])
    assert market["total_volume"] == 4000
    assert market["monthly_click_value_usd"] == 5000.0
    assert market["weighted_cpc_usd"] == 1.25
    assert market["keywords"][3]["search_volume"] is None


def test_serp_observation_finds_company_and_rivals():
    serp = {"paid": [{"domain": "rival.co.uk"}, {"domain": "storefirst.com"}, {"domain": "rival.co.uk"}],
            "organic": [{"domain": "big.com"}, {"domain": "storefirst.com"}],
            "local_pack": [{"domain": "storefirst.com"}]}
    obs = M.serp_observation(serp, "www.storefirst.com")
    assert obs == {"in_ads": True, "organic_position": 2, "in_local_pack": True,
                   "advertisers": ["rival.co.uk", "storefirst.com"], "advertiser_count": 2}
    assert M.serp_observation(serp, None)["in_ads"] is False


def test_best_listing_prefers_domain_then_brand_then_registered_name():
    listings = [{"title": "Millie's House Fulham", "domain": "other.net"}, {"title": "X", "domain": "millieshouse.net"}]
    assert M.best_listing(listings, domain="millieshouse.net", brand="Millie's House", registered_name="SWLNC")["match"] \
        == "domain"
    assert M.best_listing(listings, domain=None, brand="Millie's House", registered_name="SWLNC")["match"] == "brand"
    assert M.best_listing(listings, domain=None, brand=None, registered_name="Unrelated Name") is None


# ---------------------------------------------------------------- the run

class FakeClient:
    dataforseo_provider = "dataforseo"

    def __init__(self):
        self.calls, self.billed = [], {}

    def _spend(self, amount):
        self.billed["dataforseo"] = self.billed.get("dataforseo", 0) + amount

    def bulk_traffic(self, targets):
        self.calls.append(("traffic", tuple(targets)))
        self._spend(0.012)
        return [{"target": "storefirst.com", "organic_etv": 9000.0, "organic_count": 800, "paid_etv": 1200.0,
                 "paid_count": 45, "local_pack_etv": 300.0},
                {"target": "quiet.co.uk", "organic_etv": 50.0, "organic_count": 4, "paid_etv": 0, "paid_count": 0,
                 "local_pack_etv": None}]

    def keyword_volume(self, phrases):
        self.calls.append(("keywords", tuple(phrases)))
        self._spend(0.09)
        return [{"keyword": p, "search_volume": 100 * (i + 1), "cpc": 1.5, "competition": "HIGH",
                 "competition_index": 80, "high_top_of_page_bid": 3.0} for i, p in enumerate(phrases)]

    def ads_search(self, domain):
        self.calls.append(("ads", domain))
        self._spend(0.002)
        if domain == "storefirst.com":
            return {"creatives": [{"title": "Pay Store Ltd", "verified": True, "format": "text",
                                   "first_shown": "2023-01-01 00:00:00 +00:00",
                                   "last_shown": datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00 +00:00")}]}
        return {"creatives": []}

    def postcode_location(self, postcode):
        self.calls.append(("postcode", postcode))
        return {"latitude": 53.0, "longitude": -2.0}

    def web_search_with_ads(self, keyword, *, lat, lng):
        self.calls.append(("serp", keyword, lat, lng))
        self._spend(0.002)
        return {"paid": [{"domain": "rival.co.uk"}], "organic": [{"domain": "storefirst.com"}], "local_pack": []}

    def maps_search(self, query, *, lat, lng, zoom=14):
        self.calls.append(("maps", query))
        self._spend(0.002)
        return [{"title": "Store First Burnley", "domain": "storefirst.com", "category": "Self-storage facility",
                 "rating": 4.9, "rating_count": 900, "is_claimed": True, "latitude": 53.8, "longitude": -2.3}]


COMPANIES = [
    {"company_number": "1", "company_name": "STORE FIRST SELF STORAGE LTD", "brand": "Store First",
     "domain": "storefirst.com", "postcode": "BB12 7NG"},
    {"company_number": "2", "company_name": "QUIET LTD", "brand": "Quiet", "domain": "quiet.co.uk", "postcode": "LS1 4AP"},
    {"company_number": "3", "company_name": "NO SITE LTD", "brand": None, "domain": None, "postcode": None},
]
SEEDS = {"1": ["self storage", "storage units"], "2": ["quiet thing"]}


def test_assess_market_batches_first_then_per_company_and_makes_no_live_check_by_default(tmp_path):
    client = FakeClient()
    records = M.assess_market(COMPANIES, SEEDS, client, checkpoint=tmp_path / "cp.jsonl", log=lambda _: None)
    assert [c[0] for c in client.calls][:2] == ["traffic", "keywords"]
    assert client.calls[0] == ("traffic", ("quiet.co.uk", "storefirst.com"))
    assert client.calls[1] == ("keywords", ("self storage", "storage units", "quiet thing"))
    assert not [c for c in client.calls if c[0] == "serp"]
    store, quiet, nosite = records
    assert store["ads"]["advertising_now"] is True and store["ads"]["advertisers"] == ["Pay Store Ltd"]
    assert store["organic"] == {"organic_etv": 9000.0, "organic_count": 800, "local_pack_etv": 300.0}
    assert quiet["ads"]["ads_seen"] == 0 and quiet["ads"]["advertising_now"] is False
    assert nosite["ads"] is None and nosite["organic"] is None and nosite["market"]["total_volume"] == 0
    assert "paid_keywords" not in store and ("ads", "None") not in client.calls


def test_live_check_is_optional_and_uses_the_highest_volume_phrase(tmp_path):
    client = FakeClient()
    records = M.assess_market(COMPANIES, SEEDS, client, serp_checks=1, checkpoint=None, log=lambda _: None)
    check = records[0]["serp_checks"][0]
    assert check["keyword"] == "storage units" and check["in_ads"] is False and check["organic_position"] == 1
    assert check["advertisers"] == ["rival.co.uk"] and records[0]["location_source"] == "registered postcode"
    assert records[2]["serp_checks"] == []                      # no site: nothing to check


def test_checkpoint_skips_finished_companies_and_makes_no_repeat_calls(tmp_path):
    path = tmp_path / "cp.jsonl"
    M.assess_market(COMPANIES, SEEDS, FakeClient(), checkpoint=path, log=lambda _: None)
    again = FakeClient()
    records = M.assess_market(COMPANIES, SEEDS, again, checkpoint=path, log=lambda _: None)
    assert again.calls == [] and len(records) == 3
    path.write_text(path.read_text() + '{"company_number": "9')           # a torn final line is ignored
    assert set(M.load_checkpoint(path)) == {"1", "2", "3"}
    more = COMPANIES + [{"company_number": "4", "company_name": "NEW LTD", "domain": "new.co.uk"}]
    third = FakeClient()
    M.assess_market(more, {**SEEDS, "4": ["new phrase"]}, third, checkpoint=path, log=lambda _: None)
    assert third.calls[0] == ("traffic", ("new.co.uk",))               # only the new company needs work


def test_keyword_batches_are_split_at_1000(tmp_path):
    seeds = {"1": [f"phrase {i}" for i in range(1500)]}
    client = FakeClient()
    M.assess_market([COMPANIES[0]], seeds, client, checkpoint=None, log=lambda _: None)
    assert [len(c[1]) for c in client.calls if c[0] == "keywords"] == [1000, 500]


def test_the_listing_lookup_is_for_the_hand_supplied_harness_only(tmp_path):
    client = FakeClient()
    records = M.assess_market([COMPANIES[0]], SEEDS, client, lookup_listings=True, serp_checks=1, checkpoint=None,
                              log=lambda _: None)
    assert records[0]["listing"]["category"] == "Self-storage facility" and records[0]["listing"]["match"] == "domain"
    assert records[0]["location_source"] == "listing"
    plain = FakeClient()
    M.assess_market([COMPANIES[0]], SEEDS, plain, checkpoint=None, log=lambda _: None)
    assert not [c for c in plain.calls if c[0] == "maps"]


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_store_market_writes_all_three_tables_and_preserves_the_gap_rollup(tmp_path):
    conn = _db(tmp_path)
    client = FakeClient()
    records = M.assess_market(COMPANIES, SEEDS, client, serp_checks=1, checkpoint=None, log=lambda _: None)
    counts = M.store_market(conn, records)
    assert counts == {"companies": 3, "keywords": 3, "serp_observations": 2}
    row = conn.execute("select domain, ads_seen, advertising_now, advertiser_name, phrase_search_volume, "
                       "weighted_cpc_usd, organic_etv, organic_keywords, local_pack_etv from company_market "
                       "where company_number = '1'").fetchone()
    assert row == ("storefirst.com", 1, 1, "Pay Store Ltd", 300, 1.5, 9000.0, 800, 300.0)
    assert conn.execute("select ads_seen, advertising_now from company_market where company_number = '3'"
                        ).fetchone() == (None, None)
    kw = conn.execute("select search_volume, cpc_usd, location_code from company_keyword_market "
                      "where company_number = '1' and keyword = 'storage units'").fetchone()
    assert kw == (200, 1.5, 2826)
    obs = conn.execute("select in_ads, organic_position, in_local_pack, advertisers from serp_observations "
                       "where company_number = '1'").fetchone()
    assert obs == (0, 1, 0, '["rival.co.uk"]')
    # the findings step's rollup survives a re-store of the market data
    conn.execute("update company_market set gap_count = 4, setup_level = 'partial', gap_segment = 'greenfield' "
                 "where company_number = '1'")
    M.store_market(conn, records)
    assert conn.execute("select gap_count, setup_level, gap_segment from company_market where company_number = '1'"
                        ).fetchone() == (4, "partial", "greenfield")
    assert conn.execute("select count(*) from company_market").fetchone() == (3,)
    assert conn.execute("select count(*) from company_keyword_market").fetchone() == (3,)


def test_summary_rows_for_the_research_sheet():
    client = FakeClient()
    records = M.assess_market(COMPANIES, SEEDS, client, serp_checks=1, lookup_listings=True, checkpoint=None,
                              log=lambda _: None)
    rows = M.summary_rows(records)
    assert rows[0] == M.SUMMARY_HEADER and len(rows) == 4
    assert rows[1][4] == "Self-storage facility" and rows[1][9] == "yes" and rows[1][11] == 9000
    assert rows[3][3] == "" and rows[3][8] == ""
    assert json.loads(json.dumps(records))[0]["company_number"] == "1"       # records are JSON-serialisable
