"""Deterministic mappings from reported rows to canonical financial metrics.

The policy deliberately ranks visible statement evidence above model confidence.
Sector registration data may be supplied to the rationaliser as context, but it
never changes the eligibility of a row in this module.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


INSURANCE_METRICS = (
    "gross_premiums_written",
    "outward_reinsurance_premiums",
    "net_premiums_written",
    "net_change_unearned_premiums",
    "net_earned_premiums",
    "allocated_investment_return",
    "total_technical_income",
    "claims_incurred_net_reinsurance",
    "net_operating_expenses",
    "technical_account_result",
    "investment_income",
)

CANONICAL_METRICS = {
    "turnover", "gross_profit", "operating_result", "profit_after_tax",
    "cash", "net_assets", "employees",
}
PRIMARY_STATEMENT_TYPES = {"income_statement", "balance_sheet", "cash_flow"}
_CANONICAL_PRIMARY_STATEMENTS = {
    "turnover": {"income_statement"},
    "gross_profit": {"income_statement"},
    "operating_result": {"income_statement"},
    "profit_after_tax": {"income_statement"},
    "cash": {"balance_sheet", "cash_flow"},
    "net_assets": {"balance_sheet"},
}
_OPERATING_RESULT_LABEL_PREFIXES = (
    "operating profit",
    "operating loss",
    "operating result",
    "profit from operations",
    "loss from operations",
    "profit on ordinary activities before interest",
    "loss on ordinary activities before interest",
)
_BEFORE_TAX_LABEL_PATTERN = re.compile(
    r"\b(?:profit|loss)(?: on ordinary activities)? before tax(?:ation)?\b"
)

# Exact visible label families for the native insurance rows emitted by the
# extraction prompt. Prefix matching permits useful qualifiers such as
# "Balance on the technical account for general business" without accepting a
# generic note label such as "Total" or "Reinsurance inwards".
_INSURANCE_LABEL_PREFIXES = {
    "gross_premiums_written": ("gross premiums written",),
    "outward_reinsurance_premiums": ("outward reinsurance premiums",),
    "net_premiums_written": ("net premiums written",),
    "net_change_unearned_premiums": ("net change in the provision for unearned premiums",),
    "net_earned_premiums": ("earned premiums net of reinsurance",),
    "allocated_investment_return": ("allocated investment return",),
    "total_technical_income": ("total technical income",),
    "claims_incurred_net_reinsurance": ("claims incurred net of reinsurance",),
    "net_operating_expenses": ("net operating expenses",),
    "technical_account_result": ("balance on the technical account",),
    "investment_income": ("investment income",),
}


def _display_decimal(displayed_value: Any) -> Decimal | None:
    if displayed_value is None:
        return None
    token = str(displayed_value).strip()
    if not token:
        return None
    if re.fullmatch(r"[-\u2013\u2014]+", token):
        return Decimal(0)
    negative = token.startswith("-") or ("(" in token and ")" in token)
    cleaned = re.sub(r"[^0-9.]", "", token)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def _format_display(value: Decimal | None) -> str | None:
    if value is None:
        return None
    magnitude = abs(value)
    rendered = f"{magnitude:,.{max(0, -magnitude.as_tuple().exponent)}f}"
    return f"({rendered})" if value < 0 else rendered


def _normalised_label(value: Any) -> str:
    """Normalise visible labels without retaining formatting-only differences."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()


def insurance_label_is_compatible(metric: Any, source_label: Any) -> bool:
    """Return whether a native insurance metric has an exact visible label family."""
    prefixes = _INSURANCE_LABEL_PREFIXES.get(str(metric or ""))
    if prefixes is None:
        return True
    label = _normalised_label(source_label)
    return any(label.startswith(prefix) for prefix in prefixes)


def insurance_evidence_role(candidate: dict[str, Any]) -> tuple[str, int]:
    """Classify insurance evidence without using SIC or page order.

    Tier 2 is a compatible native row on the primary income statement; tier 4
    is an exact supporting-note fallback. Tier 5 is ineligible evidence.
    """
    if candidate.get("metric") not in INSURANCE_METRICS:
        return "not_insurance", 0
    if not insurance_label_is_compatible(candidate.get("metric"), candidate.get("source_label")):
        return "incompatible_insurance_evidence", 5
    statement_type = candidate.get("statement_type")
    if statement_type == "income_statement":
        return "primary_insurance_income_statement", 2
    if statement_type == "other":
        return "exact_insurance_note", 4
    return "incompatible_insurance_evidence", 5


def canonical_metric_label_is_compatible(metric: Any, source_label: Any) -> bool:
    """Reject component rows that cannot directly represent a canonical total."""
    label = _normalised_label(source_label)
    if metric == "operating_result":
        return any(label.startswith(prefix) for prefix in _OPERATING_RESULT_LABEL_PREFIXES)
    if metric == "profit_after_tax":
        return _BEFORE_TAX_LABEL_PATTERN.search(label) is None
    return True


def canonical_metric_statement_is_compatible(metric: Any, statement_type: Any) -> bool:
    """Require canonical primary rows to occur on the matching statement family."""
    expected = _CANONICAL_PRIMARY_STATEMENTS.get(str(metric or ""))
    if expected is None or statement_type not in PRIMARY_STATEMENT_TYPES:
        return True
    return statement_type in expected


def canonical_evidence_role(candidate: dict[str, Any]) -> tuple[str, int]:
    """Classify a direct canonical row before scope or confidence can rank it."""
    metric = candidate.get("metric")
    statement_type = candidate.get("statement_type")
    if not canonical_metric_label_is_compatible(metric, candidate.get("source_label")):
        return "incompatible_canonical_label", 5
    if not canonical_metric_statement_is_compatible(metric, statement_type):
        return "incompatible_canonical_statement", 5
    expected = _CANONICAL_PRIMARY_STATEMENTS.get(str(metric or ""))
    if expected is not None and statement_type in expected:
        return "primary_statement", 1
    if metric == "employees" and statement_type in PRIMARY_STATEMENT_TYPES:
        return "primary_statement", 1
    return "exact_supporting_note", 4


def _annotate_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    """Attach deterministic role and tier metadata used by later selection."""
    result = dict(candidate)
    metric = result.get("metric")
    statement_type = result.get("statement_type")
    if metric in INSURANCE_METRICS:
        role, tier = insurance_evidence_role(result)
    elif metric == "shareholders_funds":
        role, tier = (
            ("direct_primary_synonym", 3)
            if statement_type == "balance_sheet"
            else ("unclassified", 5)
        )
    elif metric in CANONICAL_METRICS:
        role, tier = canonical_evidence_role(result)
    elif metric == "profit_before_tax":
        role, tier = (
            ("direct_primary_synonym", 3)
            if statement_type == "income_statement"
            else ("exact_supporting_note", 4)
        )
    else:
        role, tier = "unclassified", 5
    result["source_role"] = role
    result["evidence_tier"] = tier
    return result


def _candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, int, float]:
    """Rank deterministic evidence before self-reported model confidence."""
    return (
        -int(candidate.get("evidence_tier") or 5),
        int(candidate.get("unit") != "UNKNOWN"),
        int(candidate.get("current_display") is not None)
        + int(candidate.get("previous_display") is not None),
        float(candidate.get("confidence") or 0),
    )


def _best_candidate(
    candidates: list[dict[str, Any]], metric: str,
    *, predicate: Any = None,
) -> dict[str, Any] | None:
    matching = [
        candidate for candidate in candidates
        if candidate.get("metric") == metric and int(candidate.get("evidence_tier") or 5) < 5
        and (predicate is None or predicate(candidate))
    ]
    return max(matching, key=_candidate_rank) if matching else None


def _source_provenance(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source_pages": [candidate.get("page") for candidate in candidates],
        "source_labels": [candidate.get("source_label") for candidate in candidates],
        "source_statement_types": [candidate.get("statement_type") for candidate in candidates],
        "source_roles": [candidate.get("source_role") for candidate in candidates],
    }


def _reported_equivalent(candidate: dict[str, Any], canonical_metric: str) -> dict[str, Any]:
    source_id = candidate["id"]
    result = dict(candidate)
    result.update({
        "id": f"insurance-{canonical_metric}-{source_id}",
        "metric": canonical_metric,
        **_source_provenance([candidate]),
        "derivation": {
            "policy": "general_insurance" if candidate["metric"] in INSURANCE_METRICS else "financial_summary",
            "kind": "reported_equivalent",
            "formula": candidate["metric"],
            "source_candidate_ids": [source_id],
        },
    })
    return result


def _shareholders_funds_net_assets_equivalent(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Map a standalone Company shareholders'-funds row to net assets."""
    label = _normalised_label(candidate.get("source_label"))
    if (
        candidate.get("metric") != "shareholders_funds"
        or candidate.get("statement_type") != "balance_sheet"
        or candidate.get("statement_scope") != "company"
        or label not in {"shareholders funds", "shareholder funds", "total equity"}
    ):
        return None
    result = dict(candidate)
    result.update({
        "id": f"shareholders-funds-net-assets-{candidate['id']}",
        "metric": "net_assets",
        "source_role": "direct_primary_synonym",
        "evidence_tier": 3,
        **_source_provenance([candidate]),
        "derivation": {
            "policy": "shareholders_funds_equivalent",
            "kind": "reported_equivalent",
            "formula": "shareholders_funds",
            "source_candidate_ids": [candidate["id"]],
        },
    })
    return result


def _gross_profit_equivalents(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build only same-table insurance gross-profit constructions."""
    earned_rows = [
        candidate for candidate in candidates
        if candidate.get("metric") == "net_earned_premiums" and candidate.get("evidence_tier", 5) < 5
    ]
    claims_rows = [
        candidate for candidate in candidates
        if candidate.get("metric") == "claims_incurred_net_reinsurance" and candidate.get("evidence_tier", 5) < 5
    ]
    equivalents: list[dict[str, Any]] = []
    for earned in earned_rows:
        for claims in claims_rows:
            if (
                earned.get("page") != claims.get("page")
                or earned.get("statement_scope") != claims.get("statement_scope")
                or earned.get("unit") != claims.get("unit")
                or earned.get("evidence_tier") != claims.get("evidence_tier")
            ):
                continue
            current = (_display_decimal(earned.get("current_display")), _display_decimal(claims.get("current_display")))
            previous = (_display_decimal(earned.get("previous_display")), _display_decimal(claims.get("previous_display")))
            current_total = sum(current, Decimal(0)) if all(value is not None for value in current) else None
            previous_total = sum(previous, Decimal(0)) if all(value is not None for value in previous) else None
            if current_total is None and previous_total is None:
                continue
            source_candidates = [earned, claims]
            equivalents.append({
                "id": "insurance-gross-profit-" + "-".join(item["id"] for item in source_candidates),
                "metric": "gross_profit",
                "page": earned["page"],
                "statement_type": earned.get("statement_type"),
                "statement_scope": earned.get("statement_scope"),
                "unit": earned["unit"],
                "source_label": "Insurance gross profit equivalent",
                "current_display": _format_display(current_total),
                "previous_display": _format_display(previous_total),
                "current_column": earned.get("current_column"),
                "previous_column": earned.get("previous_column"),
                "evidence_text": "Derived from earned premiums, net of reinsurance, plus signed claims incurred, net of reinsurance.",
                "confidence": min(float(item.get("confidence") or 0) for item in source_candidates),
                "source_role": earned["source_role"],
                "evidence_tier": earned["evidence_tier"],
                **_source_provenance(source_candidates),
                "derivation": {
                    "policy": "general_insurance",
                    "kind": "derived_equivalent",
                    "formula": "net_earned_premiums + claims_incurred_net_reinsurance",
                    "source_candidate_ids": [item["id"] for item in source_candidates],
                },
            })
    return equivalents


def _has_period_value(candidate: dict[str, Any], period: str) -> bool:
    if candidate.get("metric") == "employees":
        return candidate.get(f"{period}_value_count") is not None or candidate.get(f"{period}_display") is not None
    return candidate.get(f"{period}_display") is not None


def _strongest_canonical_evidence(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose only the strongest usable tier for each canonical period."""
    result: list[dict[str, Any]] = []
    for metric in CANONICAL_METRICS:
        matching = [
            candidate for candidate in candidates
            if candidate.get("metric") == metric
            and int(candidate.get("evidence_tier") or 5) < 5
        ]
        best_tier_by_period = {
            period: min(
                (
                    int(candidate.get("evidence_tier") or 5)
                    for candidate in matching
                    if _has_period_value(candidate, period)
                ),
                default=None,
            )
            for period in ("current", "previous")
        }
        for candidate in matching:
            retained = dict(candidate)
            for period, tier in best_tier_by_period.items():
                if tier is not None and int(candidate.get("evidence_tier") or 5) != tier:
                    retained[f"{period}_display"] = None
                    if metric == "employees":
                        retained[f"{period}_value_count"] = None
            if any(_has_period_value(retained, period) for period in ("current", "previous")):
                result.append(retained)
    return result


def add_canonical_equivalents(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return canonical candidates with statement evidence outranking confidence.

    The returned list keeps non-canonical raw rows for audit, but only exposes
    the strongest canonical tier per period to the text rationaliser.
    """
    annotated = [_annotate_evidence(candidate) for candidate in candidates]
    derived: list[dict[str, Any]] = []

    shareholders_funds = _best_candidate(
        annotated,
        "shareholders_funds",
        predicate=lambda candidate: _shareholders_funds_net_assets_equivalent(candidate) is not None,
    )
    if shareholders_funds is not None:
        equivalent = _shareholders_funds_net_assets_equivalent(shareholders_funds)
        if equivalent is not None:
            derived.append(equivalent)

    gross_premiums = _best_candidate(annotated, "gross_premiums_written")
    if gross_premiums is not None:
        derived.append(_reported_equivalent(gross_premiums, "turnover"))

    technical_result = _best_candidate(annotated, "technical_account_result")
    if technical_result is not None:
        derived.append(_reported_equivalent(technical_result, "operating_result"))

    profit_before_tax = _best_candidate(annotated, "profit_before_tax")
    if profit_before_tax is not None:
        derived.append(_reported_equivalent(profit_before_tax, "operating_result"))

    derived.extend(_gross_profit_equivalents(annotated))
    non_canonical = [candidate for candidate in annotated if candidate.get("metric") not in CANONICAL_METRICS]
    return non_canonical + _strongest_canonical_evidence(
        [candidate for candidate in annotated if candidate.get("metric") in CANONICAL_METRICS] + derived
    )


def add_canonical_equivalents_by_statement_scope(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add equivalents without mixing Company and Group statement evidence."""
    result: list[dict[str, Any]] = []
    scopes = ("consolidated_group", "company", "unknown")
    for scope in scopes:
        result.extend(add_canonical_equivalents([
            candidate for candidate in candidates if candidate.get("statement_scope") == scope
        ]))
    result.extend([
        candidate for candidate in candidates if candidate.get("statement_scope") not in scopes
    ])
    return result
