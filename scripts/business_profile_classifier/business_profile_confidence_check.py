"""Phase 1c of docs/BUSINESS_PROFILE_CLASSIFIER_IMPROVEMENT_PLAN.md: does the
model's self-reported `confidence` separate right answers from wrong ones?

This established (pooled point-biserial r=+0.64) that confidence is usable for
the Phase 3b confidence banding. It is kept as a re-runnable check.

It now reads a saved eval report (`logs/business-profile-eval/report-*.json`,
written by `business_profile_eval run`) rather than pulling MLflow traces:
every report's `results` list already carries each field's `confidence` (from
`score_case`) and its correctness, which is all this needs.

Usage:
    python -m scripts.profile.business_profile_confidence_check [--report PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.business_profile_classifier.business_profile_metrics import CONFIDENCE_BANDS, SCORED_FIELDS

REPORT_DIR = Path("logs/business-profile-eval")

# Confidence is only meaningful for fields that carry it -- sic_agreement does
# not ask for one (see PROMPT_TEMPLATE), so it is excluded here.
CONFIDENCE_FIELDS = tuple(f for f in SCORED_FIELDS if f != "sic_agreement")
BANDS = CONFIDENCE_BANDS


def _latest_report() -> Path:
    reports = sorted(REPORT_DIR.glob("report-*.json"))
    if not reports:
        raise SystemExit(f"No reports found in {REPORT_DIR}; run `business_profile_eval run` first.")
    return reports[-1]


def _band_label(lo: float, hi: float) -> str:
    return f"[{lo:.2f}, {hi:.2f})" if hi < 1.01 else f"[{lo:.2f}, 1.00]"


def _mean(values: Any) -> float:
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def _point_biserial(pairs: list[tuple[float, bool]]) -> float:
    """Correlation between a continuous variable (confidence) and a binary one
    (correct). Standard point-biserial; equivalent to Pearson's r with the
    binary side coded 0/1."""
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


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", type=Path, default=None, help="Eval report JSON (default: latest).")
    args = parser.parse_args(argv)

    report_path = args.report or _latest_report()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = report.get("results") or []
    print(f"report: {report_path}  ({report.get('model')}, {len(results)} cases)\n")

    pooled: list[tuple[float, bool]] = []
    per_field: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    skipped_no_confidence = 0

    for result in results:
        fields = result.get("fields") or {}
        for field in CONFIDENCE_FIELDS:
            entry = fields.get(field) or {}
            # A gold-`unclear` case has no right/wrong answer to correlate
            # confidence against.
            if entry.get("expected") in (None, "unclear"):
                continue
            confidence = entry.get("confidence")
            if confidence is None:
                skipped_no_confidence += 1
                continue
            pair = (float(confidence), bool(entry.get("correct")))
            pooled.append(pair)
            per_field[field].append(pair)

    print(f"(field, case) pairs with no confidence reported: {skipped_no_confidence}")
    print(f"scored (confidence, correct) pairs, pooled: {len(pooled)}\n")

    def report_block(label: str, pairs: list[tuple[float, bool]]) -> None:
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

    report_block("pooled across all fields", pooled)
    for field in CONFIDENCE_FIELDS:
        report_block(field, per_field[field])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
