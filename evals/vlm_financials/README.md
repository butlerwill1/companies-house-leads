# Visual PDF VLM evaluation

The JSON files in `cases/` are the source of truth. PDFs remain local and are
identified by path and SHA-256. Do not mark a case verified until every current
and previous metric has been checked against the PDF.

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

To compare batching patterns without making a copied mini-dataset, select exact
reviewed Companies House numbers. The helper below runs its three scenarios
sequentially so their timing is not affected by client-side concurrency:

```powershell
.\scripts\ocr\run_vlm_batching_ab_test.ps1
```

Its default four cases cover a control, a row-validation case, a locator-coverage
case and a known difficult rationalisation case. It compares locator/extractor
batches of `4/2`, `1/2` and `4/1`; each scenario has its own MLflow run and
output directory. Override `-CompanyNumbers` to repeat the experiment on a
different reviewed sample.

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

For a statement page that is returned with no rows, or whose rows fail
deterministic validation, the pipeline can make one targeted fallback call. Set
`recovery_vision_model` to a different vision model and
`recovery_render_long_edge` (default `2048`) in the configuration. Only that
single failed page is re-rendered and retried; normal extraction remains on the
primary vision model. Recovery-model timing, usage and cost are logged as the
separate `vision_recovery` stage.

When a rationaliser selects an evidence row for one period but leaves the other
period null, deterministic completion may reuse the same row only when it
visibly contains a valid counterpart value. This is recorded in
`rationalisation_policy.paired_period_completions`; it never chooses a new row
or invents a value.

Canonical candidates follow a deterministic evidence hierarchy within each
statement scope: an exact canonical row on a primary statement, an exact
insurance technical-account equivalent on an income statement, a direct
primary-statement synonym, then an exact supporting-note fallback. Model
confidence is considered only after those evidence properties. Insurance rows
must also have a compatible visible label: generic note labels such as `Total`
or `Reinsurance inwards` cannot be treated as earned premiums, claims incurred
or a technical-account result. Gross-profit components must come from the same
page, statement scope and unit. For insurance accounts, a visible technical
account result outranks profit before tax; the latter remains the fallback.
The rationaliser receives only canonical output candidates. A candidate derived
from `Shareholders' funds` or `Total equity` retains the original label and row
ID as provenance, but can only be selected through its canonical `net_assets`
ID. A legacy/raw synonym selection is translated only when an explicit,
traceable equivalent exists; aggregate rows such as `Current assets` are never
treated as cash. A Company income-statement fallback also outranks a Group
cash-flow fallback for the same canonical metric.

Standalone shareholders'-funds and total-equity rows are valid net-assets
equivalents in both Group and Company balance sheets. Their statement scope is
preserved, so direct consolidated Group equity excludes a competing standalone
Company net-assets row; Company evidence remains the fallback when no Group
equivalent is available.

Direct canonical rows must also match their statement family before filing
scope is considered: turnover/profit rows belong to an income statement, cash
belongs to a balance sheet or cash-flow statement, and net assets belongs to a
balance sheet. An operating result must have a total label such as `Operating
profit`, `Operating loss`, or `Profit from operations`; components such as `Net
operating expenses` and exchange movements remain rejected diagnostic evidence.
For a net-assets synonym, eligible standalone totals are filtered before
ranking, so `Total equity` cannot be displaced by an earlier `Share capital` or
`Retained earnings` component.

Each run may include the company’s stored SIC registration as advisory context
for the rationaliser and MLflow trace. SIC never enables a mapping or overrides
the visible statement type, source label, unit or scope. This is intentional:
a company’s registered SIC may be broad, stale or differ from the accounting
presentation in its PDF.

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

The full-document locator labels direct employee-count evidence. If it and the
embedded-text narrative-zero check find nothing, the pipeline makes one
targeted medium-resolution employee extraction over `other` pages in the notes
section after the first primary statement; it does not make a second PDF-wide
employee-locator pass. Only direct evidence pages receive the normal
high-resolution employee extraction, keeping the fallback bounded by likely
note pages rather than all document pages.

Employee evidence includes a numeric table count, an unambiguous dash in an
employee-count table, or an explicit narrative zero such as `The Company has
no employees`. Narrative zeroes retain the quoted evidence, a normalised count
of zero, and an explicit current/previous/both scope. The pipeline never
copies a narrative zero into a comparative period unless the document says it
applies to both. Staff-cost disclosures and qualified statements such as `no
employees other than directors` are rejected as employee-count evidence.

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

When a money row includes both current and comparative column headings but only
one cell was transcribed, it remains usable for its known period and triggers
the same focused recovery. The pipeline converts a comparative dash to zero
only if the recovery (or initial extraction) explicitly returns that dash; it
does not infer a zero from a missing cell.

After those checks, a confident primary-statement page can receive one further
high-resolution completeness recovery when it is only partially transcribed:
for example, a balance sheet has no net-assets/shareholders'-funds row, or an
income statement contains several core rows but omits another. A balance sheet
with a visible `Current assets` row but no cash row is also re-read, so an
omitted `Cash at bank and in hand` row is recovered rather than proxied. This is bounded
to three pages per document. The recovery re-reads the whole table and adds
only previously unseen rows; it never replaces the original page wholesale.
If two readings disagree for the same row, the original evidence remains and
the conflict is recorded for diagnosis rather than silently choosing the longer
response.

One evaluation run reports both the six core financial metrics and employees
separately: `core_financial_*` and `employees_*` metrics appear in MLflow and
the `report.json` artifact. Gold employee cells may carry an `evidence_kind`
of `numeric`, `dash_zero`, or `narrative_zero`; `report.json` provides a
separate score for each labelled kind. Optional `employee_evidence_pages` may
be added to a gold-label case to score the employee-page locator specifically;
it is not required for existing labels.

Employee-page discovery combines the visual locator with a conservative
embedded-text backstop for explicit narrative zeroes such as `has no employees`.
It ignores ambiguous wording such as `no employees other than directors`;
scanned-only pages still rely on visual discovery.

MLflow's Evaluation Runs grid shows aggregate metrics and trace inputs/outputs,
not a spreadsheet of expected versus extracted financial cells. Generate one
after any completed run (without calling a model again) to produce a complete
comparison and an error-only CSV. With `--log-mlflow`, both files are uploaded
to the run's **Artifacts** under `evaluation/current-labels-cell-report`:

```powershell
python .\scripts\ocr\vlm_financial_eval.py report-cell-errors `
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
