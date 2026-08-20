"""One-off A/B harness: narrative-sections vs whole-filed-document context,
across a small model shortlist, on a fixed sample of the gold set. Logs one
MLflow run per (model, context) combination in the same experiment the
regular eval harness uses, so results sit alongside it rather than in a
separate, easy-to-lose place.

Not wired into main() as a subcommand -- this is a specific comparison run,
not a piece of the standing pipeline. Run directly:

    python -m scripts.profile.business_profile_context_ab
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from core.companies_house_extractor import load_dotenv
from scripts.profile.business_profile_eval import SCORED_FIELDS, case_files, load_case
from scripts.profile.business_profile_policy import (
    FIELD_VALUES,
    PROMPT_TEMPLATE,
    PROMPT_VERSION,
    SIC_AGREEMENT_VALUES,
    TRADING_STATUS_VALUES,
    build_prompt,
    parse_json_response,
    validate_response,
)

CASES_DIR = Path("evals/business_profiles/cases")
RAW_DIR = Path("data/raw/business-profile-xhtml")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


def validate_whole_document_response(payload: dict[str, Any], whole_text: str) -> list[str]:
    """validate_response() rejects a quote whose cited `section` name isn't
    a key in the sections dict it was given -- correct when the model was
    shown several named sections, but wrong here: whole-document mode shows
    one blob of text with the filing's own internal headings still visible
    in it ("Strategic report", "Notes to the financial statements", ...),
    and the model naturally cites those instead of the synthetic wrapper
    label ("filed_report") it was never told to use. The section name isn't
    meaningful when there is only one document and no pre-defined boundary
    on our side -- so this keeps the actual hallucination check (the quote
    must be a genuine verbatim substring of the filing) and drops the
    section-name match entirely, rather than relaxing the check that
    matters."""
    errors: list[str] = []
    description = payload.get("business_description")
    if not isinstance(description, str) or not description.strip():
        errors.append("business_description is missing or empty")
    for field, allowed in FIELD_VALUES.items():
        entry = payload.get(field)
        if not isinstance(entry, dict):
            errors.append(f"{field} is missing or not an object")
            continue
        value = entry.get("value")
        if value not in allowed:
            errors.append(f"{field}.value {value!r} is not one of {allowed}")
            continue
        if value == "unclear":
            continue
        quote = entry.get("quote") or ""
        if not quote:
            errors.append(f"{field} has value {value!r} but no supporting quote")
        elif quote not in whole_text:
            errors.append(f"{field}.quote does not appear verbatim in the filed document: {quote!r}")
    sic = payload.get("sic_agreement")
    if not isinstance(sic, dict) or sic.get("value") not in SIC_AGREEMENT_VALUES:
        errors.append(f"sic_agreement.value must be one of {SIC_AGREEMENT_VALUES}")
    return errors

# Every model here is a deliberate, distinct question, not just "try a few":
#   gemini-2.5-flash    -- the configured incumbent (evals/business_profiles/configs/openrouter-gemini.yaml)
#   gemini-3.7-flash    -- newer AND cheaper on completion tokens than the incumbent; is there a reason not to switch?
#   gemini-2.5-flash-lite -- does a much cheaper tier still pass the verbatim-quote check on this task?
#   anthropic/claude-opus-5 -- frontier ceiling: what does the incumbent's accuracy cost, in accuracy?
MODELS = [
    "google/gemini-2.5-flash",
    "google/gemini-3.7-flash",
    "google/gemini-2.5-flash-lite",
    "anthropic/claude-opus-5",
]
CONTEXTS = ["narrative", "whole_document"]

# Every 3rd case, sorted -- deterministic, spans both the original 47 and the
# 10 added this session, and covers a spread of SIC groups without hand-picking
# favourable ones.
SAMPLE_STRIDE = 3


def sample_cases() -> list[dict[str, Any]]:
    paths = case_files(CASES_DIR)
    return [load_case(p) for p in paths[::SAMPLE_STRIDE]]


def whole_document_prompt(case: dict[str, Any]) -> str | None:
    md_path = RAW_DIR / f"{case['company_number']}.md"
    if not md_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    sections_block = f"[filed_report]\n{text}"
    return PROMPT_TEMPLATE.format(
        company_name=case.get("company_name") or "(unknown)",
        sections_block=sections_block,
        demand_model_values=", ".join(FIELD_VALUES["demand_model"]),
        customer_type_values=", ".join(FIELD_VALUES["customer_type"]),
        delivery_model_values=", ".join(FIELD_VALUES["delivery_model"]),
        geography_served_values=", ".join(FIELD_VALUES["geography_served"]),
        trading_status_values=", ".join(TRADING_STATUS_VALUES),
        sic_agreement_values=", ".join(SIC_AGREEMENT_VALUES),
        sic_label=case.get("sic_label") or "(none declared)",
        sic_code=case.get("sic_code") or "(none)",
    )


def call_model(api_key: str, model: str, prompt: str, timeout: int) -> tuple[str, dict[str, Any]]:
    """Returns (content, usage). A direct call, not BusinessProfileModelClient
    -- token usage is needed for real cost, which the production client
    doesn't return, and shouldn't grow a second responsibility just for
    this one-off comparison."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        OPENROUTER_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error") is not None:
        payload.pop("response_format", None)
        response = requests.post(
            OPENROUTER_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    if body.get("error") is not None:
        raise RuntimeError(str(body["error"]))
    return body["choices"][0]["message"]["content"], body.get("usage") or {}


def score(case: dict[str, Any], extracted: dict[str, Any] | None) -> dict[str, Any]:
    expected = case["expected"]
    fields: dict[str, Any] = {}
    for field in SCORED_FIELDS:
        expected_value = (expected.get(field) or {}).get("value")
        actual_value = (extracted or {}).get(field, {}).get("value") if extracted else None
        fields[field] = {
            "expected": expected_value,
            "actual": actual_value,
            "correct": expected_value is not None and expected_value == actual_value,
        }
    return fields


def run_combination(
    api_key: str, model: str, context: str, cases: list[dict[str, Any]], timeout: int = 120
) -> dict[str, Any]:
    results = []
    rejections = 0
    unclear_count = 0
    total_fields = 0
    prompt_tokens = 0
    completion_tokens = 0
    start = time.monotonic()

    for case in cases:
        whole_text = None
        if context == "narrative":
            prompt = build_prompt(
                company_name=case["company_name"],
                sections=case["sections"],
                sic_label=case["sic_label"],
                sic_code=case["sic_code"],
            )
        else:
            prompt = whole_document_prompt(case)
            if prompt is None:
                results.append({"company_number": case["company_number"], "fields": score(case, None)})
                rejections += 1
                continue
            whole_text = (RAW_DIR / f"{case['company_number']}.md").read_text(encoding="utf-8")

        try:
            raw, usage = call_model(api_key, model, prompt, timeout)
        except Exception as exc:  # noqa: BLE001 -- record and continue, one bad call shouldn't sink the run
            print(f"    ERROR {case['company_number']}: {exc}")
            results.append({"company_number": case["company_number"], "fields": score(case, None)})
            rejections += 1
            continue

        prompt_tokens += usage.get("prompt_tokens") or 0
        completion_tokens += usage.get("completion_tokens") or 0

        try:
            payload = parse_json_response(raw)
        except (ValueError, TypeError) as exc:
            print(f"    REJECTED {case['company_number']}: not valid JSON: {exc}")
            results.append({"company_number": case["company_number"], "fields": score(case, None)})
            rejections += 1
            continue

        errors = (
            validate_response(payload, case["sections"])
            if whole_text is None
            else validate_whole_document_response(payload, whole_text)
        )
        if errors:
            print(f"    REJECTED {case['company_number']}: {errors[0]}")
            results.append({"company_number": case["company_number"], "fields": score(case, None)})
            rejections += 1
            continue

        for field in FIELD_VALUES:
            total_fields += 1
            if payload.get(field, {}).get("value") == "unclear":
                unclear_count += 1
        results.append({"company_number": case["company_number"], "fields": score(case, payload)})

    elapsed = time.monotonic() - start

    field_accuracy: dict[str, dict[str, int]] = {f: {"correct": 0, "scored": 0} for f in SCORED_FIELDS}
    for r in results:
        for field, outcome in r["fields"].items():
            if outcome["expected"] is not None:
                field_accuracy[field]["scored"] += 1
                if outcome["correct"]:
                    field_accuracy[field]["correct"] += 1

    return {
        "model": model,
        "context": context,
        "cases": len(cases),
        "rejections": rejections,
        "quote_verification_pass_rate": round(1 - rejections / len(cases), 4) if cases else None,
        "unclear_rate": round(unclear_count / total_fields, 4) if total_fields else None,
        "elapsed_seconds": round(elapsed, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "field_accuracy": {
            f: round(v["correct"] / v["scored"], 4) if v["scored"] else None for f, v in field_accuracy.items()
        },
        "field_scored_counts": {f: v["scored"] for f, v in field_accuracy.items()},
        "results": results,
    }


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int, prices: dict[str, dict[str, float]]) -> float | None:
    p = prices.get(model)
    if p is None:
        return None
    return prompt_tokens * p["prompt"] + completion_tokens * p["completion"]


def fetch_prices(models: list[str]) -> dict[str, dict[str, float]]:
    r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    r.raise_for_status()
    by_id = {m["id"]: m["pricing"] for m in r.json()["data"]}
    return {
        model: {"prompt": float(by_id[model]["prompt"]), "completion": float(by_id[model]["completion"])}
        for model in models
        if model in by_id
    }


def main() -> int:
    # MLflow prints an emoji banner ("View run ... at: ...") when a run
    # ends. Windows' default console codepage (cp1252) can't encode it,
    # which crashes the run *after* every mlflow.log_* call for that
    # combination has already succeeded -- only the banner print fails.
    # Reconfiguring stdout to UTF-8 (with a safe fallback for anything
    # else unencodable) fixes the actual bug rather than routing around
    # it by suppressing MLflow's own output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(".env"))
    api_key = os.environ["OPENROUTER_API_KEY"]

    import mlflow

    mlflow.set_tracking_uri("http://127.0.0.1:5000")
    mlflow.set_experiment("companies-house-business-profile-eval")

    contexts = sys.argv[1:] or CONTEXTS  # e.g. `... whole_document` to rerun just one context

    cases = sample_cases()
    prices = fetch_prices(MODELS)
    print(f"Sample: {len(cases)} cases, {len(MODELS)} models x {len(contexts)} contexts = "
          f"{len(cases) * len(MODELS) * len(contexts)} calls\n")

    all_reports = []
    total_cost = 0.0
    for model in MODELS:
        for context in contexts:
            print(f"=== {model} / {context} ===")
            report = run_combination(api_key, model, context, cases)
            cost = cost_usd(model, report["prompt_tokens"], report["completion_tokens"], prices)
            report["estimated_cost_usd"] = round(cost, 4) if cost is not None else None
            if cost is not None:
                total_cost += cost
            all_reports.append(report)

            with mlflow.start_run(run_name=f"context-ab-{model.split('/')[-1]}-{context}"):
                mlflow.log_params({
                    "model": model,
                    "context": context,
                    "prompt_version": PROMPT_VERSION,
                    "cases": report["cases"],
                    "sample_stride": SAMPLE_STRIDE,
                })
                mlflow.log_metric("quote_verification_pass_rate", report["quote_verification_pass_rate"] or 0)
                if report["unclear_rate"] is not None:
                    mlflow.log_metric("unclear_rate", report["unclear_rate"])
                mlflow.log_metric("elapsed_seconds", report["elapsed_seconds"])
                mlflow.log_metric("prompt_tokens", report["prompt_tokens"])
                mlflow.log_metric("completion_tokens", report["completion_tokens"])
                if report["estimated_cost_usd"] is not None:
                    mlflow.log_metric("estimated_cost_usd", report["estimated_cost_usd"])
                for field, acc in report["field_accuracy"].items():
                    if acc is not None:
                        mlflow.log_metric(f"accuracy_{field}", acc)
                mlflow.log_dict(report, "report.json")

            print(f"  pass_rate={report['quote_verification_pass_rate']}  "
                  f"cost=${report['estimated_cost_usd']}  elapsed={report['elapsed_seconds']}s")
            for field, acc in report["field_accuracy"].items():
                print(f"    {field:<26} {acc}")
            print()

    out_dir = Path("logs/business-profile-context-ab")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    out_path.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")

    print(f"\nTotal estimated cost: ${total_cost:.4f}")
    print(f"Full report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
