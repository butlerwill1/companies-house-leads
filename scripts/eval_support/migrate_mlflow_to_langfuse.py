"""One-off: copy the historical MLflow eval data into Langfuse.

Reads the parked MLflow server (127.0.0.1:5000) and recreates, in Langfuse:
  - every trace, with its spans, inputs/outputs, tags and human assessments
    (assessments -> scores), keeping the **original MLflow timestamps**.
    Traces are grouped into a Langfuse session named after the source MLflow
    run (``<run name> (<id prefix>)`` -- e.g.
    ``context-ab-gemini-2.5-flash-narrative (3ba8bbe0)``).
  - where a run-linked trace's case is still in the live gold set, the trace
    is also attached to a **dataset experiment run** (via the OTel experiment
    attributes) so the run shows in the dataset's Experiments tab, next to
    the runs done after the cutover.
  - one "run summary" trace per MLflow run carrying its params and metrics.
  - the registered `business-profile-extraction` prompt (all versions).

Backdating works by dropping to the underlying OpenTelemetry tracer
(``client._otel_tracer.start_span(start_time=..., ...)``): Langfuse v4
self-hosts in events_only mode where the ingestion API only accepts scores,
so traces/spans must arrive over OTel, which takes explicit start/end times.

Idempotent: a ledger (logs/langfuse-migration-ledger.json) records every
migrated MLflow id; re-running skips them.

Usage:
    python -m scripts.eval_support.migrate_mlflow_to_langfuse --dry-run
    python -m scripts.eval_support.migrate_mlflow_to_langfuse
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_extractor import load_dotenv  # noqa: E402
from scripts.eval_support.langfuse_tracing import flush, langfuse_from_config  # noqa: E402

MLFLOW_URI = "http://127.0.0.1:5000"
LEDGER_PATH = Path("logs/langfuse-migration-ledger.json")

# experiment name -> (env key selector, live dataset, tag holding the case id)
EXPERIMENTS = {
    "companies-house-business-profile-eval": ("BUSINESS_PROFILE", "business-profile-gold", "eval.company_number"),
    "companies-house-vlm-financial-eval": ("VLM_FINANCIAL", "vlm-financial-gold", "eval.case_id"),
}
PROMPT_NAME = "business-profile-extraction"

# MLflow span type -> Langfuse observation type.
_SPAN_TYPE = {
    "LLM": "generation",
    "CHAT_MODEL": "generation",
    "EMBEDDING": "embedding",
    "TOOL": "tool",
    "RETRIEVER": "retriever",
    "AGENT": "agent",
    "CHAIN": "chain",
}


def _load_ledger() -> dict[str, Any]:
    if LEDGER_PATH.is_file():
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return {"traces": {}, "runs": {}, "prompt_versions": []}


def _save_ledger(ledger: dict[str, Any]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _eval_tags(mlflow_tags: dict[str, str]) -> list[str]:
    tags = ["migrated-from-mlflow"]
    for key, value in mlflow_tags.items():
        if key.startswith("eval.") or key.startswith("backfill."):
            tags.append(f"{key.split('.', 1)[1]}:{value}")
    return tags


def _assessment_scores(client: Any, trace_id: str, assessments: list[Any], *, timestamp: Any = None) -> int:
    count = 0
    for assessment in assessments or []:
        name = getattr(assessment, "name", None)
        value = getattr(assessment, "value", None)
        if name is None or value is None:
            continue
        kind = type(assessment).__name__
        data_type = "CATEGORICAL" if isinstance(value, str) else "NUMERIC"
        client.create_score(
            name=name,
            value=value if isinstance(value, (int, float, str)) else str(value),
            trace_id=trace_id,
            data_type=data_type,
            timestamp=timestamp,
            comment=f"migrated {kind}"
            + (f" ({assessment.source.source_id})" if getattr(assessment, "source", None) else ""),
        )
        count += 1
    return count


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _backdated_trace(
    client: Any,
    *,
    name: str,
    session_id: str | None,
    tags: list[str],
    metadata: dict[str, Any],
    input: Any,
    output: Any,
    start_ns: int,
    end_ns: int,
    children: list[dict[str, Any]],
    experiment: dict[str, str] | None = None,
) -> str:
    """Create one Langfuse trace (a root OTel span) plus its child spans,
    all with their original MLflow timestamps.

    Langfuse v4 self-hosts in events_only mode where the ingestion API only
    accepts scores -- traces/spans must arrive over OpenTelemetry. The
    high-level SDK always stamps 'now', so this drops to the underlying OTel
    tracer, which takes explicit ``start_time`` / ``end_time``.
    """
    from opentelemetry.trace import set_span_in_context
    from langfuse import LangfuseOtelSpanAttributes as A

    attributes = {
        A.OBSERVATION_TYPE: "span",
        A.TRACE_NAME: name,
        A.TRACE_TAGS: _dump(tags),
        A.TRACE_METADATA: _dump(metadata),
        A.OBSERVATION_METADATA: _dump(metadata),  # mirror, so it is inspectable via the observations API
        A.TRACE_INPUT: _dump(input),
        A.TRACE_OUTPUT: _dump(output),
        A.OBSERVATION_INPUT: _dump(input),
        A.OBSERVATION_OUTPUT: _dump(output),
    }
    if session_id:
        attributes[A.TRACE_SESSION_ID] = session_id
    if experiment:
        attributes[A.EXPERIMENT_NAME] = experiment["name"]
        attributes[A.EXPERIMENT_ID] = experiment["id"]
        attributes[A.EXPERIMENT_DATASET_ID] = experiment["dataset_id"]
        attributes[A.EXPERIMENT_ITEM_ID] = experiment["item_id"]

    tracer = client._otel_tracer
    root = tracer.start_span(name, start_time=start_ns, attributes=attributes)
    trace_id = format(root.get_span_context().trace_id, "032x")
    if experiment:
        root.set_attribute(A.EXPERIMENT_ITEM_ROOT_OBSERVATION_ID, format(root.get_span_context().span_id, "016x"))
    parent_ctx = set_span_in_context(root)
    for child in children:
        span = tracer.start_span(
            child["name"],
            start_time=child["start_ns"],
            context=parent_ctx,
            attributes={
                A.OBSERVATION_TYPE: child.get("type", "span"),
                A.OBSERVATION_INPUT: _dump(child.get("input")),
                A.OBSERVATION_OUTPUT: _dump(child.get("output")),
                A.OBSERVATION_METADATA: _dump(child.get("metadata") or {}),
            },
        )
        span.end(end_time=child["end_ns"])
    root.end(end_time=end_ns)
    return trace_id


def _migrate_trace(
    client: Any,
    trace: Any,
    *,
    session_id: str | None,
    run_id: str | None,
    run_name: str | None = None,
    experiment: dict[str, str] | None = None,
) -> str:
    info = trace.info
    metadata_map = dict(getattr(info, "trace_metadata", {}) or {})
    spans = list(trace.data.spans or [])
    root_span = spans[0] if spans else None

    trace_input = root_span.inputs if (root_span and isinstance(root_span.inputs, dict)) else None
    if trace_input is None:
        try:
            trace_input = json.loads(metadata_map.get("mlflow.traceInputs", "null"))
        except json.JSONDecodeError:
            trace_input = None
    trace_output = root_span.outputs if (root_span and isinstance(root_span.outputs, dict)) else None
    if trace_output is None:
        try:
            trace_output = json.loads(metadata_map.get("mlflow.traceOutputs", "null"))
        except json.JSONDecodeError:
            trace_output = None

    start_ms = int(getattr(info, "timestamp_ms", 0) or 0)
    duration_ms = int(getattr(info, "execution_time_ms", None) or getattr(info, "execution_duration", None) or 0)
    start_ns = start_ms * 1_000_000
    end_ns = (start_ms + max(duration_ms, 1)) * 1_000_000

    meta = {
        "mlflow_trace_id": info.trace_id,
        "mlflow_run_id": run_id,
        "mlflow_run_name": run_name,
        "mlflow_timestamp_ms": start_ms,
        "mlflow_url": f"{MLFLOW_URI}/#/experiments/{getattr(info, 'experiment_id', '')}",
    }
    name = dict(info.tags).get("mlflow.traceName") or (root_span.name if root_span else "mlflow_trace")

    children = []
    for span in spans[1:]:
        s_start = int(getattr(span, "start_time_ns", start_ns) or start_ns)
        s_end = int(getattr(span, "end_time_ns", s_start + 1) or (s_start + 1))
        children.append({
            "name": span.name,
            "type": _SPAN_TYPE.get(str(getattr(span, "span_type", "")).split(".")[-1], "span"),
            "input": span.inputs,
            "output": span.outputs,
            "metadata": {k: v for k, v in (getattr(span, "attributes", {}) or {}).items()
                         if not str(k).startswith("mlflow.")},
            "start_ns": s_start,
            "end_ns": s_end,
        })

    trace_id = _backdated_trace(
        client, name=name, session_id=session_id, tags=_eval_tags(dict(info.tags)),
        metadata=meta, input=trace_input, output=trace_output,
        start_ns=start_ns, end_ns=end_ns, children=children, experiment=experiment,
    )
    from datetime import UTC, datetime as _dt

    _assessment_scores(
        client, trace_id, list(info.assessments or []),
        timestamp=_dt.fromtimestamp(start_ms / 1000, UTC) if start_ms else None,
    )
    return trace_id


def _migrate_run_summary(client: Any, run: Any, *, session_id: str) -> str:
    data = run.data
    params = dict(data.params or {})
    metrics = dict(data.metrics or {})
    name = run.info.run_name or run.info.run_id
    start_ms = int(getattr(run.info, "start_time", 0) or 0)
    end_ms = int(getattr(run.info, "end_time", None) or (start_ms + 1000))
    trace_id = _backdated_trace(
        client, name=f"run: {name}", session_id=session_id,
        tags=["migrated-from-mlflow", "run-summary"],
        metadata={"mlflow_run_id": run.info.run_id, "params": params},
        input=params, output=metrics,
        start_ns=start_ms * 1_000_000, end_ns=max(end_ms, start_ms + 1) * 1_000_000,
        children=[],
    )
    from datetime import UTC, datetime as _dt

    ts = _dt.fromtimestamp(start_ms / 1000, UTC) if start_ms else None
    for key, value in metrics.items():
        client.create_score(name=key, value=float(value), trace_id=trace_id,
                            data_type="NUMERIC", timestamp=ts, comment="migrated run metric")
    return trace_id


def _migrate_prompt(client: Any, mlflow: Any, ledger: dict[str, Any], dry_run: bool) -> int:
    from scripts.eval_support.langfuse_prompts import to_langfuse_template

    migrated = 0
    version = 1
    while True:
        try:
            prompt = mlflow.genai.load_prompt(f"prompts:/{PROMPT_NAME}/{version}")
        except Exception:
            break
        key = f"{PROMPT_NAME}@{version}"
        if key in ledger["prompt_versions"]:
            version += 1
            continue
        if not dry_run:
            template = prompt.template
            # MLflow stores it already in {{var}} form; keep as-is.
            client.create_prompt(
                name=PROMPT_NAME, prompt=template, type="text",
                labels=["production"] if version >= 1 else [],
                tags=[str((prompt.tags or {}).get("prompt_version", f"v{version}"))],
                commit_message=f"Migrated from MLflow prompt registry version {version}.",
            )
            ledger["prompt_versions"].append(key)
        migrated += 1
        version += 1
    return migrated


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="Migrate at most this many traces per experiment.")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))

    import mlflow  # noqa: E402  (this script is the one place mlflow stays)
    mlflow.set_tracking_uri(MLFLOW_URI)
    from mlflow import MlflowClient

    mlflow_client = MlflowClient()
    ledger = _load_ledger()
    summary: dict[str, Any] = {}

    for experiment_name, (key_env, dataset_name, case_tag) in EXPERIMENTS.items():
        experiment = mlflow.set_experiment(experiment_name)
        config = {"langfuse": {"enabled": True, "key_env": key_env}}
        lf = None if args.dry_run else langfuse_from_config(config)
        if not args.dry_run and lf is None:
            print(f"Langfuse not configured for {key_env}; skipping {experiment_name}.", file=sys.stderr)
            continue

        # Map each MLflow run to a readable label and, where the trace's case
        # is still in the live gold set, to a Langfuse dataset experiment run
        # so it shows in the dataset's Experiments tab.
        runs = mlflow_client.search_runs([experiment.experiment_id], max_results=5000)
        name_of: dict[str, str] = {}
        session_of: dict[str, str] = {}
        for run in runs:
            label = run.info.run_name or run.info.run_id
            name_of[run.info.run_id] = label
            session_of[run.info.run_id] = f"{label} ({run.info.run_id[:8]})"

        dataset_id = None
        item_ids: set[str] = set()
        if lf is not None:
            try:
                dataset = lf.get_dataset(dataset_name)
                dataset_id = dataset.items[0].dataset_id if dataset.items else None
                item_ids = {item.id for item in dataset.items}
            except Exception:
                pass

        migrated_runs = 0
        for run in runs:
            if run.info.run_id in ledger["runs"]:
                continue
            if not args.dry_run:
                ledger["runs"][run.info.run_id] = _migrate_run_summary(
                    lf, run, session_id=session_of[run.info.run_id]
                )
            migrated_runs += 1

        migrated_traces = 0
        experiment_traces = 0
        skipped = 0
        page_token = None
        while True:
            page = mlflow_client.search_traces(
                locations=[experiment.experiment_id], max_results=100,
                page_token=page_token, include_spans=True,
            )
            for trace in page:
                if args.limit and migrated_traces >= args.limit:
                    break
                mlflow_trace_id = trace.info.trace_id
                if mlflow_trace_id in ledger["traces"]:
                    skipped += 1
                    continue
                md = dict(getattr(trace.info, "trace_metadata", {}) or {})
                md.update(dict(getattr(trace.info, "request_metadata", {}) or {}))
                run_id = md.get("mlflow.sourceRun")
                run_name = name_of.get(run_id) if run_id else None
                session_id = session_of.get(run_id) if run_id else f"unlinked ({experiment_name})"

                case_id = dict(trace.info.tags).get(case_tag)
                experiment_link = None
                if run_id and dataset_id and case_id in item_ids:
                    experiment_link = {
                        "name": name_of.get(run_id, run_id),
                        "id": f"mlflow-{run_id}",
                        "dataset_id": dataset_id,
                        "item_id": case_id,
                    }
                    experiment_traces += 1

                if not args.dry_run:
                    new_id = _migrate_trace(
                        lf, trace, session_id=session_id, run_id=run_id,
                        run_name=run_name, experiment=experiment_link,
                    )
                    ledger["traces"][mlflow_trace_id] = new_id
                    if migrated_traces % 25 == 0:
                        flush(lf)
                        _save_ledger(ledger)
                migrated_traces += 1
            page_token = page.token
            if not page_token or (args.limit and migrated_traces >= args.limit):
                break

        prompts = _migrate_prompt(lf, mlflow, ledger, args.dry_run) if key_env == "BUSINESS_PROFILE" else 0
        if not args.dry_run:
            flush(lf)
        summary[experiment_name] = {
            "runs_migrated": migrated_runs,
            "traces_migrated": migrated_traces,
            "traces_as_dataset_experiments": experiment_traces,
            "traces_skipped_already_done": skipped,
            "prompt_versions_migrated": prompts,
        }

    if not args.dry_run:
        _save_ledger(ledger)
    print(json.dumps({"dry_run": args.dry_run, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
