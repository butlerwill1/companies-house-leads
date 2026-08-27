from __future__ import annotations

from scripts.profile.business_profile_metrics import (
    _prf,
    compute_metrics,
    field_metrics,
    score_case,
    search_addressable_metrics,
)


def case(company_number: str, **expected_values: str) -> dict:
    return {
        "company_number": company_number,
        "expected": {field: {"value": value} for field, value in expected_values.items()},
    }


def result(company_number: str, field: str, expected: str, actual: str | None) -> dict:
    return {
        "company_number": company_number,
        "fields": {
            field: {
                "expected": expected,
                "actual": actual,
                "correct": expected == actual,
            }
        },
    }


def test_score_case_marks_a_rejected_response_incorrect_without_inventing_an_answer():
    scored = score_case(case("01", demand_model="local_service"), None)
    entry = scored["fields"]["demand_model"]
    assert entry["expected"] == "local_service"
    assert entry["actual"] is None
    assert entry["correct"] is False


def test_score_case_carries_confidence_through():
    extracted = {"demand_model": {"value": "local_service", "confidence": 0.8}}
    scored = score_case(case("01", demand_model="local_service"), extracted)
    entry = scored["fields"]["demand_model"]
    assert entry["correct"] is True
    assert entry["confidence"] == 0.8


def test_abstention_lowers_coverage_but_is_not_counted_as_a_rejection():
    """The distinction this whole module exists for: a model that answers
    "unclear" made a choice, whereas a rejected response never produced a
    usable answer at all. Collapsing the two hides which problem you have."""
    results = [
        result("01", "demand_model", "local_service", "local_service"),
        result("02", "demand_model", "local_service", "unclear"),
        result("03", "demand_model", "local_service", None),
    ]
    metrics = field_metrics(results, "demand_model")
    assert metrics["scored"] == 3
    assert metrics["abstained"] == 1
    assert metrics["rejected"] == 1
    assert metrics["coverage"] == round(1 / 3, 4)
    # One committed answer, and it was right.
    assert metrics["accuracy_when_committed"] == 1.0
    assert metrics["accuracy"] == round(1 / 3, 4)


def test_majority_baseline_exposes_a_field_that_beats_nothing():
    """A model that always answers the most common class scores well on
    accuracy alone; lift over the baseline is what shows it learned nothing."""
    results = [result(str(i), "sic_agreement", "agrees", "agrees") for i in range(8)]
    results += [result(str(i + 8), "sic_agreement", "disagrees", "agrees") for i in range(2)]
    metrics = field_metrics(results, "sic_agreement")
    assert metrics["accuracy"] == 0.8
    assert metrics["majority_baseline"] == 0.8
    assert metrics["lift_over_baseline"] == 0.0


def test_per_class_precision_and_recall_differ_when_a_class_is_over_predicted():
    """Over-triggering on one class is a precision problem, and accuracy alone
    cannot show it -- this is the observed `mixed` failure in customer_type."""
    results = [
        result("01", "customer_type", "mixed", "mixed"),
        result("02", "customer_type", "b2b", "mixed"),
        result("03", "customer_type", "b2c", "mixed"),
        result("04", "customer_type", "mixed", "mixed"),
    ]
    mixed = field_metrics(results, "customer_type")["per_class"]["mixed"]
    assert mixed["tp"] == 2
    assert mixed["fp"] == 2
    assert mixed["fn"] == 0
    assert mixed["precision"] == 0.5
    assert mixed["recall"] == 1.0


def test_f1_is_the_harmonic_mean_so_one_strong_half_cannot_hide_a_weak_one():
    """Precision 1.0 with recall 0.01 averages to 0.505 arithmetically, which
    would read as a mediocre-but-working classifier. F1 must report ~0.02."""
    metrics = _prf(tp=1, fp=0, fn=99)
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 0.01
    assert metrics["f1"] < 0.03


def test_macro_f1_ignores_classes_that_never_appear_in_the_gold_labels():
    """Averaging in a class nobody ever labelled would drag the score toward
    zero while saying nothing about the model."""
    results = [
        result("01", "geography_served", "national_uk", "national_uk"),
        result("02", "geography_served", "regional", "regional"),
    ]
    metrics = field_metrics(results, "geography_served")
    # Only the two labelled classes count, both perfect.
    assert metrics["macro_f1"] == 1.0
    assert metrics["per_class"]["local"]["support"] == 0


def test_classes_below_min_support_are_named_rather_than_silently_reported():
    results = [result(str(i), "geography_served", "national_uk", "national_uk") for i in range(9)]
    results.append(result("99", "geography_served", "local", "local"))
    metrics = field_metrics(results, "geography_served")
    assert "local" in metrics["classes_below_min_support"]
    assert "national_uk" not in metrics["classes_below_min_support"]


def test_search_addressable_treats_abstention_as_a_miss_not_a_false_alarm():
    """An abstention means the lead never surfaces, so it must cost recall.
    It puts nobody bad in front of a salesperson, so it must not cost
    precision -- that asymmetry is the point of the metric."""
    results = [
        result("01", "demand_model", "local_service", "local_service"),
        result("02", "demand_model", "consumer_search", "unclear"),
        result("03", "demand_model", "b2b_relationship", "b2b_relationship"),
    ]
    metrics = search_addressable_metrics(results)
    assert metrics["tp"] == 1
    assert metrics["fn"] == 1
    assert metrics["fp"] == 0
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 0.5
    assert metrics["missed_by_abstention"] == 1


def test_search_addressable_excludes_cases_with_no_ground_truth():
    """A gold label of "unclear" is an absence of truth, not a negative --
    scoring against it would measure agreement with our own uncertainty."""
    results = [
        result("01", "demand_model", "unclear", "local_service"),
        result("02", "demand_model", "local_service", "local_service"),
    ]
    metrics = search_addressable_metrics(results)
    assert metrics["considered"] == 1
    assert metrics["gold_positives"] == 1
    assert metrics["fp"] == 0


def test_compute_metrics_reports_every_scored_field_and_the_headline():
    results = [
        {
            "company_number": "01",
            "fields": {
                "demand_model": {"expected": "local_service", "actual": "local_service", "correct": True},
                "customer_type": {"expected": "b2c", "actual": "b2b", "correct": False},
            },
        }
    ]
    metrics = compute_metrics(results)
    assert metrics["cases"] == 1
    assert metrics["fields"]["demand_model"]["accuracy"] == 1.0
    assert metrics["fields"]["customer_type"]["accuracy"] == 0.0
    assert metrics["search_addressable"]["tp"] == 1
