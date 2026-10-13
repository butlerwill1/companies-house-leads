from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from companies_house_core.companies_house_sqlite import init_db
from scripts.search_screen_classifier import search_screen_baseline as B


def test_baseline_rejects_non_trading_holding_names_and_excluded_sic_groups():
    assert B.baseline_passes({"trading_status": "trading", "sic_label": "Hotels / B&Bs"}) is True
    assert B.baseline_passes({"trading_status": "holding"}) is False
    assert B.baseline_passes({"trading_status": "dormant"}) is False
    assert B.baseline_passes({"trading_status": "trading", "name_suggests_holding": True}) is False
    assert B.baseline_passes({"trading_status": "trading", "sic_label": "Property development"}) is False
    assert B.baseline_passes({"sic_label": "Banking / lending / credit"}) is False


def test_baseline_fails_open_when_signals_are_missing():
    assert B.baseline_passes({}) is True
    assert B.baseline_passes({"trading_status": "unknown", "sic_label": None}) is True


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)  # seeds sic_groups: 55100 is 'Hotels / B&Bs', 41100 is 'Property development'
    for number, sic in (("00000001", "55100"), ("00000002", "41100")):
        conn.execute("insert into companies (company_number, company_name, company_status, source_mode, "
                     "profile_payload, updated_at, sic_code_primary) values (?, 'X', 'active', 'api', '{}', 'now', ?)",
                     (number, sic))
        conn.execute("insert into company_signals (company_number, signal_key, signal_value_type, signal_text, "
                     "source_scope, created_at, updated_at) values (?, 'trading_status', 'text', 'trading', 'db', 'now', 'now')",
                     (number,))
    conn.commit()
    return conn


def test_load_features_reads_sic_label_and_gate_a_signals(tmp_path):
    conn = _db(tmp_path)
    features = B.load_features(conn, ["00000001", "00000002"])
    assert features["00000001"] == {"sic_label": "Hotels / B&Bs", "trading_status": "trading"}
    assert features["00000002"]["sic_label"] == "Property development"


def test_evaluate_reports_recall_and_removal_and_marks_provisional_labels(tmp_path):
    conn = _db(tmp_path)
    cases = tmp_path / "cases"
    cases.mkdir()
    for number, cohort, label in (("00000001", "random", "likely"), ("00000002", "random", "likely")):
        (cases / f"{number}.json").write_text(json.dumps({
            "company_number": number, "cohort": cohort,
            "expected": {"search_screen": {"value": None}},
            "draft": {"search_screen": {"value": label}},
            "review": {"status": "drafted"},
        }), encoding="utf-8")
    report = B.evaluate(cases, conn)["random"]
    assert report["n"] == 2 and report["provisional_labels"] == 2
    assert report["recall_on_likely"] == [1, 2]
    assert report["removed_share"] == 0.5


def test_a_verified_label_wins_over_a_draft():
    case = {"review": {"status": "verified"}, "expected": {"search_screen": {"value": "unlikely"}},
            "draft": {"search_screen": {"value": "likely"}}}
    assert B._label(case) == ("unlikely", False)
