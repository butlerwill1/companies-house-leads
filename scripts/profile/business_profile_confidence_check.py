"""Phase 1c of docs/BUSINESS_PROFILE_CLASSIFIER_IMPROVEMENT_PLAN.md: does the
model's self-reported `confidence` separate right answers from wrong ones?

This is a prerequisite for Phase 3b (confidence-banded filtering) -- if
confidence turns out to be uncorrelated with correctness, banding cannot be
built on it and a different mechanism is needed instead (a coarse three-level
certainty, or sampling agreement).

Pulls the 57 traces already logged for run 169063b5a3f0406d8e6c3322142f4edd
(google/gemini-3.7-flash, whole_document) rather than spending anything new --
`payload` on each trace already carries every field's `confidence`, and the
matching gold case on disk carries the expected value.

Usage:
    python -m scripts.profile.business_profile_confidence_check
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.profile.business_profile_eval import case_files, load_case
from scripts.profile.business_profile_metrics import CONFIDENCE_BANDS, SCORED_FIELDS, score_case

RUN_ID = "169063b5a3f0406d8e6c3322142f4edd"
CASES_DIR = Path("evals/business_profiles/cases")

# This run predates the demand_model sub-value merge documented in
# business_profile_policy.py ("originally separate values... merged into one
# value"): 18 of 57 payloads still carry considered_b2b/tender_framework/
# relationship_repeat, scored here against gold labels that already use the
# merged b2b_relationship. Left unmapped, every one of those 18 scores as a
# high-confidence miss -- not because confidence failed, but because an old
# run is being graded against a taxonomy it predates. Normalize before
# scoring so the check measures confidence calibration, not taxonomy drift.
LEGACY_VALUE_REMAP: dict[str, dict[str, str]] = {
    "demand_model": {
        "considered_b2b": "b2b_relationship",
        "tender_framework": "b2b_relationship",
        "relationship_repeat": "b2b_relationship",
        "wholesale_contract": "b2b_relationship",
    },
}


def _normalize_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return payload
    for field, remap in LEGACY_VALUE_REMAP.items():
        entry = payload.get(field)
        if isinstance(entry, dict) and entry.get("value") in remap:
            entry = {**entry, "value": remap[entry["value"]]}
            payload = {**payload, field: entry}
    return payload

# Confidence is only meaningful for fields that carry it -- sic_agreement
# does not ask for one (see PROMPT_TEMPLATE), so it is excluded here even
# though score_case scores it.
CONFIDENCE_FIELDS = tuple(f for f in SCORED_FIELDS if f != "sic_agreement")

# Shared with business_profile_metrics.confidence_bands (Phase 3b), which
# reports this same breakdown as part of every regular eval run now -- kept
# as one import rather than a second copy so the two never drift apart.
BANDS = CONFIDENCE_BANDS


def _band_label(lo: float, hi: float) -> str:
    return f"[{lo:.2f}, {hi:.2f})" if hi < 1.01 else f"[{lo:.2f}, 1.00]"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import mlflow  # local import: keep the tracking-uri-first rule enforceable

    mlflow.set_tracking_uri("http://127.0.0.1:5000")
    from mlflow import MlflowClient

    client = MlflowClient()
    run = client.get_run(RUN_ID)
    exp_id = run.info.experiment_id

    traces = client.search_traces(
        locations=[exp_id],
        filter_string=f"request_metadata.`mlflow.sourceRun` = '{RUN_ID}'",
        max_results=200,
    )
    print(f"traces linked to run {RUN_ID}: {len(traces)}")

    gold_by_company = {case["company_number"]: case for case in (load_case(p) for p in case_files(CASES_DIR))}

    # (confidence, correct) pairs, pooled across all confidence-bearing fields
    # and also kept per-field, since a field-level mechanism (Phase 3b) needs
    # to know if this holds uniformly or only for some fields.
    pooled: list[tuple[float, bool]] = []
    per_field: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    skipped_no_case = 0
    skipped_no_confidence = 0

    for trace in traces:
        company_number = trace.info.tags.get("eval.company_number")
        case = gold_by_company.get(company_number)
        if case is None:
            skipped_no_case += 1
            continue
        spans = trace.data.spans
        if not spans or not isinstance(spans[0].outputs, dict):
            continue
        payload = _normalize_payload(spans[0].outputs.get("payload"))
        result = score_case(case, payload)
        fields = result["fields"]
        for field in CONFIDENCE_FIELDS:
            entry = fields.get(field) or {}
            confidence = entry.get("confidence")
            # A gold-`unclear` case has no right/wrong answer to correlate
            # confidence against (see search_addressable_metrics for the
            # same exclusion rationale) -- only score confidence where
            # correctness is a meaningful question.
            if entry.get("expected") == "unclear":
                continue
            if confidence is None:
                skipped_no_confidence += 1
                continue
            pair = (float(confidence), bool(entry["correct"]))
            pooled.append(pair)
            per_field[field].append(pair)

    print(f"cases with no matching gold file: {skipped_no_case}")
    print(f"(field, case) pairs with no confidence reported: {skipped_no_confidence}")
    print(f"scored (confidence, correct) pairs, pooled: {len(pooled)}\n")

    def report(label: str, pairs: list[tuple[float, bool]]) -> None:
        if not pairs:
            print(f"{label}: no data")
            return
        print(f"-- {label} (n={len(pairs)}) --")
        for lo, hi in BANDS:
            band = [correct for conf, correct in pairs if lo <= conf < hi]
            if not band:
                print(f"  {_band_label(lo, hi):>14}: n=0")
                continue
            acc = sum(band) / len(band)
            print(f"  {_band_label(lo, hi):>14}: n={len(band):3}  accuracy={acc:.3f}")
        mean_conf_correct = _mean(c for c, ok in pairs if ok)
        mean_conf_wrong = _mean(c for c, ok in pairs if not ok)
        print(
            f"  mean confidence when correct: {mean_conf_correct:.3f}   "
            f"mean confidence when wrong: {mean_conf_wrong:.3f}   "
            f"gap: {mean_conf_correct - mean_conf_wrong:+.3f}"
        )
        print(f"  point-biserial correlation (confidence vs correct): {_point_biserial(pairs):+.3f}\n")

    report("pooled across all fields", pooled)
    for field in CONFIDENCE_FIELDS:
        report(field, per_field[field])

    return 0


def _mean(values: Any) -> float:
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def _point_biserial(pairs: list[tuple[float, bool]]) -> float:
    """Correlation between a continuous variable (confidence) and a binary
    one (correct). Standard point-biserial formula; equivalent to Pearson's r
    with the binary side coded 0/1."""
    n = len(pairs)
    if n < 2:
        return float("nan")
    confidences = [c for c, _ in pairs]
    mean_c = sum(confidences) / n
    var_c = sum((c - mean_c) ** 2 for c in confidences) / n
    std_c = var_c**0.5
    if std_c == 0:
        return float("nan")
    correct_confs = [c for c, ok in pairs if ok]
    wrong_confs = [c for c, ok in pairs if not ok]
    n1, n0 = len(correct_confs), len(wrong_confs)
    if n1 == 0 or n0 == 0:
        return float("nan")
    mean1 = sum(correct_confs) / n1
    mean0 = sum(wrong_confs) / n0
    return ((mean1 - mean0) / std_c) * ((n1 * n0) / (n * n)) ** 0.5


if __name__ == "__main__":
    raise SystemExit(main())
