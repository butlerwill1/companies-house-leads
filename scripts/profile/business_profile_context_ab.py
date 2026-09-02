"""One-off A/B harness: narrative-sections vs whole-filed-document context,
across a small model shortlist, on a fixed sample of the gold set. Logs one
Langfuse dataset run per (model, context) combination in the
business-profile-eval project the regular eval harness uses, so results sit
alongside it rather than in a separate, easy-to-lose place.

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
from scripts.eval_support.langfuse_runs import evaluation, run_experiment, sync_dataset
from scripts.eval_support.langfuse_tracing import flush, langfuse_from_config, observation
from scripts.profile.business_profile_eval import DATASET_NAME, case_files, load_case
from scripts.profile.business_profile_metrics import (
    SCORED_FIELDS,
    compute_metrics,
    flatten_metrics,
    score_case,
)
from scripts.profile.business_profile_policy import (
    FIELD_VALUES,
    PROMPT_TEMPLATE,
    PROMPT_VERSION,
    SIC_AGREEMENT_VALUES,
    build_prompt,
    normalize_quote_text,
    parse_json_response,
    prompt_option_blocks,
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
    in it, and the model naturally cites those instead of the synthetic
    wrapper label it was never told to use. This keeps the actual
    hallucination check (the quote must be a genuine verbatim substring of
    the filing) and drops the section-name match entirely."""
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
        elif normalize_quote_text(quote) not in normalize_quote_text(whole_text):
            errors.append(f"{field}.quote does not appear verbatim in the filed document: {quote!r}")
    sic = payload.get("sic_agreement")
    if not isinstance(sic, dict) or sic.get("value") not in SIC_AGREEMENT_VALUES:
        errors.append(f"sic_agreement.value must be one of {SIC_AGREEMENT_VALUES}")
    return errors


MODELS = [
    "google/gemini-2.5-flash",
    "google/gemini-3.7-flash",
    "google/gemini-2.5-flash-lite",
    "anthropic/claude-opus-5",
]
CONTEXTS = ["narrative", "whole_document"]

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
        **prompt_option_blocks(),
        sic_label=case.get("sic_label") or "(none declared)",
        sic_code=case.get("sic_code") or "(none)",
    )


def call_model(api_key: str, model: str, prompt: str, timeout: int) -> tuple[str, dict[str, Any]]:
    """Returns (content, usage). A direct call, not BusinessProfileModelClient
    -- token usage is needed for real cost, which the production client
    doesn't return."""
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
    return score_case(case, extracted)["fields"]


_ATTRIBUTABLE_FIELD_NAMES = set(FIELD_VALUES) | {"sic_agreement", "business_description"}


def _error_field(message: str) -> str | None:
    head = message.split(" ", 1)[0].split(".", 1)[0]
    return head if head in _ATTRIBUTABLE_FIELD_NAMES else None


def _extract_one(
    api_key: str, model: str, context: str, case: dict[str, Any], timeout: int
) -> dict[str, Any]:
    """Model call + validation + partial-rejection scoring for one case.
    Returns everything run_combination's aggregation needs."""
    whole_text: str | None = None
    prompt: str | None = None
    raw: str | None = None
    payload: dict[str, Any] | None = None
    errors: list[str] = []
    usage: dict[str, Any] = {}

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
            errors = ["no whole-document filing available for this company"]
        else:
            whole_text = (RAW_DIR / f"{case['company_number']}.md").read_text(encoding="utf-8")

    if prompt is not None and not errors:
        try:
            raw, usage = call_model(api_key, model, prompt, timeout)
        except Exception as exc:  # noqa: BLE001
            errors = [f"request failed: {exc}"]

    if raw is not None and not errors:
        try:
            payload = parse_json_response(raw)
        except (ValueError, TypeError) as exc:
            errors = [f"response was not valid JSON: {exc}"]

    if payload is not None and not errors:
        errors = (
            validate_response(payload, case["sections"])
            if whole_text is None
            else validate_whole_document_response(payload, whole_text)
        )

    tainted_fields: set[str] = set()
    fully_rejected = False
    if errors:
        attributed = {_error_field(e) for e in errors}
        if None in attributed or "business_description" in attributed:
            fully_rejected = True
        else:
            tainted_fields = attributed & set(SCORED_FIELDS)

    if fully_rejected:
        fields = score(case, None)
        outcome = "rejected"
    elif tainted_fields:
        fields = score(case, payload)
        for f in tainted_fields:
            fields[f] = {"expected": fields[f]["expected"], "actual": None, "correct": False}
        outcome = "partial"
    else:
        fields = score(case, payload)
        outcome = "scored"

    committed_unclear = 0
    counted_fields = 0
    if payload is not None and not fully_rejected:
        for field in FIELD_VALUES:
            if field in tainted_fields:
                continue
            counted_fields += 1
            if payload.get(field, {}).get("value") == "unclear":
                committed_unclear += 1

    return {
        "case": case,
        "prompt": prompt,
        "raw": raw,
        "payload": payload,
        "errors": errors,
        "fields": fields,
        "outcome": outcome,
        "fully_rejected": fully_rejected,
        "tainted_fields": sorted(tainted_fields),
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "committed_unclear": committed_unclear,
        "counted_fields": counted_fields,
    }


def run_combination(
    lf: Any,
    api_key: str,
    model: str,
    context: str,
    cases: list[dict[str, Any]],
    run_name: str,
    timeout: int = 120,
) -> dict[str, Any]:
    by_id = {case["company_number"]: case for case in cases}
    outcomes: list[dict[str, Any]] = []
    start = time.monotonic()

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        case = by_id[item.id]
        outcome = _extract_one(api_key, model, context, case, timeout)
        with observation(
            lf,
            name="business_profile_context_ab",
            as_type="generation",
            model=model,
            input=outcome["prompt"],
            output={"raw_response": outcome["raw"], "payload": outcome["payload"], "errors": outcome["errors"]},
            metadata={"context": context, "outcome": outcome["outcome"]},
        ):
            pass
        if outcome["outcome"] == "rejected":
            print(f"    REJECTED {case['company_number']}: {(outcome['errors'] or ['?'])[0]}")
        elif outcome["outcome"] == "partial":
            print(f"    PARTIAL  {case['company_number']}: nulling {outcome['tainted_fields']}")
        outcomes.append(outcome)
        return outcome

    def evaluate(*, output: dict[str, Any], **_: Any) -> list[Any]:
        evals: list[Any] = [evaluation("outcome", output["outcome"], data_type="CATEGORICAL")]
        for field, result in output["fields"].items():
            if result.get("expected") is None:
                continue
            evals.append(
                evaluation(f"field.{field}", 1.0 if result["correct"] else 0.0, data_type="NUMERIC")
            )
        return evals

    def aggregate(*, item_results: list[Any], **_: Any) -> list[Any]:
        metrics = compute_metrics([{"company_number": o["case"]["company_number"], "fields": o["fields"]} for o in outcomes])
        rejections = sum(1 for o in outcomes if o["outcome"] in ("rejected", "partial"))
        total_fields = sum(o["counted_fields"] for o in outcomes)
        unclear = sum(o["committed_unclear"] for o in outcomes)
        evals = [
            evaluation("quote_verification_pass_rate",
                       round(1 - rejections / len(outcomes), 4) if outcomes else 0.0, data_type="NUMERIC"),
            evaluation("partial_rejections",
                       float(sum(1 for o in outcomes if o["outcome"] == "partial")), data_type="NUMERIC"),
            evaluation("prompt_tokens", float(sum(o["prompt_tokens"] for o in outcomes)), data_type="NUMERIC"),
            evaluation("completion_tokens", float(sum(o["completion_tokens"] for o in outcomes)), data_type="NUMERIC"),
        ]
        if total_fields:
            evals.append(evaluation("unclear_rate", round(unclear / total_fields, 4), data_type="NUMERIC"))
        evals += [evaluation(name, value, data_type="NUMERIC") for name, value in flatten_metrics(metrics).items()]
        return evals

    result = run_experiment(
        lf,
        dataset_name=DATASET_NAME,
        run_name=run_name,
        task=task,
        evaluators=[evaluate],
        run_evaluators=[aggregate],
        description=f"context A/B: {model} / {context} @ {PROMPT_VERSION}",
        metadata={"model": model, "context": context, "prompt_version": PROMPT_VERSION,
                  "sample_stride": str(SAMPLE_STRIDE)},
    )
    flush(lf)

    metrics = compute_metrics([{"company_number": o["case"]["company_number"], "fields": o["fields"]} for o in outcomes])
    rejections = sum(1 for o in outcomes if o["outcome"] in ("rejected", "partial"))
    total_fields = sum(o["counted_fields"] for o in outcomes)
    unclear = sum(o["committed_unclear"] for o in outcomes)
    report = {
        "model": model,
        "context": context,
        "run_url": result.dataset_run_url,
        "cases": len(outcomes),
        "rejections": rejections,
        "partial_rejections": sum(1 for o in outcomes if o["outcome"] == "partial"),
        "quote_verification_pass_rate": round(1 - rejections / len(outcomes), 4) if outcomes else None,
        "unclear_rate": round(unclear / total_fields, 4) if total_fields else None,
        "elapsed_seconds": round(time.monotonic() - start, 1),
        "prompt_tokens": sum(o["prompt_tokens"] for o in outcomes),
        "completion_tokens": sum(o["completion_tokens"] for o in outcomes),
        "metrics": metrics,
        "field_accuracy": {f: m.get("accuracy") for f, m in metrics["fields"].items()},
        "field_scored_counts": {f: m.get("scored", 0) for f, m in metrics["fields"].items()},
        "results": [{"company_number": o["case"]["company_number"], "fields": o["fields"]} for o in outcomes],
    }
    return report


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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(".env"))
    api_key = os.environ["OPENROUTER_API_KEY"]

    lf = langfuse_from_config({"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}})
    if lf is None:
        print("Langfuse not configured (see docs/LANGFUSE_SETUP.md).", file=sys.stderr)
        return 1

    contexts = sys.argv[1:] or CONTEXTS

    cases = sample_cases()
    records = [
        {
            "id": case["company_number"],
            "input": {"company_name": case["company_name"], "sic_code": case["sic_code"]},
            "expected": case.get("expected"),
            "metadata": {"company_number": case["company_number"]},
        }
        for case in cases
    ]
    sync_dataset(lf, DATASET_NAME, records, description="Business-profile gold set")

    prices = fetch_prices(MODELS)
    print(f"Sample: {len(cases)} cases, {len(MODELS)} models x {len(contexts)} contexts = "
          f"{len(cases) * len(MODELS) * len(contexts)} calls\n")

    all_reports = []
    total_cost = 0.0
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    for model in MODELS:
        for context in contexts:
            print(f"=== {model} / {context} ===")
            run_name = f"context-ab-{model.split('/')[-1]}-{context}-{stamp}"
            report = run_combination(lf, api_key, model, context, cases, run_name)
            cost = cost_usd(model, report["prompt_tokens"], report["completion_tokens"], prices)
            report["estimated_cost_usd"] = round(cost, 4) if cost is not None else None
            if cost is not None:
                total_cost += cost
            all_reports.append(report)

            search = report["metrics"]["search_addressable"]
            print(f"  pass_rate={report['quote_verification_pass_rate']}  "
                  f"cost=${report['estimated_cost_usd']}  elapsed={report['elapsed_seconds']}s  {report['run_url']}")
            print(f"  search-addressable: P={search['precision']} R={search['recall']} F1={search['f1']}")
            for field, m in report["metrics"]["fields"].items():
                if m.get("scored"):
                    print(f"    {field:<26} acc={m['accuracy']:.3f} cover={m['coverage']:.3f} "
                          f"macroF1={(m['macro_f1'] if m['macro_f1'] is not None else 0):.3f}")
            print()

    out_dir = Path("logs/business-profile-context-ab")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report-{stamp}.json"
    out_path.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")

    print(f"\nTotal estimated cost: ${total_cost:.4f}")
    print(f"Full report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
