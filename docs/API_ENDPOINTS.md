# Companies House API Endpoints

This project is built around the official Companies House APIs, not the public
website HTML.

## Core endpoints

`GET /search/companies?q=...`

- Purpose: free-text company lookup by name or number.
- Use when: a user gives you a company name and you need candidate entities.
- Returns: search hits with `company_number`, `title`, `company_status`, and address snippet.
- Example:
  `https://api.company-information.service.gov.uk/search/companies?q=mesh%20ai`

`GET /company/{company_number}`

- Purpose: the main company profile record.
- Use when: you already know the exact company number.
- Returns: legal name, status, type, SIC codes, incorporation date, registered office, accounts metadata, confirmation statement dates, and more.
- Example:
  `https://api.company-information.service.gov.uk/company/13406761`

`GET /company/{company_number}/filing-history?category=accounts&items_per_page=100`

- Purpose: list filings, typically filtered to accounts.
- Use when: you need the latest accounts filing or filing history.
- Returns: filing items with dates, types, descriptions, and `links.document_metadata` for documents.
- Example:
  `https://api.company-information.service.gov.uk/company/13406761/filing-history?category=accounts&items_per_page=100`

`GET https://document-api.company-information.service.gov.uk/document/{document_id}`

- Purpose: document metadata lookup.
- Use when: a filing has `links.document_metadata` and you need available formats.
- Returns: metadata plus `resources` such as `application/pdf` and `application/xhtml+xml`.

`GET https://document-api.company-information.service.gov.uk/document/{document_id}/content`

- Purpose: fetch the actual document.
- Use when: you want XHTML/iXBRL for parsing, or PDF for audit/download.
- Returns: the requested format, selected through the `Accept` header.
- Headers:
  - `Accept: application/xhtml+xml`
  - `Accept: application/pdf`

## Useful enrichment endpoints

`GET /company/{company_number}/officers`

- Purpose: directors and officers.
- Use when: building lead/account context or governance views.

`GET /company/{company_number}/persons-with-significant-control`

- Purpose: PSC data.
- Use when: understanding ownership and control.

`GET /company/{company_number}/charges`

- Purpose: charges and security interests.
- Use when: reviewing lending or security activity.

`GET /advanced-search/companies?...`

- Purpose: structured company filtering.
- Use when: you want to discover lots of companies by SIC, status, date range, location, or company type.
- Good parameters:
  - `company_status`
  - `company_type`
  - `incorporated_from`
  - `incorporated_to`
  - `location`
  - `sic_codes`
  - `size`

## What this project currently uses

The extractor currently relies on:

1. `search/companies`
2. `company/{company_number}`
3. `company/{company_number}/filing-history`
4. `document/{document_id}`
5. `document/{document_id}/content`

That is enough for:

- entity resolution
- profile data
- accounts filing discovery
- latest filed XHTML/PDF lookup
- financial extraction from XHTML/iXBRL

## Do you need downloads?

Usually, no.

If the filing has XHTML, this project can:

- fetch the XHTML in memory
- parse the numeric facts
- write the extracted structured data to JSON

That means the JSON output is the main usable artifact for downstream work.

Downloads are still useful when you want:

- an audit trail
- manual inspection
- to re-parse later without hitting the API again
- a PDF copy for sharing

## XHTML vs PDF

For extraction, XHTML is usually better than PDF.

Why:

- XHTML/iXBRL contains machine-readable tagged facts.
- PDF is presentation-first and harder to parse reliably.
- The JSON returned by this project is derived from XHTML, which is why it can already contain the main financial metrics you care about.

Caveat:

- XHTML often contains nearly all of the useful financial data.
- PDF may still be useful for edge-case narrative text, layout verification, or when a filing is only available as PDF.

## Best way to process many companies

Do not pull documents for every company in the country up front. Split the job into stages.

### Stage 1: Build the company universe

Use one of:

- the monthly Companies House data product snapshot for broad coverage
- `advanced-search/companies` for targeted discovery

This stage gives you:

- company number
- company name
- status
- SIC code
- incorporation date
- location

### Stage 2: Enrich the shortlist

For the companies you actually care about, fetch:

1. company profile
2. officers / PSC if needed
3. filing history
4. latest accounts document metadata

Only fetch XHTML/PDF for companies where:

- they are active
- their SIC/location fits your target
- they have relevant accounts
- you actually need financial extraction

### Stage 3: Extract and store

Recommended tables:

- `companies`
- `company_profiles`
- `filings`
- `document_metadata`
- `financial_metrics`
- `officers`
- `psc`

Recommended keys:

- `company_number` as the main company key
- `transaction_id` or document id for filings/documents

### Stage 4: Refresh incrementally

Instead of full re-runs every time:

- refresh profile/filing data on a schedule
- only re-fetch a document when the latest accounts filing changes
- store `last_seen_filing_date` and `last_seen_document_id`

## Practical batching guidance

The Companies House developer guidelines currently describe a rate limit of
`600 requests per 5 minutes`.

That means:

- batch with a queue
- keep concurrency moderate
- cache company/profile/filing responses
- avoid downloading documents unless needed

A sensible first pass is:

1. use advanced search or a seed list
2. store company numbers
3. enrich profiles in batches
4. fetch filing history only for shortlisted companies
5. fetch XHTML only for the subset where you want financial metrics

## Best approach for "lots of companies at once"

If your goal is broad lead generation:

1. seed from `advanced-search/companies` or bulk data
2. filter by SIC, status, age, geography
3. persist the company list locally
4. enrich in batches over time
5. treat accounts-document extraction as a second-pass enrichment job

If your goal is financial analysis at scale:

1. identify companies with recent accounts filings
2. fetch only their latest XHTML
3. normalize metrics into your own schema
4. refresh only when a newer filing appears

## Official docs

- API overview: https://developer.company-information.service.gov.uk/
- Public data API reference: https://developer-specs.company-information.service.gov.uk/companies-house-public-data-api/reference
- Company profile resource: https://developer-specs.company-information.service.gov.uk/companies-house-public-data-api/resources/companyprofile?v=latest
- Filing history reference: https://developer-specs.company-information.service.gov.uk/companies-house-public-data-api/reference/filing-history/list
- Advanced company search: https://developer-specs.company-information.service.gov.uk/companies-house-public-data-api/reference/search/advanced-company-search
- Document API reference: https://developer-specs.company-information.service.gov.uk/document-api/reference
- Developer guidelines: https://developer.company-information.service.gov.uk/developer-guidelines
- Data products: https://www.gov.uk/guidance/companies-house-data-products
- Streaming API overview: https://developer-specs.company-information.service.gov.uk/streaming-api/guides/overview
