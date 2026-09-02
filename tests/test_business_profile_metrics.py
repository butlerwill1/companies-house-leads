from __future__ import annotations

from pathlib import Path

from scripts.profile.business_profile_eval import case_files, load_case
from scripts.profile.business_profile_metrics import (
    FIELD_ALLOWED_VALUES,
    _prf,
    compute_metrics,
    confidence_bands,
    field_metrics,
    score_case,
    search_addressable_metrics,
)

CASES_DIR = Path("evals/business_profiles/cases")


def test_every_allowed_value_has_a_definition_for_the_prompt():
    """A value added to a taxonomy without a definition would either crash
    prompt building or, worse, silently reach the model as a bare enum name --
    which is the exact condition that left demand_model performing at its
    majority-class baseline."""
    from scripts.profile.business_profile_policy import FIELD_DEFINITIONS, format_field_options

    for field, allowed in FIELD_ALLOWED_VALUES.items():
        definitions = FIELD_DEFINITIONS[field]
        missing = [value for value in allowed if value not in definitions]
        assert not missing, f"{field} values without a definition: {missing}"
        # Must render without raising, and mention every value.
        rendered = format_field_options(field)
        for value in allowed:
            assert value in rendered


def test_every_gold_label_is_a_value_the_taxonomy_still_allows():
    """Pruning or merging a taxonomy value orphans any gold case still using
    it: the case can never be scored correct again, and nothing else would
    complain. This is the guard that made the saas and wholesale_contract
    merges safe to make."""
    orphaned = []
    for path in case_files(CASES_DIR):
        case = load_case(path)
        expected = case.get("expected") or {}
        for field, allowed in FIELD_ALLOWED_VALUES.items():
            value = (expected.get(field) or {}).get("value")
            if value is not None and value not in allowed:
                orphaned.append(f"{path.name}: {field}={value}")
    assert not orphaned, f"gold labels outside the allowed taxonomy: {orphaned}"


def case(company_number: str, **expected_values: str) -> dict:
    return {
        "company_number": company_number,
        "expected": {field: {"value": value} for field, value in expected_values.items()},
    }


def result(company_number: str, field: str, expected: str, actual: str | None, confidence: float | None = None) -> dict:
    return {
        "company_number": company_number,
        "fields": {
            field: {
                "expected": expected,
                "actual": actual,
                "correct": expected == actual,
                "confidence": confidence,
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


def test_accuracy_when_committed_on_answerable_ignores_gold_unclear_cases():
    """accuracy_when_committed penalizes every commitment against a gold
    label of "unclear", even though there is no correct committed answer to
    have given -- a perfect model still loses points there, capping the
    metric below 100% regardless of how good the model is. That is the
    number Phase 3's go/no-go check ("precision holds near 90%") was written
    against, and on a field where >10% of gold labels are "unclear" the
    criterion is unreachable by construction.
    accuracy_when_committed_on_answerable is the fix: it only scores
    commitments against cases that have a real right answer."""
    results = [
        result("01", "demand_model", "local_service", "local_service"),  # answerable, correct
        result("02", "demand_model", "unclear", "local_service"),  # gold unclear, model committed
    ]
    metrics = field_metrics(results, "demand_model")
    assert metrics["answerable"] == 1
    # Old metric: penalized by the gold-unclear case the model had no way to
    # get "right" by committing.
    assert metrics["accuracy_when_committed"] == 0.5
    # New metric: perfect, because the one answerable case was answered
    # correctly -- unaffected by what the model did on the unanswerable one.
    assert metrics["accuracy_when_committed_on_answerable"] == 1.0


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


def test_macro_f1_excludes_unclear_even_when_it_has_support():
    """"unclear" is an abstention, not a classification target -- Phase 3
    deliberately drives its recall toward zero by design, and macro_f1
    scoring that as class damage would report the intended effect of the
    prompt change as a regression. Coverage is where abstention belongs;
    macro_f1 should reflect only the substantive classes."""
    results = [
        result("01", "geography_served", "national_uk", "national_uk"),
        result("02", "geography_served", "regional", "regional"),
        # Gold says unclear, model guessed wrong (predicting a class that
        # appears nowhere else here, so it doesn't also cost national_uk or
        # regional precision -- that would be real signal, not the artifact
        # this test targets). "unclear" the class scores f1=0 for this case;
        # that must not drag macro_f1 down.
        result("03", "geography_served", "unclear", "international"),
    ]
    metrics = field_metrics(results, "geography_served")
    assert metrics["macro_f1"] == 1.0
    assert metrics["per_class"]["unclear"]["support"] == 1
    assert metrics["per_class"]["unclear"]["f1"] == 0.0
    assert "unclear" not in metrics["classes_below_min_support"]


def test_classes_below_min_support_are_named_rather_than_silently_reported():
    results = [result(str(i), "geography_served", "national_uk", "national_uk") for i in range(9)]
    results.append(result("99", "geography_served", "local", "local"))
    metrics = field_metrics(results, "geography_served")
    assert "local" in metrics["classes_below_min_support"]
    assert "national_uk" not in metrics["classes_below_min_support"]


def test_confidence_bands_reports_accuracy_per_band():
    """Phase 3b's payoff: confidence was captured per-case since score_case
    but nothing ever turned it into a number a report shows. This is the
    curve a downstream confidence threshold would actually be chosen from."""
    results = [
        result("01", "demand_model", "local_service", "local_service", confidence=0.95),
        result("02", "demand_model", "local_service", "local_service", confidence=0.92),
        result("03", "demand_model", "consumer_search", "local_service", confidence=0.6),
    ]
    bands = confidence_bands(results, "demand_model")
    high = next(b for b in bands["bands"] if b["range"] == [0.9, 1.0])
    mid = next(b for b in bands["bands"] if b["range"] == [0.5, 0.75])
    assert high["support"] == 2
    assert high["accuracy"] == 1.0
    assert mid["support"] == 1
    assert mid["accuracy"] == 0.0


def test_confidence_bands_excludes_abstentions_and_gold_unclear():
    """Neither has a confidence-vs-correctness question to ask: an
    abstention made no claim to be confident about, and a gold-`unclear`
    case has no correct committed answer to band by confidence against --
    the same population as accuracy_when_committed_on_answerable."""
    results = [
        # Model abstained -- no commitment to band by confidence.
        result("01", "demand_model", "local_service", "unclear", confidence=0.9),
        # Gold says unclear -- no right answer to be confidently right about.
        result("02", "demand_model", "unclear", "local_service", confidence=0.9),
        # The one real, scoreable data point.
        result("03", "demand_model", "local_service", "local_service", confidence=0.95),
    ]
    bands = confidence_bands(results, "demand_model")
    total_support = sum(b["support"] for b in bands["bands"])
    assert total_support == 1


def test_confidence_bands_counts_missing_confidence_separately_rather_than_dropping_it():
    results = [result("01", "demand_model", "local_service", "local_service", confidence=None)]
    bands = confidence_bands(results, "demand_model")
    assert bands["missing_confidence"] == 1
    assert sum(b["support"] for b in bands["bands"]) == 0


def test_confidence_bands_is_none_for_a_field_with_no_confidence_in_its_schema():
    """sic_agreement's response shape is {value, reason} -- no confidence at
    all (see PROMPT_TEMPLATE). Banding it would only ever report "no
    confidence given" for every case, which says nothing real."""
    results = [result("01", "sic_agreement", "agrees", "agrees")]
    assert confidence_bands(results, "sic_agreement") is None


def test_field_metrics_carries_confidence_bands_through():
    results = [result("01", "demand_model", "local_service", "local_service", confidence=0.95)]
    metrics = field_metrics(results, "demand_model")
    assert metrics["confidence_bands"] is not None
    assert metrics["confidence_bands"]["bands"]


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
