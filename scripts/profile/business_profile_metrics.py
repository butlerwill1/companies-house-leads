"""Scoring and metrics for the business-profile stage, shared by the gold-set
harness and the model/context comparison harness.

This exists because accuracy alone was actively misleading about how well the
classifier works. Two failure modes it could not show:

1. **Abstention is not the same as being wrong.** A field where the model says
   "unclear" half the time and is right the rest scores the same as one that
   answers everything and is wrong half the time. Those need completely
   different fixes, so `coverage` is reported separately from accuracy.
2. **Class imbalance flatters accuracy.** On the 57-case gold set 44 of 57
   `sic_agreement` labels are `agrees`, so a model that always answered
   "agrees" scores 77.2%. Reporting 80.7% without that baseline beside it
   implies a working classifier where there is barely any signal. Every field's
   `majority_baseline` is therefore reported alongside its accuracy, and
   per-class precision/recall/F1 expose which specific classes carry the error.

See docs/BUSINESS_PROFILE_HARNESS_REVIEW_2026-08-21.md for the analysis that
motivated each of these.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from scripts.profile.business_profile_policy import (
    DEMAND_MODEL_VALUES,
    FIELD_VALUES,
    SIC_AGREEMENT_VALUES,
)

SCORED_FIELDS: tuple[str, ...] = (*FIELD_VALUES.keys(), "sic_agreement")

FIELD_ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    **FIELD_VALUES,
    "sic_agreement": SIC_AGREEMENT_VALUES,
}

UNCLEAR = "unclear"

# The two demand_model values that mean "this company's customers arrive by
# searching". Everything else is some non-search channel. Collapsing to this
# binary is the question the whole stage exists to answer -- whether paid
# search can reach this company at all -- so it gets reported as its own
# headline metric rather than being buried inside demand_model's 7-way accuracy.
SEARCH_ADDRESSABLE_VALUES = frozenset({"consumer_search", "local_service"})

# A class needs some minimum number of gold examples before its precision or
# recall means anything; below this, one case moves the number by more than the
# differences we are trying to detect. Reported rather than hidden, so an
# unmeasurable class is visibly unmeasurable.
MIN_RELIABLE_SUPPORT = 5


def score_case(
    case: dict[str, Any],
    extracted: dict[str, Any] | None,
    scored_fields: tuple[str, ...] = SCORED_FIELDS,
) -> dict[str, Any]:
    """Compare one case's gold labels against one model response.

    `extracted` is None when the response was rejected outright (bad JSON, a
    failed request, a case-level validation error). A rejected response scores
    every field as incorrect with `actual` None -- distinct from the model
    answering "unclear", which is a real answer the model chose to give.
    """
    expected = case.get("expected") or {}
    fields: dict[str, Any] = {}
    for field in scored_fields:
        expected_value = (expected.get(field) or {}).get("value")
        entry = (extracted or {}).get(field) if extracted else None
        actual_value = entry.get("value") if isinstance(entry, dict) else None
        confidence = entry.get("confidence") if isinstance(entry, dict) else None
        fields[field] = {
            "expected": expected_value,
            "actual": actual_value,
            "correct": expected_value is not None and expected_value == actual_value,
            "confidence": confidence,
        }
    return {"company_number": case.get("company_number"), "fields": fields}


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    """Precision, recall, and their harmonic mean.

    F1 is the harmonic mean rather than the arithmetic one precisely so that a
    class cannot look healthy by being excellent on one and useless on the
    other -- perfect precision with 1% recall averages to 0.5 but scores 0.02
    on F1, which is the honest reading.
    """
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def _pairs(results: list[dict[str, Any]], field: str) -> list[tuple[str, str | None]]:
    """(expected, actual) for every case carrying a gold label for this field.

    Cases with no gold label are dropped -- they cannot be scored either way,
    and counting them would silently deflate every metric.
    """
    out = []
    for result in results:
        outcome = (result.get("fields") or {}).get(field)
        if not outcome:
            continue
        expected = outcome.get("expected")
        if expected is None:
            continue
        out.append((expected, outcome.get("actual")))
    return out


def field_metrics(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """Accuracy, coverage, majority baseline, and per-class precision/recall/F1."""
    pairs = _pairs(results, field)
    if not pairs:
        return {"scored": 0}

    scored = len(pairs)
    correct = sum(1 for expected, actual in pairs if expected == actual)
    committed = sum(1 for _, actual in pairs if actual is not None and actual != UNCLEAR)
    rejected = sum(1 for _, actual in pairs if actual is None)
    abstained = sum(1 for _, actual in pairs if actual == UNCLEAR)

    # Accuracy among the cases where the model actually committed to an answer.
    # Read together with coverage: high value here plus low coverage means the
    # model is trustworthy but silent, which is a different problem from being
    # wrong, and has a different fix.
    committed_correct = sum(
        1 for expected, actual in pairs if actual not in (None, UNCLEAR) and expected == actual
    )

    # The same thing, but only over cases with a real answer to be right or
    # wrong about. A gold label of "unclear" has no correct committed answer
    # by definition -- every one counts against accuracy_when_committed above
    # even for a perfect model, capping it at (answerable / scored) regardless
    # of how good the model is. That makes the plan's Phase 3 go/no-go
    # criterion ("precision holds near 90%") unreachable whenever gold-unclear
    # makes up more than ~10% of a field, which several fields do. This is the
    # number Phase 3d should actually be read against.
    answerable_pairs = [(e, a) for e, a in pairs if e != UNCLEAR]
    answerable = len(answerable_pairs)
    committed_on_answerable = sum(
        1 for _, actual in answerable_pairs if actual not in (None, UNCLEAR)
    )
    committed_correct_on_answerable = sum(
        1 for expected, actual in answerable_pairs if actual not in (None, UNCLEAR) and expected == actual
    )

    gold_counts = Counter(expected for expected, _ in pairs)
    _, majority_n = gold_counts.most_common(1)[0]

    per_class: dict[str, Any] = {}
    for value in FIELD_ALLOWED_VALUES.get(field, tuple(sorted(gold_counts))):
        tp = sum(1 for e, a in pairs if e == value and a == value)
        fp = sum(1 for e, a in pairs if e != value and a == value)
        fn = sum(1 for e, a in pairs if e == value and a != value)
        support = gold_counts.get(value, 0)
        entry = _prf(tp, fp, fn)
        entry["support"] = support
        entry["reliable"] = support >= MIN_RELIABLE_SUPPORT
        per_class[value] = entry

    # Macro-F1 over substantive classes that actually appear in the gold
    # labels. Two exclusions, both deliberate:
    # - Absent classes: averaging in a class nobody ever labelled would drag
    #   the score toward zero for something that says nothing about the model.
    # - "unclear": it is an abstention, not a classification target. Phase 3
    #   deliberately drives coverage up, which drives unclear's own recall (as
    #   a "class") toward zero -- scoring that into macro-F1 would report the
    #   intended effect of Phase 3 as macro-F1 damage. Abstention already has
    #   its own metric (coverage); per-class precision/recall for "unclear" is
    #   still computed above and left in per_class for anyone who wants it.
    present = [v for v, e in per_class.items() if e["support"] > 0 and v != UNCLEAR]
    reliable = [v for v in present if per_class[v]["reliable"]]
    macro_f1 = sum(per_class[v]["f1"] for v in present) / len(present) if present else None
    macro_f1_reliable = (
        sum(per_class[v]["f1"] for v in reliable) / len(reliable) if reliable else None
    )

    return {
        "scored": scored,
        "accuracy": round(correct / scored, 4),
        "majority_baseline": round(majority_n / scored, 4),
        "lift_over_baseline": round(correct / scored - majority_n / scored, 4),
        "coverage": round(committed / scored, 4),
        "accuracy_when_committed": round(committed_correct / committed, 4) if committed else None,
        "answerable": answerable,
        "accuracy_when_committed_on_answerable": (
            round(committed_correct_on_answerable / committed_on_answerable, 4)
            if committed_on_answerable
            else None
        ),
        "abstained": abstained,
        "rejected": rejected,
        "macro_f1": round(macro_f1, 4) if macro_f1 is not None else None,
        "macro_f1_reliable_classes_only": (
            round(macro_f1_reliable, 4) if macro_f1_reliable is not None else None
        ),
        "classes_below_min_support": sorted(v for v in present if not per_class[v]["reliable"]),
        "per_class": per_class,
    }


def search_addressable_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The headline business metric: can paid search reach this company?

    Scoring choices, both deliberate:

    - Cases whose *gold* demand_model is "unclear" are excluded. There is no
      ground truth to score against, so including them would measure agreement
      with our own uncertainty rather than with reality.
    - A *predicted* "unclear" counts as a miss for recall but is not a false
      positive for precision. That matches how the output is actually used: an
      abstention means the lead never surfaces (a real miss), but it does not
      put a bad company in front of anyone (not a false alarm).
    """
    tp = fp = fn = 0
    abstained_on_positive = 0
    considered = 0

    for result in results:
        outcome = (result.get("fields") or {}).get("demand_model")
        if not outcome:
            continue
        expected, actual = outcome.get("expected"), outcome.get("actual")
        if expected is None or expected == UNCLEAR:
            continue
        considered += 1
        gold_positive = expected in SEARCH_ADDRESSABLE_VALUES
        predicted_positive = actual in SEARCH_ADDRESSABLE_VALUES

        if gold_positive and predicted_positive:
            tp += 1
        elif not gold_positive and predicted_positive:
            fp += 1
        elif gold_positive and not predicted_positive:
            fn += 1
            if actual in (None, UNCLEAR):
                abstained_on_positive += 1

    metrics = _prf(tp, fp, fn)
    metrics.update(
        {
            "considered": considered,
            "gold_positives": tp + fn,
            "missed_by_abstention": abstained_on_positive,
        }
    )
    return metrics


def compute_metrics(
    results: list[dict[str, Any]], scored_fields: tuple[str, ...] = SCORED_FIELDS
) -> dict[str, Any]:
    """Full metric suite for a run: per field, plus the search-addressable headline."""
    per_field = {field: field_metrics(results, field) for field in scored_fields}
    accuracies = [m["accuracy"] for m in per_field.values() if m.get("accuracy") is not None]
    return {
        "cases": len(results),
        "mean_field_accuracy": round(sum(accuracies) / len(accuracies), 4) if accuracies else None,
        "search_addressable": search_addressable_metrics(results),
        "fields": per_field,
    }


def flatten_for_mlflow(metrics: dict[str, Any]) -> dict[str, float]:
    """The subset worth logging as MLflow metrics (scalars only, stable names).

    Per-class precision/recall stays out of this deliberately: it is a table to
    read in the report, not a time series worth charting, and logging ~40 extra
    scalars per run makes the MLflow comparison view unusable.
    """
    flat: dict[str, float] = {}
    if metrics.get("mean_field_accuracy") is not None:
        flat["mean_field_accuracy"] = metrics["mean_field_accuracy"]

    search = metrics.get("search_addressable") or {}
    for key in ("precision", "recall", "f1"):
        if search.get(key) is not None:
            flat[f"search_addressable_{key}"] = search[key]

    for field, m in (metrics.get("fields") or {}).items():
        for key in (
            "accuracy",
            "coverage",
            "macro_f1",
            "lift_over_baseline",
            "accuracy_when_committed_on_answerable",
        ):
            if m.get(key) is not None:
                flat[f"{key}_{field}"] = m[key]
    return flat
