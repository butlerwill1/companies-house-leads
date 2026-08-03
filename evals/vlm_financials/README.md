# Visual PDF VLM evaluation

The JSON files in `cases/` are the source of truth. PDFs remain local and are
identified by path and SHA-256. Do not mark a case verified until every current
and previous metric has been checked against the PDF.

## Create the 50 review cases

```powershell
python .\scripts\ocr\vlm_financial_eval.py initialise --db .\companies-house.db
python .\scripts\ocr\vlm_financial_review.py
```

Open `http://127.0.0.1:8765`, review all 35 development cases and 15 holdout
cases, then save them as verified. The selector uses only filings without an
XHTML URL and round-robins SIC divisions and account categories.

## Start MLflow and run an experiment

```powershell
python -m pip install -r .\requirements-eval.txt
mlflow server --host 127.0.0.1 --port 5000

python .\scripts\ocr\vlm_financial_eval.py run `
  --config .\evals\vlm_financials\configs\openrouter-gemini.yaml `
  --output-dir .\logs\vlm-eval-openrouter-gemini
```

Use the same command with `ollama-open-weight.yaml` after opening the private
SSM tunnel. Run a three-case `--include-unreviewed` smoke test before paying for
a full evaluation. Evaluation output files, model responses and MLflow artifacts
are audit records; the gold labels are never replaced by model output.

## JSON response reliability

Each model stage uses the same reliability contract. OpenRouter configurations
can request JSON mode and Ollama uses its native JSON format. The runner then:

1. parses the response as strict JSON;
2. applies only conservative syntax repair (surrounding prose or trailing
   structural commas), without filling missing values or completing strings;
3. validates the locator, extraction or rationalisation response shape;
4. retries only that failed request up to `json_max_attempts` (two by default);
5. records every raw response, repair method, validation error, timing, usage,
   cost and provider identifier.

The extractor also enforces statement-page coverage. It distinguishes pages the
locator directly classified as an income statement, balance sheet or cash-flow
statement from neighbouring context pages added for visual context. Each direct
statement page must return at least one transcribed row. A missing or empty
statement-page result causes one focused, single-page recovery request before
rationalisation. If the focused response still omits the page entirely, the
document fails at the vision stage. If the page is explicitly returned with an
empty `rows` list, the pipeline records a coverage warning and continues with
the other evidence; missing metrics are then scored as false negatives rather
than excluding the document. The extraction trace records required, returned,
recovered, empty and missing statement pages under `statement_page_coverage`.

If all attempts fail, the PDF result has `status: error` and `error_stage`, but
retains outputs and charge from earlier successful stages. The same response
attempts appear in each MLflow stage span under `response_reliability`.

## Employee evidence and row validation

The existing full-document locator also labels direct employee-count evidence;
it does not make a second locator pass. Only those flagged pages receive a
specialised high-resolution employee extraction, so the additional cost is
normally a small number of images rather than a second PDF-wide request.

After primary-statement extraction, deterministic row checks reject evidence
with a missing source label/value, an unknown money unit, a year used as a
value, or a clear metric/label conflict (for example `cash` sourced from a
`Current assets` row). A failed check triggers one focused re-extraction of the
same rendered page. The recovered response replaces the original only if it
improves those checks; otherwise the rejected candidate and its reason remain
in the trace and cannot reach rationalisation. These checks do not pretend to
verify individual digits from pixels, so clean-looking visual transcription
errors remain model/evaluation issues rather than causing every page to be
retried.

One evaluation run reports both the six core financial metrics and employees
separately: `core_financial_*` and `employees_*` metrics appear in MLflow and
the `report.json` artifact. Optional `employee_evidence_pages` may be added to
a future gold-label case to score the employee-page locator specifically; it is
not required for existing labels.

## Review saved results in MLflow

Import a completed benchmark as one trace per PDF, including the source PDF,
stage outputs and a 16-question gold-label form:

```powershell
python .\scripts\ocr\vlm_financial_eval.py import-traces `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50
```

Open the `Financial PDF gold-label review` queue in MLflow. MLflow embeds each
PDF in the full trace drawer; its focused Review screen does not place that PDF
directly beside the question form, so use **View full trace** while checking the
document. Completed answers can be validated and copied into the repository
gold-label JSON with:

```powershell
python .\scripts\ocr\vlm_financial_eval.py export-reviews `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml `
  --results-dir .\logs\vlm-eval-openrouter-qwen35-9b-50
```

## Publish the verified labels as an MLflow dataset

The repository JSON remains the source of truth. Once reviews have been exported
and verified, publish a read-only snapshot to MLflow's **Datasets** page:

```powershell
python .\scripts\ocr\vlm_financial_eval.py create-mlflow-dataset `
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
python .\scripts\ocr\vlm_financial_eval.py sync-review-queue `
  --config .\evals\vlm_financials\configs\openrouter-open-weight.yaml
```

It keeps the newest existing trace for each selected case, creates a PDF-only
manual-review trace where a case has not yet been benchmarked, and removes all
other items from the review queue. It does not delete historical MLflow traces.
