from __future__ import annotations

from scripts.profile import business_profile_search_recall as R


def _case(number: str = "00000001") -> dict:
    return {
        "company_number": number,
        "company_name": "EXAMPLE HOTEL LTD",
        "sic_label": "Hotels",
        "sections": {"principal_activity": "The company operates a hotel."},
        "expected": {
            "demand_model": {"value": "relationship_or_contract", "quote": "operates a hotel", "section": "principal_activity"},
            "customer_type": {"value": "b2c", "quote": "operates a hotel", "section": "principal_activity"},
            "delivery_model": {"value": "hospitality", "quote": "operates a hotel", "section": "principal_activity"},
            "trading_status_confirmed": {"value": "trading", "quote": "operates a hotel", "section": "principal_activity"},
        },
        "review": {"status": "verified"},
    }


def test_draft_review_preserves_existing_human_decision(tmp_path) -> None:
    path = tmp_path / "review.json"
    created = R.load_or_draft_review([_case()], path)
    assert created["cases"]["00000001"]["proposal"]["value"] == "yes"
    created["cases"]["00000001"]["review"]["value"] = "no"
    path.write_text(__import__("json").dumps(created), encoding="utf-8")

    rerun = R.load_or_draft_review([_case()], path)
    assert rerun["cases"]["00000001"]["review"]["value"] == "no"


def test_analysis_lists_mixed_fallback_admissions() -> None:
    item = _case()
    review = {"status": "draft_for_human_review", "cases": {"00000001": {"proposal": R._proposal(item), "review": {}}}}
    report = {"results": [{"company_number": "00000001", "fields": {
        "demand_model": {"actual": "unclear"}, "customer_type": {"actual": "mixed"},
        "delivery_model": {"actual": "hospitality"}, "trading_status_confirmed": {"actual": "trading"},
    }}]}
    rows = R.analysis_rows([item], review, report)
    assert any(row[0] == "00000001" for row in rows)
