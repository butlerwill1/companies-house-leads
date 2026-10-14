"""Batch safety: selection, budget, checkpoints and stopping across companies."""
import json
import sqlite3

from scripts.website_analysis import ads_transparency_experiment as experiment


def test_bare_country_suffix_is_not_a_company_website():
    assert not experiment.valid_company_domain("com.tr")
    assert not experiment.valid_company_domain("co.uk")
    assert experiment.valid_company_domain("esas.com.tr")


def test_selection_is_seeded_and_deduplicates_domains(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "fixture.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("create table companies(company_number text, company_name text);"
                           "create table company_web_identity_current(company_number text, domain text, role text);")
        for i, domain in enumerate(["a.example", "b.example", "a.example", "c.example"]):
            conn.execute("insert into companies values(?, ?)", (str(i), f"Company {i}"))
            conn.execute("insert into company_web_identity_current values(?, ?, 'main')", (str(i), domain))
    first = experiment.selection(db, 3, 42)
    assert first == experiment.selection(db, 3, 42)
    assert len(first) == len({row["domain"] for row in first}) == 3


def test_batch_block_checkpoints_and_never_calls_next_company(tmp_path, monkeypatch):
    cases = [{"domain": f"{i}.example", "name": f"Company {i}", "company_number": str(i),
              "selection_group": "seeded_random"} for i in range(3)]
    monkeypatch.setattr(experiment, "selection", lambda *args: cases)
    monkeypatch.setattr(experiment, "langfuse_from_config", lambda *args: None)
    calls = []

    def blocked(client, **kwargs):
        calls.append(kwargs["domain"])
        client.requests_made = 1
        return {"domain": kwargs["domain"], "blocked": True, "error": "HTTP 429",
                "status": "blocked", "creatives": [],
                "ads": {"ads_seen": None, "advertising_now": None, "last_shown": None}}

    monkeypatch.setattr(experiment.at, "check", blocked)
    args = ["--output", str(tmp_path / "run"), "--count", "3", "--rpc-cap", "3"]
    assert experiment.main(args) == 2
    assert calls == ["0.example"]
    rows = (tmp_path / "run/results.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["blocked"] is True
    assert experiment.main(args) == 2
    assert calls == ["0.example"]  # resume must not route around a recorded block


def test_report_uses_latest_attempt_without_hiding_attempt_count(tmp_path):
    cases = [{"domain": "a.example"}]
    base = {"domain": "a.example", "selection_group": "seeded_random", "creatives": [], "error": None,
            "ads": {"ads_seen": 0, "advertising_now": False, "last_shown": None}}
    experiment.report(tmp_path, cases, [{**base, "status": "failed"}, {**base, "status": "complete"}])
    report = (tmp_path / "REPORT.md").read_text()
    assert "attempted 1 (2 total case attempts)" in report
    assert "{'complete': 1}" in report
