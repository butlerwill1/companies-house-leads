---
name: mlflow-eval-discipline
description: Use whenever running a model evaluation, comparison, or benchmark in this repo (companies-house-leads) -- "run an evaluation," "compare models," "test model X vs Y," "try a different prompt/context," "score the gold set," or writing any new eval/comparison script under scripts/profile/ or scripts/vlm/. Also use whenever calling any mlflow.* function directly (mlflow.start_run, MlflowClient(), mlflow.log_metric, etc.) outside the existing harness functions, since that is exactly where both mistakes below have actually happened. Two hard-learned failure modes this exists to prevent: (1) a second local MLflow tracking store getting created by accident, wasting real API spend on runs nobody can find later, and (2) an evaluation run that logs aggregate metrics but zero per-case traces, which defeats the entire point of using MLflow here and has directly frustrated the user before ("this is ridiculous... every time there's a problem with the runs").
---

# MLflow eval discipline

Two rules. Both come from real, expensive mistakes made in this repo, not
theoretical risk. Follow the checklist before writing or running anything
that touches `mlflow`.

## Why this exists

This repo runs every eval harness (`evals/vlm_financials/`,
`evals/business_profiles/`, and any new one) against **one** MLflow server:
`http://127.0.0.1:5000`, backed by
`C:\Users\wwwwi\mlflow-server\data\mlflow.db`. A new harness is a new
*experiment* inside that server, never a new server, never a new
`tracking_uri`. This is already the rule in `AGENTS.md` and `README.md` --
this skill exists because the rule got broken anyway, twice, by calling an
`mlflow.*` function before telling it which server to use.

Separately: the entire reason the user wants results in MLflow rather than
a printed table or a JSON report is so they can open a run and read the
actual model conversations -- the prompt, the response, what was scored
right or wrong -- not just look at an accuracy percentage. A run with
metrics but no traces answers "how well" but not "show me," which is the
part that actually matters here. A comparison harness built without
per-case tracing shipped, ran real paid API calls, and produced a page that
said "No traces found" for every single run. That is not acceptable output
for this repo, ever, regardless of how good the metrics look.

## Checklist, in order

**1. Set the tracking URI before touching anything else `mlflow`.**

```python
import mlflow
mlflow.set_tracking_uri("http://127.0.0.1:5000")   # <-- first mlflow call, no exceptions
mlflow.set_experiment("companies-house-business-profile-eval")  # or the relevant experiment
```

The concrete failure: calling `mlflow.MlflowClient()` -- or `mlflow.get_trace`,
`mlflow.log_expectation`, anything -- as the *first* mlflow call in a script
or a one-off `python -c "..."` snippet silently defaults to a local
file-based store and writes a stray `mlflow.db` wherever the process's
working directory happens to be. It does not error. It does not warn. The
only way to notice is `git status` turning up a file you didn't expect,
after money has already been spent against the wrong store. So:

- `set_tracking_uri` is the *first* line of mlflow code in any script,
  including throwaway `python -c` one-liners used to poke at a trace or
  check a run's data. Not "near the top" -- first.
- After running anything ad hoc against mlflow, `git status` the repo root
  before moving on. A `mlflow.db` appearing there means the rule above got
  skipped somewhere and real spend may be sitting in the wrong place.
- Never run `mlflow server` or `mlflow ui` from this repo. If a local
  server needs starting, that is a decision for the user to make
  explicitly (see `evals/vlm_financials/README.md`), not something a
  script should do on its own.

**2. Every case that gets a model call gets its own trace. No exceptions,
including rejections and errors.**

An eval run that only calls `mlflow.log_params` / `mlflow.log_metric` /
`mlflow.log_dict` on the aggregate `Run` produces a page with numbers on it
and nothing to click into. That is half a job. Every individual case --
whether the model's answer was accepted, rejected for a bad quote, failed
to parse as JSON, or the request itself errored -- needs a trace recording
what was actually sent and what actually came back, tagged so it is findable
(`eval.company_number`, `eval.model`, `eval.context`, or equivalent).

The pattern already exists twice in this repo. Copy it, don't reinvent it:

- `scripts/profile/business_profile_eval.py`: `_log_case_trace` (one trace
  per company, created once) and `_refresh_trace_snapshot` (keeping that
  trace's Inputs/Outputs current on re-sync via `set_trace_tag`, since a
  trace's span-level inputs/outputs are immutable once written but its
  tags are not). `sync_review_queue` shows the two-phase pattern for a
  *batch* of traces: create/find every trace_id first, `mlflow.
  flush_trace_async_logging()`, **then** do per-trace work like seeding
  expectations -- because a trace is exported asynchronously and touching
  it before it lands server-side fails with `RESOURCE_DOES_NOT_EXIST`.
- `scripts/profile/business_profile_context_ab.py`: `log_case_trace` (one
  trace per case *per model/context combination*) plus `run_combination`
  collecting the resulting trace_ids and `main()` linking the whole batch
  to its aggregate `Run` with `MlflowClient().link_traces_to_run(trace_ids=
  ..., run_id=run.info.run_id)` -- again only *after*
  `flush_trace_async_logging()`, for the same reason.

Minimal shape for a new eval/comparison script, model-agnostic:

```python
from mlflow.entities import SpanType

def log_case_trace(case, **context_tags) -> str:
    with mlflow.start_span(name="<something-descriptive>", span_type=SpanType.WORKFLOW) as root:
        mlflow.update_current_trace(
            tags={"eval.company_number": case["company_number"], **context_tags},
            request_preview=f"{case.get('company_name')} ({context_tags})",
            response_preview="<short summary of the outcome, including rejections>",
        )
        root.set_inputs({"prompt": prompt, **context_tags})
        root.set_outputs({"raw_response": raw, "payload": payload, "errors": errors})
    return mlflow.get_last_active_trace_id()

# ... after the batch, inside or after the aggregate run ...
mlflow.flush_trace_async_logging()
mlflow.MlflowClient().link_traces_to_run(trace_ids=trace_ids, run_id=run.info.run_id)
```

Verify it worked, don't just trust it compiled -- run one or two cheap
cases for real and confirm the trace is actually queryable and actually
linked, the same way `client.search_traces(locations=[experiment_id],
filter_string="request_metadata.\`mlflow.sourceRun\` = '<run_id>'")`
returns something non-empty. A run that logs without erroring is not
proof the UI will show anything under it.

## Windows console note

`mlflow.start_run()`'s exit path and `MlflowClient().set_terminated()` print
an emoji banner ("🏃 View run..."). Windows' default console codepage can't
encode it and the process crashes on exit, *after* the actual logging
already succeeded. Add this once, near the top of `main()`, before any
mlflow call:

```python
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

## When re-running to add tracing to something that already ran

If a comparison already produced valid metrics but no traces (because this
skill wasn't followed the first time), do not blindly re-run the whole
matrix to backfill it -- that spends real API budget again for data that
already exists. Fix the harness, then either: re-run only the specific
model/context combination the user actually wants inspectable, or run a
tiny 1-2 case smoke test to prove tracing now works, and say plainly that
older runs in the same experiment won't have traces retroactively.
