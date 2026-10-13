"""Offline tests for Brave public-search candidate discovery helpers."""
from scripts.website_analysis import web_brave_browser_search as B


def test_candidate_urls_keep_sites_but_drop_directories_and_duplicates():
    links = [
        {"url": "https://endole.co.uk/company/1"},
        {"url": "https://www.example.co.uk/about"},
        {"url": "https://shop.example.co.uk/"},
        {"url": "https://other.example.com/"},
        {"url": "mailto:info@example.com"},
    ]
    assert B.candidate_urls(links) == ["https://www.example.co.uk/about", "https://other.example.com/"]


def test_block_detection_covers_status_captcha_and_google_sorry():
    assert B.is_blocked_page("https://search.brave.com/search", "CAPTCHA required", 200)
    assert B.is_blocked_page("https://www.google.com/sorry/index", "", 200)
    assert B.is_blocked_page("https://search.brave.com/search", "", 429)
    assert not B.is_blocked_page("https://search.brave.com/search", "normal results", 200)


def test_query_uses_prepared_search_phrase():
    assert B.query_for({"company_name": "TEST LIMITED", "search_query": "Test Leeds"}) == "Test Leeds official website"


def test_run_checkpoints_and_stops_after_a_challenge(tmp_path):
    calls = []

    class FakeSearch:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def search(self, query):
            calls.append(query)
            return {"status": 200, "final_url": "https://search.brave.com/search", "title": "x",
                    "blocked": len(calls) == 2, "candidate_urls": ["https://candidate.example/"], "result_count": 1}

    companies = [{"company_number": "1", "company_name": "One", "search_query": "One"},
                 {"company_number": "2", "company_name": "Two", "search_query": "Two"},
                 {"company_number": "3", "company_name": "Three", "search_query": "Three"}]
    output = tmp_path / "discovery.jsonl"
    counts = B.run(companies, output, pause_seconds=0, searcher_factory=FakeSearch)
    assert counts == {"already_completed": 0, "searched": 2, "with_candidates": 1, "blocked": 1}
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
    assert calls == ["One official website", "Two official website"]


def test_blocked_record_has_no_candidate_urls_and_is_not_resumed_as_done(tmp_path):
    record = B.discovery_record({"company_number": "1", "company_name": "One", "search_query": "One"}, {
        "blocked": True, "candidate_urls": ["https://search.brave.com/help/pow-captcha"],
    })
    path = tmp_path / "discovery.jsonl"
    path.write_text(__import__("json").dumps(record) + "\n", encoding="utf-8")
    assert record["candidate_urls"] == []
    assert B.completed(path) == set()


def test_api_search_adapts_provider_results_to_candidate_urls():
    class Client:
        def brave_web_search(self, query):
            assert query == "One official website"
            return {"organic": [{"url": "https://one.example.co.uk/about"},
                                {"url": "https://endole.co.uk/company/1"}]}

    result = B.BraveApiSearch(client=Client()).search("One official website")
    assert result["candidate_urls"] == ["https://one.example.co.uk/about"]
    assert result["result_count"] == 2
