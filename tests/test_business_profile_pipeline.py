from __future__ import annotations

import json
import sqlite3

import pytest

from core.companies_house_sqlite import init_db, upsert_company_profile
from scripts.profile.business_profile_eval import (
    _draft_expected_from_extraction,
    build_case,
    score_case,
    select_candidate_companies,
)
from scripts.profile.companies_house_business_profile import (
    extract_business_profile,
    fetch_narrative_context,
    process_company,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    init_db(connection)
    return connection


def _company(conn: sqlite3.Connection, number: str, name: str, sic: str = "93110") -> None:
    conn.execute(
        """
        insert into companies (company_number, company_name, company_status, company_type,
            date_of_creation, source_mode, profile_payload, updated_at, sic_codes, sic_code_primary)
        values (?, ?, 'active', 'ltd', '2020-01-01', 'public_api', '{}', '2026-06-22T10:00:00+00:00', ?, ?)
        """,
        (number, name, f'["{sic}"]', sic),
    )


def _narrative_run(conn: sqlite3.Connection, company_number: str, sections: dict[str, dict]) -> int:
    cursor = conn.execute(
        "insert into narrative_runs (document_id, company_number, text_source, text_quality_payload, raw_payload, created_at) "
        "values (?, ?, 'xhtml', '{}', '{}', '2026-06-22T10:00:00+00:00')",
        (f"doc-{company_number}", company_number),
    )
    run_id = cursor.lastrowid
    for key, section in sections.items():
        conn.execute(
            "insert into narrative_sections (narrative_run_id, section_key, section_title, page_number, section_text, section_payload) "
            "values (?, ?, ?, ?, ?, ?)",
            (run_id, key, section.get("heading"), section.get("page"), section["text"], json.dumps(section)),
        )
    return run_id


class _FakeClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls = 0

    def generate(self, model: str, prompt: str, timeout: int) -> str:
        self.calls += 1
        return self.response_text


VALID_JSON = json.dumps({
    "business_description": "A community football club.",
    "demand_model": {"quote": "football club", "section": "principal_activity", "reason": "No customer channel is described.", "value": "not_customer_facing", "confidence": 0.9},
    "customer_type": {"quote": "football club", "section": "principal_activity", "reason": "Supporters are individuals.", "value": "b2c", "confidence": 0.8},
    "delivery_model": {"quote": "football club", "section": "principal_activity", "reason": "Running a club is people-delivered.", "value": "professional_service", "confidence": 0.6},
    "geography_served": {"quote": "football club", "section": "principal_activity", "reason": "A community club serves its area.", "value": "local", "confidence": 0.7},
    "trading_status_confirmed": {"quote": "football club", "section": "principal_activity", "reason": "The company operates the club itself.", "value": "trading", "confidence": 0.85},
    "sic_agreement": {"quote": "football club", "section": "principal_activity", "reason": "Matches sports facility SIC.", "value": "agrees"},
})


def test_fetch_narrative_context_uses_only_the_latest_run(conn: sqlite3.Connection) -> None:
    """A company can have several narrative_runs since the history
    backfill. Mixing text from different filing years into one context
    would corrupt the extraction."""
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "old year text", "is_auditor_text": False}})
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "newest year football club text", "is_auditor_text": False}})
    conn.commit()

    context = fetch_narrative_context(conn, "00482197")

    assert context["sections"] == {"principal_activity": "newest year football club text"}
    assert context["sic_code"] == "93110"


def test_fetch_narrative_context_excludes_auditor_flagged_sections(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {
        "principal_activity": {"text": "football club text", "is_auditor_text": False},
        "principal_risks": {"text": "we have audited the accounts", "is_auditor_text": True},
    })
    conn.commit()

    context = fetch_narrative_context(conn, "00482197")

    assert "principal_risks" not in context["sections"]
    assert context["sections"]["principal_activity"] == "football club text"


def test_fetch_narrative_context_returns_none_without_a_narrative_run(conn: sqlite3.Connection) -> None:
    _company(conn, "00000001", "NO NARRATIVE LTD")
    conn.commit()

    assert fetch_narrative_context(conn, "00000001") is None


def test_process_company_persists_a_valid_extraction(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "football club text", "is_auditor_text": False}})
    conn.commit()
    client = _FakeClient(VALID_JSON)

    status = process_company(conn, client, "test-model", "00482197", dry_run=False)

    assert status == "profiled"
    assert client.calls == 1
    row = conn.execute(
        "select business_description, demand_model, customer_type, extraction_model, prompt_version "
        "from company_profiles where company_number = '00482197'"
    ).fetchone()
    assert row[0] == "A community football club."
    assert row[1] == "not_customer_facing"
    assert row[2] == "b2c"
    assert row[3] == "test-model"
    assert row[4]


def test_process_company_persists_the_v6_evidence_columns(conn: sqlite3.Connection) -> None:
    """v6 added a reason per classification field and a quote/section for
    sic_agreement. Without the columns and the upsert wiring the model
    generates them on every call and they are silently dropped."""
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "football club text", "is_auditor_text": False}})
    conn.commit()

    process_company(conn, _FakeClient(VALID_JSON), "test-model", "00482197", dry_run=False)

    row = conn.execute(
        "select customer_type_reason, demand_model_reason, sic_agreement_quote, sic_agreement_section "
        "from company_profiles where company_number = '00482197'"
    ).fetchone()
    assert row[0] == "Supporters are individuals."
    assert row[1] == "No customer channel is described."
    assert row[2] == "football club"
    assert row[3] == "principal_activity"


def test_extract_business_profile_returns_prompt_and_raw_on_success() -> None:
    """The gold-set eval harness needs both back to log a per-case MLflow
    trace (business_profile_eval.py's _log_gold_eval_case_trace) -- before
    this, only `errors` and the parsed profile came back, and a real
    extraction run had nothing to show underneath its aggregate metrics."""
    context = {
        "company_name": "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED",
        "sections": {"principal_activity": "football club text"},
        "sic_label": "Sports facility operation",
        "sic_code": "93110",
    }
    client = _FakeClient(VALID_JSON)

    profile, errors, prompt, raw = extract_business_profile(client, "test-model", context)

    assert profile is not None
    assert errors == []
    assert "football club text" in prompt
    assert raw == VALID_JSON


def test_extract_business_profile_drops_only_the_field_whose_quote_fails() -> None:
    """A fabricated quote costs the field it supports, not the response.
    The other fields passed the same verbatim check, which is the only
    grounding guarantee there is; the rejection is recorded in the dropped
    field's reason, and the prompt and raw response are still returned for
    the trace."""
    tampered = json.loads(VALID_JSON)
    tampered["demand_model"]["quote"] = "this text never appeared anywhere"
    context = {
        "company_name": "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED",
        "sections": {"principal_activity": "football club text"},
        "sic_label": "Sports facility operation",
        "sic_code": "93110",
    }
    client = _FakeClient(json.dumps(tampered))

    profile, errors, prompt, raw = extract_business_profile(client, "test-model", context)

    assert profile is not None
    assert profile["demand_model"]["value"] is None and profile["demand_model"]["quote"] is None
    assert profile["demand_model"]["reason"].startswith("rejected: demand_model.quote")
    # The surviving field is untouched apart from the record of how its
    # quote was found.
    assert profile["customer_type"] == {**json.loads(VALID_JSON)["customer_type"], "quote_match": "exact"}
    assert errors and "demand_model.quote" in errors[0]
    assert prompt is not None
    assert raw == json.dumps(tampered)


def test_extract_business_profile_rejects_outright_only_when_there_is_no_json() -> None:
    context = {"company_name": "X", "sections": {"principal_activity": "text"}, "sic_label": None, "sic_code": None}
    profile, errors, prompt, raw = extract_business_profile(_FakeClient("not json at all"), "m", context)
    assert profile is None and errors and raw == "not json at all"


def test_process_company_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "football club text", "is_auditor_text": False}})
    conn.commit()
    client = _FakeClient(VALID_JSON)

    status = process_company(conn, client, "test-model", "00482197", dry_run=True)

    assert status == "profiled"
    assert conn.execute("select count(*) from company_profiles").fetchone()[0] == 0


def test_process_company_persists_the_good_fields_and_nulls_the_bad_one(conn: sqlite3.Connection) -> None:
    """A fabricated quote must never reach the database as evidence; the
    field it supported is stored null with the rejection as its reason, and
    the fields that passed are stored as normal."""
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "football club text", "is_auditor_text": False}})
    conn.commit()
    tampered = json.loads(VALID_JSON)
    tampered["demand_model"]["quote"] = "this text does not appear anywhere"
    client = _FakeClient(json.dumps(tampered))

    status = process_company(conn, client, "test-model", "00482197", dry_run=False)

    assert status == "profiled"
    row = conn.execute(
        "select demand_model, demand_model_quote, demand_model_reason, customer_type from company_profiles"
    ).fetchone()
    assert row[0] is None and row[1] is None
    assert row[2].startswith("rejected: demand_model.quote")
    assert row[3] == json.loads(VALID_JSON)["customer_type"]["value"]


def test_process_company_does_not_persist_a_non_json_response(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "football club text", "is_auditor_text": False}})
    conn.commit()
    status = process_company(conn, _FakeClient("nope"), "test-model", "00482197", dry_run=False)
    assert status == "invalid_response"
    assert conn.execute("select count(*) from company_profiles").fetchone()[0] == 0


def test_process_company_skips_companies_with_no_usable_sections(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "AUDITOR ONLY LTD")
    _narrative_run(conn, "00482197", {"principal_risks": {"text": "we have audited the accounts", "is_auditor_text": True}})
    conn.commit()
    client = _FakeClient(VALID_JSON)

    status = process_company(conn, client, "test-model", "00482197", dry_run=False)

    assert status == "no_usable_sections"
    assert client.calls == 0


def test_upsert_company_profile_is_idempotent(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    conn.commit()
    profile = json.loads(VALID_JSON)

    upsert_company_profile(conn, "00482197", 2024, profile, narrative_run_id=None, extraction_model="m1", prompt_version="v1")
    upsert_company_profile(conn, "00482197", 2024, profile, narrative_run_id=None, extraction_model="m2", prompt_version="v1")

    rows = conn.execute("select extraction_model from company_profiles where company_number='00482197'").fetchall()
    assert rows == [("m2",)]


def test_upsert_company_profile_stores_unclear_fields_too(conn: sqlite3.Connection) -> None:
    """unclear is a legitimate, storable answer, not a gap to leave null."""
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    conn.commit()
    profile = json.loads(VALID_JSON)
    profile["delivery_model"] = {"value": "unclear", "confidence": 0.0, "quote": "", "section": None}

    upsert_company_profile(conn, "00482197", 2024, profile, narrative_run_id=None, extraction_model="m1", prompt_version="v1")

    row = conn.execute("select delivery_model, delivery_model_quote from company_profiles where company_number='00482197'").fetchone()
    assert row == ("unclear", "")


def test_build_case_and_score_case_roundtrip(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED")
    _narrative_run(conn, "00482197", {"principal_activity": {"text": "football club text", "is_auditor_text": False}})
    conn.commit()

    case = build_case(conn, "00482197")
    assert case is not None
    case["expected"]["demand_model"]["value"] = "not_customer_facing"

    extracted = json.loads(VALID_JSON)
    result = score_case(case, extracted)

    assert result["fields"]["demand_model"]["correct"] is True
    # sic_agreement was never reviewed (still null in expected), so it must
    # not count toward accuracy even though the model answered it.
    assert result["fields"]["sic_agreement"]["expected"] is None


def test_build_case_returns_none_without_usable_sections(conn: sqlite3.Connection) -> None:
    _company(conn, "00482197", "AUDITOR ONLY LTD")
    _narrative_run(conn, "00482197", {"principal_risks": {"text": "we have audited the accounts", "is_auditor_text": True}})
    conn.commit()

    assert build_case(conn, "00482197") is None


def test_select_candidate_companies_spreads_across_trading_status(conn: sqlite3.Connection) -> None:
    for i, status in enumerate(("trading", "trading", "holding", "unknown")):
        number = f"{i:08d}"
        _company(conn, number, f"COMPANY {i}")
        _narrative_run(conn, number, {"principal_activity": {"text": f"text {i}", "is_auditor_text": False}})
        conn.execute(
            "insert into company_signals (company_number, signal_key, signal_value_type, signal_text, source_scope, created_at, updated_at) "
            "values (?, 'trading_status', 'text', ?, 'triage', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (number, status),
        )
    conn.commit()

    selected = select_candidate_companies(conn, count=4, seed=1)

    assert set(selected) == {"00000000", "00000001", "00000002", "00000003"}


def test_select_candidate_companies_prefers_sic_prefixes(conn: sqlite3.Connection) -> None:
    consumer = {"00000000": "47110", "00000001": "47710"}
    b2b = {"00000002": "70100", "00000003": "70229", "00000004": "71121"}
    for number, sic in {**consumer, **b2b}.items():
        _company(conn, number, f"COMPANY {number}", sic=sic)
        _narrative_run(conn, number, {"principal_activity": {"text": f"text {number}", "is_auditor_text": False}})
        conn.execute(
            "insert into company_signals (company_number, signal_key, signal_value_type, signal_text, source_scope, created_at, updated_at) "
            "values (?, 'trading_status', 'text', 'trading', 'triage', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (number,),
        )
    conn.commit()

    selected = select_candidate_companies(conn, count=2, seed=1, prefer_sic_prefixes=("47",))

    assert set(selected) == set(consumer)


def test_draft_expected_from_extraction_shapes_a_valid_extraction() -> None:
    extracted = {
        "business_description": "  Sells homeware online to consumers.  ",
        "demand_model": {"value": "consumer_search", "quote": "our website", "section": "principal_activity", "confidence": 0.7},
        "customer_type": {"value": "b2c", "quote": "retail customers", "section": "principal_activity", "confidence": 0.8},
        "sic_agreement": {"value": "agrees", "reason": "retail matches"},
    }

    expected = _draft_expected_from_extraction(extracted)

    assert expected["business_description"] == "Sells homeware online to consumers."
    assert expected["demand_model"]["value"] == "consumer_search"
    assert expected["customer_type"]["section"] == "principal_activity"
    assert expected["sic_agreement"] == {"value": "agrees", "reason": "retail matches"}


def test_draft_expected_from_extraction_drops_out_of_taxonomy_values() -> None:
    expected = _draft_expected_from_extraction(
        {"demand_model": {"value": "not_a_real_value", "quote": "x", "section": "y"}}
    )

    assert expected["demand_model"] == {"value": None, "quote": None, "section": None}


def test_draft_expected_from_extraction_handles_a_failed_call() -> None:
    assert _draft_expected_from_extraction(None) == _draft_expected_from_extraction({})
