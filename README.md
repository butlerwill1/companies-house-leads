# Companies House Leads

## Financial currencies and GBP analysis

Financial extraction preserves the currency and scale shown in the filing.  A
reported USD or EUR figure is never treated as sterling.  Run the separate,
resumable enrichment only when GBP analytical values are needed:

```powershell
python .\scripts\analysis\enrich_financial_fx.py --db .\companies-house.db --currency USD --from 2024-01-01 --to 2024-12-31
```

It imports immutable Bank of England daily indicative spots and uses the
period-end rate, or the nearest prior published rate within ten calendar days.
Original reported values remain authoritative; missing dates, unsupported
currencies and conflicting evidence remain unconverted and are excluded from
PPC sterling thresholds.

API-first Companies House extraction tool for:

- company search
- company profile lookup
- filing history lookup
- accounts document metadata lookup
- XHTML/iXBRL financial extraction
- optional PDF narrative extraction
- optional local SQLite storage

The default path is the official Companies House API only. The website scraper
has been split into [companies_house_website_fallback.py](C:/Users/Will/Documents/GitHub/companies-house-leads/companies_house_website_fallback.py:1)
and is only used if you explicitly pass `--allow-website-fallback`.

## Usage

Reusable extractor/storage modules still live at the repository root. Operational
entry points now live under `scripts/` by workflow:

- `scripts/ingestion/` filters Companies House bulk CSV data into lead CSVs.
- `scripts/enrichment/` loads/enriches leads and monitors long-running batches.
- `scripts/analysis/` derives PPC estimates and imports browser investigation outputs.
- `scripts/ocr/` runs OCR/VLM PDF extraction experiments.
- `scripts/browser/` contains browser-based website investigation tooling.

Exact company number:

```powershell
python .\companies_house_extractor.py `
  --company-number 13406761 `
  --label "Sample company extract" `
  --output-json .\sample-company-extract.json `
  --output-report .\sample-company-extract-report.md
```

Search by company name:

```powershell
python .\companies_house_extractor.py `
  --query "Example Ltd" `
  --output-json .\example.json `
  --output-report .\example-report.md
```

Download source documents as well:

```powershell
python .\companies_house_extractor.py `
  --query "Example Ltd" `
  --output-json .\example.json `
  --download-dir .\downloads
```

Store extraction output in a local SQLite database:

```powershell
python .\companies_house_sqlite.py `
  --db .\companies-house.db `
  --extract-json .\sample-company-extract.json
```

Enable the parked website fallback explicitly:

```powershell
python .\companies_house_extractor.py `
  --query "Example Ltd" `
  --output-json .\example.json `
  --allow-website-fallback
```

Filter bulk data and enrich leads:

```powershell
python -m scripts.ingestion.ch_bulk_filter `
  --input-dir .\ch-data `
  --output .\data\ch-leads.csv `
  --min-score 70

python -m scripts.enrichment.ch_batch_enrich `
  --leads-csv .\data\ch-leads.csv `
  --db .\companies-house.db `
  --limit 100
```

Run the MCP server over stdio:

```powershell
python -m companies_house_mcp.server --db .\companies-house.db
```

Benchmark the financial VLM process through the private GPU's Ollama tunnel:

```powershell
# In private-llm-chat, keep this open while the benchmark runs:
.\scripts\gpu-session.ps1 -InstanceId i-0123456789abcdef0

# In this repository, in a separate shell:
python .\scripts\ocr\ch_vlm_financial_sample.py `
  --provider ollama `
  --comparison-db .\companies-house.db `
  --output-dir .\logs\vlm-financial-ollama-10 `
  --sample-size 10 `
  --locator-model <installed-vlm-model> `
  --vision-model <installed-vlm-model> `
  --rationalisation-model <installed-vlm-model>
```

The OpenRouter and Ollama options use the same page-selection, extraction and
rationalisation process. The benchmark summary records end-to-end time and the
time for each model call; OpenRouter also records token pricing, while a private
GPU reports no per-request API charge.

For a manually verified 50-PDF quality, speed and cost comparison, see
[evals/vlm_financials/README.md](C:/Users/Will/Documents/GitHub/companies-house-leads/evals/vlm_financials/README.md:1).

## API key

Put your key in `.env`:

```dotenv
COMPANIES_HOUSE_API_KEY=your_key_here
```

Or set it in the shell:

```powershell
$env:COMPANIES_HOUSE_API_KEY="your_key_here"
```

## Notes

- The extractor parses XHTML in memory even when you do not download files.
- `downloaded_files` stays empty unless you pass `--download-dir`.
- The JSON output is the main artifact for downstream processing.
- Shared narrative-section and performance-sentence extraction over plain text lives in [companies_house_pdf_text.py](C:/Users/Will/Documents/GitHub/companies-house-leads/companies_house_pdf_text.py:1).
- PDF financial extraction is now VLM-based; see [scripts/ocr/companies_house_pdf_vlm_financials.py](C:/Users/Will/Documents/GitHub/companies-house-leads/scripts/ocr/companies_house_pdf_vlm_financials.py:1) and [evals/vlm_financials/README.md](C:/Users/Will/Documents/GitHub/companies-house-leads/evals/vlm_financials/README.md:1). The earlier free-local-OCR (Tesseract/RapidOCR) path has been retired.
- Local persistence lives in [companies_house_sqlite.py](C:/Users/Will/Documents/GitHub/companies-house-leads/companies_house_sqlite.py:1).
- See [docs/API_ENDPOINTS.md](C:/Users/Will/Documents/GitHub/companies-house-leads/docs/API_ENDPOINTS.md:1) for the relevant endpoints and the recommended bulk-processing approach.
- See [docs/FUTURE_SCHEMA.md](C:/Users/Will/Documents/GitHub/companies-house-leads/docs/FUTURE_SCHEMA.md:1) for the longer-term PostgreSQL/`jsonb`/vector shape.

## Reporting rules

Useful official guidance on why some Companies House filings contain much more detail than others:

- Accounts filing guidance: https://www.gov.uk/government/publications/life-of-a-company-annual-requirements/life-of-a-company-part-1-accounts
- Small, micro and dormant company guidance: https://www.gov.uk/annual-accounts/microentities-small-and-dormant-companies
- Reporting requirements overview: https://www.gov.uk/government/calls-for-evidence/smarter-regulation-non-financial-reporting-review-call-for-evidence/annex-individual-reporting-requirements
- 2024 threshold changes: https://www.legislation.gov.uk/uksi/2024/1303/made
