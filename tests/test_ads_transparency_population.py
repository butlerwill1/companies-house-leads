import json

import pytest

from scripts.website_analysis import ads_transparency_population as population


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def pacer(clock, **kwargs):
    return population.Pacer(clock=clock.clock, sleep=clock.sleep, uniform=lambda a, b: b, **kwargs)


def test_shared_pacing_covers_requests_across_companies():
    clock = Clock()
    timing = pacer(clock)
    timing.before_request()
    timing.before_request()
    assert clock.now == 4
    timing.rest(60, "batch_break")
    timing.before_request()
    assert clock.now == 64
    assert max(clock.sleeps) <= 30


def test_backoff_grows_and_stops_after_three_errors():
    clock = Clock()
    timing = pacer(clock)
    timing.before_request()
    timing.failed()
    timing.before_request()
    assert clock.now == 30
    timing.failed("90")
    timing.before_request()
    assert clock.now == 120
    timing.failed()
    with pytest.raises(population.RunStopped, match="three consecutive"):
        timing.before_request()


def test_http_cap_is_global_and_cannot_be_overshot():
    timing = pacer(Clock(), cap=2, used=1)
    timing.before_request()
    with pytest.raises(population.RunStopped, match="cap of 2"):
        timing.before_request()
    assert timing.used == 2


def test_new_authorisation_does_not_hide_a_subsequent_block():
    old = {"http_status": 302, "location": "https://www.google.com/sorry/index", "started_at": "2026-10-14T09:26:08+00:00"}
    new = {**old, "started_at": "2026-10-14T09:41:31+00:00"}
    assert population.newly_blocked([old], None)
    assert not population.newly_blocked([old], "2026-10-14T09:40:17+00:00")
    assert population.newly_blocked([old, new], "2026-10-14T09:40:17+00:00")


def test_population_keeps_missing_failed_and_unchecked_unknown(tmp_path):
    manifest = {"companies": [
        {"company_number": "01", "domain": "a.example", "skip_reason": None},
        {"company_number": "02", "domain": "a.example", "skip_reason": None},
        {"company_number": "03", "domain": None, "skip_reason": "no_chosen_website"},
        {"company_number": "04", "domain": "b.example", "skip_reason": None}],
        "domains": [{"domain": "a.example"}, {"domain": "b.example"}]}
    result = {"domain": "a.example", "status": "partial", "complete": False, "creatives": [{}],
              "error": None, "ads": {"ads_seen": 40, "advertising_now": True}}
    summary = population.export(tmp_path, manifest, [result], {"stop_reason": None})
    assert summary["companies_with_ads"] == 2
    assert summary["companies_unknown"] == 2
    assert summary["checked_domains"] == 1
    rows = json.loads((tmp_path / "companies.json").read_text())
    assert rows[2]["has_ads"] is None and rows[3]["has_ads"] is None


def test_resume_never_sends_after_saved_http_block(tmp_path, monkeypatch):
    manifest = {"companies": [], "domains": []}
    monkeypatch.setattr(population, "build_manifest", lambda *args: manifest)
    case = tmp_path / "case-0001"
    case.mkdir()
    (case / "http.json").write_text(json.dumps([{"http_status": 429}]))
    monkeypatch.setattr(population, "langfuse_from_config", lambda *args: pytest.fail("must not start live run"))
    assert population.main(["--output", str(tmp_path)]) == 2


def test_cache_only_can_fill_saved_evidence_without_unlocking_block(tmp_path, monkeypatch):
    manifest = {"companies": [{"company_number": "01", "domain": "a.example", "skip_reason": None}],
                "domains": [{"domain": "a.example", "company_numbers": ["01"]}]}
    monkeypatch.setattr(population, "build_manifest", lambda *args: manifest)
    monkeypatch.setattr(population, "langfuse_from_config", lambda *args: None)
    monkeypatch.setattr(population.requests.Session, "request", lambda *args, **kwargs: pytest.fail("Google request forbidden"))
    client = population.at.TransparencyClient(cache_dir=tmp_path / "cache", cache_only=True)
    payload = population.at.creatives_payload(query="a.example", region_codes=[2826])
    client._write_cache(client._cache_path(population.at.SEARCH_CREATIVES, payload),
                        population.at.SEARCH_CREATIVES, payload, {"1": []})
    population.save(tmp_path / "state.json", {"http_attempts": 29, "stop_reason": "Google block"})
    assert population.main(["--output", str(tmp_path), "--cache-only"]) == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["companies_with_no_ads"] == 1
    assert summary["http_attempts"] == 29 and summary["stop_reason"] == "Google block"
