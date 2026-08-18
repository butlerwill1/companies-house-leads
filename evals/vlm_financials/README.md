# Visual PDF VLM evaluation

The JSON files in `cases/` are the source of truth. PDFs remain local and are
identified by path and SHA-256. Do not mark a case verified until every current
and previous metric has been checked against the PDF.

This document covers **how to build and run evaluations**. For what the
extraction pipeline actually does — evidence tiers, insurance handling, retry
and recovery behaviour — see [scripts/vlm/README.md](../../scripts/vlm/README.md).

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
python .\scripts\vlm\vlm_financial_eval.py initialise --db .\companies-house.db
python .\scripts\vlm\vlm_financial_review.py
```

Open `http://127.0.0.1:8765`, review all 35 development cases and 15 holdout
cases, then save them as verified. The selector uses only filings without an
XHTML URL and round-robins SIC divisions and account categories.

## Start MLflow and run an experiment

MLflow runs only as the `mlflow-local` Docker Compose stack in
`C:\Users\wwwwi\mlflow-server`, which serves `127.0.0.1:5000` from
`data\mlflow.db` and `data\artifacts`. Never start a second server with
`mlflow server` from this repository: Windows lets a second process bind port
5000 without reporting an error, and runs and traces then scatter across two
databases.

```powershell
python -m pip install -r .\requirements-eval.txt
docker compose -f C:\Users\wwwwi\mlflow-server\compose.yaml up -d

python .\scripts\vlm\vlm_financial_eval.py run `
  --config .\evals\vlm_financials\configs\openrouter-gemini.yaml `
  --output-dir .\logs\vlm-eval-openrouter-gemini
```

Use the same command with `ollama-open-weight.yaml` after opening the private
SSM tunnel. Run a three-case `--include-unreviewed` smoke test before paying for
a full evaluation. Evaluation output files, model responses and MLflow artifacts
are audit records; the gold labels are never replaced by model output.

To compare batching patterns without making a copied mini-dataset, select exact
reviewed Companies House numbers. The helper below runs its three scenarios
sequentially so their timing is not affected by client-side concurrency:

```powershell
.\scripts\vlm\run_vlm_batching_ab_test.ps1
```

Its default four cases cover a control, a row-validation case, a locator-coverage
case and a known difficult rationalisation case. It compares locator/extractor
batches of `4/2`, `1/2` and `4/1`; each scenario has its own MLflow run and
output directory. Override `-CompanyNumbers` to repeat the experiment on a
different reviewed sample.

## What a run reports

One evaluation run reports both the six core financial metrics and employees
separately: `core_financial_*` and `employees_*` metrics appear in MLflow and
the `report.json` artifact. Gold employee cells may carry an `evidence_kind`
of `numeric`, `dash_zero`, or `narrative_zero`; `report.json` provides a
separate score for each labelled kind. Optional `employee_evidence_pages` may
be added to a gold-label case to score the employee-page locator specifically;
it is not required for existing labels.

A document that continues past a coverage warning is still scored: its missing
metrics count as false negatives rather than excluding the document. A document
that fails at the vision stage carries `status: error` and an `error_stage`.

## Generate a cell-level comparison

MLflow's Evaluation Runs grid shows aggregate metrics and trace inputs/outputs,
not a spreadsheet of expected versus extracted financial cells. Generate one
after any completed run (without calling a model again) to produce a complete
comparison and an error-only CSV. With `--log-mlflow`, both files are uploaded
to the run's **Artifacts** under `evaluation/current-labels-cell-report`:

```powershell
python .\scripts\vlm\vlm_financial_eval.py report-cell-errors `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50 `
  --log-mlflow
```

The report is deliberately labelled `current-labels`: it re-scores saved model
outputs against the repository labels as they are now, and does not overwrite
the historical score recorded when the run first completed.

## Review saved results in MLflow

Import a completed benchmark as one trace per PDF, including the source PDF,
stage outputs and a 16-question gold-label form:

```powershell
python .\scripts\vlm\vlm_financial_eval.py import-traces `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50
```

Open the `Financial PDF gold-label review` queue in MLflow. MLflow embeds each
PDF in the full trace drawer; its focused Review screen does not place that PDF
directly beside the question form, so use **View full trace** while checking the
document. Completed answers can be validated and copied into the repository
gold-label JSON with:

```powershell
python .\scripts\vlm\vlm_financial_eval.py export-reviews `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50
```

## Publish the verified labels as an MLflow dataset

The repository JSON remains the source of truth. Once reviews have been exported
and verified, publish a read-only snapshot to MLflow's **Datasets** page:

```powershell
python .\scripts\vlm\vlm_financial_eval.py create-mlflow-dataset `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml
```

The dataset contains the Companies House identifiers, PDF SHA-256 hash, split,
metadata, statement pages and labelled canonical values. It intentionally does
not upload PDFs or local paths. The command is idempotent for the same label
content; create a new `--dataset-name` (for example `...-v2`) after changing
gold labels so prior experiments stay reproducible.

## Keep the review queue aligned to the selected cases

The queue is a view over MLflow traces, so it can otherwise retain documents
from previous selections. Synchronise it after changing `cases/`:

```powershell
python .\scripts\vlm\vlm_financial_eval.py sync-review-queue `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml
```

It keeps the newest existing trace for each selected case, creates a PDF-only
manual-review trace where a case has not yet been benchmarked, and removes all
other items from the review queue. It does not delete historical MLflow traces.
