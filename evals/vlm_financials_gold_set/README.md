# Visual PDF VLM evaluation

The JSON files in `cases/` are the source of truth. PDFs remain local and are
identified by path and SHA-256. Do not mark a case verified until every current
and previous metric has been checked against the PDF.

This document covers **how to build and run evaluations**. For what the
extraction pipeline actually does — evidence tiers, insurance handling, retry
and recovery behaviour — see [scripts/pdf_vision_extraction/README.md](../../scripts/pdf_vision_extraction/README.md).

## Currency contract

Financial extraction and evaluation use the amount as reported in the filing.
Each monetary metric retains its ISO currency code, displayed unit and scale,
and a `reported_value` normalised into source-currency major units. Exact scoring
compares the currency code and `reported_value` directly; it does not apply an
exchange rate. `value_pence` remains a backwards-compatible GBP-only field.
Any later GBP conversion is stored separately and does not affect extraction
accuracy.

## Create the 50 review cases

```powershell
python .\scripts\pdf_vision_extraction\vlm_financial_eval.py initialise --db .\companies-house.db
python .\scripts\pdf_vision_extraction\vlm_financial_review.py
```

Open `http://127.0.0.1:8765`, review all 35 development cases and 15 holdout
cases, then save them as verified. The selector uses only filings without an
XHTML URL and round-robins SIC divisions and account categories.

## Start Langfuse and run an experiment

Langfuse runs only as the Docker Compose stack in `~/langfuse-server/`, which
serves `http://localhost:3000` -- see
[docs/LANGFUSE_SETUP.md](../../docs/LANGFUSE_SETUP.md). Never stand up a
second instance from this repository; every stage talks to the one instance,
keyed by the `langfuse:` block in each config.

```powershell
python -m pip install -r .\requirements-eval.txt
docker compose -f $env:USERPROFILE\langfuse-server\docker-compose.yaml up -d

python .\scripts\pdf_vision_extraction\vlm_financial_eval.py run `
  --config .\evals\vlm_financials_gold_set\configs\openrouter-gemini.yaml `
  --output-dir .\logs\vlm-eval-openrouter-gemini
```

Use the same command with `ollama-open-weight.yaml` after opening the private
SSM tunnel. Run a three-case `--include-unreviewed` smoke test before paying for
a full evaluation. `--no-langfuse` scores locally and writes JSON artifacts
without a Langfuse run. Evaluation output files, model responses and the
Langfuse dataset run are audit records; the gold labels are never replaced by
model output.

Batching patterns were compared in August 2026 on four exact reviewed Companies
House numbers (a control, a row-validation case, a locator-coverage case and a
known difficult rationalisation case), run sequentially so their timing was not
affected by client-side concurrency. It compared locator/extractor batches of
`4/2`, `1/2` and `4/1`, each scenario as its own Langfuse run (via `--run-name`).
The helper script has since been removed; the runs remain in Langfuse.

## What a run reports

One evaluation run reports both the six core financial metrics and employees
separately: `core_financial_*` and `employees_*` scores appear on the Langfuse
dataset run and in the `summary.json` file. Gold employee cells may carry an `evidence_kind`
of `numeric`, `dash_zero`, or `narrative_zero`; `report.json` provides a
separate score for each labelled kind. Optional `employee_evidence_pages` may
be added to a gold-label case to score the employee-page locator specifically;
it is not required for existing labels.

A document that continues past a coverage warning is still scored: its missing
metrics count as false negatives rather than excluding the document. A document
that fails at the vision stage carries `status: error` and an `error_stage`.

## Generate a cell-level comparison

The Langfuse dataset-run view shows per-item scores and trace inputs/outputs,
not a spreadsheet of expected versus extracted financial cells. Generate one
after any completed run (without calling a model again) to produce a complete
comparison and an error-only CSV under
`<results-dir>/current-labels-cell-report/`. With `--log-langfuse` and
`--run-name`, the four headline metrics are attached as scores to that
Langfuse run:

```powershell
python .\scripts\pdf_vision_extraction\vlm_financial_eval.py report-cell-errors `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50 `
  --config .\evals\vlm_financials_gold_set\configs\openrouter-gemini.yaml `
  --run-name openrouter-qwen35-9b-... --log-langfuse
```

The report is deliberately labelled `current-labels`: it re-scores saved model
outputs against the repository labels as they are now, and does not overwrite
the historical score recorded when the run first completed.

## Review saved results in Langfuse

Import a completed benchmark as one trace per PDF, including the source PDF and
stage outputs, into the Langfuse annotation queue (15-question gold-label
score configs):

```powershell
python .\scripts\pdf_vision_extraction\vlm_financial_eval.py import-traces `
  --config .\evals\vlm_financials_gold_set\configs\openrouter-open-weight.yaml `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50
```

Open the `Financial PDF gold-label review` annotation queue at
`http://localhost:3000`; the trace drawer holds the source PDF. Completed
answers (all 15 questions annotated by a human) can be validated and copied
into the repository gold-label JSON with:

```powershell
python .\scripts\pdf_vision_extraction\vlm_financial_eval.py export-annotations `
  --config .\evals\vlm_financials_gold_set\configs\openrouter-open-weight.yaml
```

## Publish the verified labels as a dataset snapshot

The repository JSON remains the source of truth. Once reviews have been exported
and verified, publish a read-only snapshot as an immutable Langfuse dataset:

```powershell
python .\scripts\pdf_vision_extraction\vlm_financial_eval.py publish-dataset `
  --config .\evals\vlm_financials_gold_set\configs\openrouter-open-weight.yaml
```

The dataset contains the Companies House identifiers, PDF SHA-256 hash, split,
metadata, statement pages and labelled canonical values. It intentionally does
not upload PDFs or local paths. The content digest is stored on the dataset;
re-publishing with changed labels is refused -- use a new `--dataset-name`
(for example `...-v2`) so prior experiments stay reproducible.

## Keep the review queue aligned to the selected cases

The queue is a view over Langfuse traces, so it can otherwise retain documents
from previous selections. Synchronise it after changing `cases/`:

```powershell
python .\scripts\pdf_vision_extraction\vlm_financial_eval.py sync-annotation-queue `
  --config .\evals\vlm_financials_gold_set\configs\openrouter-open-weight.yaml
```

It creates a PDF-only manual-review trace (seeded with the verified case's
draft answers) where a case has none yet, tracks them in
`logs/vlm-financial-eval/annotation-traces.json`, and adds exactly the current
cases to the queue. It does not delete historical Langfuse traces.
