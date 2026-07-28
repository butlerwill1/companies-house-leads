"""Deterministic mappings from sector-native rows to canonical metrics."""

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

_REPORTED_EQUIVALENTS = {
    "gross_premiums_written": "turnover",
    "technical_account_result": "operating_result",
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


def _best_candidate(
    candidates: list[dict[str, Any]], metric: str
) -> dict[str, Any] | None:
    matching = [candidate for candidate in candidates if candidate.get("metric") == metric]
    if not matching:
        return None
    return max(
        matching,
        key=lambda candidate: (
            int(candidate.get("unit") != "UNKNOWN"),
            int(candidate.get("current_display") is not None)
            + int(candidate.get("previous_display") is not None),
            float(candidate.get("confidence") or 0),
        ),
    )


def _reported_equivalent(
    candidate: dict[str, Any], canonical_metric: str
) -> dict[str, Any]:
    source_id = candidate["id"]
    result = dict(candidate)
    result.update({
        "id": f"insurance-{canonical_metric}-{source_id}",
        "metric": canonical_metric,
        "derivation": {
            "policy": "general_insurance",
            "kind": "reported_equivalent",
            "formula": candidate["metric"],
            "source_candidate_ids": [source_id],
        },
    })
    return result


def _gross_profit_equivalent(
    earned_premiums: dict[str, Any],
    claims_incurred: dict[str, Any],
) -> dict[str, Any] | None:
    if earned_premiums.get("unit") != claims_incurred.get("unit"):
        return None
    current = (
        _display_decimal(earned_premiums.get("current_display")),
        _display_decimal(claims_incurred.get("current_display")),
    )
    previous = (
        _display_decimal(earned_premiums.get("previous_display")),
        _display_decimal(claims_incurred.get("previous_display")),
    )

    def total(values: tuple[Decimal | None, Decimal | None]) -> Decimal | None:
        return sum(values, Decimal(0)) if all(value is not None for value in values) else None

    current_total = total(current)
    previous_total = total(previous)
    if current_total is None and previous_total is None:
        return None
    source_ids = [earned_premiums["id"], claims_incurred["id"]]
    return {
        "id": "insurance-gross-profit-" + "-".join(source_ids),
        "metric": "gross_profit",
        "page": earned_premiums["page"],
        "unit": earned_premiums["unit"],
        "source_label": "Insurance gross profit equivalent",
        "current_display": _format_display(current_total),
        "previous_display": _format_display(previous_total),
        "current_column": earned_premiums.get("current_column"),
        "previous_column": earned_premiums.get("previous_column"),
        "evidence_text": (
            "Derived from earned premiums, net of reinsurance, plus signed "
            "claims incurred, net of reinsurance."
        ),
        "confidence": min(
            float(earned_premiums.get("confidence") or 0),
            float(claims_incurred.get("confidence") or 0),
        ),
        "derivation": {
            "policy": "general_insurance",
            "kind": "derived_equivalent",
            "formula": (
                "net_earned_premiums + claims_incurred_net_reinsurance"
            ),
            "source_candidate_ids": source_ids,
        },
    }


def add_canonical_equivalents(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append canonical insurance equivalents without changing reported rows."""
    result = list(candidates)
    for native_metric, canonical_metric in _REPORTED_EQUIVALENTS.items():
        candidate = _best_candidate(candidates, native_metric)
        if candidate is not None:
            result.append(_reported_equivalent(candidate, canonical_metric))

    earned_premiums = _best_candidate(candidates, "net_earned_premiums")
    claims_incurred = _best_candidate(candidates, "claims_incurred_net_reinsurance")
    if earned_premiums is not None and claims_incurred is not None:
        gross_profit = _gross_profit_equivalent(earned_premiums, claims_incurred)
        if gross_profit is not None:
            result.append(gross_profit)
    return result
