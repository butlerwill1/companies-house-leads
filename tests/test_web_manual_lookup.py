"""Offline tests for queue membership and verification of already-supplied URLs.

These fixture tests do not run company-name searches, use provider credits or
scrape real websites. They check identity evidence, input bounds and the rule
that browser rendering must not retry a refusal.
"""
import sqlite3
from types import SimpleNamespace

from companies_house_core.companies_house_sqlite import init_db
from scripts.website_analysis import web_manual_lookup as M
from scripts.website_analysis.web_fetch import Page
from scripts.website_analysis.web_rank_order import SCREEN_VERSION


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get(self, url, accept=("html",)):
        self.asked.append(url)
        return self.pages.get(url, Page(url, None, None, "", error="unreachable"))


def company():
    return {"company_number": "01234567", "company_name": "PIPERFINN LIMITED",
            "postcode": "LS1 4AP", "trading_names": []}


def test_queue_excludes_attempted_and_duplicate_companies(tmp_path):
    conn = sqlite3.connect(tmp_path / "fixture.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for number in ("1", "2", "3", "4"):
        conn.execute("insert into companies(company_number, company_name, profile_payload, updated_at) "
                     "values (?, ?, '{}', '2026')",
                     (number, f"Company {number}"))
        conn.execute("insert into company_search_screen(company_number, prompt_version, model, input_kind, "
                     "answer, passes, screened_at) values (?, ?, 'm', 'short', 'likely', ?, '2026')",
                     (number, SCREEN_VERSION, int(number != "4")))
    conn.execute("insert into company_web_identity(company_number, resolver_version, role, tier, resolved_at) "
                 "values ('2', 'v', 'none', 'none', '2026')")
    conn.execute("insert into company_signals(company_number, signal_key, signal_value_type, signal_text, "
                 "created_at, updated_at) values ('3', 'duplicate_of', 'text', '1', '2026', '2026')")
    conn.commit()
    rows = M.prepare_queue(conn)
    assert [r["company_number"] for r in rows] == ["1"]
    assert rows[0]["candidate_urls"] == []
    assert "search_query" in rows[0]
    conn.close()


def test_supplied_url_verified_from_company_number_without_search_provider():
    url = "https://piperfinn.com/privacy"
    fetcher = FakeFetcher({url: Page(url, url, 200, "<p>Company registration number 01234567</p>")})
    record = M.verify_candidates({**company(), "candidate_urls": [url, url]}, fetcher)
    assert record["tier"] == "verified"
    assert record["domain"] == "piperfinn.com"
    assert record["candidates"][0]["evidence"]["number_page"] == url
    assert fetcher.asked == [url]
    assert record["searches"] == []


def test_namesake_is_not_accepted_as_chosen_site():
    url = "https://piperfinn.com/"
    fetcher = FakeFetcher({url: Page(url, url, 200, "<p>Welcome to Piperfinn</p>")})
    record = M.verify_candidates({**company(), "candidate_urls": [url]}, fetcher)
    assert record["tier"] == "ambiguous"


def test_blocked_and_directory_candidates_do_not_become_verified():
    url = "https://piperfinn.com/"
    fetcher = FakeFetcher({url: Page(url, url, 403, "", error="blocked", blocked=True)})
    record = M.verify_candidates({**company(), "candidate_urls": ["https://endole.co.uk/company/1", url]}, fetcher)
    assert record["tier"] == "blocked"
    assert all("endole" not in u for u in fetcher.asked)


def test_known_directory_domains_are_not_checked():
    fetcher = FakeFetcher({})
    record = M.verify_candidates({**company(), "candidate_urls": ["https://firmsearch.co.uk/company/01234567"]}, fetcher)
    assert record["tier"] == "none"
    assert fetcher.asked == []


def test_company_directory_page_with_a_registration_number_is_not_an_official_site():
    url = "https://example-directory.test/company/01234567"
    fetcher = FakeFetcher({url: Page(url, url, 200, "<title>Company profile | LEI register</title>"
                                               "<p>PIPERFINN LIMITED registration number 01234567</p>")})
    record = M.verify_candidates({**company(), "candidate_urls": [url]}, fetcher)
    assert record["tier"] == "none"
    assert record["candidates"][0]["evidence"]["directory_site"] is True


def test_invalid_urls_and_too_many_sites_are_bounded(monkeypatch):
    asked = []
    monkeypatch.setattr(M, "check_site", lambda domain, *args, **kwargs: asked.append(domain) or {"error": "unreachable"})
    urls = ["javascript:alert(1)", "file:///tmp/test"] + [f"https://site{i}.com/" for i in range(20)]
    record = M.verify_candidates({**company(), "candidate_urls": urls}, FakeFetcher({}))
    assert len(asked) == M.MAX_SITE_CHECKS
    assert record["tier"] == "none"
    assert len(record["problems"]) == 3


def test_optional_browser_renders_shell_but_never_retries_refusal():
    url = "https://piperfinn.com/"
    rendered = Page(url, url, 200, "<p>Registered number 01234567</p>", fetched_with="browser")
    browser_calls = []
    browser = SimpleNamespace(fetch=lambda u: browser_calls.append(u) or rendered)
    fetcher = FakeFetcher({url: Page(url, url, 200, "<script>load()</script>")})
    fetcher.cache_only = False
    assert M.CandidateFetcher(fetcher, browser).get(url) == rendered
    fetcher.pages[url] = Page(url, url, 403, "", error="blocked", blocked=True)
    assert M.CandidateFetcher(fetcher, browser).get(url).blocked
    assert browser_calls == [url]


def test_discovery_jsonl_is_hydrated_with_registered_company_inputs(tmp_path):
    conn = sqlite3.connect(tmp_path / "fixture.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("insert into companies(company_number, company_name, profile_payload, updated_at) "
                 "values ('01234567', 'PIPERFINN LIMITED', ?, '2026')",
                 ('{\"registered_office_address\": {\"postal_code\": \"LS1 4AP\"}}',))
    source = tmp_path / "candidates.jsonl"
    source.write_text('{\"company_number\":\"01234567\",\"company_name\":\"Wrong name\",'
                      '\"candidate_urls\":[\"https://piperfinn.com/\"]}\n', encoding="utf-8")
    rows = M.load_candidate_companies(source, conn)
    assert rows == [{"company_number": "01234567", "company_name": "PIPERFINN LIMITED",
                     "postcode": "LS1 4AP", "locality": None, "trading_names": [],
                     "candidate_urls": ["https://piperfinn.com/"]}]
    conn.close()


def test_completed_verifications_ignores_other_versions_and_bad_lines(tmp_path):
    output = tmp_path / "checkpoint.jsonl"
    output.write_text('{"company_number":"1","resolver_version":"identity-manual-candidates-v3"}\n'
                      '{"company_number":"2","resolver_version":"other"}\nnot-json\n', encoding="utf-8")
    assert M.completed_verifications(output) == {"1"}
