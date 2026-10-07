from __future__ import annotations

import json

import pytest

from scripts.website_analysis import search_providers as sp

# Hand-written from each provider's documentation; replaced by real recordings
# after the first pilot call (docs/WEB_STAGE.md).
SERPER_MAPS = {"places": [{"position": 1, "title": "Piperfinn", "address": "4 Park Row, Leeds LS1 4AP",
                           "latitude": 53.8, "longitude": -1.55, "rating": 4.8, "ratingCount": 112,
                           "type": "Shoe store", "types": ["Shoe store", "Children's clothing store"],
                           "website": "https://www.piperfinn.com/", "phoneNumber": "0113 000 0000",
                           "cid": "123456789", "placeId": "ChIJabc"}], "credits": 3}
SERPER_SEARCH = {"organic": [{"position": 1, "title": "Piperfinn", "link": "https://shop.piperfinn.com/about",
                              "snippet": "Leather baby shoes"}],
                 "knowledgeGraph": {"title": "Piperfinn", "type": "Shoe store", "website": "https://piperfinn.com"}}
DFS_KEYWORDS = {"cost": 0.09, "tasks": [{"status_code": 20000, "result": [
    {"keyword": "baby shoes", "search_volume": 9900, "cpc": 0.61, "competition": "HIGH", "competition_index": 100}]}]}
DFS_SERP = {"cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "paid", "rank_absolute": 1, "title": "Ad", "url": "https://rival.co.uk/shoes"},
    {"type": "local_pack", "rank_absolute": 2, "title": "Piperfinn", "domain": "piperfinn.com", "cid": "1"},
    {"type": "organic", "rank_absolute": 3, "title": "Piperfinn", "url": "https://www.piperfinn.com/"}]}]}]}
SERPAPI_ADS = {"search_information": {"total_results": 7}, "ad_creatives": [
    {"advertiser_id": "AR1", "advertiser": "Piperfinn Ltd", "target_domain": "piperfinn.com", "format": "text",
     "first_shown": 1700000000, "last_shown": 1760000000, "total_days_shown": 40}]}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture()
def keys(monkeypatch):
    for name in ("SERPER_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPAPI_API_KEY"):
        monkeypatch.setenv(name, "test-" + name.lower())


def _client(tmp_path, session, **kwargs):
    return sp.SearchClient(cache_dir=tmp_path / "cache", ledger=tmp_path / "usage.jsonl", session=session,
                           sleep=lambda _: None, **kwargs)


def test_registrable_domain_and_postcode():
    assert sp.registrable_domain("https://shop.example.co.uk/x?y=1") == "example.co.uk"
    assert sp.registrable_domain("www.example.com") == "example.com"
    assert sp.registrable_domain("http://192.168.0.1/") is None
    assert sp.normalise_postcode("4 Park Row, Leeds ls14ap") == "LS1 4AP"


def test_maps_search_parses_listing_caches_and_ledgers_credits(tmp_path, keys):
    session = FakeSession(FakeResponse(SERPER_MAPS))
    client = _client(tmp_path, session)
    listings = client.maps_search("Piperfinn", lat=53.8, lng=-1.55)
    assert listings[0]["category"] == "Shoe store" and listings[0]["domain"] == "piperfinn.com"
    assert listings[0]["postcode"] == "LS1 4AP" and listings[0]["rating_count"] == 112
    assert session.calls[0][2]["json"]["ll"] == "@53.800000,-1.550000,14z"
    assert sp.ledger_usage("serper", tmp_path / "usage.jsonl") == 3      # the response's own credit count
    again = client.maps_search("Piperfinn", lat=53.8, lng=-1.55)         # cached: no second request
    assert again == listings and len(session.calls) == 1


def test_cache_file_and_ledger_never_hold_the_key(tmp_path, keys):
    client = _client(tmp_path, FakeSession(FakeResponse(SERPAPI_ADS)))
    client.ads_transparency("piperfinn.com")
    written = "".join(p.read_text() for p in (tmp_path / "cache").rglob("*.json"))
    written += (tmp_path / "usage.jsonl").read_text()
    assert "test-serpapi_api_key" not in written


def test_cache_only_raises_instead_of_sending(tmp_path):
    session = FakeSession()
    client = _client(tmp_path, session, cache_only=True)
    with pytest.raises(sp.CacheMiss):
        client.web_search("Piperfinn")
    assert session.calls == []


def test_allowance_cap_stops_before_sending(tmp_path, keys):
    session = FakeSession()
    client = _client(tmp_path, session, limits={"serper": 2})
    (tmp_path / "usage.jsonl").write_text(json.dumps({"at": "2026-10-01T00:00:00+00:00", "provider": "serper",
                                                      "used": 2}) + "\n")
    with pytest.raises(sp.AllowanceExhausted):
        client.web_search("Piperfinn")
    assert session.calls == []


def test_monthly_allowance_counts_only_this_month(tmp_path):
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in (
        {"at": "2026-09-30T23:00:00+00:00", "provider": "serpapi", "used": 1},
        {"at": "2026-10-01T09:00:00+00:00", "provider": "serpapi", "used": 1})) + "\n{half a line")
    from datetime import datetime, timezone
    assert sp.ledger_usage("serpapi", ledger, now=datetime(2026, 10, 5, tzinfo=timezone.utc)) == 1


def test_web_search_parses_organic_and_knowledge_graph(tmp_path, keys):
    client = _client(tmp_path, FakeSession(FakeResponse(SERPER_SEARCH)))
    result = client.web_search("Piperfinn")
    assert result["organic"][0]["domain"] == "piperfinn.com"
    assert result["knowledge_graph"]["type"] == "Shoe store"


def test_dataforseo_keywords_and_serp_bill_from_cost(tmp_path, keys):
    client = _client(tmp_path, FakeSession(FakeResponse(DFS_KEYWORDS), FakeResponse(DFS_SERP)))
    rows = client.keyword_volume(["baby shoes"])
    assert rows[0]["search_volume"] == 9900 and rows[0]["cpc"] == 0.61
    serp = client.web_search_with_ads("baby shoes", lat=53.8, lng=-1.55)
    assert serp["paid"][0]["domain"] == "rival.co.uk"
    assert serp["local_pack"][0]["domain"] == "piperfinn.com" and serp["organic"][0]["rank"] == 3
    assert round(sp.ledger_usage("dataforseo", tmp_path / "usage.jsonl"), 3) == 0.092


def test_dataforseo_task_error_raises_and_is_not_cached(tmp_path, keys):
    failed = {"cost": 0, "tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    client = _client(tmp_path, FakeSession(FakeResponse(failed)))
    with pytest.raises(sp.ProviderError):
        client.keyword_volume(["baby shoes"])
    assert not list((tmp_path / "cache").rglob("*.json"))


def test_no_search_results_is_an_empty_answer_cached_and_billed(tmp_path, keys):
    empty = {"cost": 0.002, "tasks": [{"status_code": 40102, "status_message": "No Search Results.", "result": None}]}
    session = FakeSession(FakeResponse(empty))
    client = _client(tmp_path, session)
    assert client.ads_search("psg-law.co.uk") == {"total_results": 0, "creatives": []}
    assert client.ads_search("psg-law.co.uk")["total_results"] == 0 and len(session.calls) == 1   # cached
    assert sp.ledger_usage("dataforseo", tmp_path / "usage.jsonl") == 0.002


def test_a_billed_failed_task_reaches_the_ledger_before_raising(tmp_path, keys):
    failed = {"cost": 0.002, "tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    client = _client(tmp_path, FakeSession(FakeResponse(failed)))
    with pytest.raises(sp.ProviderError):
        client.ads_search("example.com")
    record = json.loads((tmp_path / "usage.jsonl").read_text())
    assert record["used"] == 0.002 and record["note"] == "failed call, still billed"
    assert not list((tmp_path / "cache").rglob("*.json"))


def test_retries_on_429_then_succeeds(tmp_path, keys):
    session = FakeSession(FakeResponse({}, status=429), FakeResponse(SERPER_SEARCH))
    client = _client(tmp_path, session)
    assert client.web_search("Piperfinn")["organic"]
    assert len(session.calls) == 2


def test_ads_transparency_parse(tmp_path, keys):
    client = _client(tmp_path, FakeSession(FakeResponse(SERPAPI_ADS)))
    result = client.ads_transparency("piperfinn.com")
    assert result["total_results"] == 7 and result["creatives"][0]["advertiser"] == "Piperfinn Ltd"


DFS_USER = {"tasks": [{"status_code": 20000, "result": [{"login": "someone@example.com", "money": {
    "total": 0, "balance": 1.0, "limits": {"day": {"total": 0}}}}]}]}
DFS_CATEGORIES = {"tasks": [{"status_code": 20000, "result": [
    {"category_name": "dental_clinic", "business_count": 120000},
    {"category_name": "kitchen_remodeler", "business_count": 30000}]}]}


def test_dataforseo_account_is_free_and_never_cached(tmp_path, keys):
    session = FakeSession(FakeResponse(DFS_USER), FakeResponse(DFS_USER))
    client = _client(tmp_path, session)
    assert client.dataforseo_account() == {"login": "someone@example.com", "balance": 1.0, "total_paid": 0,
                                           "day_limit": {"total": 0}}
    client.dataforseo_account()
    assert len(session.calls) == 2 and session.calls[0][0] == "GET"
    assert session.calls[0][2]["auth"] == ("test-dataforseo_login", "test-dataforseo_password")
    assert not (tmp_path / "usage.jsonl").exists()


def test_business_categories_cached_and_unledgered(tmp_path, keys):
    session = FakeSession(FakeResponse(DFS_CATEGORIES))
    client = _client(tmp_path, session)
    assert [c["category_name"] for c in client.business_categories()] == ["dental_clinic", "kitchen_remodeler"]
    assert client.business_categories()[0]["business_count"] == 120000
    assert len(session.calls) == 1 and not (tmp_path / "usage.jsonl").exists()


def test_check_reports_keys_and_live_balance(tmp_path, monkeypatch, keys):
    monkeypatch.delenv("SERPAPI_API_KEY")
    lines: list[str] = []
    assert sp.check(_client(tmp_path, FakeSession(FakeResponse(DFS_USER))), out=lines.append)
    flat = [" ".join(line.split()) for line in lines]
    assert "serpapi missing SERPAPI_API_KEY" in flat
    assert any("login ok (someone@example.com); live balance $1.0, ledger allows $1 more" in line for line in flat)


def test_check_explains_a_failed_login(tmp_path, keys):
    lines: list[str] = []
    session = FakeSession(FakeResponse({"status_code": 40100, "status_message": "auth"}, status=401))
    assert not sp.check(_client(tmp_path, session), out=lines.append)
    assert any("API password" in line for line in lines)


DFS_MAPS = {"cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "maps_search", "title": "Bott & Co Solicitors", "category": "Law firm",
     "additional_categories": ["Solicitor"], "category_ids": ["law_firm", "solicitor"],
     "url": "https://www.bottonline.co.uk/", "domain": "www.bottonline.co.uk", "address": "Wilmslow",
     "address_info": {"zip": "SK9 1HQ"}, "rating": {"value": 4.7, "votes_count": 900}, "cid": 99,
     "is_claimed": True}]}]}]}
DFS_ORGANIC = {"cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "knowledge_graph", "title": "Bott & Co", "sub_title": "Law firm", "url": "https://www.bottonline.co.uk"},
    {"type": "organic", "rank_group": 1, "title": "Bott and Co", "url": "https://www.bottonline.co.uk/about",
     "description": "Flight delay claims"}]}]}]}
DFS_ADS = {"cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "ads_search", "advertiser_id": "AR1", "title": "Bott & Co Solicitors Ltd", "verified": True,
     "format": "text", "first_shown": "2023-01-01 00:00:00 +00:00", "last_shown": "2026-09-30 00:00:00 +00:00"}]}]}]}


def test_dataforseo_answers_the_identity_searches_in_the_same_shape(tmp_path, keys):
    session = FakeSession(FakeResponse(DFS_MAPS), FakeResponse(DFS_MAPS), FakeResponse(DFS_ORGANIC))
    client = _client(tmp_path, session, identity_provider="dataforseo")
    listing = client.maps_search("Bott and Co Solicitors", lat=53.3, lng=-2.2)[0]
    assert listing["category"] == "Law firm" and listing["categories"] == ["Law firm", "Solicitor"]
    assert listing["domain"] == "bottonline.co.uk" and listing["postcode"] == "SK9 1HQ"
    assert listing["rating"] == 4.7 and listing["rating_count"] == 900 and listing["is_claimed"] is True
    assert session.calls[0][2]["json"][0]["location_coordinate"] == "53.3000000,-2.2000000,14z"
    assert client.places_search("Bott and Co Solicitors")[0]["source"] == "dataforseo_maps"
    organic = client.web_search("Bott and Co Solicitors")
    assert organic["organic"][0]["domain"] == "bottonline.co.uk"
    assert organic["knowledge_graph"]["type"] == "Law firm"
    assert not (tmp_path / "usage.jsonl").read_text().count("serper")


def test_ads_search_parses_transparency_items(tmp_path, keys):
    client = _client(tmp_path, FakeSession(FakeResponse(DFS_ADS)))
    result = client.ads_search("bottonline.co.uk")
    assert result["total_results"] == 1 and result["creatives"][0]["verified"] is True


def test_cheap_searches_still_run_below_the_keyword_tasks_worst_case(tmp_path, keys):
    (tmp_path / "usage.jsonl").write_text(json.dumps({"at": "2026-10-01T00:00:00+00:00", "provider": "dataforseo",
                                                      "used": 0.95}) + "\n")
    client = _client(tmp_path, FakeSession(FakeResponse(DFS_ADS)))
    assert client.ads_search("bottonline.co.uk")["total_results"] == 1          # $0.05 left covers a $0.002 search
    with pytest.raises(sp.AllowanceExhausted):
        client.keyword_volume(["solicitors"])                                   # but not a $0.09 task


DFS_TRAFFIC = {"cost": 0.0121, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"target": "storefirst.com", "metrics": {"organic": {"etv": 9000.5, "count": 800},
                                              "paid": {"etv": 1200.0, "count": 45}, "local_pack": None}}]}]}]}
def test_labs_traffic_asks_for_organic_and_map_presence_only(tmp_path, keys):
    session = FakeSession(FakeResponse(DFS_TRAFFIC))
    client = _client(tmp_path, session)
    traffic = client.bulk_traffic(["storefirst.com"])
    assert traffic == [{"target": "storefirst.com", "organic_etv": 9000.5, "organic_count": 800, "paid_etv": 1200.0,
                        "paid_count": 45, "local_pack_etv": None, "local_pack_count": None}]
    assert session.calls[0][2]["json"][0]["item_types"] == ["organic", "local_pack"]
    assert round(sp.ledger_usage("dataforseo", tmp_path / "usage.jsonl"), 4) == 0.0121


@pytest.fixture()
def friend_keys(monkeypatch):
    monkeypatch.setenv("DATAFORSEO_FRIEND_LOGIN", "friend@example.com")
    monkeypatch.setenv("DATAFORSEO_FRIEND_PASSWORD", "friend-api-password")


def test_friend_account_has_no_default_cap(tmp_path, keys, friend_keys):
    session = FakeSession()
    client = _client(tmp_path, session, dataforseo_account="friend")
    assert client.dataforseo_provider == "dataforseo:friend" and client.remaining("dataforseo:friend") == 0
    with pytest.raises(sp.AllowanceExhausted):
        client.ads_search("example.com")
    assert session.calls == []


def test_friend_account_uses_its_own_keys_ledger_and_cap(tmp_path, keys, friend_keys):
    session = FakeSession(FakeResponse(DFS_ADS))
    client = _client(tmp_path, session, dataforseo_account="friend", limits={"dataforseo:friend": 0.50})
    assert client.ads_search("example.com")["total_results"] == 1
    assert session.calls[0][2]["auth"] == ("friend@example.com", "friend-api-password")
    ledger = tmp_path / "usage.jsonl"
    assert sp.ledger_usage("dataforseo:friend", ledger) == 0.002 and sp.ledger_usage("dataforseo", ledger) == 0
    assert client.billed == {"dataforseo:friend": 0.002}
    assert client.remaining("dataforseo:friend") == pytest.approx(0.498)


def test_both_accounts_share_one_cache(tmp_path, keys, friend_keys):
    mine = _client(tmp_path, FakeSession(FakeResponse(DFS_ADS)))
    mine.ads_search("example.com")
    session = FakeSession()
    friend = _client(tmp_path, session, dataforseo_account="friend")   # no allowance at all, but the call is cached
    assert friend.ads_search("example.com")["total_results"] == 1 and session.calls == []
    assert not (tmp_path / "usage.jsonl").read_text().count("friend")
    assert not list((tmp_path / "cache").glob("dataforseo:*"))          # the cache folder name is shared, and valid on Windows


def test_unknown_account_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _client(tmp_path, FakeSession(), dataforseo_account="someone")


def test_check_reports_both_accounts(tmp_path, keys, friend_keys, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY")
    friend_user = {"tasks": [{"status_code": 20000, "result": [{"login": "friend@example.com",
                                                                "money": {"total": 50, "balance": 12.5}}]}]}
    session = FakeSession(FakeResponse(DFS_USER), FakeResponse(friend_user))
    lines: list[str] = []
    assert sp.check(_client(tmp_path, session), out=lines.append)
    flat = [" ".join(line.split()) for line in lines]
    assert "dataforseo login ok (someone@example.com); live balance $1.0, ledger allows $1 more" in flat
    assert "dataforseo:friend login ok (friend@example.com); live balance $12.5, ledger allows $0 more" in flat
    assert session.calls[1][2]["auth"][0] == "friend@example.com"


def test_usage_report_lists_the_friend_account(tmp_path):
    names = [row["provider"] for row in sp.usage_report(tmp_path / "none.jsonl")]
    assert names == ["serper", "dataforseo", "dataforseo:friend", "serpapi"]


def test_postcode_location_is_cached_and_unledgered(tmp_path):
    payload = {"result": {"postcode": "LS1 4AP", "latitude": 53.8, "longitude": -1.55, "admin_district": "Leeds"}}
    session = FakeSession(FakeResponse(payload))
    client = _client(tmp_path, session)
    assert client.postcode_location("ls14ap")["district"] == "Leeds"
    assert client.postcode_location("LS1 4AP")["latitude"] == 53.8
    assert len(session.calls) == 1 and not (tmp_path / "usage.jsonl").exists()


def test_parallel_calls_cannot_overshoot_the_allowance(tmp_path, monkeypatch):
    import threading
    import time as _time
    from scripts.website_analysis import search_providers as sp_mod
    monkeypatch.setenv("SERPER_API_KEY", "k")
    client = sp_mod.SearchClient(cache_dir=tmp_path / "cache", ledger=tmp_path / "ledger.jsonl",
                                 limits={"serper": 3}, sleep=lambda s: None)
    monkeypatch.setattr(client, "_http", lambda *a, **k: (_time.sleep(0.05), {"credits": 1, "places": []})[1])
    outcomes = []

    def call(i):
        try:
            client.places_search(f"query {i}")
            outcomes.append("ok")
        except sp_mod.AllowanceExhausted:
            outcomes.append("refused")

    threads = [threading.Thread(target=call, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("ok") == 3 and outcomes.count("refused") == 5
    assert sp_mod.ledger_usage("serper", tmp_path / "ledger.jsonl") == 3
