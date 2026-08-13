from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies_house_sqlite import init_db, insert_vlm_financial_payload
from scripts.ocr import companies_house_pdf_vlm_financials as vlm_financials
from scripts.ocr.companies_house_pdf_vlm_financials import (
    OpenRouterVlmModelClient,
    OllamaVlmModelClient,
    RenderedPage,
    ModelCallResult,
    combine_model_calls,
    extraction_candidates,
    incomplete_statement_extractions,
    located_statement_pages,
    canonical_rationalisation_candidates,
    resolve_canonical_rationalisation_choices,
    selected_metrics,
    statement_completeness_recovery_pages,
    statement_pages,
    to_pence,
)
from scripts.ocr.financial_metric_policy import (
    add_canonical_equivalents,
    add_canonical_equivalents_by_statement_scope,
)


class FakeResponse:
    """Small requests response double for offline provider contract tests."""

    def __init__(self, body: dict[str, object]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._body


def test_statement_pages_includes_statement_neighbours() -> None:
    locator = {"pages": [
        {"page": 2, "statement_type": "other"},
        {"page": 5, "statement_type": "income_statement"},
        {"page": 9, "statement_type": "balance_sheet"},
    ]}
    assert statement_pages(locator, 10) == [4, 5, 6, 8, 9, 10]
    assert located_statement_pages(locator, 10) == [5, 9]


def test_statement_pages_accepts_numeric_page_strings() -> None:
    locator = {"pages": [
        {"page": "5", "statement_type": "income_statement"},
        {"page": "not-a-page", "statement_type": "balance_sheet"},
    ]}

    assert statement_pages(locator, 10) == [4, 5, 6]


def test_statement_pages_accepts_document_page_labels() -> None:
    locator = {"pages": [
        {"page": "Document page 5", "statement_type": "income_statement"},
    ]}

    assert statement_pages(locator, 10) == [4, 5, 6]


def test_statement_completeness_recovery_targets_only_confident_partial_primary_pages() -> None:
    locator = {"pages": [
        {"page": 4, "statement_type": "income_statement", "confidence": 0.92},
        {"page": 7, "statement_type": "balance_sheet", "confidence": 0.95},
        {"page": 9, "statement_type": "cash_flow", "confidence": 0.70},
    ]}
    extraction = {"pages": [
        {"page": 4, "statement_type": "income_statement", "unit": "GBP", "rows": [
            {"metric": "turnover", "source_label": "Turnover", "current_display": "100", "previous_display": "90"},
            {"metric": "profit_after_tax", "source_label": "Profit for the year", "current_display": "10", "previous_display": "9"},
        ]},
        {"page": 7, "statement_type": "balance_sheet", "unit": "GBP", "rows": [
            {"metric": "cash", "source_label": "Cash at bank", "current_display": "20", "previous_display": "15"},
            {"metric": "shareholders_funds", "source_label": "Total equity", "current_display": "80", "previous_display": "70"},
        ]},
        {"page": 9, "statement_type": "cash_flow", "unit": "GBP", "rows": [
            {"metric": "administrative_expenses", "source_label": "Administration", "current_display": "5", "previous_display": "4"},
        ]},
    ]}

    report = statement_completeness_recovery_pages(locator, extraction)

    assert report["recovery_pages"] == [4]
    assert report["triggers_by_page"][4] == ["income_statement_partial_core_family"]
    assert 7 not in report["triggers_by_page"]
    assert 9 not in report["triggers_by_page"]


def test_statement_completeness_recovery_does_not_retry_complete_or_optional_absence() -> None:
    locator = {"pages": [
        {"page": 2, "statement_type": "income_statement", "confidence": 0.9},
        {"page": 5, "statement_type": "balance_sheet", "confidence": 0.9},
    ]}
    extraction = {"pages": [
        {"page": 2, "statement_type": "income_statement", "unit": "GBP", "rows": [
            {"metric": "turnover", "source_label": "Turnover", "current_display": "100", "previous_display": "90"},
            {"metric": "gross_profit", "source_label": "Gross profit", "current_display": "40", "previous_display": "30"},
            {"metric": "operating_result", "source_label": "Operating profit", "current_display": "20", "previous_display": "15"},
            {"metric": "profit_after_tax", "source_label": "Profit for the year", "current_display": "10", "previous_display": "9"},
        ]},
        {"page": 5, "statement_type": "balance_sheet", "unit": "GBP", "rows": [
            {"metric": "net_assets", "source_label": "Net assets", "current_display": "50", "previous_display": "40"},
        ]},
    ]}

    assert statement_completeness_recovery_pages(locator, extraction)["recovery_pages"] == []


def test_statement_completeness_recovers_cash_only_when_current_assets_are_visible() -> None:
    locator = {"pages": [
        {"page": 5, "statement_type": "balance_sheet", "confidence": 0.9},
    ]}
    extraction = {"pages": [
        {"page": 5, "statement_type": "balance_sheet", "unit": "GBP", "rows": [
            {"metric": "current_assets", "source_label": "Current assets", "current_display": "100", "previous_display": "80"},
            {"metric": "net_assets", "source_label": "Net assets", "current_display": "50", "previous_display": "40"},
        ]},
    ]}

    report = statement_completeness_recovery_pages(locator, extraction)

    assert report["recovery_pages"] == [5]
    assert report["triggers_by_page"] == {5: ["balance_sheet_current_assets_without_cash"]}


def test_locator_scope_overrides_extraction_scope_and_group_policy_excludes_company_balance_sheet() -> None:
    extraction = {"pages": [
        {
            "page": 13,
            "statement_type": "balance_sheet",
            "statement_scope": "unknown",
            "unit": "GBP",
            "rows": [{
                "metric": "net_assets",
                "source_label": "Net assets",
                "current_display": "4,256,780",
                "previous_display": "18,357,126",
            }],
        },
        {
            "page": 14,
            "statement_type": "balance_sheet",
            "statement_scope": "company",
            "unit": "GBP",
            "rows": [{
                "metric": "net_assets",
                "source_label": "Net assets",
                "current_display": "10,856,340",
                "previous_display": "10,856,340",
            }],
        },
        {
            "page": 12,
            "statement_type": "income_statement",
            "statement_scope": "company",
            "unit": "GBP",
            "rows": [{
                "metric": "turnover",
                "source_label": "Turnover",
                "current_display": "100",
                "previous_display": "90",
            }],
        },
    ]}
    locator = {"pages": [
        {"page": 13, "statement_type": "balance_sheet", "statement_scope": "consolidated_group"},
        {"page": 14, "statement_type": "balance_sheet", "statement_scope": "company"},
        {"page": 12, "statement_type": "income_statement", "statement_scope": "company"},
    ]}

    vlm_financials.apply_locator_statement_scopes(extraction, locator)
    candidates, _, _ = vlm_financials.validate_extraction_candidates(extraction)
    kept, report = vlm_financials.apply_consolidated_scope_policy(candidates)

    assert extraction["pages"][0]["statement_scope"] == "consolidated_group"
    assert extraction["pages"][0]["statement_type"] == "balance_sheet"
    assert [candidate["id"] for candidate in kept] == ["p13-r0", "p12-r0"]
    assert report == {
        "name": "prefer_direct_group_evidence_then_stronger_company_evidence",
        "consolidated_metrics": ["net_assets"],
        "excluded_company_candidate_ids": ["p14-r0"],
    }


def test_scope_policy_keeps_company_metrics_missing_from_group_evidence() -> None:
    candidates = [
        {
            "id": "group-turnover", "metric": "turnover", "statement_type": "income_statement",
            "statement_scope": "consolidated_group", "unit": "GBP", "current_display": "100",
        },
        {
            "id": "company-turnover", "metric": "turnover", "statement_type": "income_statement",
            "statement_scope": "company", "unit": "GBP", "current_display": "90",
        },
        {
            "id": "company-gross-profit", "metric": "gross_profit", "statement_type": "income_statement",
            "statement_scope": "company", "unit": "GBP", "current_display": "30",
        },
        {
            "id": "company-profit-after-tax", "metric": "profit_after_tax", "statement_type": "income_statement",
            "statement_scope": "company", "unit": "GBP", "current_display": "10",
        },
    ]

    kept, report = vlm_financials.apply_consolidated_scope_policy(candidates)

    assert [candidate["id"] for candidate in kept] == [
        "group-turnover", "company-gross-profit", "company-profit-after-tax",
    ]
    assert report["consolidated_metrics"] == ["turnover"]
    assert report["excluded_company_candidate_ids"] == ["company-turnover"]


def test_insurance_equivalents_do_not_cross_statement_scopes() -> None:
    candidates = [
        {
            "id": "company-premiums", "metric": "gross_premiums_written", "statement_scope": "company",
                "statement_type": "income_statement",
            "unit": "GBP", "source_label": "Gross premiums written", "current_display": "100", "previous_display": "90", "confidence": 0.9,
        },
        {
            "id": "group-premiums", "metric": "gross_premiums_written", "statement_scope": "consolidated_group",
                "statement_type": "income_statement",
            "unit": "GBP", "source_label": "Gross premiums written", "current_display": "200", "previous_display": "180", "confidence": 0.9,
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)
    turnovers = [candidate for candidate in equivalents if candidate["metric"] == "turnover"]

    assert [(candidate["statement_scope"], candidate["current_display"]) for candidate in turnovers] == [
        ("consolidated_group", "200"), ("company", "100"),
    ]


def test_company_shareholders_funds_adds_traceable_net_assets_equivalent() -> None:
    candidates = [{
        "id": "p14-r3",
        "metric": "shareholders_funds",
        "page": 14,
        "statement_type": "balance_sheet",
        "statement_scope": "company",
        "unit": "GBP",
        "source_label": "Shareholders' funds",
        "current_display": "3,716,109",
        "previous_display": "100",
        "confidence": 0.95,
    }]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)

    equivalent = next(candidate for candidate in equivalents if candidate["metric"] == "net_assets")
    assert equivalent["id"] == "shareholders-funds-net-assets-p14-r3"
    assert equivalent["source_label"] == "Shareholders' funds"
    assert equivalent["current_display"] == "3,716,109"
    assert equivalent["derivation"] == {
        "policy": "shareholders_funds_equivalent",
        "kind": "reported_equivalent",
        "formula": "shareholders_funds",
        "source_candidate_ids": ["p14-r3"],
    }


def test_total_equity_adds_traceable_net_assets_equivalent() -> None:
    candidates = [{
        "id": "p10-r4", "metric": "shareholders_funds", "page": 10,
        "statement_type": "balance_sheet", "statement_scope": "company",
        "unit": "GBP_THOUSANDS", "source_label": "TOTAL EQUITY",
        "current_display": "111", "previous_display": "103",
    }]

    equivalent = next(
        candidate for candidate in add_canonical_equivalents_by_statement_scope(candidates)
        if candidate["metric"] == "net_assets"
    )

    assert equivalent["id"] == "shareholders-funds-net-assets-p10-r4"
    assert equivalent["derivation"]["source_candidate_ids"] == ["p10-r4"]


def test_total_equity_equivalent_is_selected_after_ineligible_equity_components() -> None:
    candidates = [
        {
            "id": "p10-r1", "metric": "shareholders_funds", "page": 10,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP_THOUSANDS", "source_label": "Share capital",
            "current_display": "100", "previous_display": "100",
        },
        {
            "id": "p10-r2", "metric": "shareholders_funds", "page": 10,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP_THOUSANDS", "source_label": "Retained earnings",
            "current_display": "11", "previous_display": "3",
        },
        {
            "id": "p10-r3", "metric": "shareholders_funds", "page": 10,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP_THOUSANDS", "source_label": "TOTAL EQUITY",
            "current_display": "111", "previous_display": "103",
        },
    ]

    equivalents = add_canonical_equivalents(candidates)

    net_assets = [candidate for candidate in equivalents if candidate["metric"] == "net_assets"]
    assert len(net_assets) == 1
    assert net_assets[0]["current_display"] == "111"
    assert net_assets[0]["previous_display"] == "103"
    assert net_assets[0]["derivation"]["source_candidate_ids"] == ["p10-r3"]


def test_total_liabilities_and_shareholders_funds_is_not_a_net_assets_equivalent() -> None:
    candidates = [{
        "id": "p10-r4", "metric": "shareholders_funds", "page": 10,
        "statement_type": "balance_sheet", "statement_scope": "company",
        "unit": "GBP", "source_label": "Total liabilities and shareholders' funds",
        "current_display": "500", "previous_display": "450",
    }]

    equivalents = add_canonical_equivalents(candidates)

    assert not any(candidate["metric"] == "net_assets" for candidate in equivalents)


def test_rationalisation_receives_only_canonical_candidates_with_source_provenance() -> None:
    candidates = [
        {
            "id": "p10-r4", "metric": "shareholders_funds", "page": 10,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP", "source_label": "TOTAL EQUITY",
            "current_display": "111", "previous_display": "103",
        },
        {
            "id": "p11-r2", "metric": "profit_before_tax", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Profit before tax",
            "current_display": "10", "previous_display": "4",
        },
    ]

    rationalisation_candidates = canonical_rationalisation_candidates(
        add_canonical_equivalents_by_statement_scope(candidates)
    )

    assert {candidate["metric"] for candidate in rationalisation_candidates} == {
        "net_assets", "operating_result"
    }
    assert next(candidate for candidate in rationalisation_candidates if candidate["metric"] == "net_assets")[
        "derivation"
    ]["source_candidate_ids"] == ["p10-r4"]


def test_original_synonym_choice_resolves_to_its_canonical_equivalent() -> None:
    candidates = add_canonical_equivalents_by_statement_scope([{
        "id": "p10-r4", "metric": "shareholders_funds", "page": 10,
        "statement_type": "balance_sheet", "statement_scope": "company",
        "unit": "GBP", "source_label": "TOTAL EQUITY",
        "current_display": "111", "previous_display": "103",
    }])

    resolved, translations = resolve_canonical_rationalisation_choices(candidates, {
        "financial_period_summaries": {
            "current": {"net_assets": {"candidate_id": "p10-r4", "reason": "Total equity"}},
            "previous": {},
        },
    })

    assert resolved["financial_period_summaries"]["current"]["net_assets"]["candidate_id"] == (
        "shareholders-funds-net-assets-p10-r4"
    )
    assert translations == [{
        "period": "current", "metric": "net_assets", "source_candidate_id": "p10-r4",
        "canonical_candidate_id": "shareholders-funds-net-assets-p10-r4",
    }]


def test_current_assets_choice_does_not_resolve_as_cash() -> None:
    candidates = [{
        "id": "p12-r0", "metric": "current_assets", "page": 12,
        "statement_type": "balance_sheet", "statement_scope": "company",
        "unit": "GBP", "source_label": "Current assets",
        "current_display": "221,586", "previous_display": "224,855",
    }]

    resolved, translations = resolve_canonical_rationalisation_choices(candidates, {
        "financial_period_summaries": {
            "current": {"cash": {"candidate_id": "p12-r0", "reason": "proxy"}},
            "previous": {},
        },
    })

    assert resolved["financial_period_summaries"]["current"]["cash"]["candidate_id"] == "p12-r0"
    assert translations == []


def test_shareholders_funds_equivalent_requires_a_company_balance_sheet() -> None:
    candidates = [{
        "id": "group-p14-r3",
        "metric": "shareholders_funds",
        "page": 14,
        "statement_type": "balance_sheet",
        "statement_scope": "consolidated_group",
        "unit": "GBP",
        "source_label": "Shareholders' funds",
        "current_display": "3,716,109",
        "previous_display": "100",
    }]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)

    assert [candidate["metric"] for candidate in equivalents] == ["shareholders_funds"]


def test_direct_net_assets_outranks_shareholders_funds_synonym() -> None:
    candidates = [
        {
            "id": "p14-r2", "metric": "net_assets", "page": 14,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP", "source_label": "Net assets",
            "current_display": "3,716,109", "previous_display": "100",
        },
        {
            "id": "p14-r3", "metric": "shareholders_funds", "page": 14,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP", "source_label": "Shareholders' funds",
            "current_display": "3,716,109", "previous_display": "100",
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)

    assert [candidate["id"] for candidate in equivalents if candidate["metric"] == "net_assets"] == [
        "p14-r2"
    ]


def test_lower_tier_synonym_can_fill_only_a_period_missing_from_direct_row() -> None:
    candidates = [
        {
            "id": "p14-r2", "metric": "net_assets", "page": 14,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP", "source_label": "Net assets",
            "current_display": "3,716,109", "previous_display": None,
        },
        {
            "id": "p14-r3", "metric": "shareholders_funds", "page": 14,
            "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP", "source_label": "Shareholders' funds",
            "current_display": "3,716,109", "previous_display": "100",
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)
    net_assets = [candidate for candidate in equivalents if candidate["metric"] == "net_assets"]

    assert [(candidate["id"], candidate["current_display"], candidate["previous_display"]) for candidate in net_assets] == [
        ("p14-r2", "3,716,109", None),
        ("shareholders-funds-net-assets-p14-r3", None, "100"),
    ]


def test_direct_canonical_rows_outrank_insurance_derivations() -> None:
    candidates = [
        {
            "id": "p11-r0", "metric": "gross_profit", "statement_scope": "company",
            "unit": "GBP", "source_label": "Gross profit",
            "current_display": "916,149", "previous_display": "9,540,563",
        },
        {
            "id": "p28-r1", "metric": "net_earned_premiums", "page": 28,
            "statement_scope": "company",
            "unit": "GBP", "source_label": "Earned premiums, net of reinsurance",
            "current_display": "5,355,650", "previous_display": "28,077,188",
        },
        {
            "id": "p28-r2", "metric": "claims_incurred_net_reinsurance", "page": 28,
            "statement_scope": "company", "unit": "GBP",
            "source_label": "Claims incurred, net of reinsurance",
            "current_display": "-", "previous_display": "-",
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)

    assert [candidate["id"] for candidate in equivalents if candidate["metric"] == "gross_profit"] == [
        "p11-r0"
    ]


def test_primary_insurance_technical_account_outranks_profit_before_tax() -> None:
    candidates = [
        {
            "id": "p12-r0", "metric": "profit_before_tax", "statement_scope": "company",
            "statement_type": "income_statement",
            "unit": "GBP", "source_label": "Profit before taxation",
            "current_display": "3,330,865", "previous_display": "368,777",
        },
        {
            "id": "p28-r4", "metric": "technical_account_result",
            "statement_scope": "company", "unit": "GBP",
            "statement_type": "income_statement",
            "source_label": "Balance on the technical account for general business",
            "current_display": "3,510,510", "previous_display": "422,146",
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)
    operating_results = [
        candidate for candidate in equivalents if candidate["metric"] == "operating_result"
    ]

    assert [candidate["derivation"]["formula"] for candidate in operating_results] == [
        "technical_account_result"
    ]


def test_profit_before_tax_remains_operating_result_fallback_without_technical_account() -> None:
    equivalents = add_canonical_equivalents([{
        "id": "p12-r0", "metric": "profit_before_tax", "page": 12,
        "statement_type": "income_statement", "statement_scope": "company",
        "unit": "GBP", "source_label": "Profit before taxation",
        "current_display": "3,330,865", "previous_display": "368,777",
    }])

    operating_results = [
        candidate for candidate in equivalents if candidate["metric"] == "operating_result"
    ]
    assert [candidate["derivation"]["formula"] for candidate in operating_results] == [
        "profit_before_tax"
    ]


def test_company_income_statement_profit_before_tax_beats_group_cash_flow_fallback() -> None:
    candidates = [
        {
            "id": "company-p11-r2", "metric": "profit_before_tax", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP_THOUSANDS", "source_label": "Profit before tax",
            "current_display": "10", "previous_display": "4",
        },
        {
            "id": "group-p13-r0", "metric": "profit_before_tax", "page": 13,
            "statement_type": "cash_flow", "statement_scope": "consolidated_group",
            "unit": "GBP_THOUSANDS", "source_label": "Profit for the year",
            "current_display": "8", "previous_display": "3",
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)
    kept, report = vlm_financials.apply_consolidated_scope_policy(equivalents)
    operating_results = [candidate for candidate in kept if candidate["metric"] == "operating_result"]

    assert [candidate["derivation"]["source_candidate_ids"] for candidate in operating_results] == [
        ["company-p11-r2"]
    ]
    assert report["consolidated_metrics"] == []


def test_insurance_primary_statement_evidence_beats_higher_confidence_other_page() -> None:
    candidates = [
        {
            "id": "p11-earned", "metric": "net_earned_premiums", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Earned premiums, net of reinsurance",
            "current_display": "(122,496)", "previous_display": "11,441,868", "confidence": 0.95,
        },
        {
            "id": "p11-claims", "metric": "claims_incurred_net_reinsurance", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Claims incurred, net of reinsurance",
            "current_display": "1,038,645", "previous_display": "(1,901,305)", "confidence": 0.95,
        },
        {
            "id": "p11-result", "metric": "technical_account_result", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Balance on the technical account for general business",
            "current_display": "3,510,510", "previous_display": "422,146", "confidence": 0.95,
        },
        {
            "id": "p12-profit", "metric": "profit_before_tax", "page": 12,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Profit before taxation",
            "current_display": "3,330,865", "previous_display": "368,777", "confidence": 0.95,
        },
        {
            "id": "p28-earned", "metric": "net_earned_premiums", "page": 28,
            "statement_type": "other", "statement_scope": "company", "unit": "GBP",
            "source_label": "Reinsurance inwards", "current_display": "4,317,005",
            "previous_display": "29,978,493", "confidence": 0.98,
        },
        {
            "id": "p28-claims", "metric": "claims_incurred_net_reinsurance", "page": 28,
            "statement_type": "other", "statement_scope": "company", "unit": "GBP",
            "source_label": "Reinsurance inwards", "current_display": "1,038,645",
            "previous_display": "(1,901,305)", "confidence": 0.98,
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)

    gross_profit = next(candidate for candidate in equivalents if candidate["metric"] == "gross_profit")
    operating_result = next(candidate for candidate in equivalents if candidate["metric"] == "operating_result")
    assert gross_profit["current_display"] == "916,149"
    assert gross_profit["previous_display"] == "9,540,563"
    assert gross_profit["derivation"]["source_candidate_ids"] == ["p11-earned", "p11-claims"]
    assert operating_result["derivation"]["source_candidate_ids"] == ["p11-result"]


def test_exact_insurance_note_is_fallback_only_when_primary_statement_is_missing() -> None:
    note_rows = [
        {
            "id": "p28-earned", "metric": "net_earned_premiums", "page": 28,
            "statement_type": "other", "statement_scope": "company", "unit": "GBP",
            "source_label": "Earned premiums, net of reinsurance",
            "current_display": "50", "previous_display": "40",
        },
        {
            "id": "p28-claims", "metric": "claims_incurred_net_reinsurance", "page": 28,
            "statement_type": "other", "statement_scope": "company", "unit": "GBP",
            "source_label": "Claims incurred, net of reinsurance",
            "current_display": "(20)", "previous_display": "(10)",
        },
    ]

    fallback = add_canonical_equivalents_by_statement_scope(note_rows)
    gross_profit = next(candidate for candidate in fallback if candidate["metric"] == "gross_profit")
    assert gross_profit["current_display"] == "30"
    assert gross_profit["evidence_tier"] == 4
    assert gross_profit["source_role"] == "exact_insurance_note"


def test_insurance_gross_profit_does_not_mix_pages_scopes_or_units() -> None:
    candidates = [
        {
            "id": "p11-earned", "metric": "net_earned_premiums", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Earned premiums, net of reinsurance",
            "current_display": "50", "previous_display": "40",
        },
        {
            "id": "p12-claims", "metric": "claims_incurred_net_reinsurance", "page": 12,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "GBP", "source_label": "Claims incurred, net of reinsurance",
            "current_display": "(20)", "previous_display": "(10)",
        },
        {
            "id": "group-claims", "metric": "claims_incurred_net_reinsurance", "page": 11,
            "statement_type": "income_statement", "statement_scope": "consolidated_group",
            "unit": "GBP", "source_label": "Claims incurred, net of reinsurance",
            "current_display": "(20)", "previous_display": "(10)",
        },
        {
            "id": "usd-claims", "metric": "claims_incurred_net_reinsurance", "page": 11,
            "statement_type": "income_statement", "statement_scope": "company",
            "unit": "USD", "source_label": "Claims incurred, net of reinsurance",
            "current_display": "(20)", "previous_display": "(10)",
        },
    ]

    equivalents = add_canonical_equivalents_by_statement_scope(candidates)
    assert not [candidate for candidate in equivalents if candidate["metric"] == "gross_profit"]
def test_employee_evidence_pages_are_selected_without_statement_neighbours() -> None:
    locator = {"pages": [
        {"page": 4, "statement_type": "other", "contains_employee_count": True},
        {"page": 7, "statement_type": "income_statement", "contains_employee_count": False},
        {"page": 9, "statement_type": "other", "contains_employee_count": False},
    ]}

    assert vlm_financials.employee_evidence_pages(locator, 10) == [4]
    assert statement_pages(locator, 10) == [6, 7, 8]


def test_employee_note_candidate_pages_start_at_the_financial_statement_section() -> None:
    locator = {"pages": [
        {"page": 1, "statement_type": "other"},
        {"page": 2, "statement_type": "income_statement"},
        {"page": 3, "statement_type": "other"},
        {"page": 4, "statement_type": "balance_sheet"},
        {"page": 5, "statement_type": "other"},
        {"page": 6, "statement_type": "other"},
    ]}

    assert vlm_financials.employee_note_candidate_pages(locator, 6) == [3, 5, 6]


def test_narrative_zero_employee_page_scanner_recognises_unambiguous_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def get_text(self, _kind: str) -> str:
            return self.text

    class FakeDocument:
        page_count = 3

        def load_page(self, index: int) -> FakePage:
            return [
                FakePage("Notes"),
                FakePage("The Company has no employees during the year."),
                FakePage("No employees other than directors."),
            ][index]

        def close(self) -> None:
            return None

    class FakeFitz:
        @staticmethod
        def open(_path: str) -> FakeDocument:
            return FakeDocument()

    monkeypatch.setattr(vlm_financials, "fitz", FakeFitz)

    assert vlm_financials.narrative_zero_employee_pages(Path("example.pdf"), None) == [2]


def test_targeted_employee_note_extraction_recovers_narrative_zero_missed_by_locator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pdf_path = tmp_path / "case.pdf"
    pdf_path.write_bytes(b"placeholder")
    rendered = [RenderedPage(page, "aGVsbG8=") for page in range(1, 4)]
    monkeypatch.setattr(vlm_financials, "render_pages", lambda *_args, **kwargs: (
        [rendered[number - 1] for number in kwargs["page_numbers"]]
        if kwargs.get("page_numbers") is not None else rendered
    ))
    monkeypatch.setattr(vlm_financials, "narrative_zero_employee_pages", lambda *_args: [])

    class EmployeeNoteClient:
        provider_name = "test"

        def generate_json(
            self, _model: str, prompt: str, pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult({"pages": [
                    {
                        "statement_type": "income_statement" if page.page == 1 else "other",
                        "contains_employee_count": False,
                    }
                    for page in pages
                ]}, {}, 0.1)
            if prompt == vlm_financials.EMPLOYEE_EXTRACTION_PROMPT:
                return ModelCallResult({"pages": [
                    {
                        "statement_type": "employee_note" if page.page == 3 else "other",
                        "unit": "COUNT",
                        "rows": ([{
                            "metric": "employees", "source_label": "Employees",
                            "current_value_count": 0, "previous_value_count": None,
                            "current_evidence_kind": "narrative_zero",
                            "previous_evidence_kind": "none", "period_scope": "current",
                            "evidence_text": "The Company has no employees.", "confidence": 1.0,
                        }] if page.page == 3 else []),
                    }
                    for page in pages
                ]}, {}, 0.1)
            if prompt == vlm_financials.EXTRACTION_PROMPT:
                return ModelCallResult({"pages": [
                    {
                        "statement_type": "income_statement" if page.page == 1 else "other",
                        "unit": "GBP",
                        "rows": ([{
                            "metric": "turnover", "source_label": "Turnover",
                            "current_display": "10", "previous_display": "9",
                        }] if page.page == 1 else []),
                    }
                    for page in pages
                ]}, {}, 0.1)
            return ModelCallResult({"financial_period_summaries": {
                "current": {"employees": {"candidate_id": "employee-p3-r0", "confidence": 1.0}},
                "previous": {"employees": {"candidate_id": None, "reason": "not disclosed"}},
            }}, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    payload = vlm_financials.process_pdf_vlm_financials(pdf_path, EmployeeNoteClient())

    assert payload["employee_evidence_pages"] == [3]
    assert payload["employee_evidence_pages_by_source"]["targeted_note_extraction"] == [3]
    assert payload["raw_extraction"]["employee_locator"] is None
    assert payload["metrics"][0]["value_count"] == 0


def test_employee_narrative_zero_is_a_current_period_count_with_verbatim_evidence() -> None:
    extraction = {"pages": [{
        "page": 17,
        "statement_type": "employee_note",
        "unit": "COUNT",
        "rows": [{
            "metric": "employees",
            "source_label": "Employees and directors",
            "current_display": None,
            "previous_display": None,
            "current_value_count": 0,
            "previous_value_count": None,
            "current_evidence_kind": "narrative_zero",
            "previous_evidence_kind": "none",
            "period_scope": "current",
            "current_column": "Year ended 31 December 2025",
            "previous_column": "Year ended 31 December 2024",
            "evidence_text": "The Company has no employees.",
        }],
    }]}

    candidates, accepted, report = vlm_financials.validate_extraction_candidates(
        extraction, id_prefix="employee-"
    )
    assert report["invalid_pages"] == []
    assert candidates[0]["current_value_count"] == 0
    assert candidates[0]["current_evidence_kind"] == "narrative_zero"

    metrics = selected_metrics(accepted, {
        "financial_period_summaries": {
            "current": {"employees": {"candidate_id": "employee-p17-r0", "reason": "direct note"}},
            "previous": {"employees": {"candidate_id": None, "reason": "no comparative disclosure"}},
        },
    })

    assert [(metric["period_type"], metric["value_count"]) for metric in metrics] == [("current", 0)]
    assert metrics[0]["displayed_value"] is None
    assert metrics[0]["evidence_text"] == "The Company has no employees."
    assert metrics[0]["validation"]["evidence_kind"] == "narrative_zero"


def test_employee_narrative_zero_rejects_staff_costs_and_qualified_claims() -> None:
    extraction = {"pages": [{
        "page": 17,
        "statement_type": "employee_note",
        "unit": "COUNT",
        "rows": [
            {
                "metric": "employees", "source_label": "Employees", "current_value_count": 0,
                "current_evidence_kind": "narrative_zero", "evidence_text": "No staff costs were capitalised.",
            },
            {
                "metric": "employees", "source_label": "Employees", "current_value_count": 0,
                "current_evidence_kind": "narrative_zero",
                "evidence_text": "The Company has no employees other than directors.",
            },
        ],
    }]}

    candidates, accepted, report = vlm_financials.validate_extraction_candidates(
        extraction, id_prefix="employee-"
    )

    assert accepted == []
    assert report["invalid_pages"] == [17]
    assert [candidate["row_validation"]["issues"][0]["code"] for candidate in candidates] == [
        "invalid_narrative_zero_evidence", "ambiguous_narrative_zero_evidence",
    ]


def test_employee_narrative_zero_accepts_average_staff_was_nil_for_both_periods() -> None:
    evidence = "The average number of staff employed by the Company during the period was nil (2023 - nil)."
    _, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 19,
        "statement_type": "employee_note",
        "statement_scope": "company",
        "unit": "COUNT",
        "rows": [{
            "metric": "employees",
            "source_label": "Staff employed",
            "current_display": None,
            "previous_display": None,
            "current_value_count": 0,
            "previous_value_count": 0,
            "current_evidence_kind": "narrative_zero",
            "previous_evidence_kind": "narrative_zero",
            "period_scope": "both",
            "evidence_text": evidence,
        }],
    }]})

    assert len(accepted) == 1
    assert report["invalid_pages"] == []


def test_employee_prompts_explicitly_support_narrative_zero_with_period_scope() -> None:
    assert "has no employees" in vlm_financials.LOCATOR_PROMPT
    assert "narrative_zero" in vlm_financials.EMPLOYEE_EXTRACTION_PROMPT
    assert "period_scope" in vlm_financials.EMPLOYEE_EXTRACTION_PROMPT


def test_row_validation_rejects_clear_metric_label_conflicts_and_unknown_units() -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 12,
        "unit": "GBP",
        "rows": [
            {
                "metric": "cash",
                "source_label": "Current assets",
                "current_display": "100",
                "previous_display": "90",
            },
            {
                "metric": "turnover",
                "source_label": "Turnover",
                "current_display": "2025",
                "previous_display": "2024",
            },
        ],
    }]})

    assert accepted == []
    assert report["invalid_pages"] == [12]
    assert [candidate["row_validation"]["issues"][0]["code"] for candidate in candidates] == [
        "metric_label_conflict",
        "year_used_as_value",
    ]


def test_row_validation_rejects_total_equity_and_liabilities_as_net_assets() -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 10,
        "unit": "GBP_THOUSANDS",
        "rows": [{
            "metric": "net_assets",
            "source_label": "TOTAL EQUITY AND LIABILITIES",
            "current_display": "114",
            "previous_display": "104",
        }],
    }]})

    assert accepted == []
    assert report["invalid_pages"] == [10]
    assert candidates[0]["row_validation"]["issues"][0]["code"] == "metric_label_conflict"


def test_row_validation_rejects_vague_labels_for_native_insurance_metrics() -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 28,
        "statement_type": "other",
        "statement_scope": "company",
        "unit": "GBP",
        "rows": [{
            "metric": "net_earned_premiums",
            "source_label": "Reinsurance inwards",
            "current_display": "4,317,005",
            "previous_display": "29,978,493",
        }],
    }]})

    assert accepted == []
    assert report["invalid_pages"] == [28]
    assert candidates[0]["row_validation"]["issues"][-1]["code"] == (
        "insurance_metric_label_conflict"
    )


def test_row_validation_accepts_standalone_shareholders_funds_as_net_assets() -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 10,
        "statement_type": "balance_sheet",
        "unit": "GBP",
        "rows": [{
            "metric": "net_assets",
            "source_label": "Shareholders' funds",
            "current_display": "16,132",
            "previous_display": "1",
        }],
    }]})

    assert [candidate["id"] for candidate in accepted] == ["p10-r0"]
    assert candidates[0]["row_validation"]["status"] == "accepted"
    assert report["invalid_pages"] == []


def test_row_validation_marks_incomplete_two_period_money_rows_for_recovery() -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 13,
        "statement_type": "balance_sheet",
        "unit": "GBP",
        "rows": [{
            "metric": "cash",
            "source_label": "Cash at bank and in hand",
            "current_display": "203,776",
            "previous_display": None,
            "current_column": "2024 £",
            "previous_column": "2023 £",
        }],
    }]})

    assert [candidate["id"] for candidate in accepted] == ["p13-r0"]
    assert candidates[0]["row_validation"]["status"] == "accepted"
    assert report["invalid_pages"] == []
    assert report["incomplete_period_pair_pages"] == [13]
    assert report["incomplete_period_pairs_by_page"][13][0]["issue"]["code"] == (
        "incomplete_two_period_money_row"
    )


def test_row_validation_rejects_combined_total_as_shareholders_funds() -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 14,
        "statement_type": "balance_sheet",
        "statement_scope": "company",
        "unit": "GBP",
        "rows": [{
            "metric": "shareholders_funds",
            "source_label": "Total liabilities and shareholders' funds",
            "current_display": "22,058,582",
            "previous_display": "21,942,368",
        }],
    }]})

    assert accepted == []
    assert report["invalid_pages"] == [14]
    assert candidates[0]["row_validation"]["issues"][0]["code"] == "metric_label_conflict"


@pytest.mark.parametrize(
    "source_label",
    ["Net operating expenses", "Loss/(profit) on exchange", "Auditor's remuneration"],
)
def test_row_validation_rejects_operating_result_component_rows(source_label: str) -> None:
    candidates, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 31,
        "statement_type": "income_statement",
        "statement_scope": "consolidated_group",
        "unit": "GBP",
        "rows": [{
            "metric": "operating_result",
            "source_label": source_label,
            "current_display": "(2,191,447)",
            "previous_display": "9,390,248",
        }],
    }]})

    assert len(candidates) == 1
    assert accepted == []
    assert report["issues_by_page"][31][0]["issues"][0]["code"] == "metric_label_conflict"


def test_row_validation_accepts_direct_operating_profit_row() -> None:
    _, accepted, report = vlm_financials.validate_extraction_candidates({"pages": [{
        "page": 11,
        "statement_type": "income_statement",
        "statement_scope": "company",
        "unit": "GBP",
        "rows": [{
            "metric": "operating_result",
            "source_label": "Operating profit",
            "current_display": "3,510,510",
            "previous_display": "422,146",
        }],
    }]})

    assert len(accepted) == 1
    assert report["invalid_pages"] == []


def test_income_statement_cash_cannot_override_company_balance_sheet_cash_by_scope() -> None:
    extraction = {"pages": [
        {
            "page": 10, "statement_type": "balance_sheet", "statement_scope": "company",
            "unit": "GBP", "rows": [{
                "metric": "cash", "source_label": "Cash and cash equivalents",
                "current_display": "11,285", "previous_display": "-",
            }],
        },
        {
            "page": 21, "statement_type": "income_statement",
            "statement_scope": "consolidated_group", "unit": "GBP", "rows": [{
                "metric": "cash", "source_label": "Cash at bank and in hand",
                "current_display": "11,285", "previous_display": "11,285",
            }],
        },
    ]}

    all_candidates, accepted, report = vlm_financials.validate_extraction_candidates(extraction)
    equivalents = add_canonical_equivalents_by_statement_scope(accepted)
    kept, scope_report = vlm_financials.apply_consolidated_scope_policy(equivalents)

    assert len(all_candidates) == 2
    assert report["invalid_pages"] == [21]
    cash = [candidate for candidate in kept if candidate["metric"] == "cash"]
    assert [(candidate["page"], candidate["previous_display"]) for candidate in cash] == [(10, "-")]
    assert scope_report["consolidated_metrics"] == []


def test_rationalisation_diagnostics_distinguish_model_omission_from_rejected_evidence() -> None:
    accepted = [{
        "id": "p14-r3", "metric": "net_assets", "page": 14, "unit": "GBP",
        "current_display": "16,132", "previous_display": "1",
    }]
    rejected = [{
        "id": "p14-r2", "metric": "net_assets", "page": 14, "unit": "GBP",
        "current_display": "22,058,582", "previous_display": "21,942,368",
        "row_validation": {"status": "rejected", "issues": [{
            "code": "metric_label_conflict",
        }]},
    }]
    decisions = {"financial_period_summaries": {
        "current": {"net_assets": {
            "candidate_id": None, "reason": "No selection", "confidence": 0.0,
        }},
        "previous": {},
    }}

    diagnostics = vlm_financials.rationalisation_diagnostics(
        accepted, accepted + rejected, decisions
    )

    current = diagnostics["current"]["net_assets"]
    assert current["status"] == "unselected_despite_usable_candidate"
    assert current["usable_candidate_ids"] == ["p14-r3"]
    assert current["rejected_candidates"][0]["id"] == "p14-r2"


def test_rationalisation_response_requires_a_reason_for_no_selection() -> None:
    with pytest.raises(ValueError, match="decision object with a reason"):
        vlm_financials.validate_rationalisation_response({
            "financial_period_summaries": {"current": {"net_assets": None}, "previous": {}},
        })
    vlm_financials.validate_rationalisation_response({
        "financial_period_summaries": {"current": {"net_assets": {
            "candidate_id": None, "reason": "No balance-sheet candidate", "confidence": 0.0,
        }}, "previous": {}},
    })


def test_paired_period_completion_reuses_only_the_same_visible_row() -> None:
    candidates = [{
        "id": "p13-r3",
        "metric": "net_assets",
        "page": 13,
        "unit": "GBP",
        "current_display": "4,256,780",
        "previous_display": "18,357,126",
    }]
    model_output = {"financial_period_summaries": {
        "current": {"net_assets": {"candidate_id": "p13-r3", "confidence": 0.95}},
        "previous": {"net_assets": None},
    }}

    resolved, completions = vlm_financials.complete_paired_period_choices(
        candidates, model_output
    )

    assert model_output["financial_period_summaries"]["previous"]["net_assets"] is None
    assert resolved["financial_period_summaries"]["previous"]["net_assets"] == {
        "candidate_id": "p13-r3",
        "reason": "paired_period_same_statement_row",
        "confidence": 0.95,
    }
    assert completions == [{
        "metric": "net_assets",
        "source_period": "current",
        "target_period": "previous",
        "candidate_id": "p13-r3",
    }]


def test_rationalisation_prompt_treats_visible_dashes_as_zero_values() -> None:
    assert "valid reported zero" in vlm_financials.RATIONALISATION_PROMPT
    assert "select that row for both periods" in vlm_financials.RATIONALISATION_PROMPT
    assert "do not select it or infer a dash from `evidence_text`" in vlm_financials.RATIONALISATION_PROMPT
    assert "Shareholders' funds" in vlm_financials.RATIONALISATION_PROMPT
    assert "shareholders_funds" in vlm_financials.EXTRACTION_PROMPT
    assert "return it literally as `-`, never null" in vlm_financials.EXTRACTION_PROMPT
    assert "must be returned literally as `-`, never null" in vlm_financials.ROW_VALIDATION_RECOVERY_PROMPT
    assert "Never return bare `null`" in vlm_financials.RATIONALISATION_PROMPT
    assert "SIC information" in vlm_financials.RATIONALISATION_PROMPT


def test_company_context_is_advisory_and_document_evidence_can_override_it() -> None:
    context = vlm_financials.normalise_company_context({
        "company_number": "14732484",
        "sic_codes": ["65110 - Life insurance", "65110 - Life insurance"],
    })
    diagnostics = vlm_financials.company_context_diagnostics(context, [{
        "id": "p11-r9", "source_role": "primary_insurance_income_statement",
    }])

    assert context == {
        "company_number": "14732484", "sic_codes": ["65110 - Life insurance"],
    }
    assert diagnostics["sic_document_alignment"] == "agreement"
    assert diagnostics["primary_insurance_candidate_ids"] == ["p11-r9"]


def test_database_company_context_reads_sic_without_an_external_request(tmp_path: Path) -> None:
    database = tmp_path / "companies-house.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table leads (company_number text primary key, sic_1 text)")
        connection.execute(
            "insert into leads values (?, ?)", ("14732484", "65110 - Life insurance")
        )

    context = vlm_financials.company_context_from_sqlite(database, "14732484")

    assert context == {
        "company_number": "14732484", "sic_codes": ["65110 - Life insurance"],
    }


def test_failed_statement_page_uses_high_resolution_configured_recovery_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render_edges: list[int] = []

    def fake_render_pages(
        *_args: object, long_edge: int, page_numbers: list[int] | None = None, **_kwargs: object
    ) -> list[RenderedPage]:
        render_edges.append(long_edge)
        return [RenderedPage(1, "aGVsbG8=")]

    monkeypatch.setattr(vlm_financials, "render_pages", fake_render_pages)

    class RecoveryClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def generate_json(
            self, model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            self.calls.append((model, prompt))
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult({"pages": [{"statement_type": "balance_sheet"}]}, {}, 0.1)
            if vlm_financials.HIGH_RESOLUTION_RECOVERY_PROMPT in prompt:
                return ModelCallResult({"pages": [{
                    "statement_type": "balance_sheet", "unit": "GBP", "rows": [{
                        "metric": "net_assets", "source_label": "Net assets",
                        "current_display": "100", "previous_display": "90",
                        "current_column": "2025", "previous_column": "2024",
                        "evidence_text": "Net assets 100 90", "confidence": 0.95,
                    }],
                }]}, {}, 0.1)
            if prompt == vlm_financials.EXTRACTION_PROMPT:
                return ModelCallResult({"pages": [{
                    "statement_type": "balance_sheet", "unit": "GBP", "rows": [],
                }]}, {}, 0.1)
            return ModelCallResult({"financial_period_summaries": {
                "current": {"net_assets": {"candidate_id": "p1-r0", "confidence": 0.95}},
                "previous": {"net_assets": {"candidate_id": "p1-r0", "confidence": 0.95}},
            }}, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = RecoveryClient()
    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"),
        client,
        vision_model="primary-vision",
        recovery_vision_model="recovery-vision",
    )

    assert render_edges == [384, 1440, 2048]
    assert any(
        model == "recovery-vision" and vlm_financials.HIGH_RESOLUTION_RECOVERY_PROMPT in prompt
        for model, prompt in client.calls
    )
    assert payload["usage"]["vision_recovery"]["model"] == "recovery-vision"
    assert payload["raw_extraction"]["coverage"]["recovery_pages"] == [1]


def test_ollama_client_uses_native_vision_payload_and_returns_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse({
            "message": {"content": '{"pages":[]}'},
            "prompt_eval_count": 31,
            "eval_count": 7,
            "total_duration": 2_000_000,
        })

    monkeypatch.setattr(vlm_financials.requests, "post", fake_post)
    result = OllamaVlmModelClient().generate_json(
        "qwen3-vl:test",
        "Find statement pages.",
        [RenderedPage(page=4, image_b64="image-one"), RenderedPage(page=5, image_b64="image-two")],
        60,
    )

    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["format"] == "json"
    assert payload["messages"][0]["images"] == ["image-one", "image-two"]
    assert "Image 1 is document page 4." in payload["messages"][0]["content"]
    assert result.payload == {"pages": []}
    assert result.usage["prompt_tokens"] == 31
    assert result.elapsed_seconds >= 0


def test_ollama_client_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="local SSH or SSM tunnel"):
        OllamaVlmModelClient("http://10.0.0.12:11434")


def test_ollama_health_check_uses_tags_without_starting_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse({"models": [{"name": "private-vision:latest"}]})

    monkeypatch.setattr(vlm_financials.requests, "get", fake_get)
    assert OllamaVlmModelClient().health_check({"private-vision"}) == ["private-vision:latest"]
    assert captured["url"] == "http://127.0.0.1:11434/api/tags"
    assert captured["timeout"] == 10


def test_ollama_health_check_reports_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        vlm_financials.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse({"models": [{"name": "other-model"}]}),
    )
    with pytest.raises(RuntimeError, match="required model"):
        OllamaVlmModelClient().health_check({"private-vision"})


def test_batched_locator_calls_combine_usage_and_page_results() -> None:
    combined = combine_model_calls(
        [
            ModelCallResult({"pages": [{"page": 1}]}, {"prompt_tokens": 5}, 1.2, 10, 0.9),
            ModelCallResult({"pages": [{"page": 2}]}, {"prompt_tokens": 7}, 2.3, 20, 1.8),
        ],
        pages=[{"page": 1}, {"page": 2}],
    )
    assert combined.payload == {"pages": [{"page": 1}, {"page": 2}]}
    assert combined.usage == {"prompt_tokens": 12}
    assert combined.elapsed_seconds == 3.5
    assert combined.image_payload_bytes == 30
    assert combined.model_reported_seconds == 2.7


def test_code_attaches_document_pages_by_response_order_and_discards_model_page() -> None:
    batch = [RenderedPage(page, "aGVsbG8=") for page in (9, 10, 11)]
    returned = [
        {"page": 99, "statement_type": "income_statement"},
        {"page": "Document page 1", "statement_type": "balance_sheet"},
        {"statement_type": "other"},
    ]

    assert vlm_financials.attach_document_pages(returned, batch) == [
        {"page": 9, "statement_type": "income_statement"},
        {"page": 10, "statement_type": "balance_sheet"},
        {"page": 11, "statement_type": "other"},
    ]
    with pytest.raises(ValueError, match="count must equal"):
        vlm_financials.attach_document_pages(returned[:2], batch)


def test_pdf_pipeline_batches_locator_and_extraction_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = [RenderedPage(page, "aGVsbG8=") for page in range(1, 6)]
    render_calls: list[tuple[int, list[int] | None]] = []

    def fake_render_pages(
        *_args: object,
        long_edge: int,
        page_numbers: list[int] | None = None,
        **_kwargs: object,
    ) -> list[RenderedPage]:
        render_calls.append((long_edge, page_numbers))
        return (
            [rendered[number - 1] for number in page_numbers]
            if page_numbers is not None
            else rendered
        )

    monkeypatch.setattr(vlm_financials, "render_pages", fake_render_pages)

    class RecordingClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.calls: list[tuple[str, list[int]]] = []

        def generate_json(
            self, _model: str, prompt: str, pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            self.calls.append((prompt, [page.page for page in pages]))
            if prompt == vlm_financials.LOCATOR_PROMPT:
                payload = {
                    "pages": [
                        {
                            "page": 999,
                            "statement_type": (
                                "income_statement" if page.page == 3 else "other"
                            ),
                        }
                        for page in pages
                    ]
                }
            else:
                if prompt == vlm_financials.EXTRACTION_PROMPT:
                    payload = {
                        "pages": [
                            {
                                "page": page.page,
                                "statement_type": "income_statement",
                                "unit": "GBP",
                                "rows": [{
                                    "metric": "turnover",
                                    "current_display": "100",
                                    "previous_display": "90",
                                }] if page.page == 3 else [],
                            }
                            for page in pages
                        ]
                    }
                else:
                    payload = {
                        "financial_period_summaries": {
                            "current": {"turnover": {"candidate_id": "p3-r0"}},
                            "previous": {"turnover": {"candidate_id": "p3-r0"}},
                        }
                    }
            return ModelCallResult(payload, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = RecordingClient()
    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"),
        client,
        locator_batch_size=2,
        extraction_batch_size=2,
    )
    locator_calls = [pages for prompt, pages in client.calls if prompt == vlm_financials.LOCATOR_PROMPT]
    extraction_calls = [
        pages for prompt, pages in client.calls if prompt == vlm_financials.EXTRACTION_PROMPT
    ]
    assert locator_calls == [[1, 2], [3, 4], [5]]
    assert extraction_calls == [[2, 3], [4]]
    assert render_calls == [(384, None), (1440, [2, 3, 4]), (2048, [3])]
    assert payload["timing"]["locator_batches"] == 3
    assert payload["timing"]["extraction_batches"] == 2


def test_statement_page_coverage_recovers_an_empty_statement_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = [RenderedPage(page, "aGVsbG8=") for page in range(1, 5)]
    monkeypatch.setattr(vlm_financials, "render_pages", lambda *_args, **kwargs: (
        [rendered[number - 1] for number in kwargs["page_numbers"]]
        if kwargs.get("page_numbers") is not None else rendered
    ))

    class OmittedPageClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.vision_pages: list[list[int]] = []

        def generate_json(
            self, _model: str, prompt: str, pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            page_numbers = [page.page for page in pages]
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {
                        "pages": [
                            {
                                "statement_type": (
                                    "income_statement" if page.page == 2 else "other"
                                )
                            }
                            for page in pages
                        ]
                    },
                    {},
                    0.1,
                )
            if prompt.startswith(vlm_financials.EXTRACTION_PROMPT):
                self.vision_pages.append(page_numbers)
                return ModelCallResult(
                    {
                        "pages": [
                            {
                                "statement_type": (
                                    "income_statement" if page.page == 2 else "other"
                                ),
                                "unit": "GBP",
                                "rows": (
                                    [
                                            {
                                                "metric": "turnover",
                                                "source_label": "Turnover",
                                                "current_display": "100",
                                            "previous_display": "90",
                                        }
                                    ]
                                    if len(page_numbers) == 1
                                    else []
                                ),
                            }
                            for page in pages
                        ]
                    },
                    {},
                    0.1,
                )
            return ModelCallResult({"financial_period_summaries": {
                "current": {"turnover": {"candidate_id": "p2-r0"}},
                "previous": {"turnover": {"candidate_id": "p2-r0"}},
            }}, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = OmittedPageClient()
    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"), client, extraction_batch_size=3
    )

    assert client.vision_pages == [[1, 2, 3], [2]]
    assert payload["status"] == "complete"
    assert payload["raw_extraction"]["coverage"] == {
        "required_statement_pages": [2],
        "returned_statement_pages": [2],
        "recovery_pages": [2],
        "missing_after_recovery_pages": [],
        "empty_after_recovery_pages": [],
        "unrecovered_pages": [],
        "warnings": [],
    }
    assert [item["source_page"] for item in payload["metrics"]] == [2, 2]


def test_incomplete_statement_extractions_requires_rows() -> None:
    assert incomplete_statement_extractions(
        [{"page": 2, "rows": []}, {"page": 3, "rows": [{"metric": "turnover"}]}],
        {2, 3, 4},
    ) == [2, 4]


def test_statement_page_coverage_warns_and_continues_after_empty_focused_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = [RenderedPage(1, "aGVsbG8=")]
    monkeypatch.setattr(vlm_financials, "render_pages", lambda *_args, **_kwargs: rendered)

    class EmptyStatementClient:
        provider_name = "test"

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {"pages": [{"page": 1, "statement_type": "income_statement"}]}, {}, 0.1
                )
            return ModelCallResult(
                {"pages": [{"page": 1, "statement_type": "income_statement", "rows": []}]},
                {},
                0.1,
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"), EmptyStatementClient()
    )

    assert payload["status"] == "complete"
    assert "error_stage" not in payload
    assert payload["raw_extraction"]["coverage"]["empty_after_recovery_pages"] == [1]
    assert payload["raw_extraction"]["coverage"]["unrecovered_pages"] == [1]
    assert payload["warnings"] == [{
        "code": "empty_statement_page_rows_after_recovery",
        "page": 1,
        "message": (
            "Document page 1 was classified as a statement but returned no financial rows "
            "after focused recovery"
        ),
    }]
    assert payload["usage"]["vision"]["reliability"]["failed_attempt_count"] == 0


def test_page_response_count_mismatch_fails_before_coverage_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = [RenderedPage(1, "aGVsbG8=")]
    monkeypatch.setattr(vlm_financials, "render_pages", lambda *_args, **_kwargs: rendered)

    class MissingStatementClient:
        provider_name = "test"

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {"pages": [{"page": 1, "statement_type": "income_statement"}]}, {}, 0.1
                )
            return ModelCallResult({"pages": []}, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"), MissingStatementClient()
    )

    assert payload["status"] == "error"
    assert payload["error_stage"] == "vision"
    coverage = payload["raw_extraction"]["coverage"]
    assert coverage["missing_after_recovery_pages"] == []
    assert coverage["empty_after_recovery_pages"] == []
    assert coverage["unrecovered_pages"] == []
    reliability = payload["usage"]["vision"]["reliability"]
    assert reliability["failed_attempt_count"] == 2
    assert all("count must equal" in attempt["error"] for attempt in reliability["attempts"])


def test_employee_extraction_reuses_locator_and_only_reads_flagged_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = [RenderedPage(page, "aGVsbG8=") for page in range(1, 5)]
    render_calls: list[tuple[int, list[int] | None]] = []

    def fake_render_pages(
        *_args: object,
        long_edge: int,
        page_numbers: list[int] | None = None,
        **_kwargs: object,
    ) -> list[RenderedPage]:
        render_calls.append((long_edge, page_numbers))
        return rendered if page_numbers is None else [rendered[page - 1] for page in page_numbers]

    monkeypatch.setattr(vlm_financials, "render_pages", fake_render_pages)

    class EmployeeClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.calls: list[tuple[str, list[int]]] = []

        def generate_json(
            self, _model: str, prompt: str, pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            self.calls.append((prompt, [page.page for page in pages]))
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {"pages": [
                        {
                            "statement_type": "other",
                            "contains_employee_count": page.page == 3,
                        }
                        for page in pages
                    ]},
                    {},
                    0.1,
                )
            if prompt == vlm_financials.EMPLOYEE_EXTRACTION_PROMPT:
                return ModelCallResult(
                    {"pages": [{
                        "statement_type": "employee_note",
                        "unit": "COUNT",
                        "rows": [{
                            "metric": "employees",
                            "source_label": "Average number of employees",
                            "current_display": "12",
                            "previous_display": "10",
                            "current_column": "2025",
                            "previous_column": "2024",
                        }],
                    }]},
                    {},
                    0.1,
                )
            return ModelCallResult(
                {"financial_period_summaries": {
                    "current": {"employees": {"candidate_id": "employee-p3-r0"}},
                    "previous": {"employees": {"candidate_id": "employee-p3-r0"}},
                }},
                {},
                0.1,
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = EmployeeClient()
    payload = vlm_financials.process_pdf_vlm_financials(vlm_financials.Path("example.pdf"), client)

    assert [pages for prompt, pages in client.calls if prompt == vlm_financials.LOCATOR_PROMPT] == [[1, 2, 3, 4]]
    assert [pages for prompt, pages in client.calls if prompt == vlm_financials.EMPLOYEE_EXTRACTION_PROMPT] == [[3]]
    assert render_calls == [(384, None), (1440, [3])]
    assert payload["employee_evidence_pages"] == [3]
    assert [metric["value_count"] for metric in payload["metrics"]] == [12, 10]


def test_row_validation_reextracts_only_the_invalid_page_when_it_improves_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_financials,
        "render_pages",
        lambda *_args, **_kwargs: [RenderedPage(1, "aGVsbG8=")],
    )

    class ValidationRecoveryClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.vision_prompts: list[str] = []

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {"pages": [{"statement_type": "balance_sheet"}]}, {}, 0.1
                )
            if prompt.startswith(vlm_financials.EXTRACTION_PROMPT):
                self.vision_prompts.append(prompt)
                label = (
                    "Cash at bank"
                    if "deterministic evidence check" in prompt
                    else "Current assets"
                )
                return ModelCallResult(
                    {"pages": [{
                        "statement_type": "balance_sheet",
                        "unit": "GBP",
                        "rows": [{
                            "metric": "cash",
                            "source_label": label,
                            "current_display": "100",
                            "previous_display": "90",
                        }],
                    }]},
                    {},
                    0.1,
                )
            return ModelCallResult(
                {"financial_period_summaries": {
                    "current": {"cash": {"candidate_id": "p1-r0"}},
                    "previous": {"cash": {"candidate_id": "p1-r0"}},
                }},
                {},
                0.1,
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = ValidationRecoveryClient()
    payload = vlm_financials.process_pdf_vlm_financials(vlm_financials.Path("example.pdf"), client)

    assert len(client.vision_prompts) == 2
    assert payload["raw_extraction"]["row_validation"]["financial"]["recovery_pages"] == [1]
    assert payload["raw_extraction"]["row_validation"]["financial"]["replaced_pages"] == [1]
    assert payload["raw_extraction"]["row_validation"]["financial"]["remaining_invalid_pages"] == []
    assert [metric["value_pence"] for metric in payload["metrics"]] == [10_000, 9_000]


def test_statement_completeness_recovery_adds_missing_balance_sheet_row_without_replacing_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_financials,
        "render_pages",
        lambda *_args, **_kwargs: [RenderedPage(1, "aGVsbG8=")],
    )

    class CompletenessRecoveryClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.completeness_calls = 0

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {"pages": [{"statement_type": "balance_sheet", "confidence": 0.95}]}, {}, 0.1
                )
            if prompt.startswith(vlm_financials.EXTRACTION_PROMPT):
                complete = "deterministic completeness check" in prompt
                self.completeness_calls += int(complete)
                rows = [{
                    "metric": "cash",
                    "source_label": "Cash at bank and in hand",
                    "current_display": "100",
                    "previous_display": "90",
                }]
                if complete:
                    rows.append({
                        "metric": "net_assets",
                        "source_label": "Net assets",
                        "current_display": "400",
                        "previous_display": "300",
                    })
                return ModelCallResult(
                    {"pages": [{"statement_type": "balance_sheet", "unit": "GBP", "rows": rows}]}, {}, 0.1
                )
            return ModelCallResult(
                {"financial_period_summaries": {
                    "current": {
                        "cash": {"candidate_id": "p1-r0"},
                        "net_assets": {"candidate_id": "p1-r1"},
                    },
                    "previous": {
                        "cash": {"candidate_id": "p1-r0"},
                        "net_assets": {"candidate_id": "p1-r1"},
                    },
                }},
                {},
                0.1,
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = CompletenessRecoveryClient()
    payload = vlm_financials.process_pdf_vlm_financials(vlm_financials.Path("example.pdf"), client)

    completeness = payload["raw_extraction"]["statement_completeness"]
    assert client.completeness_calls == 1
    assert completeness["recovery_pages"] == [1]
    assert completeness["triggers_by_page"] == {1: ["balance_sheet_missing_net_assets"]}
    assert completeness["added_rows_by_page"][1] == [{"metric": "net_assets", "source_label": "Net assets"}]
    assert next(
        candidate for candidate in payload["raw_extraction"]["candidates"]
        if candidate["metric"] == "net_assets"
    )["extraction_source"] == "statement_completeness"
    assert [metric["value_pence"] for metric in payload["metrics"]] == [10_000, 40_000, 9_000, 30_000]


def test_incomplete_two_period_money_row_uses_recovery_without_inventing_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_financials,
        "render_pages",
        lambda *_args, **_kwargs: [RenderedPage(1, "aGVsbG8=")],
    )

    class PairedPeriodRecoveryClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.recovery_calls = 0

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                return ModelCallResult(
                    {"pages": [{"statement_type": "balance_sheet"}]}, {}, 0.1
                )
            if prompt.startswith(vlm_financials.EXTRACTION_PROMPT):
                recovered = "deterministic evidence check" in prompt
                self.recovery_calls += int(recovered)
                return ModelCallResult(
                    {"pages": [{
                        "statement_type": "balance_sheet",
                        "unit": "GBP",
                        "rows": [{
                            "metric": "cash",
                            "source_label": "Cash at bank and in hand",
                            "current_display": "100",
                            "previous_display": "-" if recovered else None,
                            "current_column": "2024 £",
                            "previous_column": "2023 £",
                        }],
                    }]},
                    {},
                    0.1,
                )
            return ModelCallResult(
                {"financial_period_summaries": {
                    "current": {"cash": {"candidate_id": "p1-r0", "confidence": 0.95}},
                    "previous": {"cash": {"candidate_id": "p1-r0", "confidence": 0.95}},
                }},
                {},
                0.1,
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = PairedPeriodRecoveryClient()
    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"), client
    )

    financial_validation = payload["raw_extraction"]["row_validation"]["financial"]
    assert client.recovery_calls == 1
    assert financial_validation["recovery_pages"] == [1]
    assert financial_validation["replaced_pages"] == [1]
    assert financial_validation["remaining_incomplete_period_pair_pages"] == []
    assert [metric["value_pence"] for metric in payload["metrics"]] == [10_000, 0]


def test_openrouter_client_includes_configured_request_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(_url: str, **kwargs: object) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse({
            "id": "gen-test",
            "model": "qwen/qwen3.5-9b",
            "provider": "DeepInfra",
            "openrouter_metadata": {"provider_name": "DeepInfra"},
            "choices": [{"message": {"content": '{"pages":[]}'}}],
        })

    monkeypatch.setattr(vlm_financials.requests, "post", fake_post)
    result = OpenRouterVlmModelClient("not-a-key", {"reasoning": {"enabled": False}}).generate_json(
        "qwen/qwen3.5-9b", "Find pages", [], 60
    )
    assert captured["json"]["reasoning"] == {"enabled": False}
    assert captured["headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert result.provider_metadata == {
        "generation_id": "gen-test",
        "model": "qwen/qwen3.5-9b",
        "provider": "DeepInfra",
        "openrouter_metadata": {"provider_name": "DeepInfra"},
    }


def test_openrouter_client_reports_provider_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class ErrorResponse(FakeResponse):
        text = "provider error"

        def raise_for_status(self) -> None:
            raise vlm_financials.requests.HTTPError("400")

    monkeypatch.setattr(
        vlm_financials.requests,
        "post",
        lambda *_args, **_kwargs: ErrorResponse({"error": {"message": "image limit exceeded"}}),
    )
    with pytest.raises(RuntimeError, match="image limit exceeded"):
        OpenRouterVlmModelClient("not-a-key").generate_json(
            "qwen/qwen3.5-9b", "Find pages", [], 60
        )


def test_json_repair_only_removes_structural_trailing_commas() -> None:
    payload, handling = vlm_financials._json_response_with_handling(
        'Here is the JSON:\n```json\n{"pages":[{"page":1,"reason":"keep,}"},],}\n```'
    )

    assert payload == {"pages": [{"page": 1, "reason": "keep,}"}]}
    assert handling["repaired"] is True
    assert "removed_trailing_commas" in handling["method"]


def test_invalid_json_exception_retains_the_exact_raw_response() -> None:
    raw = '{"pages":[{"page":1,"reason":"unfinished}'

    with pytest.raises(vlm_financials.ModelResponseError) as raised:
        vlm_financials._response_result(
            raw,
            usage={"cost": 0.01},
            elapsed_seconds=1.2,
            image_payload_bytes=50,
        )

    assert raised.value.attempt["raw_response"] == raw
    assert raised.value.attempt["usage"] == {"cost": 0.01}
    assert raised.value.attempt["status"] == "invalid_json"


def test_invalid_schema_is_retried_without_rerunning_other_stages() -> None:
    class SchemaRetryClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.responses = [
                ModelCallResult(
                    {"pagez": []}, {"cost": 0.01}, 0.1,
                    raw_response='{"pagez":[]}',
                    response_handling={"method": "strict", "repaired": False},
                ),
                ModelCallResult(
                    {"pages": []}, {"cost": 0.02}, 0.2,
                    raw_response='{"pages":[]}',
                    response_handling={"method": "strict", "repaired": False},
                ),
            ]

        def generate_json(
            self, _model: str, _prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            return self.responses.pop(0)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    result = vlm_financials.generate_json_reliably(
        SchemaRetryClient(),
        "model",
        "prompt",
        [],
        60,
        stage="locator",
        validator=lambda payload: vlm_financials.validate_page_response(
            payload, require_rows=False
        ),
        max_attempts=2,
    )

    assert result.payload == {"pages": []}
    assert result.usage["cost"] == pytest.approx(0.03)
    assert [attempt["status"] for attempt in result.response_attempts] == [
        "invalid_schema",
        "parsed",
    ]


def test_page_result_count_mismatch_is_retried_before_page_mapping() -> None:
    class CountRetryClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.responses = [
                ModelCallResult({"pages": [{"statement_type": "other"}]}, {}, 0.1),
                ModelCallResult(
                    {
                        "pages": [
                            {"statement_type": "income_statement"},
                            {"statement_type": "other"},
                        ]
                    },
                    {},
                    0.1,
                ),
            ]

        def generate_json(
            self, _model: str, _prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            return self.responses.pop(0)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    batch = [RenderedPage(13, "aGVsbG8="), RenderedPage(14, "aGVsbG8=")]
    result = vlm_financials.generate_json_reliably(
        CountRetryClient(),
        "model",
        "prompt",
        batch,
        60,
        stage="locator",
        validator=lambda payload: vlm_financials.validate_page_response(
            payload, require_rows=False, expected_page_count=len(batch)
        ),
        max_attempts=2,
    )

    assert [attempt["status"] for attempt in result.response_attempts] == [
        "invalid_schema",
        "parsed",
    ]
    assert vlm_financials.attach_document_pages(result.payload["pages"], batch) == [
        {"statement_type": "income_statement", "page": 13},
        {"statement_type": "other", "page": 14},
    ]


def test_pipeline_retries_only_failed_stage_and_keeps_partial_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_financials,
        "render_pages",
        lambda *_args, **_kwargs: [RenderedPage(1, "aGVsbG8=")],
    )

    class FailingRationalisationClient:
        provider_name = "test"

        def __init__(self) -> None:
            self.calls = {"locator": 0, "vision": 0, "rationalisation": 0}

        @staticmethod
        def result(payload: dict[str, object], cost: float) -> ModelCallResult:
            raw = vlm_financials.json.dumps(payload)
            return ModelCallResult(
                payload,
                {"cost": cost},
                0.1,
                raw_response=raw,
                response_handling={"method": "strict", "repaired": False},
            )

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                self.calls["locator"] += 1
                return self.result(
                    {"pages": [{"page": 1, "statement_type": "income_statement"}]},
                    0.01,
                )
            if prompt == vlm_financials.EXTRACTION_PROMPT:
                self.calls["vision"] += 1
                return self.result(
                        {"pages": [{"page": 1, "unit": "GBP", "rows": [{
                            "metric": "turnover", "source_label": "Turnover",
                        "current_display": "100",
                        "previous_display": "90",
                    }]}]},
                    0.02,
                )
            self.calls["rationalisation"] += 1
            raw = '{"financial_period_summaries":{"current":'
            raise vlm_financials.ModelResponseError(
                "truncated JSON",
                {
                    "status": "invalid_json",
                    "error": "truncated JSON",
                    "raw_response": raw,
                    "usage": {"cost": 0.03},
                    "elapsed_seconds": 0.1,
                    "image_payload_bytes": 0,
                    "provider_metadata": {},
                },
            )

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    client = FailingRationalisationClient()
    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("example.pdf"),
        client,
        json_max_attempts=2,
    )

    assert client.calls == {"locator": 1, "vision": 1, "rationalisation": 2}
    assert payload["status"] == "error"
    assert payload["error_stage"] == "rationalisation"
    assert payload["raw_extraction"]["candidates"]
    assert payload["usage"]["locator"]["usage"]["cost"] == pytest.approx(0.01)
    assert payload["usage"]["vision"]["usage"]["cost"] == pytest.approx(0.02)
    assert payload["usage"]["rationalisation"]["usage"]["cost"] == pytest.approx(0.06)
    assert payload["cost"]["usd"] == pytest.approx(0.09)
    attempts = payload["usage"]["rationalisation"]["reliability"]["attempts"]
    assert [attempt["raw_response"] for attempt in attempts] == [
        '{"financial_period_summaries":{"current":',
        '{"financial_period_summaries":{"current":',
    ]


def test_money_conversion_preserves_scale_and_sign() -> None:
    assert to_pence("1,234", "GBP", "turnover") == 123_400
    assert to_pence("(1,234)", "GBP_THOUSANDS", "cost_of_sales") == -123_400_000
    assert to_pence("2.5", "GBP_MILLIONS", "turnover") == 250_000_000
    assert to_pence("12", "UNKNOWN", "turnover") is None
    assert to_pence("-", "GBP", "turnover") == 0


def test_insurance_rows_gain_traceable_canonical_equivalents() -> None:
    extraction = {"pages": [{"page": 12, "statement_type": "income_statement", "unit": "GBP", "rows": [
        {
            "metric": "gross_premiums_written",
            "source_label": "Gross premiums written",
            "current_display": "1,027,336",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Gross premiums written 1,027,336 -",
            "confidence": 0.99,
        },
        {
            "metric": "net_earned_premiums",
            "source_label": "Earned premiums, net of reinsurance",
            "current_display": "426,652",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Earned premiums, net of reinsurance 426,652 -",
            "confidence": 0.98,
        },
        {
            "metric": "claims_incurred_net_reinsurance",
            "source_label": "Claims incurred, net of reinsurance",
            "current_display": "(275,956)",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Claims incurred, net of reinsurance (275,956) -",
            "confidence": 0.98,
        },
        {
            "metric": "technical_account_result",
            "source_label": "Balance on the technical account for general business",
            "current_display": "(12,552)",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Balance on the technical account (12,552) -",
            "confidence": 0.99,
        },
    ]}]}

    candidates = add_canonical_equivalents(extraction_candidates(extraction))
    canonical = {
        candidate["metric"]: candidate
        for candidate in candidates
        if candidate["metric"] in {"turnover", "gross_profit", "operating_result"}
    }

    assert canonical["turnover"]["current_display"] == "1,027,336"
    assert canonical["gross_profit"]["current_display"] == "150,696"
    assert canonical["operating_result"]["current_display"] == "(12,552)"
    assert canonical["gross_profit"]["derivation"] == {
        "policy": "general_insurance",
        "kind": "derived_equivalent",
        "formula": "net_earned_premiums + claims_incurred_net_reinsurance",
        "source_candidate_ids": ["p12-r1", "p12-r2"],
    }


def test_selected_insurance_equivalent_keeps_derivation_provenance() -> None:
    candidates = add_canonical_equivalents([
        {
            "id": "p12-r0",
            "metric": "technical_account_result",
            "page": 12,
            "statement_type": "income_statement",
            "unit": "GBP",
            "source_label": "Balance on the technical account for general business",
            "current_display": "(12,552)",
            "previous_display": "-",
            "current_column": "2024",
            "previous_column": "2023",
            "evidence_text": "Balance on the technical account (12,552) -",
            "confidence": 0.99,
        },
    ])
    candidate = next(item for item in candidates if item["metric"] == "operating_result")
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"operating_result": {
            "candidate_id": candidate["id"],
            "reason": "Insurance technical-account equivalent",
            "confidence": 0.99,
        }},
    }})

    assert metrics[0]["value_pence"] == -1_255_200
    assert metrics[0]["validation"]["derivation"] == {
        "policy": "general_insurance",
        "kind": "reported_equivalent",
        "formula": "technical_account_result",
        "source_candidate_ids": ["p12-r0"],
    }


def test_profit_before_tax_is_an_explicit_operating_result_equivalent() -> None:
    candidates = add_canonical_equivalents([{
        "id": "p12-r4",
        "metric": "profit_before_tax",
        "page": 12,
        "unit": "GBP",
        "source_label": "Loss before tax",
        "current_display": "(369,534)",
        "previous_display": "-",
        "current_column": "2024",
        "previous_column": "2023",
        "evidence_text": "Loss before tax (369,534) -",
        "confidence": 0.95,
    }])
    candidate = next(item for item in candidates if item["metric"] == "operating_result")

    assert candidate["current_display"] == "(369,534)"
    assert candidate["previous_display"] == "-"
    assert candidate["derivation"] == {
        "policy": "financial_summary",
        "kind": "reported_equivalent",
        "formula": "profit_before_tax",
        "source_candidate_ids": ["p12-r4"],
    }


def test_pdf_pipeline_maps_insurance_rows_before_rationalisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_financials,
        "render_pages",
        lambda *_args, **_kwargs: [RenderedPage(1, "aGVsbG8=")],
    )

    class InsuranceClient:
        provider_name = "test"

        def generate_json(
            self, _model: str, prompt: str, _pages: list[RenderedPage], _timeout: int
        ) -> ModelCallResult:
            if prompt == vlm_financials.LOCATOR_PROMPT:
                payload = {"pages": [{"page": 1, "statement_type": "income_statement"}]}
            elif prompt == vlm_financials.EXTRACTION_PROMPT:
                payload = {"pages": [{"page": 1, "unit": "GBP", "rows": [{
                    "metric": "gross_premiums_written",
                    "source_label": "Gross premiums written",
                    "current_display": "1,027,336",
                    "previous_display": "-",
                    "current_column": "2024",
                    "previous_column": "2023",
                    "evidence_text": "Gross premiums written 1,027,336 -",
                    "confidence": 0.99,
                }]}]}
            else:
                assert "insurance-turnover-p1-r0" in prompt
                assert "COMPANY_CONTEXT_ADVISORY_ONLY" in prompt
                assert "65110 - Life insurance" in prompt
                payload = {"financial_period_summaries": {
                    "current": {"turnover": {
                        "candidate_id": "insurance-turnover-p1-r0",
                        "reason": "Reported gross premiums written",
                        "confidence": 0.99,
                    }},
                    "previous": {"turnover": {
                        "candidate_id": "insurance-turnover-p1-r0",
                        "reason": "Reported gross premiums written",
                        "confidence": 0.99,
                    }},
                }}
            return ModelCallResult(payload, {}, 0.1)

        def pricing_snapshot(self) -> dict[str, dict[str, str]]:
            return {}

    payload = vlm_financials.process_pdf_vlm_financials(
        vlm_financials.Path("insurance.pdf"),
        InsuranceClient(),
        company_context={"company_number": "14732484", "sic_codes": ["65110 - Life insurance"]},
    )

    assert [
        (metric["period_type"], metric["value_pence"])
        for metric in payload["metrics"]
    ] == [("current", 102_733_600), ("previous", 0)]
    assert all(
        metric["validation"]["derivation"]["policy"] == "general_insurance"
        for metric in payload["metrics"]
    )


def test_rationalisation_selects_only_a_matching_candidate() -> None:
    extraction = {"pages": [{"page": 4, "unit": "GBP_THOUSANDS", "rows": [{
        "metric": "turnover", "source_label": "Turnover", "current_display": "1,234",
        "previous_display": "1,100", "current_column": "2025", "previous_column": "2024",
        "evidence_text": "Turnover 1,234 1,100", "confidence": 0.98,
    }]}]}
    candidates = extraction_candidates(extraction)
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"turnover": {"candidate_id": "p4-r0", "reason": "Primary statement row", "confidence": 0.95}},
        "previous": {"turnover": {"candidate_id": "p4-r0", "reason": "Primary statement row", "confidence": 0.95}},
    }})
    assert [item["value_pence"] for item in metrics] == [123_400_000, 110_000_000]
    assert all(item["source_page"] == 4 for item in metrics)


def test_extraction_candidates_accept_numeric_page_strings() -> None:
    extraction = {"pages": [{"page": "12", "unit": "GBP", "rows": [{
        "metric": "net_assets", "source_label": "Net assets",
        "current_display": "99,538,865", "previous_display": "86,490,628",
        "current_column": "2025", "previous_column": "2024",
        "evidence_text": "Net assets 99,538,865 86,490,628", "confidence": 0.95,
    }]}]}

    candidates = extraction_candidates(extraction)

    assert candidates[0]["id"] == "p12-r0"
    assert candidates[0]["page"] == 12


def test_rationalisation_rejects_a_candidate_for_the_wrong_column() -> None:
    candidates = [
        {"id": "a", "metric": "cash", "page": 2, "unit": "GBP", "current_display": "100", "previous_display": None, "source_label": "Cash", "evidence_text": "", "confidence": 0.9},
    ]
    metrics = selected_metrics(candidates, {"financial_period_summaries": {
        "current": {"turnover": {"candidate_id": "a", "reason": "wrong", "confidence": 0.9}},
    }})
    assert metrics == []


def test_vlm_results_record_models_and_cost() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    payload = {
        "pdf_path": "example.pdf",
        "models": {"locator": "locator", "vision": "vision", "rationalisation": "text-reviewer"},
        "status": "complete",
        "pages_scanned": [1, 2],
        "candidate_pages": [2],
        "raw_extraction": {},
        "rationalisation": {},
        "usage": {},
        "cost": {"pricing": {"gbp_per_usd": 0.75}, "usd": 0.01, "gbp": 0.0075, "method": "provider_reported"},
        "metrics": [{
            "period_type": "current", "metric_name": "turnover", "value_pence": 123_400,
            "value_count": None, "displayed_value": "1,234", "unit": "GBP", "source_page": 2,
            "source_label": "Turnover", "evidence_text": "Turnover 1,234", "confidence": 0.9,
            "validation": {"unit_known": True},
        }],
    }
    run_id = insert_vlm_financial_payload(conn, payload, "00000001", "document")
    run = conn.execute("select locator_model, vision_model, rationalisation_model, cost_gbp from vlm_financial_extraction_runs where id=?", (run_id,)).fetchone()
    metric = conn.execute("select company_number, metric_name, value_pence, vision_model from vlm_financial_metrics where extraction_run_id=?", (run_id,)).fetchone()
    canonical = conn.execute("select turnover, data_source from financial_period_summaries where company_number=? and document_id=? and period_type='current'", ("00000001", "document")).fetchone()
    assert run == ("locator", "vision", "text-reviewer", 0.0075)
    assert metric == ("00000001", "turnover", 123_400, "vision")
    assert canonical == (1_234, "vlm")


def test_vlm_canonical_summary_does_not_overwrite_xhtml_data() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute(
        """
        insert into financial_period_summaries (
            company_number, document_id, period_type, turnover, raw_payload, data_source
        ) values ('00000002', 'document', 'current', 999, '{}', 'xhtml')
        """
    )
    payload = {
        "pdf_path": "example.pdf",
        "models": {"locator": "locator", "vision": "vision", "rationalisation": "text-reviewer"},
        "status": "complete", "pages_scanned": [], "candidate_pages": [],
        "raw_extraction": {}, "rationalisation": {}, "usage": {},
        "cost": {"pricing": {}, "usd": 0, "gbp": 0, "method": "provider_reported"},
        "metrics": [{
            "period_type": "current", "metric_name": "turnover", "value_pence": 123_400,
            "value_count": None, "displayed_value": "1,234", "unit": "GBP", "source_page": 1,
            "source_label": "Turnover", "evidence_text": "", "confidence": 1,
            "validation": {"unit_known": True},
        }],
    }
    insert_vlm_financial_payload(conn, payload, "00000002", "document")
    assert conn.execute(
        "select turnover, data_source from financial_period_summaries where company_number='00000002'"
    ).fetchone() == (999, "xhtml")
