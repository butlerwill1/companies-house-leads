# Future Schema Sketch

This is a practical schema for a Companies House extraction project that may
later support OCR, narrative analysis, lead scoring, embeddings, and AI-agent
workflows.

## Recommendation

Use PostgreSQL as the main database.

- relational tables for stable entities
- `jsonb` for flexible payloads and extracted document structures
- `pgvector` later for embeddings

## Core tables

### `companies`

One row per legal entity.

Key columns:

- `company_number` `text primary key`
- `company_name` `text not null`
- `company_status` `text`
- `company_type` `text`
- `jurisdiction` `text`
- `date_of_creation` `date`
- `registered_office_postcode` `text`
- `registered_office_country` `text`
- `sic_codes` `text[]`
- `accounts_next_due_on` `date`
- `accounts_last_made_up_to` `date`
- `confirmation_statement_next_due_on` `date`
- `source_payload` `jsonb`
- `created_at` `timestamptz not null default now()`
- `updated_at` `timestamptz not null default now()`

Indexes:

- primary key on `company_number`
- index on `company_status`
- GIN index on `sic_codes`

### `company_aliases`

Useful for brand names, alternate spellings, and matching domains to legal entities.

Key columns:

- `id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `alias_type` `text not null`
- `alias_value` `text not null`
- `confidence_score` `numeric(5,4)`
- `created_at` `timestamptz not null default now()`

Examples:

- brand name
- prior company name
- website domain
- manual override

### `company_profiles`

Snapshot-style profile records if you want to keep history over time.

Key columns:

- `id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `fetched_at` `timestamptz not null`
- `profile_version_hash` `text`
- `payload` `jsonb not null`

Indexes:

- unique on `(company_number, fetched_at)`

### `filings`

One row per filing event.

Key columns:

- `transaction_id` `text primary key`
- `company_number` `text not null references companies(company_number)`
- `filing_date` `date`
- `category` `text`
- `type` `text`
- `description` `text`
- `action_date` `date`
- `pages` `integer`
- `document_metadata_url` `text`
- `filing_payload` `jsonb not null`
- `created_at` `timestamptz not null default now()`

Indexes:

- index on `company_number`
- index on `(company_number, filing_date desc)`
- index on `category`

### `documents`

One row per document attached to a filing.

Key columns:

- `document_id` `text primary key`
- `transaction_id` `text references filings(transaction_id)`
- `company_number` `text not null references companies(company_number)`
- `document_type` `text`
- `filename` `text`
- `pages` `integer`
- `has_xhtml` `boolean not null default false`
- `has_pdf` `boolean not null default false`
- `document_metadata` `jsonb not null`
- `xhtml_url` `text`
- `pdf_url` `text`
- `downloaded_pdf_path` `text`
- `downloaded_xhtml_path` `text`
- `created_at` `timestamptz not null default now()`

Indexes:

- index on `company_number`
- index on `transaction_id`

### `financial_metrics`

Normalized structured metrics extracted from XHTML.

Key columns:

- `id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `document_id` `text not null references documents(document_id)`
- `period_end_on` `date`
- `period_type` `text not null`
- `metric_name` `text not null`
- `metric_value` `numeric`
- `currency_code` `text default 'GBP'`
- `unit_type` `text default 'currency'`
- `context_ref` `text`
- `source_method` `text not null`
- `created_at` `timestamptz not null default now()`

Examples:

- turnover
- gross_profit
- operating_result
- profit_after_tax
- current_assets
- employees

Indexes:

- unique on `(document_id, period_type, metric_name)`
- index on `company_number`
- index on `metric_name`

### `financial_period_summaries`

Convenience table for one-row-per-period analytics.

Key columns:

- `id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `document_id` `text not null references documents(document_id)`
- `period_end_on` `date not null`
- `turnover` `numeric`
- `gross_profit` `numeric`
- `operating_result` `numeric`
- `profit_after_tax` `numeric`
- `cash` `numeric`
- `net_assets` `numeric`
- `employees` `numeric`
- `gross_margin_pct` `numeric`
- `operating_margin_pct` `numeric`
- `net_margin_pct` `numeric`
- `current_ratio` `numeric`
- `summary_payload` `jsonb`

Indexes:

- unique on `(company_number, period_end_on)`

## Narrative and OCR tables

### `document_text_runs`

Raw extracted text per document and method.

Key columns:

- `id` `bigserial primary key`
- `document_id` `text not null references documents(document_id)`
- `company_number` `text not null references companies(company_number)`
- `text_source` `text not null`
- `ocr_engine` `text`
- `ocr_version` `text`
- `page_count` `integer`
- `text_quality_score` `numeric`
- `payload` `jsonb`
- `created_at` `timestamptz not null default now()`

`text_source` examples:

- `xhtml_visible_text`
- `pdf_text`
- `ocr`

### `narrative_sections`

Structured sections extracted from reports.

Key columns:

- `id` `bigserial primary key`
- `document_id` `text not null references documents(document_id)`
- `company_number` `text not null references companies(company_number)`
- `section_key` `text not null`
- `section_title` `text`
- `page_start` `integer`
- `page_end` `integer`
- `section_text` `text`
- `section_payload` `jsonb`
- `extraction_method` `text not null`
- `created_at` `timestamptz not null default now()`

Examples:

- strategic_report
- directors_report
- principal_activity
- going_concern
- future_developments

Indexes:

- index on `company_number`
- index on `section_key`
- full text index on `section_text`

### `performance_statements`

Sentence-level or paragraph-level subjective statements.

Key columns:

- `id` `bigserial primary key`
- `document_id` `text not null references documents(document_id)`
- `company_number` `text not null references companies(company_number)`
- `page_number` `integer`
- `statement_text` `text not null`
- `statement_type` `text`
- `sentiment_label` `text`
- `confidence_score` `numeric`
- `created_at` `timestamptz not null default now()`

Examples:

- management performance commentary
- growth claims
- cash/liquidity comments
- client demand comments

## People and control tables

### `officers`

- `officer_id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `name` `text not null`
- `role` `text`
- `appointed_on` `date`
- `resigned_on` `date`
- `officer_payload` `jsonb`

### `psc`

- `psc_id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `name` `text`
- `kind` `text`
- `notified_on` `date`
- `ceased_on` `date`
- `nature_of_control` `text[]`
- `psc_payload` `jsonb`

## Commercial / AI tables

### `lead_scores`

Derived commercial scoring separate from source truth.

Key columns:

- `id` `bigserial primary key`
- `company_number` `text not null references companies(company_number)`
- `score_type` `text not null`
- `score_value` `numeric not null`
- `score_reasoning` `jsonb`
- `scored_at` `timestamptz not null default now()`

Examples:

- account quality score
- likely paid media spender
- outreach priority
- financial risk

### `document_chunks`

Chunks for embeddings and retrieval.

Key columns:

- `id` `bigserial primary key`
- `document_id` `text not null references documents(document_id)`
- `company_number` `text not null references companies(company_number)`
- `section_id` `bigint references narrative_sections(id)`
- `chunk_index` `integer not null`
- `chunk_text` `text not null`
- `token_count` `integer`
- `metadata` `jsonb`
- `created_at` `timestamptz not null default now()`

Indexes:

- unique on `(document_id, chunk_index)`
- full text index on `chunk_text`

### `embeddings`

Add this only when you actually need semantic retrieval.

Key columns:

- `id` `bigserial primary key`
- `chunk_id` `bigint not null references document_chunks(id)`
- `embedding_model` `text not null`
- `embedding_dim` `integer not null`
- `embedding` `vector`
- `created_at` `timestamptz not null default now()`

Indexes:

- vector index on `embedding`

## Why this design

This keeps the stable parts normalized:

- company
- filing
- document
- metrics

And keeps the less stable parts flexible:

- raw API payloads
- OCR payloads
- narrative extraction payloads
- scoring payloads

That is usually better than going NoSQL-first.

## Development path

### Phase 1

- `companies`
- `filings`
- `documents`
- `financial_metrics`

### Phase 2

- `financial_period_summaries`
- `document_text_runs`
- `narrative_sections`

### Phase 3

- `lead_scores`
- `document_chunks`
- `embeddings`

## Local development note

For a slower machine:

- use Docker with a single Postgres container
- keep memory limits modest
- avoid running OCR workers continuously

If Postgres still feels heavy during very early development, SQLite is fine for
initial prototyping, but PostgreSQL should be the target database once you want:

- concurrent jobs
- richer indexing
- `jsonb`
- full text search
- `pgvector`
- cleaner Docker deployment parity
