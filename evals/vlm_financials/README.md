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
