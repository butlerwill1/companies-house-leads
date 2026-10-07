from __future__ import annotations

import csv
import sqlite3

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.website_analysis import web_handoff as H


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _company(conn, number, name, *, segment, gaps=(), strengths=(), gap_count=None, turnover=5_000_000,
             domain="x.co.uk", advertising_now=0, volume=900, listing=None, profile=None, trading=None):
    conn.execute("insert into companies (company_number, company_name, company_status, source_mode, profile_payload, "
                 "updated_at) values (?, ?, 'active', 'api', '{}', '2026')", (number, name))
    conn.execute("insert into financial_period_summaries (company_number, period_type, financial_year, turnover, "
                 "profit_after_tax, employees, raw_payload, data_source, currency_validation_status) "
                 "values (?, 'current', 2025, ?, 100000, 12, '{}', 'xhtml', 'unknown')", (number, turnover))
    conn.execute("insert into company_market (company_number, market_version, domain, advertising_now, ads_last_shown, "
                 "phrase_search_volume, weighted_cpc_usd, gap_count, strength_count, setup_level, gap_segment, "
                 "assessed_at) values (?, 'mv', ?, ?, '2026-10-01', ?, 4.5, ?, ?, 'basic', ?, '2026')",
                 (number, domain, advertising_now, volume, gap_count if gap_count is not None else len(gaps),
                  len(strengths), segment))
    for kind, items in (("gap", gaps), ("strength", strengths)):
        for index, detail in enumerate(items):
            conn.execute("insert into company_setup_findings (company_number, finding_version, finding, kind, detail) "
                         "values (?, 'findings-v1', ?, ?, ?)", (number, f"{kind}{index}", kind, detail))
    if listing:
        conn.execute("insert into company_google_listing (company_number, resolver_version, title, category, rating, "
                     "rating_count, cid, match, source, found_at, address) values (?, 'rv', ?, ?, ?, ?, ?, "
                     "'name+postcode', 'serper_maps', '2026', 'Leeds')", (number, *listing))
    if profile:
        conn.execute("insert into company_web_profile (company_number, profile_version, model, summary, customer_type, "
                     "conversion_action, geography, main_town, urgency, ticket_band, profiled_at) values "
                     "(?, 'pv', 'm', ?, 'consumer', 'quote_form', 'regional', 'Leeds', 'planned', '100_to_1000', '2026')",
                     (number, profile))
    if trading:
        conn.execute("insert into company_trading_names (company_number, name, source, found_at) "
                     "values (?, ?, 'website', '2026')", (number, trading))


def _seed(conn):
    _company(conn, "00000001", "ALPHA LTD", segment="greenfield", gaps=["No CRM."], listing=("Alpha", "Law firm", 4.5, 120, "999"),
             profile="A law firm.", trading="Alpha Law")
    _company(conn, "00000002", "BETA LTD", segment="advertising_poorly", gaps=["g1", "g2", "g3"], strengths=["Has a CRM."],
             advertising_now=1)
    _company(conn, "00000003", "GAMMA LTD", segment="greenfield", gaps=["g1", "g2"])
    _company(conn, "00000004", "DELTA LTD", segment="advertising_well", strengths=["Tracks calls."], advertising_now=1)
    _company(conn, "00000005", "EPSILON LTD", segment="site_first", gaps=[])
    _company(conn, "00000006", "ZETA LTD", segment="low_demand")
    _company(conn, "00000007", "ETA LTD", segment="unknown")


def test_lead_row_joins_everything_with_links(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    row = H.lead_row(conn, "00000001", market_version="mv", finding_version="findings-v1", queue_position=7)
    assert row["company"] == "ALPHA LTD" and row["trading_as"] == "Alpha Law" and row["turnover"] == 5_000_000
    assert row["category"] == "Law firm" and row["description"] == "A law firm." and row["area"] == "regional Leeds"
    assert row["website"] == "https://x.co.uk/" and row["place_link"] == "https://www.google.com/maps?cid=999"
    assert row["companies_house"].endswith("/company/00000001") and row["gaps"] == ["No CRM."]
    assert row["segment"] == "greenfield" and row["queue_position"] == 7 and row["volume"] == 900
    assert H.lead_row(conn, "99999999", market_version="mv", finding_version="findings-v1") is None
    bare = H.lead_row(conn, "00000003", market_version="mv", finding_version="findings-v1")
    assert bare["category"] is None and bare["place_link"] is None and bare["trading_as"] == ""


def test_order_is_segment_then_gaps_then_financial_queue_and_site_first_is_left_out(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    queue = {"00000003": 1, "00000001": 2, "00000002": 3, "00000004": 4}
    leads = H.build_leads(conn, [f"{i:08d}" for i in range(1, 8)], market_version="mv", queue=queue)
    assert [r["company_number"] for r in leads] == ["00000003", "00000001", "00000002", "00000004", "00000006",
                                                    "00000007"]
    # gamma (2 gaps) before alpha (1 gap) though both are greenfield; site_first (epsilon) is absent
    with_site_first = H.build_leads(conn, [f"{i:08d}" for i in range(1, 8)], market_version="mv", queue=queue,
                                    site_first="lead")
    assert with_site_first[-1]["company_number"] == "00000005"
    assert [r["company_number"] for r in H.build_leads(conn, ["00000001", "00000002", "00000003"], market_version="mv",
                                                       queue=queue, limit=2)] == ["00000003", "00000001"]
    with pytest.raises(ValueError):
        H.select_leads([], site_first="maybe")


def test_sheet_rows_format_gaps_as_bullets_and_hide_nothing_important(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    leads = H.build_leads(conn, ["00000002", "00000001"], market_version="mv", queue={})
    rows = H.sheet_rows(leads)
    assert rows[0] == H.LEAD_HEADER and len(rows) == 3
    first = dict(zip(H.LEAD_HEADER, rows[1]))
    assert first["rank"] == 1 and first["company number"] == "00000001" and first["how customers convert"] == "quote form"
    assert first["typical sale"] == "100 to 1000" and first["gaps (what to pitch)"] == "- No CRM."
    second = dict(zip(H.LEAD_HEADER, rows[2]))
    assert second["gaps (what to pitch)"] == "- g1\n- g2\n- g3" and second["already doing"] == "- Has a CRM."
    assert second["advertising now"] == "yes" and second["turnover GBP"] == 5000000
    out = tmp_path / "sheet.csv"
    H.write_csv(rows, out)
    with out.open(encoding="utf-8", newline="") as handle:
        assert list(csv.reader(handle))[2][H.LEAD_HEADER.index("gaps (what to pitch)")] == "- g1\n- g2\n- g3"


def test_register_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    assert H.register_handover(conn, ["00000001", "00000002"], "sheet-1") == 2
    assert H.register_handover(conn, ["00000001", "00000002", "00000003"], "sheet-1") == 1
    assert H.register_handover(conn, ["00000001"], "sheet-2") == 1


def _feedback(tmp_path, rows, header=("company number", "contacted", "replied", "meeting", "won", "notes")):
    path = tmp_path / "feedback.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def test_outcomes_import_summary_and_segments(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    H.register_handover(conn, ["00000001", "00000002", "00000003", "00000004"], "s1")
    path = _feedback(tmp_path, [["1", "2026-10-05", "yes", "yes", "no", "wants a call back"],
                                ["00000002", "yes", "no", "", "", ""],
                                ["00000003", "2026-10-06", "", "", "", "no answer"]])
    assert H.import_outcomes(conn, path, "s1") == 3                           # Sheets dropped the leading zeros of "1"
    row = conn.execute("select contacted_at, replied, meeting, won, notes from lead_outcomes "
                       "where company_number = '00000001'").fetchone()
    assert row == ("2026-10-05", 1, 1, 0, "wants a call back")
    summary = H.outcome_summary(conn, "s1")
    assert {k: summary[k] for k in ("handed_over", "contacted", "replied", "meetings", "won")} == {
        "handed_over": 4, "contacted": 3, "replied": 1, "meetings": 1, "won": 0}
    assert summary["reply_rate_of_contacted"] == 0.333 and summary["meeting_rate_of_contacted"] == 0.333
    segments = {s["segment"]: s for s in summary["by_segment"]}
    assert segments["greenfield"]["handed_over"] == 2 and segments["greenfield"]["meetings"] == 1
    later = _feedback(tmp_path, [["00000001", "", "", "", "yes", "signed"]])           # a later update keeps earlier facts
    H.import_outcomes(conn, later, "s1")
    assert conn.execute("select contacted_at, meeting, won, notes from lead_outcomes where company_number = '00000001'"
                        ).fetchone() == ("2026-10-05", 1, 1, "signed")


@pytest.mark.parametrize("rows,message", [
    ([["00000001", "next week", "", "", "", ""]], "contacted must be a date"),
    ([["00000001", "", "maybe", "", "", ""]], "replied must be yes or no"),
    ([["00000001", "", "yes", "", "", ""]], "needs a contacted date"),
    ([["00000099", "yes", "", "", "", ""]], "not handed over"),
])
def test_bad_feedback_is_rejected_before_anything_is_written(tmp_path, rows, message):
    conn = _db(tmp_path)
    _seed(conn)
    H.register_handover(conn, ["00000001", "00000002"], "s1")
    good_first = [["00000002", "2026-10-05", "yes", "", "", ""]]
    with pytest.raises(ValueError, match=message):
        H.import_outcomes(conn, _feedback(tmp_path, good_first + rows), "s1")
    assert conn.execute("select count(*) from lead_outcomes where contacted_at is not null").fetchone() == (0,)


def test_feedback_without_a_company_number_column_is_rejected(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ValueError, match="company number"):
        H.import_outcomes(conn, _feedback(tmp_path, [], header=("company", "contacted")), "s1")


def test_outcome_summary_of_an_empty_sheet(tmp_path):
    summary = H.outcome_summary(_db(tmp_path), "none")
    assert summary["handed_over"] == 0 and summary["reply_rate_of_contacted"] is None
