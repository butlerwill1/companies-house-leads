---
name: langfuse-eval-discipline
description: Use whenever running a model evaluation, comparison, or benchmark in this repo (companies-house-leads) -- "run an evaluation," "compare models," "test model X vs Y," "try a different prompt/context," "score the gold set," or writing any new eval/comparison script under scripts/profile/ or scripts/vlm/. Also use whenever calling the Langfuse SDK directly (langfuse.Langfuse(...), lf.api.*, run_experiment, create_score, etc.) outside the existing harness functions. Two hard-learned failure modes this exists to prevent: (1) a second Langfuse instance or a stray unconfigured client getting used by accident, wasting real API spend on runs nobody can find later, and (2) an evaluation run that logs aggregate scores but zero per-case traces, which defeats the entire point of using Langfuse here and has directly frustrated the user before.
---

# Langfuse eval discipline

Two rules. Both come from real, expensive mistakes made in this repo (they
started life under MLflow; the lessons carried over). Follow the checklist
before writing or running anything that touches Langfuse.

## Why this exists

This repo runs every eval harness (`evals/vlm_financials/`,
`evals/business_profiles/`, and any new one) against **one** self-hosted
Langfuse instance: `http://localhost:3000`, the Docker Compose stack in
`~/langfuse-server/` (see `docs/LANGFUSE_SETUP.md`). A new harness is a new
**dataset** (and, if it needs review, a new annotation queue) inside that
instance -- never a new instance, never a different host. Both harnesses
currently share one project; keys come from `.env`
(`LANGFUSE_PUBLIC_KEY_<KEY_ENV>` / `_SECRET_KEY_<KEY_ENV>`), selected by the
`langfuse.key_env` field in each config YAML.

Separately: the entire reason the user wants results in Langfuse rather than a
printed table or a JSON report is so they can open a dataset run and read the
actual model conversations -- the prompt, the response, what was scored right
or wrong -- not just look at an accuracy percentage. A run with scores but no
per-case traces answers "how well" but not "show me", which is the part that
actually matters here.

## Checklist, in order

**1. Build the client from a config, never with hardcoded keys.**

```python
from scripts.eval_support.langfuse_tracing import langfuse_from_config
lf = langfuse_from_config(config)   # reads config["langfuse"], keys from .env
if lf is None:                       # disabled / unconfigured / package missing
    ...                              # fall back to local-only scoring, don't crash
```

- Never call `Langfuse(public_key=..., secret_key=...)` with literals in an
  eval script. `langfuse_from_config` is the one place that decides which
  project every stage talks to.
- Never run `docker compose ... up` for a second Langfuse stack. If the
  instance needs starting, that is the user's decision
  (`docker compose -f ~/langfuse-server/docker-compose.yaml up -d`).
- Langfuse v4 self-hosts in **events_only mode**: `lf.api.trace.get`,
  `lf.api.datasets.get_run(s)`, `lf.api.dataset_run_items.list` and the
  deprecated `lf.api.scores.get_many` all return 404. Use the experiment
  runner for dataset runs, `lf.api.scores_v3.get_many_v3` /
  `lf.api.observations.get_many` for reads, and the sidecar
  `logs/*/annotation-traces.json` maps to find review traces.

**2. Every case that gets a model call gets its own trace. No exceptions,
including rejections and errors.**

A run that only produces aggregate scores is half a job. Use the experiment
runner -- it creates one trace per dataset item automatically, links it to the
run, and attaches the scores your evaluators return:

```python
from scripts.eval_support.langfuse_runs import run_experiment, evaluation
from scripts.eval_support.langfuse_tracing import observation, flush

def task(*, item, **_):
    with observation(lf, name="llm_call", as_type="generation", model=model, input=prompt) as gen:
        result = call_model(...)             # never raises -- a failed call is a traced rejection
        gen.update(output=result)
    return {...}

def evaluate(*, output, expected_output, **_):
    return [evaluation("field.x", 1.0 if ok else 0.0, data_type="NUMERIC"), ...]

def aggregate(*, item_results, **_):
    return [evaluation(name, value, data_type="NUMERIC") for name, value in flatten_metrics(m).items()]

result = run_experiment(lf, dataset_name=DATASET, run_name=run_name,
                        task=task, evaluators=[evaluate], run_evaluators=[aggregate])
flush(lf)
```

Reference implementations: `scripts/profile/business_profile_eval.py`
(`_score_langfuse`), `scripts/vlm/vlm_financial_eval.py` (`run_evaluation`),
`scripts/profile/business_profile_context_ab.py` (`run_combination`).

Standalone traces (review seeds, backfill replacements, the migration) use
`case_trace(...)` from `scripts.eval_support.langfuse_tracing` and must still
`flush(lf)` before the process exits.

Verify it worked, don't just trust it compiled -- run one or two cheap cases
for real and confirm in the UI (or via
`lf.api.scores_v3.get_many_v3(name="field.x")`) that the traces and scores
actually landed.

## Windows console note

The Langfuse SDK and OpenTelemetry can emit non-ASCII to stderr. Windows'
default console codepage can't encode it. Add this once, near the top of
`main()`, before any Langfuse call:

```python
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

## When re-running to add tracing to something that already ran

If a comparison already produced valid scores but no per-case traces (because
this skill wasn't followed), do not blindly re-run the whole matrix to
backfill -- that spends real API budget again for data that already exists.
Fix the harness, then either re-run only the specific model/context the user
wants inspectable, or run a tiny 1-2 case smoke to prove tracing works, and
say plainly that older runs won't have traces retroactively.
