from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.website_analysis import ads_transparency as at
from scripts.website_analysis.search_providers import CacheMiss


# Hand-written from the public reverse-engineering write-ups' recorded shapes
# (module docstring: field numbers, the mandatory "7", the total range). The
# first live call of each kind is the check: its cached response replaces
# these fixtures.
SUGGESTIONS_NONE = {"1": []}

SUGGESTIONS = {"1": [
    {"1": "Piperfinn Ltd", "2": "AR12956355609636110337", "3": "GB", "4": {"2": {"1": "6", "2": "6"}},
     "5": True, "6": "piperfinn.com"},
    {"1": "Piperfinn Footwear", "2": "AR12956355609636110339", "3": "FR", "4": {"2": {"1": "100", "2": "1k"}}},
]}

CREATIVES_PAGE1 = {
    "1": [
        {"1": "CR10456789012345678", "2": "AR12956355609636110337", "3": {"1": {"1": "Piperfinn Ltd"}},
         "4": {"1": {"2": ["https://tpc.googlesyndication.com/archive/simgad/1234"]},
               "5": "2026-05-02", "6": "2026-10-01"}},
        {"1": "CR10456789012345679", "2": "AR12956355609636110337",
         "5": {"1": {"2": ["https://www.youtube.com/embed/abc123"]},
               "9": {"1": "2026-04-20", "2": "2026-10-12"}}},
    ],
    "2": "cursor-1", "4": 12, "5": 40,
}
CREATIVES_PAGE2 = {
    "1": [{"1": "CR10456789012345680", "2": "AR12956355609636110337",
           "7": 1_760_000_000, "8": 1_760_086_400}],
    "2": None,
}
CREATIVES_UNREADABLE = {"1": [{"1": "CR10456789012345678", "13": {"9": "a creative row without dates"}}], "2": None}

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)

# Minimal structural fixture from the saved 13 October Google response.
LIVE_ROW = {"1": "AR17753547003654045697", "2": "CR09545563105963016193",
            "3": {"3": {"2": '<img src="https://tpc.googlesyndication.com/archive/simgad/15474246341605297075">'},
                  "5": True}, "4": 1,
            "6": {"1": "1778667610", "2": 124555000},
            "7": {"1": "1791911271", "2": 954431000},
            "12": "Rettie & Company Ltd", "14": "rettie.co.uk"}


def test_live_timestamps_ignore_nanoseconds_and_read_string_seconds():
    row = at.parse_creatives({"1": [LIVE_ROW], "4": "41", "5": "41"})["creatives"][0]
    assert row["first_shown"] == "2026-05-13"
    assert row["last_shown"] == "2026-10-13"
    assert row["dates_read"] is True
    assert row["format"] == "TEXT"
    assert row["verified"] is True
    assert row["title"] == "Rettie & Company Ltd"
    assert row["domain"] == "rettie.co.uk"


def test_live_dates_missing_do_not_fall_back_to_nanoseconds():
    row = {**LIVE_ROW, "7": {"2": 954431000}}
    assert at.parse_creatives({"1": [row]})["creatives"][0]["last_shown"] is None


def test_partial_page_failure_preserves_received_ads(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS_NONE),
                          FakeResponse({"1": [LIVE_ROW], "2": "next"}),
                          FakeResponse(status=502))
    result = at.check(_client(tmp_path, session), domain="rettie.co.uk",
                      now=datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert result["status"] == "partial"
    assert result["ads"]["ads_seen"] == 1
    assert result["ads"]["advertising_now"] is True
    assert result["error"]


def test_unmatched_suggestion_does_not_trigger_advertiser_fallback(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS), FakeResponse({"1": []}))
    result = at.check(_client(tmp_path, session), domain="other.example", name="Other Ltd")
    assert len(session.calls) == 2
    assert result["ads"]["ads_seen"] == 0


def test_missing_creatives_schema_is_failure_not_empty(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS_NONE), FakeResponse({"unexpected": []}))
    result = at.check(_client(tmp_path, session), domain="x.example")
    assert result["status"] == "failed"
    assert result["ads"]["ads_seen"] is None
    assert result["ads"]["advertising_now"] is None


@pytest.mark.parametrize("status", [403, 429])
def test_robots_block_stops_before_rpc(tmp_path, no_proxy, status):
    session = FakeSession()
    session.get = lambda *args, **kwargs: FakeResponse(status=status)
    client = _client(tmp_path, session)
    result = at.check(client, domain="rettie.co.uk")
    assert result["status"] == "blocked"
    assert session.calls == []
    with pytest.raises(at.Blocked):
        client.search_suggestions("other.example")


def test_cap_is_partial_and_unreadable_dates_are_unknown(tmp_path, no_proxy):
    row = {**LIVE_ROW, "7": {"2": 954431000}}
    session = FakeSession(FakeResponse(SUGGESTIONS_NONE), FakeResponse({"1": [row], "2": "more"}))
    result = at.check(_client(tmp_path, session), domain="rettie.co.uk", max_ads=1)
    assert result["status"] == "partial" and result["complete"] is False
    assert result["ads"]["ads_seen"] == 1 and result["ads"]["advertising_now"] is None


def test_live_verification_not_overwritten_by_missing_suggestion_field(tmp_path, no_proxy):
    suggestions = {"1": [{"1": {"1": "Rettie & Company Ltd", "2": LIVE_ROW["1"], "3": "GB"}}]}
    session = FakeSession(FakeResponse(suggestions), FakeResponse({"1": [LIVE_ROW]}))
    result = at.check(_client(tmp_path, session), domain="rettie.co.uk", name="Rettie & Company Ltd")
    assert result["ads"]["verified"] is True


def test_refresh_sends_again_instead_of_reusing_cache(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS), FakeResponse(SUGGESTIONS_NONE))
    client = _client(tmp_path, session)
    assert client.search_suggestions("x")
    client.refresh = True
    assert client.search_suggestions("x") == []
    assert len(session.calls) == 2


def test_domain_only_presence_check_needs_one_rpc(tmp_path, no_proxy):
    session = FakeSession(FakeResponse({"1": [LIVE_ROW]}))
    result = at.check(_client(tmp_path, session), domain="rettie.co.uk", domain_only=True)
    assert result["ads"]["ads_seen"] == 1
    assert result["found"] is True and len(session.calls) == 1
    assert "SearchCreatives" in session.calls[0][0]


class FakeResponse:
    def __init__(self, payload=None, *, status=200, ctype="application/json", text=None, url=""):
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": ctype}
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Answers in call order; records (method, url, kwargs) for assertions."""

    def __init__(self, *responses, robots=None):
        self.responses = list(responses)
        self.calls = []
        self._robots = robots

    def get(self, url, **kwargs):
        # no robots.txt published when robots is None: the real host today
        return FakeResponse(text=self._robots or "", status=(404 if self._robots is None else 200))

    def post(self, url, **kwargs):
        answer = self.responses.pop(0)
        response = answer(kwargs["data"]["f.req"]) if callable(answer) else answer
        self.calls.append((url, kwargs))
        return response


@pytest.fixture()
def no_proxy(monkeypatch):
    for name in ("TRANSPARENCY_PROXY_URL", "OXYLABS_USERNAME", "OXYLABS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def _client(tmp_path, session, **kwargs):
    return at.TransparencyClient(cache_dir=tmp_path / "cache", cache_only=kwargs.pop("cache_only", False),
                                 session=session, sleep=lambda _: None, **kwargs)


# ---------------------------------------------------------------- payloads

def test_creatives_payload_mandatory_seven_and_fields():
    payload = at.creatives_payload(query="piperfinn.com")
    assert payload["7"] == {"1": 1, "2": 0}     # without member 7 the answer is {} for anyone
    assert payload["2"] == 40
    assert payload["3"]["12"] == {"1": "piperfinn.com", "2": True}
    assert "13" not in payload["3"] and "4" not in payload
    paged = at.creatives_payload(advertiser_ids=["AR1", "AR1"], cursor="next")
    assert paged["4"] == "next" and paged["3"]["13"] == {"1": ["AR1"]}


def test_region_codes_2000_plus_iso_numeric():
    assert at.region_codes(["GB"]) == [2826]
    assert at.region_codes(["GB", "IE"]) == [2826, 2372]
    assert at.region_codes(None) is None
    with pytest.raises(ValueError):
        at.region_codes(["ZZ"])


def test_suggestions_payload_counts_are_integers():
    assert at.suggestions_payload("nike") == {"1": "nike", "2": 10, "3": 10}


# ---------------------------------------------------------------- parsing

def test_parse_suggestions_names_and_ids_wherever_they_appear():
    parsed = at.parse_suggestions(SUGGESTIONS)
    assert len(parsed) == 2
    assert parsed[0] == {"name": "Piperfinn Ltd", "advertiser_id": "AR12956355609636110337", "region": "GB",
                         "verified": True, "domain": "piperfinn.com"}
    assert parsed[1]["region"] == "FR" and parsed[1]["advertiser_id"].startswith("AR")
    assert parsed[1]["domain"] is None and parsed[1]["verified"] is None


def test_parse_creatives_iso_dates_cursor_and_format():
    parsed = at.parse_creatives(CREATIVES_PAGE1)
    rows = parsed["creatives"]
    assert [r["creative_id"] for r in rows] == ["CR10456789012345678", "CR10456789012345679"]
    assert rows[0]["first_shown"] == "2026-05-02" and rows[0]["last_shown"] == "2026-10-01"
    assert rows[0]["dates_read"] and rows[0]["format"] == "IMAGE"
    assert rows[1]["format"] == "VIDEO" and rows[1]["first_shown"] == "2026-04-20"
    assert parsed["next_cursor"] == "cursor-1" and parsed["total_range"] == (12, 40)


def test_parse_creatives_unix_seconds_and_no_cursor():
    parsed = at.parse_creatives(CREATIVES_PAGE2)
    row = parsed["creatives"][0]
    assert row["first_shown"] == "2025-10-09"         # 1760000000
    assert row["last_shown"] == "2025-10-10"
    assert parsed["next_cursor"] is None


def test_parse_dates_passed_in_milliseconds():
    assert at._dates({"5": {"1": 1_760_000_000_000, "2": 1_760_096_400_000}}) == ("2025-10-09", "2025-10-10")


def test_row_without_dates_or_ids_is_kept_and_marked_unread():
    parsed = at.parse_creatives(CREATIVES_UNREADABLE)
    row = parsed["creatives"][0]
    assert row["creative_id"] == "CR10456789012345678" and row["dates_read"] is False


def test_parse_an_empty_answer():
    assert at.parse_creatives({"1": []}) == {"creatives": [], "next_cursor": None, "total_range": None}
    assert at.parse_creatives({}) == {"creatives": [], "next_cursor": None, "total_range": (0, 0)}
    assert at.parse_suggestions({}) == []


# ---------------------------------------------------------------- plumbing

def test_rpc_posts_form_and_caches_whole_responses(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS), FakeResponse(CREATIVES_PAGE1), FakeResponse(CREATIVES_PAGE2))
    client = _client(tmp_path, session)
    client.search_suggestions("piperfinn.com")
    client.search_creatives(query="piperfinn.com")
    assert len(session.calls) == 3                 # the robots.txt GET is not a post; creatives page twice
    for url, kwargs in session.calls:
        assert "authuser" in url
        assert kwargs["headers"]["User-Agent"] == "Mozilla/5.0 (compatible; companies-house-leads-research/0.1)"
        assert kwargs["proxies"] is None           # no proxy configured for this run
    again = client.search_suggestions("piperfinn.com")      # cached: no fourth post
    assert again[0]["name"] == "Piperfinn Ltd" and len(session.calls) == 3
    cache_files = list((tmp_path / "cache").rglob("*.json"))
    assert len(cache_files) == 3
    assert any(json.loads(p.read_text())["response"] == SUGGESTIONS for p in cache_files)
    assert all(json.loads(p.read_text()).get("fetched_at") for p in cache_files)


def test_cache_only_never_sends(tmp_path):
    session = FakeSession()
    client = _client(tmp_path, session, cache_only=True)
    with pytest.raises(CacheMiss):
        client.search_suggestions("piperfinn.com")
    assert session.calls == []


def test_request_budget_stops_a_run_as_a_local_limit(tmp_path, no_proxy):
    # cursors that keep changing: the repeated-cursor guard must not be what stops this run
    page = {"1": [], "2": "cursor-a"}
    pageb = {"1": [], "2": "cursor-b"}
    pagec = {"1": [], "2": None}
    session = FakeSession(*(FakeResponse(page) if i == 0 else FakeResponse(pageb) if i == 1 else FakeResponse(pagec)
                            for i in range(5)))
    client = _client(tmp_path, session, budget=at.RequestBudget(limit=2))
    with pytest.raises(at.BudgetReached, match="request budget of 2"):
        client.search_creatives(query="piperfinn.com", max_ads=1000)


def test_http_429_and_sorry_redirects_are_blocks_not_results(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(status=429, text="<html>too fast</html>"))
    with pytest.raises(at.Blocked, match="429"):
        _client(tmp_path, session).search_suggestions("x")
    session = FakeSession(FakeResponse(status=302, text="", url="https://www.google.com/sorry/index"))
    with pytest.raises(at.Blocked, match="sorry"):
        _client(tmp_path, session).search_suggestions("x")


def test_non_json_answer_reads_as_a_challenge(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(text="<html>challenge</html>", ctype="text/html"))
    with pytest.raises(at.Blocked, match="challenged"):
        _client(tmp_path, session).search_suggestions("x")


def test_service_error_body_is_a_failure_and_not_cached(tmp_path, no_proxy):
    session = FakeSession(FakeResponse({"error": {"message": "bad request"}}))
    with pytest.raises(at.ProviderDown, match="bad request"):
        _client(tmp_path, session).search_suggestions("x")
    assert list((tmp_path / "cache").rglob("*.json")) == []


def test_other_http_errors_are_a_down_service_not_a_block(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(status=502, text="bad gateway", ctype="text/html"))
    with pytest.raises(at.ProviderDown, match="502"):
        _client(tmp_path, session).search_suggestions("x")


def test_robots_disallowing_the_rpc_paths_blocks_the_run(tmp_path, no_proxy):
    session = FakeSession(robots="user-agent: *\ndisallow: /anji/")
    with pytest.raises(at.Blocked, match="robots"):
        _client(tmp_path, session).search_suggestions("x")


def test_no_robots_file_published_allows_the_run(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS))
    assert _client(tmp_path, session).search_suggestions("x")[0]["name"] == "Piperfinn Ltd"


def test_paging_walks_the_cursor_then_stops(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(CREATIVES_PAGE1), FakeResponse(CREATIVES_PAGE2))
    creatives = _client(tmp_path, session).search_creatives(query="piperfinn.com", max_ads=10)
    assert len(creatives) == 3 and len(session.calls) == 2
    assert '"4":"cursor-1"' in session.calls[1][1]["data"]["f.req"]   # the second page asks with the cursor


def test_a_repeated_cursor_stops_the_run_without_spinning(tmp_path, no_proxy):
    session = FakeSession(*(FakeResponse({"1": [], "2": "same"}) for _ in range(5)))
    with pytest.raises(at.ProviderDown, match="repeated pagination"):
        _client(tmp_path, session).search_creatives(query="x", max_ads=10)
    assert len(session.calls) == 2


# ---------------------------------------------------------------- proxy

def test_proxy_from_transparency_url(monkeypatch):
    monkeypatch.setenv("TRANSPARENCY_PROXY_URL", "http://user:pass@proxy.example:8000")
    assert at.proxy_config() == {"http": "http://user:pass@proxy.example:8000",
                                 "https": "http://user:pass@proxy.example:8000"}


def test_proxy_from_oxylabs_credentials(monkeypatch):
    monkeypatch.setenv("OXYLABS_USERNAME", "user")
    monkeypatch.setenv("OXYLABS_PASSWORD", "pass")
    assert at.proxy_config()["https"] == "http://user:pass@pr.oxylabs.io:7777"
    monkeypatch.setenv("OXYLABS_PROXY_HOST", "residential.oxylabs.io")
    monkeypatch.setenv("OXYLABS_PROXY_PORT", "60000")
    assert at.proxy_config()["http"] == "http://user:pass@residential.oxylabs.io:60000"


def test_no_proxy_set_means_direct_requests(no_proxy):
    assert at.proxy_config() is None


def test_run_through_the_proxy_keeps_credentials_out_of_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSPARENCY_PROXY_URL", "http://user:top-secret@proxy.example:8000")
    session = FakeSession(FakeResponse(SUGGESTIONS))
    client = _client(tmp_path, session)
    client.search_suggestions("x")
    assert session.calls[0][1]["proxies"] == {"http": "http://user:top-secret@proxy.example:8000",
                                              "https": "http://user:top-secret@proxy.example:8000"}
    written = "".join(p.read_text() for p in (tmp_path / "cache").rglob("*.json"))
    assert "top-secret" not in written


# ---------------------------------------------------------------- the check

def test_check_reports_ads_in_the_w4_summary_shape(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS), FakeResponse(CREATIVES_PAGE1), FakeResponse(CREATIVES_PAGE2))
    summary = at.check(_client(tmp_path, session), domain="https://shop.piperfinn.com/", name="Piperfinn Ltd",
                       now=NOW)
    assert summary["domain"] == "piperfinn.com"
    assert summary["found"] is True               # the suggestion carries the domain
    assert summary["ads"]["ads_seen"] == 3
    assert summary["ads"]["advertising_now"] is True      # last shown 2026-10-12 is inside 30 days
    assert summary["ads"]["first_shown"] == "2025-10-09" and summary["ads"]["last_shown"] == "2026-10-12"
    assert summary["ads"]["verified"] is True
    assert summary["ads"]["dates_read"] is True


def test_check_zero_ads_is_a_real_result(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS), FakeResponse({"1": []}),   # creatives by domain
                          FakeResponse({"1": []}))                             # creatives by ids
    summary = at.check(_client(tmp_path, session), domain="piperfinn.com", now=NOW)
    assert summary["found"] is True and summary["creatives"] == [] and len(session.calls) == 3
    assert summary["ads"]["ads_seen"] == 0 and summary["ads"]["advertising_now"] is False


def test_check_other_advertisers_are_not_ours(tmp_path, no_proxy):
    unrelated = {"1": [{"1": "Decathlon", "2": "AR9999999999", "3": "FR"}]}
    session = FakeSession(FakeResponse(unrelated), FakeResponse(CREATIVES_PAGE1), FakeResponse(CREATIVES_PAGE2))
    summary = at.check(_client(tmp_path, session), domain="piperfinn.com", now=NOW)
    assert summary["found"] is False
    assert summary["ads"]["ads_seen"] > 0


def test_check_asks_by_advertiser_ids_when_the_domain_search_answers_nothing(tmp_path, no_proxy):
    many = {"1": [{"1": f"Sibling {i}", "2": f"AR{i:020d}", "6": "piperfinn.com"} for i in range(8)]}
    session = FakeSession(FakeResponse(many),                            # suggestions
                          FakeResponse({"1": []}),                       # creatives by domain
                          FakeResponse(CREATIVES_PAGE1), FakeResponse(CREATIVES_PAGE2))   # creatives by ids
    summary = at.check(_client(tmp_path, session), domain="piperfinn.com", now=NOW)
    assert summary["ads"]["ads_seen"] == 3 and len(session.calls) == 4
    assert '"13":{"1"' in session.calls[2][1]["data"]["f.req"]


def test_check_falls_back_to_the_name_lookup(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS_NONE),          # suggestions for the domain
                          FakeResponse(SUGGESTIONS),               # suggestions for the name
                          FakeResponse({"1": []}),                 # creatives for the domain
                          FakeResponse(CREATIVES_PAGE1), FakeResponse(CREATIVES_PAGE2))   # creatives by ids
    summary = at.check(_client(tmp_path, session), domain="piperfinn.com", name="Piperfinn Footwear", now=NOW)
    assert summary["found"] is True
    assert summary["ads"]["ads_seen"] == 3 and len(session.calls) == 5


def test_check_records_a_block_instead_of_crashing(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(status=429, text="slow down"))
    summary = at.check(_client(tmp_path, session), domain="piperfinn.com", now=NOW)
    assert summary["blocked"] is True and "429" in summary["error"]
    assert summary["creatives"] == [] and summary["found"] is None
    assert summary["ads"]["ads_seen"] is None


def test_check_marks_dates_it_could_not_read(tmp_path, no_proxy):
    session = FakeSession(FakeResponse(SUGGESTIONS_NONE), FakeResponse(CREATIVES_UNREADABLE))
    summary = at.check(_client(tmp_path, session), domain="piperfinn.com", now=NOW)
    assert summary["found"] is False
    assert summary["ads"]["ads_seen"] == 1 and summary["ads"]["dates_read"] is False
    assert summary["ads"]["advertising_now"] is None


# ---------------------------------------------------------------- the CLI

def _identity_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript("""
        create table companies (company_number text primary key, company_name text);
        create table company_web_identity (
            id integer primary key autoincrement, company_number text, resolver_version text,
            domain text, role text);
        create view company_web_identity_current as
        select i.* from company_web_identity i
        where i.resolver_version = (select j.resolver_version from company_web_identity j
            where j.company_number = i.company_number order by j.id desc limit 1);
    """)
    conn.execute("insert into companies values ('13406761', 'MESH AI LTD')")
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role) "
                 "values ('13406761', 'identity-v1', 'meshold.com', 'main')")
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role) "
                 "values ('13406761', 'identity-v2', 'meshai.com', 'main')")
    conn.commit()
    conn.close()


def test_company_identity_reads_the_web_stage_store(tmp_path):
    _identity_db(tmp_path / "companies-house.db")
    assert at.company_identity(tmp_path / "companies-house.db", "13406761") == \
        {"company_name": "MESH AI LTD", "domain": "meshai.com"}
    with pytest.raises(LookupError):
        at.company_identity(tmp_path / "companies-house.db", "99999999")


def _prime_cache(endpoint: str, payload: dict, response: dict) -> None:
    client = at.TransparencyClient()
    path = client._cache_path(endpoint, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"endpoint": endpoint, "request": payload, "response": response}),
                    encoding="utf-8")


def test_main_check_by_domain(tmp_path, no_proxy, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)                    # the cache lives under the run's working directory
    _prime_cache(at.SEARCH_SUGGESTIONS, at.suggestions_payload("meshai.com"),
                 {"1": [{"1": "MESH AI", "2": "AR12956355609636110337", "3": "GB", "5": True, "7": "meshai.com"}]})
    _prime_cache(at.SEARCH_CREATIVES, at.creatives_payload(query="meshai.com"), CREATIVES_PAGE1)
    _prime_cache(at.SEARCH_CREATIVES, at.creatives_payload(query="meshai.com", cursor="cursor-1"), CREATIVES_PAGE2)
    code = at.main(["check", "--domain", "meshai.com", "--cache-only"])
    out = capsys.readouterr()
    summary = json.loads(out.out)
    assert code == 0
    assert summary["domain"] == "meshai.com" and summary["ads"]["ads_seen"] == 3
    assert summary["found"] is True
    assert "checked meshai.com" in out.err


def test_main_check_by_company_number(tmp_path, no_proxy, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _identity_db(tmp_path / "companies-house.db")
    _prime_cache(at.SEARCH_SUGGESTIONS, at.suggestions_payload("meshai.com"),
                 {"1": [{"1": "MESH AI", "2": "AR12956355609636110337", "3": "GB", "5": True, "7": "meshai.com"}]})
    _prime_cache(at.SEARCH_CREATIVES, at.creatives_payload(query="meshai.com"), CREATIVES_PAGE1)
    _prime_cache(at.SEARCH_CREATIVES, at.creatives_payload(query="meshai.com", cursor="cursor-1"), CREATIVES_PAGE2)
    code = at.main(["check", "--company", "13406761", "--cache-only"])
    out = capsys.readouterr()
    assert code == 0 and json.loads(out.out)["found"] is True


def test_main_reports_an_unanswered_run_with_exit_2(tmp_path, no_proxy, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = at.main(["check", "--domain", "never-checked.example", "--cache-only"])
    out = capsys.readouterr()
    assert code == 2
    summary = json.loads(out.out)
    assert summary["error"] and summary["blocked"] is False
