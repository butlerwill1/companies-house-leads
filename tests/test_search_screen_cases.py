from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.companies_house_sqlite import init_db
from scripts.screen import search_screen_cases as C


def _add_company(conn, number, name, *, turnover=1000.0, profit=10.0, principal="The company sells widgets."):
    conn.execute(
        "insert into companies (company_number, company_name, company_status, source_mode, profile_payload, updated_at, "
        "sic_code_primary) values (?, ?, 'active', 'api', '{}', '2026-01-01', '47190')",
        (number, name),
    )
    if turnover is not None or profit is not None:
        conn.execute(
            "insert into financial_period_summaries (company_number, period_type, financial_year, turnover, "
            "profit_after_tax, employees, currency_code, data_source, raw_payload) values (?, 'current', 2025, ?, ?, 12, 'GBP', 'test', '{}')",
            (number, turnover, profit),
        )
    run = conn.execute(
        "insert into narrative_runs (company_number, text_quality_payload, raw_payload) values (?, '{}', '{}')",
        (number,),
    ).lastrowid
    if principal is not None:
        conn.execute(
            "insert into narrative_sections (narrative_run_id, section_key, section_text, section_payload) "
            "values (?, 'principal_activity', ?, ?)",
            (run, principal, json.dumps({"text": principal, "is_auditor_text": False})),
        )
    return run


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_target_population_needs_financials_and_principal_activity(tmp_path):
    conn = _db(tmp_path)
    _add_company(conn, "00000001", "GOOD LTD")
    _add_company(conn, "00000002", "NO PROFIT LTD", profit=None)
    _add_company(conn, "00000003", "NO ACTIVITY LTD", principal=None)
    _add_company(conn, "00000004", "IN GOLD LTD")
    conn.commit()
    population = C.target_population(conn, exclude={"00000004"})
    assert population == ["00000001"]


def test_random_draw_is_seeded_and_stable(tmp_path):
    numbers = [f"{i:08d}" for i in range(1, 60)]
    first = C.draw_random(numbers, 10, seed=7)
    assert first == C.draw_random(list(reversed(numbers)), 10, seed=7)
    assert len(set(first)) == 10
    assert first != C.draw_random(numbers, 10, seed=8)


def test_blind_subset_is_a_prefix_of_the_random_draw():
    drawn = [f"{i:08d}" for i in range(1, 31)]
    assert C.blind_subset(drawn, 5) == drawn[:5]


def _gold(number, name, *, demand="relationship_or_contract", customer="b2b", delivery="professional_service",
          trading="trading", text=""):
    def block(value):
        return {"value": value}
    return {
        "company_number": number,
        "company_name": name,
        "sections": {"filed_report": text},
        "expected": {
            "demand_model": block(demand),
            "customer_type": block(customer),
            "delivery_model": block(delivery),
            "trading_status_confirmed": block(trading),
        },
    }


def test_hard_categories_are_assigned_from_gold_labels():
    assert C.hard_category(_gold("1", "ACME HOLDINGS LTD")) == "holding_parent"
    assert C.hard_category(_gold("2", "VEHICLE LTD", trading="spv")) == "captive"
    assert C.hard_category(_gold("3", "PRACTICE LTD", customer="b2c", text="most work is NHS funded")) == "nhs_funded"
    assert C.hard_category(_gold("4", "SHOP LTD", demand="consumer_search", customer="mixed",
                                 delivery="product_physical")) == "multichannel_retail"
    assert C.hard_category(_gold("5", "BUILD LTD", customer="public_sector", delivery="contracting")) == "tender_only"
    assert C.hard_category(_gold("6", "PLAIN LTD", customer="b2c", delivery="hospitality", demand="local_service")) is None


def test_pick_hard_cases_caps_each_category_and_never_repeats():
    gold = [_gold(f"{i:08d}", f"CO{i} HOLDINGS LTD") for i in range(10)]
    gold += [_gold(f"{i:08d}", f"VEH{i} LTD", trading="spv") for i in range(20, 24)]
    picked = C.pick_hard_cases(gold, per_category=3, seed=1)
    numbers = [number for number, _ in picked]
    assert len(numbers) == len(set(numbers)) == 6
    assert sorted(cat for _, cat in picked) == ["captive"] * 3 + ["holding_parent"] * 3


def test_build_case_carries_financials_cohort_and_an_empty_label(tmp_path):
    conn = _db(tmp_path)
    _add_company(conn, "00000001", "GOOD LTD", turnover=5000.0, profit=250.0)
    conn.commit()
    case = C.build_case(conn, "00000001", cohort="random", blind=True)
    assert case["cohort"] == "random" and case["blind"] is True
    assert case["financials"]["turnover"] == 5000.0 and case["financials"]["profit_after_tax"] == 250.0
    assert case["sections"]["principal_activity"] == "The company sells widgets."
    assert case["expected"]["search_screen"]["value"] is None
    assert case["review"]["status"] == "unreviewed"


def test_write_cases_never_overwrites_a_case_that_has_work_in_it(tmp_path):
    conn = _db(tmp_path)
    _add_company(conn, "00000001", "GOOD LTD")
    conn.commit()
    cases_dir = tmp_path / "cases"
    plan = [("00000001", "random", False, None)]
    assert C.write_cases(conn, plan, cases_dir) == 1
    path = cases_dir / "00000001.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["review"]["status"] = "verified"
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert C.write_cases(conn, plan, cases_dir) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["review"]["status"] == "verified"


_XHTML = (
    "<html><body><p>Strategic report</p><p>The company sells shoes through its own website.</p>"
    "<p>Independent auditor's report to the members</p><p>We have audited the financial statements.</p>"
    "<p>Statement of comprehensive income</p></body></html>"
)


def _write_case(cases_dir: Path, number: str, **overrides) -> Path:
    case = {
        "company_number": number, "cohort": "random", "blind": False,
        "sections": {"principal_activity": "stored junk"},
        "expected": {"search_screen": {"value": None}},
        "draft": {"search_screen": {"value": None}},
        "review": {"status": "unreviewed"},
    }
    case.update(overrides)
    cases_dir.mkdir(parents=True, exist_ok=True)
    path = cases_dir / f"{number}.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    return path


def test_refresh_sections_replaces_stored_text_with_the_filed_report_only(tmp_path):
    cases, raw = tmp_path / "cases", tmp_path / "raw"
    raw.mkdir()
    path = _write_case(cases, "00000001")
    (raw / "00000001.xhtml").write_text(_XHTML, encoding="utf-8")
    result = C.refresh_sections(cases, raw)
    assert result["refreshed"] == ["00000001"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved["sections"]) == ["filed_report"]
    assert "sells shoes through its own website" in saved["sections"]["filed_report"]
    assert "We have audited" not in saved["sections"]["filed_report"]
    assert saved["expected"] == {"search_screen": {"value": None}}


def test_refresh_sections_reports_missing_raw_and_leaves_worked_cases_alone(tmp_path):
    cases, raw = tmp_path / "cases", tmp_path / "raw"
    raw.mkdir()
    _write_case(cases, "00000001")
    drafted = _write_case(cases, "00000002", draft={"search_screen": {"value": "likely"}})
    (raw / "00000002.xhtml").write_text(_XHTML, encoding="utf-8")
    result = C.refresh_sections(cases, raw)
    assert result["missing_raw"] == ["00000001"]
    assert result["has_work"] == ["00000002"]
    assert json.loads(drafted.read_text(encoding="utf-8"))["sections"] == {"principal_activity": "stored junk"}


def test_replace_cases_swaps_in_the_reserve_and_inherits_blind(tmp_path):
    conn = _db(tmp_path)
    _add_company(conn, "00000001", "FAILED LTD")
    _add_company(conn, "00000002", "RESERVE LTD")
    conn.commit()
    cases = tmp_path / "cases"
    _write_case(cases, "00000001", blind=True)
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"random": ["00000001"], "blind": ["00000001"], "reserve": ["00000002"]}),
                         encoding="utf-8")
    pairs = C.replace_cases(conn, ["00000001"], cases, selection)
    assert pairs == [("00000001", "00000002")]
    assert not (cases / "00000001.json").exists()
    added = json.loads((cases / "00000002.json").read_text(encoding="utf-8"))
    assert added["blind"] is True and added["cohort"] == "random"
    saved = json.loads(selection.read_text(encoding="utf-8"))
    assert saved["random"] == ["00000002"] and saved["blind"] == ["00000002"]
    assert saved["used_reserve"] == ["00000002"]


def test_replace_cases_never_removes_a_case_with_a_draft(tmp_path):
    conn = _db(tmp_path)
    _add_company(conn, "00000002", "RESERVE LTD")
    conn.commit()
    cases = tmp_path / "cases"
    path = _write_case(cases, "00000001", draft={"search_screen": {"value": "likely"}})
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"random": ["00000001"], "blind": [], "reserve": ["00000002"]}), encoding="utf-8")
    assert C.replace_cases(conn, ["00000001"], cases, selection) == []
    assert path.exists()


def test_build_keeps_an_existing_selection_and_only_adds_the_missing_reserve(tmp_path):
    conn = _db(tmp_path)
    for i in range(1, 6):
        _add_company(conn, f"{i:08d}", f"CO {i} LTD")
    conn.commit()
    conn.close()
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"random": ["00000001"], "blind": [], "hard": []}), encoding="utf-8")
    gold = tmp_path / "gold"
    gold.mkdir()
    summary = C.build(tmp_path / "t.db", tmp_path / "cases", selection, gold, seed=1)
    assert summary["kept_existing_selection"] is True
    saved = json.loads(selection.read_text(encoding="utf-8"))
    assert saved["random"] == ["00000001"]
    assert "00000001" not in saved["reserve"] and len(saved["reserve"]) == 4
    assert not (tmp_path / "cases").exists()
