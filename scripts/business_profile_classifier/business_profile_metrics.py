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

from scripts.business_profile_classifier.business_profile_policy import (
    DEMAND_MODEL_VALUES,
    FIELD_VALUES,
    RETIRED_VALUES,
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

# Deliverables that individuals find for themselves. Paired with customer_type
# b2c, these say "paid search can reach this company" on the strength of WHAT
# the business is, when demand_model failed to say HOW its customers arrive.
#
# This exists because the gold-`unclear` exclusion in
# search_addressable_metrics was silently deleting real leads. 07538544 BIRD
# OVERSEAS is the case that found it: a hotel group whose filing never once
# says how guests arrive -- no "platform", "booking", "online", "website" or
# "agent" anywhere in 54,711 characters -- so `unclear` is the honest
# demand_model. Under the old rule it then dropped out of the headline number
# entirely, which is worse than a wrong label: a wrong label at least surfaces
# the company for someone to correct.
#
# Excluded on purpose: property and lending (demand often arrives through
# brokers and portals), product_digital (storefronts and streaming),
# contracting (tendered), rental_leasing, and unclear.
CATEGORY_FLOOR_DELIVERY_MODELS = frozenset(
    {"hospitality", "leisure_venue", "professional_service", "product_physical", "trade_service"}
)

# Customer types the floor accepts: any customer base with a significant
# consumer share. `mixed` was added on 2026-09-28. Under b2c-only, 8 of V12's
# 18 missed leads were demand `unclear` + a floor delivery model + `mixed`
# (VW HERITAGE, LYONS, ST JOHNSTONE, NORTHAMPTON, HALLS, PILL BOX, CPJ FIELD,
# BIRD OVERSEAS), and in four of them the gold was `mixed` too -- the model
# was right and the rule shut the lead out. The b2c/mixed boundary is also
# the least stable answer across runs (28 of 124 customer_type answers moved
# between V10 and V12 with the same customer_type instructions), so a rule
# that hinged on it made recall depend on noise. b2b, public_sector and
# unclear stay outside: nothing says an individual is searching.
CATEGORY_FLOOR_CUSTOMER_TYPES = frozenset({"b2c", "mixed"})

# This is deliberately an experimental *downstream* rule.  It answers a
# broader commercial question than `is_search_addressable`: could a material
# external line be independently discoverable through search, irrespective of
# the channel through which the current business happens to arrive?  It reads
# the existing four extracted fields; it is not another instruction or output
# field for the model.
#
# The broad delivery set is used to draft human-review labels, not to change
# production lead selection.  In particular, a B2B professional service may
# still be won through a framework or relationship, so the review sheet must
# decide whether that case represents a meaningful search opportunity.
EXPERIMENTAL_SEARCH_OPPORTUNITY_DELIVERY_MODELS = CATEGORY_FLOOR_DELIVERY_MODELS
EXTERNAL_CUSTOMER_TYPES = frozenset({"b2c", "b2b", "mixed", "public_sector"})
SEARCH_OPPORTUNITY_VALUES = frozenset({"yes", "no", UNCLEAR})

# A class needs some minimum number of gold examples before its precision or
# recall means anything; below this, one case moves the number by more than the
# differences we are trying to detect. Reported rather than hidden, so an
# unmeasurable class is visibly unmeasurable.
MIN_RELIABLE_SUPPORT = 5

# Boundaries for confidence_bands, below. Matches the bands used in the
# Phase 1c correlation check (scripts/business_profile_classifier/business_profile_confidence_check.py)
# so a report produced here and that one-off analysis read the same way.
CONFIDENCE_BANDS: tuple[tuple[float, float], ...] = ((0.9, 1.01), (0.75, 0.9), (0.5, 0.75), (0.0, 0.5))

# The fields the model actually reports a confidence for -- sic_agreement's
# schema has no confidence field at all (see PROMPT_TEMPLATE: its object is
# {"value": ..., "reason": ...}, nothing else), so banding it would only
# ever report 100% "no confidence given" and say nothing real.
CONFIDENCE_BEARING_FIELDS = frozenset(FIELD_VALUES.keys())


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
        # A response saved under an older prompt may answer with a value the
        # taxonomy has since retired; score it as what that value became.
        actual_value = RETIRED_VALUES.get(field, {}).get(actual_value, actual_value)
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
        "confidence_bands": confidence_bands(results, field),
    }


def confidence_bands(results: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    """Accuracy by self-reported confidence -- Phase 3b's "carry it through
    scoring": confidence was requested, returned, and scored per-case since
    score_case, but nothing downstream ever looked at it. Phase 1c already
    established that confidence separates correct from incorrect (pooled
    point-biserial r=+0.64); this is what turns that into something a
    report actually shows, and what a downstream confidence threshold would
    be chosen from.

    Restricted to the same population as accuracy_when_committed_on_answerable
    -- committed answers on cases with a real answer to be right or wrong
    about. A gold-`unclear` case has no correct committed answer to band by
    confidence against, and an abstention has no confidence-vs-correctness
    question to ask in the first place.

    Returns None for a field with no confidence in its schema at all
    (sic_agreement) rather than a table of empty bands that would just say
    "no confidence given" for every case.
    """
    if field not in CONFIDENCE_BEARING_FIELDS:
        return None

    pairs: list[tuple[float, bool]] = []
    missing_confidence = 0
    for result in results:
        outcome = (result.get("fields") or {}).get(field)
        if not outcome:
            continue
        expected, actual = outcome.get("expected"), outcome.get("actual")
        if expected is None or expected == UNCLEAR or actual in (None, UNCLEAR):
            continue
        confidence = outcome.get("confidence")
        if confidence is None:
            missing_confidence += 1
            continue
        pairs.append((float(confidence), expected == actual))

    bands = []
    for lo, hi in CONFIDENCE_BANDS:
        in_band = [correct for conf, correct in pairs if lo <= conf < hi]
        bands.append(
            {
                "range": [lo, min(hi, 1.0)],
                "support": len(in_band),
                "accuracy": round(sum(in_band) / len(in_band), 4) if in_band else None,
            }
        )
    return {"bands": bands, "missing_confidence": missing_confidence}


def is_search_addressable(
    demand: str | None, delivery: str | None, customer: str | None
) -> bool:
    """Can paid search reach this company? The rule, in one place.

    demand_model answers it directly when it committed to an answer. When it
    said "unclear" -- or was never produced -- the category floor answers it
    from what the business IS: a hotel, salon, clinic, shop or jobbing trade
    with a b2c or mixed customer base is reachable whether or not its filing
    described the channel.

    Rescue only, never override. A demand_model that gave a definite
    non-search answer is respected, which is what the final `return False`
    enforces. Overriding would promote 13043443 VENTRESS (hotel,
    platform_intermediated) correctly, but at the cost of 10713956 NINJA TUNE,
    whose demand genuinely does arrive through streaming platforms, and two
    care providers whose work arrives through council commissioning. A wrong
    label is a labelling problem; routing around it here would only hide it.

    Anything importing this -- lead selection, when it is built -- gets the
    same rule rather than a second copy of it.
    """
    if demand in SEARCH_ADDRESSABLE_VALUES:
        return True
    if demand in (None, UNCLEAR):
        return delivery in CATEGORY_FLOOR_DELIVERY_MODELS and customer in CATEGORY_FLOOR_CUSTOMER_TYPES
    return False


def search_opportunity_from_profile(
    demand: str | None,
    delivery: str | None,
    customer: str | None,
    trading_status: str | None,
) -> str:
    """Experimental search-opportunity assessment from an existing profile.

    ``yes`` means the profile describes an external operating business whose
    type is commonly independently discoverable.  ``no`` protects captive,
    holding and explicitly non-customer-facing activity.  ``unclear`` keeps
    missing customer, delivery or trading evidence visible for review.

    This must remain separate from ``is_search_addressable`` until the review
    snapshot is approved.  The latter is the historical metric and production
    rule; changing it would make its historical series incomparable.
    """
    if trading_status in {"spv", "investment_holding"} or demand == "not_customer_facing":
        return "no"
    if demand in SEARCH_ADDRESSABLE_VALUES:
        return "yes"
    if customer in EXTERNAL_CUSTOMER_TYPES and delivery in EXPERIMENTAL_SEARCH_OPPORTUNITY_DELIVERY_MODELS:
        return "yes"
    if trading_status in (None, UNCLEAR) or customer in (None, UNCLEAR) or delivery in (None, UNCLEAR):
        return UNCLEAR
    return "no"


def _field_pair(result: dict[str, Any], field: str) -> tuple[Any, Any]:
    """(expected, actual) for one field, tolerating its absence entirely.

    score_case writes all six scored fields unconditionally, but the unit-test
    fixtures build single-field results and the context-A/B harness rewrites
    tainted fields with a narrower dict, so nothing here may index."""
    outcome = (result.get("fields") or {}).get(field) or {}
    return outcome.get("expected"), outcome.get("actual")


def search_addressable_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The headline business metric: can paid search reach this company?

    Scoring choices, all deliberate:

    - A case is excluded only when its gold answer is genuinely unknowable:
      no gold demand_model at all, or a gold "unclear" that the category floor
      cannot resolve either. A gold "unclear" the floor DOES resolve has a
      ground truth (positive) and is scored -- that is the whole point of the
      floor, and it is why `considered` is higher than it was before it
      existed.
    - A *predicted* "unclear" counts as a miss for recall but is not a false
      positive for precision. That matches how the output is actually used: an
      abstention means the lead never surfaces (a real miss), but it does not
      put a bad company in front of anyone (not a false alarm). A predicted
      "unclear" the floor rescues is not an abstention at all -- the lead does
      surface.
    - Both sides go through is_search_addressable, gold from the expected
      values and predicted from the actual ones, so the floor cannot flatter
      the model by applying to only one of them.
    """
    tp = fp = fn = 0
    abstained_on_positive = 0
    considered = 0
    floor_rescued_gold = floor_rescued_predicted = 0

    for result in results:
        expected, actual = _field_pair(result, "demand_model")
        if expected is None:
            continue
        gold_delivery, actual_delivery = _field_pair(result, "delivery_model")
        gold_customer, actual_customer = _field_pair(result, "customer_type")

        gold_positive = is_search_addressable(expected, gold_delivery, gold_customer)
        if expected == UNCLEAR:
            if not gold_positive:
                # No committed answer and no category to fall back on: there is
                # nothing to score this case against.
                continue
            floor_rescued_gold += 1

        considered += 1
        predicted_positive = is_search_addressable(actual, actual_delivery, actual_customer)
        if predicted_positive and actual in (None, UNCLEAR):
            floor_rescued_predicted += 1

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
            "floor_rescued_gold": floor_rescued_gold,
            "floor_rescued_predicted": floor_rescued_predicted,
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


def flatten_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    """The subset worth logging as run-level scores (scalars only, stable names).

    Per-class precision/recall stays out of this deliberately: it is a table to
    read in the report, not a time series worth charting, and logging ~40 extra
    scalars per run makes the run comparison view unusable.
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
