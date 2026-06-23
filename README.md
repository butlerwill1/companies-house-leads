# Companies House Leads

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

Extract narrative text from a scanned PDF using free local OCR:

```powershell
python .\companies_house_pdf_full.py `
  --pdf .\downloads\13406761-latest-accounts.pdf `
  --output-json .\narrative.json `
  --ocr-if-needed
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
- Full PDF extraction (qualitative sections + financials) lives in [companies_house_pdf_full.py](C:/Users/Will/Documents/GitHub/companies-house-leads/companies_house_pdf_full.py:1).
- Fast financial-only PDF extraction (statement pages only) lives in [companies_house_pdf_financials.py](C:/Users/Will/Documents/GitHub/companies-house-leads/companies_house_pdf_financials.py:1).
- For text PDFs, that script can extract narrative sections directly. For scanned PDFs like the sample Mesh AI filing, it can use free local OCR. The current default OCR preference is `RapidOCR`, with `Tesseract` as a fallback if installed.
- Local persistence lives in [companies_house_sqlite.py](C:/Users/Will/Documents/GitHub/companies-house-leads/companies_house_sqlite.py:1).
- See [docs/API_ENDPOINTS.md](C:/Users/Will/Documents/GitHub/companies-house-leads/docs/API_ENDPOINTS.md:1) for the relevant endpoints and the recommended bulk-processing approach.
- See [docs/FUTURE_SCHEMA.md](C:/Users/Will/Documents/GitHub/companies-house-leads/docs/FUTURE_SCHEMA.md:1) for the longer-term PostgreSQL/`jsonb`/vector shape.

## Reporting rules

Useful official guidance on why some Companies House filings contain much more detail than others:

- Accounts filing guidance: https://www.gov.uk/government/publications/life-of-a-company-annual-requirements/life-of-a-company-part-1-accounts
- Small, micro and dormant company guidance: https://www.gov.uk/annual-accounts/microentities-small-and-dormant-companies
- Reporting requirements overview: https://www.gov.uk/government/calls-for-evidence/smarter-regulation-non-financial-reporting-review-call-for-evidence/annex-individual-reporting-requirements
- 2024 threshold changes: https://www.legislation.gov.uk/uksi/2024/1303/made
