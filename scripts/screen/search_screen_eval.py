#!/usr/bin/env python3
"""Run the search screen over the verified gold cases and score it.

Every finished case is appended to a checkpoint (fsync'd) the moment it
returns, so an interrupted run loses nothing and a re-run replays finished
cases without a model call. Entries for a different prompt version are ignored.

Usage:
    python -m scripts.screen.search_screen_eval run --input short --limit 2
    python -m scripts.screen.search_screen_eval run --input full
    python -m scripts.screen.search_screen_eval score --input short --input full
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from scripts.business_profile_classifier.business_profile_eval import case_files, load_case  # noqa: E402
from scripts.screen.search_screen_cases import CASES_DIR, SCREEN_LABELS  # noqa: E402
from scripts.screen.search_screen_policy import (  # noqa: E402
    INPUT_BUILDERS,
    PASSING,
    PROMPT_VERSION,
    build_prompt,
    parse_answer,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_MODEL = "openai/gpt-5.4-mini"
RUNS_DIR = Path("logs/search-screen/runs")
CHECKPOINT = RUNS_DIR / "checkpoint.jsonl"
# A cap so a pathological filing cannot cost much: about 25k tokens of input.
MAX_INPUT_CHARS = 110_000


def gold_cases(cases_dir: Path) -> list[dict[str, Any]]:
    cases = [load_case(p) for p in case_files(cases_dir)]
    return [c for c in cases if (c.get("review") or {}).get("status") == "verified"
            and ((c.get("expected") or {}).get("search_screen") or {}).get("value") in SCREEN_LABELS]


def gold_label(case: dict[str, Any]) -> str:
    return case["expected"]["search_screen"]["value"]


def call_model(api_key: str, model: str, prompt: str, timeout: int) -> tuple[str, dict[str, Any]]:
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {}
    for attempt in range(3):
        try:
            response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            if body.get("error") is not None and "response_format" in payload:
                payload.pop("response_format")
                continue
            if body.get("error") is not None:
                raise RuntimeError(str(body["error"]))
            return body["choices"][0]["message"]["content"], body.get("usage") or {}
        except (requests.RequestException, RuntimeError, KeyError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def model_prices(model: str) -> tuple[float, float] | None:
    """(input, output) USD per token from OpenRouter, or None."""
    try:
        data = requests.get(OPENROUTER_MODELS_URL, timeout=30).json().get("data", [])
        for entry in data:
            if entry.get("id") == model:
                pricing = entry.get("pricing") or {}
                return float(pricing["prompt"]), float(pricing["completion"])
    except Exception:  # noqa: BLE001
        return None
    return None


Key = tuple[str, str, str, int, str]


def _key(model: str, kind: str, number: str, version: str | None = None, run_id: int = 1) -> Key:
    """(model, input, prompt version, run id, company). The run id lets the same
    prompt be run twice for a noise check: a second run is not a replay."""
    return (model, kind, version or PROMPT_VERSION, run_id, number)


def load_checkpoint(path: Path = CHECKPOINT) -> dict[Key, dict[str, Any]]:
    """Every saved response, of every prompt version: a record is only ever
    looked up under the version that produced it. Records written before run
    ids existed count as run 1."""
    done: dict[Key, dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        done[_key(rec["model"], rec["input"], rec["company_number"], rec["prompt_version"], rec.get("run_id", 1))] = rec
    return done


def append_checkpoint(record: dict[str, Any], path: Path = CHECKPOINT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_case(api_key: str, model: str, kind: str, case: dict[str, Any], timeout: int,
             version: str | None = None, run_id: int = 1) -> dict[str, Any]:
    version = version or PROMPT_VERSION
    text = INPUT_BUILDERS[kind](case)[:MAX_INPUT_CHARS]
    prompt = build_prompt(company_name=case["company_name"], sic_label=case.get("sic_label"), text=text,
                          version=version)
    raw: str | None = None
    usage: dict[str, Any] = {}
    error: str | None = None
    try:
        raw, usage = call_model(api_key, model, prompt, timeout)
    except Exception as exc:  # noqa: BLE001 -- one bad call must not sink the run; it fails open
        error = f"request failed: {exc}"
    parsed = parse_answer(raw, text) if error is None else {
        "answer": None, "quote": None, "reason": None, "quote_ok": None, "problem": error, "passes": True}
    return {"company_number": case["company_number"], "model": model, "input": kind,
            "prompt_version": version, "run_id": run_id, "text_chars": len(text), "raw": raw, "usage": usage,
            **parsed}


def run(kind: str, model: str, cases: list[dict[str, Any]], *, workers: int, timeout: int,
        version: str | None = None, run_id: int = 1) -> list[dict[str, Any]]:
    load_dotenv(Path(".env"))
    version = version or PROMPT_VERSION
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY not set in .env or the environment.")
    done = load_checkpoint()
    results: dict[str, dict[str, Any]] = {}
    todo = []
    for case in cases:
        record = done.get(_key(model, kind, case["company_number"], version, run_id))
        if record is not None and not (record.get("problem") or "").startswith("request failed"):
            results[case["company_number"]] = record
        else:
            todo.append(case)
    print(f"{kind} {version} run {run_id}: {len(results)} replayed from checkpoint, {len(todo)} to call",
          file=sys.stderr)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_case, api_key, model, kind, case, timeout, version, run_id): case
                   for case in todo}
        for index, future in enumerate(as_completed(futures), 1):
            record = future.result()
            append_checkpoint(record)
            results[record["company_number"]] = record
            if index % 20 == 0 or index == len(todo):
                print(f"  {index}/{len(todo)}", file=sys.stderr)
    return [results[c["company_number"]] for c in cases]


def confusion(pairs: list[tuple[str, str | None]]) -> dict[str, dict[str, int]]:
    table = {g: {a: 0 for a in (*SCREEN_LABELS, "none")} for g in SCREEN_LABELS}
    for gold, answer in pairs:
        table[gold][answer or "none"] += 1
    return table


def score(cases: list[dict[str, Any]], records: list[dict[str, Any]], *, draft_key: str | None = None) -> dict[str, Any]:
    """Scores ``records`` (or, with ``draft_key``, the drafter's stored labels)
    against the gold. Recall is on the screen decision: a gold case is saved if
    the screen passes it."""
    by_number = {r["company_number"]: r for r in records}
    pairs: list[tuple[str, str | None]] = []
    passes: list[tuple[str, bool]] = []
    for case in cases:
        gold = gold_label(case)
        if draft_key:
            answer = ((case.get(draft_key) or {}).get("search_screen") or {}).get("value")
            passed = answer in PASSING if answer else True
        else:
            record = by_number[case["company_number"]]
            answer, passed = record.get("answer"), bool(record.get("passes"))
        pairs.append((gold, answer))
        passes.append((gold, passed))

    def recall(golds: set[str]) -> tuple[int, int]:
        relevant = [p for g, p in passes if g in golds]
        return sum(relevant), len(relevant)

    n = len(cases)
    likely, both = recall({"likely"}), recall({"likely", "possible"})
    return {
        "n": n,
        "exact_agreement": sum(g == a for g, a in pairs) / n,
        "recall_likely": likely, "recall_likely_or_possible": both,
        "removal_rate": sum(not p for _, p in passes) / n,
        "gold_unlikely_rejected": (sum(1 for g, p in passes if g == "unlikely" and not p),
                                   sum(1 for g, _ in passes if g == "unlikely")),
        "confusion": confusion(pairs),
        "gold_counts": {label: sum(1 for g, _ in pairs if g == label) for label in SCREEN_LABELS},
    }


def cost(records: list[dict[str, Any]], prices: tuple[float, float] | None) -> dict[str, Any]:
    prompt = sum(int((r.get("usage") or {}).get("prompt_tokens") or 0) for r in records)
    completion = sum(int((r.get("usage") or {}).get("completion_tokens") or 0) for r in records)
    usd = prompt * prices[0] + completion * prices[1] if prices else None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "usd": usd}


GOLD_DATASET = "search-screen-gold"
GOLD_ID_PREFIX = "search-screen-gold:"  # distinct from the draft dataset's ids: item ids are project-unique


HELDOUT_DATASET = "search-screen-heldout-draft"
RUN_INDEX = Path("logs/search-screen/langfuse-runs.jsonl")


def dataset_for(kind: str, label_status: str) -> tuple[str, str]:
    """(dataset name, item id prefix). Each combination of input and label
    status is its own dataset: items are immutable once published."""
    if label_status != "verified":
        return HELDOUT_DATASET, "search-screen-heldout:"
    return (GOLD_DATASET, GOLD_ID_PREFIX) if kind == "short" else (f"{GOLD_DATASET}-{kind}", f"{GOLD_DATASET}-{kind}:")


def record_run(entry: dict[str, Any]) -> None:
    """Append a logged run to a local index. Langfuse's own run-listing
    endpoint is switched off on a v4 events-only server (it returns 404), so
    this file is how the runs are found again; `list-runs` reads it."""
    from datetime import UTC, datetime

    RUN_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with RUN_INDEX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"logged_at": datetime.now(UTC).isoformat(timespec="seconds"), **entry},
                                ensure_ascii=False) + "\n")


def heldout_cases(cases_dir: Path) -> list[dict[str, Any]]:
    """The blind cases with no human label, scored against the drafter's label
    held in memory only. Nothing is written to the case files."""
    import copy

    out = []
    for path in case_files(cases_dir):
        case = load_case(path)
        if case.get("blind") and not case.get("blind_review"):
            case = copy.deepcopy(case)
            case["expected"] = {"search_screen": {"value": case["draft_full"]["search_screen"]["value"]}}
            out.append(case)
    return out


def gold_dataset_records(cases: list[dict[str, Any]], kind: str, label_status: str = "verified") -> list[dict[str, Any]]:
    prefix = dataset_for(kind, label_status)[1]
    return [{
        "id": f"{prefix}{c['company_number']}",
        "input": {"company_name": c["company_name"], "sic_label": c.get("sic_label"),
                  "text": INPUT_BUILDERS[kind](c)[:MAX_INPUT_CHARS], "input_kind": kind},
        "expected": {"search_screen": gold_label(c)},
        "metadata": {"company_number": c["company_number"], "company_name": c["company_name"],
                     "cohort": c["cohort"], "hard_category": c.get("hard_category"), "label_status": label_status},
    } for c in cases]


def log_run_to_langfuse(cases: list[dict[str, Any]], records: list[dict[str, Any]], *, model: str, kind: str,
                        version: str, run_id: int, prompt_reference: str | None,
                        label_status: str = "verified") -> str | None:
    """Replay a finished run (from the checkpoint: no model calls) into Langfuse
    as one dataset run over the verified gold, so prompt version, responses and
    per-case correctness sit beside the prompt registry entry."""
    from datetime import UTC, datetime

    from scripts.eval_support.langfuse_runs import (
        dataset_digest, evaluation, experiment_run_name, run_experiment, sync_dataset)
    from scripts.eval_support.langfuse_tracing import flush, langfuse_from_config, observation
    from scripts.screen.search_screen_publish import LANGFUSE_CONFIG

    lf = langfuse_from_config(LANGFUSE_CONFIG)
    if lf is None:
        return None
    dataset_name, _ = dataset_for(kind, label_status)
    dataset = gold_dataset_records(cases, kind, label_status)
    note = ("expected = the reviewer's label. The 25 blind cases are not in it." if label_status == "verified"
            else "expected = Claude's draft label, NOT human-reviewed. Held-out: no prompt was tuned on these.")
    sync_dataset(lf, dataset_name, dataset, digest=dataset_digest(dataset), immutable=True,
                 description=f"Search-screen cases: {len(cases)}, {kind} input, {note}")
    by_number = {r["company_number"]: r for r in records}
    by_case = {c["company_number"]: c for c in cases}

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        number = item.metadata["company_number"]
        record = by_number[number]
        case = by_case[number]
        with observation(lf, name="search_screen", as_type="generation", model=model,
                         input=build_prompt(company_name=case["company_name"], sic_label=case.get("sic_label"),
                                            text=INPUT_BUILDERS[kind](case)[:MAX_INPUT_CHARS], version=version),
                         output={"raw_response": record.get("raw"), "answer": record.get("answer"),
                                 "passes": record.get("passes"), "problem": record.get("problem")}):
            pass
        flush(lf)
        return record

    def evaluate(*, output: dict[str, Any], expected_output: Any = None, **_: Any) -> list[Any]:
        gold = (expected_output or {}).get("search_screen")
        gold_passes = gold in PASSING
        out = [evaluation("pass_decision_correct", 1.0 if bool(output.get("passes")) == gold_passes else 0.0,
                          data_type="NUMERIC", comment=f"gold={gold} answer={output.get('answer')}"),
               evaluation("exact_label", 1.0 if output.get("answer") == gold else 0.0, data_type="NUMERIC")]
        if gold_passes:
            out.append(evaluation("lead_kept", 1.0 if output.get("passes") else 0.0, data_type="NUMERIC"))
        if output.get("quote_ok") is not None:
            out.append(evaluation("quote_verbatim", 1.0 if output["quote_ok"] else 0.0, data_type="NUMERIC"))
        return out

    def aggregate(*, item_results: list[Any], **_: Any) -> list[Any]:
        result = score(cases, records)
        likely, both = result["recall_likely"], result["recall_likely_or_possible"]
        return [evaluation("recall_likely", likely[0] / max(likely[1], 1), data_type="NUMERIC"),
                evaluation("recall_likely_or_possible", both[0] / max(both[1], 1), data_type="NUMERIC"),
                evaluation("removal_rate", result["removal_rate"], data_type="NUMERIC"),
                evaluation("exact_agreement", result["exact_agreement"], data_type="NUMERIC")]

    run_name = experiment_run_name(model=model, when=datetime.now(UTC), label=f"{version} run{run_id}")
    result = run_experiment(lf, dataset_name=dataset_name, run_name=run_name, task=task, evaluators=[evaluate],
                            run_evaluators=[aggregate], description=f"{model} @ {version} (run {run_id}, {kind})",
                            metadata={"prompt_version": version, "prompt": prompt_reference or "", "model": model,
                                      "input": kind, "run_id": str(run_id), "label_status": label_status})
    flush(lf)
    record_run({"dataset": dataset_name, "run_name": run_name, "url": result.dataset_run_url,
                "prompt_version": version, "run_id": run_id, "input": kind, "model": model,
                "label_status": label_status, "cases": len(cases)})
    return result.dataset_run_url


def compare(cases: list[dict[str, Any]], model: str, kind: str, runs: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """One row per (version, run) over all verified cases and over the random
    cohort, plus the pass or reject flips between consecutive runs of a version."""
    done = load_checkpoint()
    rows = []
    previous: dict[str, dict[str, dict[str, Any]]] = {}
    for version, run_id in runs:
        records = {c["company_number"]: done[_key(model, kind, c["company_number"], version, run_id)] for c in cases}
        row: dict[str, Any] = {"version": version, "run": run_id}
        for name, subset in (("all", cases), ("random", [c for c in cases if c["cohort"] == "random"])):
            result = score(subset, [records[c["company_number"]] for c in subset])
            row[name] = {"recall_likely": result["recall_likely"], "recall_l+p": result["recall_likely_or_possible"],
                         "removed": round(result["removal_rate"], 3), "exact": round(result["exact_agreement"], 3),
                         "unlikely_rejected": result["gold_unlikely_rejected"]}
        if version in previous:
            before = previous[version]
            row["flips_vs_previous_run"] = {
                "pass_or_reject": sum(before[n]["passes"] != records[n]["passes"] for n in records),
                "label": sum(before[n]["answer"] != records[n]["answer"] for n in records)}
        previous[version] = records
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-dir", default=str(CASES_DIR))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--input", choices=tuple(INPUT_BUILDERS), required=True)
    runner.add_argument("--prompt-version", default=PROMPT_VERSION)
    runner.add_argument("--run-id", type=int, default=1)
    runner.add_argument("--limit", type=int)
    runner.add_argument("--workers", type=int, default=4)
    runner.add_argument("--timeout", type=int, default=180)
    logger = commands.add_parser("log-langfuse", help="Replay a saved run into Langfuse as a dataset run (no model calls).")
    logger.add_argument("--input", choices=tuple(INPUT_BUILDERS), default="short")
    logger.add_argument("--prompt-version", required=True)
    logger.add_argument("--run-id", type=int, default=1)
    logger.add_argument("--cases", choices=("gold", "heldout"), default="gold",
                        help="gold = the verified cases; heldout = the unlabelled blind cases against Claude's drafts")
    commands.add_parser("list-runs", help="List the runs logged to Langfuse (from the local index).")
    comparer = commands.add_parser("compare")
    comparer.add_argument("--input", choices=tuple(INPUT_BUILDERS), default="short")
    comparer.add_argument("--run", action="append", required=True, metavar="VERSION:RUN_ID")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cases = gold_cases(Path(args.cases_dir))
    prices = model_prices(args.model)
    if args.command == "run":
        subset = cases[: args.limit] if args.limit else cases
        records = run(args.input, args.model, subset, workers=args.workers, timeout=args.timeout,
                      version=args.prompt_version, run_id=args.run_id)
        print(json.dumps({"input": args.input, "prompt_version": args.prompt_version, "run_id": args.run_id,
                          **score(subset, records), "cost": cost(records, prices),
                          "quote_valid": sum(bool(r.get("quote_ok")) for r in records),
                          "problems": sum(bool(r.get("problem")) for r in records)}, indent=2))
    elif args.command == "log-langfuse":
        from scripts.eval_support.langfuse_tracing import langfuse_from_config
        from scripts.screen.search_screen_prompt_registry import prompt_reference
        from scripts.screen.search_screen_publish import LANGFUSE_CONFIG
        load_dotenv(Path(".env"))
        done = load_checkpoint()
        label_status = "verified"
        if args.cases == "heldout":
            cases, label_status = heldout_cases(Path(args.cases_dir)), "draft-unreviewed"
        records = [done[_key(args.model, args.input, c["company_number"], args.prompt_version, args.run_id)] for c in cases]
        reference = prompt_reference(langfuse_from_config(LANGFUSE_CONFIG))
        print(log_run_to_langfuse(cases, records, model=args.model, kind=args.input, version=args.prompt_version,
                                  run_id=args.run_id, prompt_reference=reference, label_status=label_status))
    elif args.command == "list-runs":
        for line in RUN_INDEX.read_text(encoding="utf-8").splitlines():
            run_entry = json.loads(line)
            print(f"{run_entry['dataset']:32} {run_entry['prompt_version']:40} run{run_entry['run_id']} "
                  f"{run_entry['input']:5} {run_entry['label_status']:16} {run_entry['url']}")
    else:
        runs = [(v, int(r)) for v, r in (x.rsplit(":", 1) for x in args.run)]
        print(json.dumps(compare(cases, args.model, args.input, runs), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
