# Database Schema

`companies-house.db` is SQLite today, kept portable for an eventual
PostgreSQL migration per [AGENTS.md](../AGENTS.md). This document replaces
the deleted `FUTURE_SCHEMA.md`, which sketched an aspirational Postgres
schema written before the VLM pipeline existed. This one documents what is
actually in the live database, flags what is dead weight, and lays out the
concrete tables needed for multi-year history and AI-derived business
profiling.

Schema source of truth remains
[core/companies_house_sqlite.py](../core/companies_house_sqlite.py)
(`SCHEMA_SQL` plus the `ensure_*_columns` additive migrations run from
`init_db`). Read this document for the *why*; read that file for the exact
current column list.

## Current schema, grouped by role

### Ingestion and entity

- **`leads`** (255,921 rows) — the full funnel from `scripts/ingestion`,
  filtered from Companies House bulk data. `status` tracks
  `pending` / `no_xhtml` / `done` / `error` through enrichment. Pre-enrichment
  `lead_score` / `score_reasons` live here.
- **`companies`** (8,169 rows) — one row per enriched company.
  `profile_payload` is the full CH `/company/{number}` API response as JSON
  text. `sic_codes` (JSON array) and `sic_code_primary` are lifted out of
  that payload so SIC is queryable without JSON parsing;
  `sic_code_primary` is what joins to `sic_groups`. Note the other SIC
  source, `leads.sic_1`, comes from the bulk snapshot and packs the code and
  its label into one string ("45111 - Sale of new cars..."); the two agree
  on 8,167 of 8,169 companies, so either works, but the API columns are
  authoritative and cleaner to join on.

### Filing history and documents

- **`filings`** (8,169 rows) — one row per filing-history transaction.
  **Currently accounts-only**: live data is 8,157 `AA` + 12 `AAMD`, because
  `scripts/enrichment` only walks to the latest accounts filing. See
  [Design note: filings vs documents](#design-note-filings-vs-documents).
- **`documents`** (8,167 rows) — one row per document attached to a filing.
  `xhtml_url` / `pdf_url` are always populated (8,167/8,167);
  `downloaded_xhtml_path` / `downloaded_pdf_path` are **never** populated
  (0/8,167) — nothing in the current pipeline writes to disk through this
  path. The VLM pipeline reads PDFs from `vlm-noxhtml-pdfs/` by filename
  pattern (`{company_number}-{document_id}.pdf`) and never writes back to
  this table. Recommend dropping these two columns or repurposing them (see
  [Migration path](#migration-path)).

### Financial extraction — XHTML path

- **`financial_period_summaries`** (16,351 rows) — fixed-column metrics
  (`turnover`, `gross_profit`, `operating_result`, `profit_after_tax`,
  `cash`, `net_assets`, `employees`) per `(company_number, document_id,
  period_type)`. Exactly 2 rows per company today (current + comparative
  period from the one filing that gets fetched) — see
  [Multi-year history](#multi-year-history-is-the-actual-gap).
  `data_source`: 16,334 `xhtml`, 17 `vlm`.
- **`fx_rates`**, **`financial_period_conversions`** — defined in
  `SCHEMA_SQL` but **not yet present in the live database**; `init_db`
  creates them with `create table if not exists` the next time any
  enrichment write path runs. Not urgent, just noted so it isn't mistaken
  for schema drift you need to fix by hand.

### Narrative extraction — XHTML path

- **`narrative_runs`** (2,962 rows), **`narrative_sections`** (17,864 rows),
  **`performance_statements`** (153,283 rows) — active. This is where
  `principal_activity`, `going_concern`, `strategic_report`,
  `directors_report`, `results_and_dividends`, `principal_risks`,
  `future_developments`, `business_review`, `post_balance_sheet` text
  currently comes from, parsed out of XHTML by
  [core/companies_house_pdf_text.py](../core/companies_house_pdf_text.py).
  `narrative_runs.ocr_requested` / `ocr_used` / `ocr_engine_used` are
  vestigial: `core/companies_house_extractor.py` hardcodes
  `"ocr_financials": {}`, so these are always `0` / `null` / `null`. Cheap to
  leave, fine to strip in a later migration.

### Financial extraction — VLM/PDF path

- **`vlm_financial_extraction_runs`** (10 rows) — one row per PDF run: models
  used, cost, full raw payloads.
- **`vlm_financial_metrics`** (180 rows) — EAV-shaped, one row per `(run,
  period_type, metric_name)`. See
  [Design note: EAV vs fixed columns](#design-note-eav-vs-fixed-columns).

Neither table is wired into `companies_house_mcp` yet (only
`search_leads`, `get_company_snapshot`, `search_narrative_sections`,
`compare_companies`, etc. exist in `contract.py`). Worth adding once volume
justifies it.

### Commercial scoring

- **`sic_groups`** (103 rows) — `sic_code -> sic_label, sic_group` lookup
  only. This replaces `ppc_ratio_rules`, which additionally carried a flat
  `annual_ppc_ratio` per SIC code. That ratio (turnover x a fixed
  percentage per code) was removed: a single SIC code covers businesses
  with wildly different acquisition models and margins (a gym and a
  football club both sit under "sport / fitness"; a staffing agency and a
  product company both sit under "software / IT"), so the flat percentage
  produced estimates that didn't survive contact with real companies (e.g.
  implying a football club running a -40% operating margin should spend
  £11k/month on member-acquisition PPC). See `sql/README.md` for the
  reasoning and `tmp/dropped-tables/` for the exported data.
- **`company_signals`** — Gate A entity-triage output, written by
  `scripts/analysis/ch_company_triage.py` via `core/company_triage.py`. EAV
  shaped, one row per `(company_number, signal_key)`; current keys are
  `trading_status`, `trading_status_reason`, `duplicate_of`,
  `revenue_per_employee`, `revenue_per_employee_flagged`,
  `turnover_without_employees`, `sic_is_catch_all`, `name_suggests_holding`,
  `gross_margin_pct`. Every rule is deterministic and reads only stored
  data, so the pass is free and re-runnable; run it after any enrichment
  batch or history backfill. On the live data it classifies 2,524 trading /
  104 holding / 351 dormant / 31 non-trading / 5,159 unknown, and finds 56
  duplicate entities (one business consolidated twice, e.g. JOHN BANKS
  GROUP HOLDINGS against JOHN BANKS LIMITED at £120,564,355 each).
  `unknown` is large and honest: 4,729 current-period rows carry neither
  turnover nor employees.
- **`website_investigations`** (50 rows), **`website_signals`** (1,600 rows)
  — browser-pilot website findings. `website_signals` is the EAV pattern
  `company_signals` reuses.

### Deprecated

- **`ocr_financial_period_summaries`** — dropped. It was dead: the insert in
  `core/companies_house_sqlite.py` only fired from
  `payload["ocr_financials"]["by_period"]`, and
  `core/companies_house_extractor.py:534` hardcodes that key to `{}`. Its
  1,228 rows were frozen from before local OCR was removed (AGENTS.md: "No
  local OCR runs anywhere in this repository"), exported to
  `tmp/dropped-tables/ocr_financial_period_summaries.csv` before the drop.
  The dead insert loop, its schema block, its index, and its entry in the
  `financial_year` additive migration were removed from
  `core/companies_house_sqlite.py`; the stale comparison-mode helper reading
  it in `scripts/vlm/ch_vlm_financial_sample.py` was removed too.
- **`ppc_ratio_rules`**, **`ppc_company_estimates`** — dropped (2,322 and
  103 rows exported to `tmp/dropped-tables/` first). See "Commercial
  scoring" above. The `get_top_ppc_candidates` MCP tool was removed with
  them; `get_company_snapshot`, `explain_lead_score`, and
  `compare_companies` no longer surface a PPC estimate.

## Design note: filings vs documents

`filings` is the general filing-history ledger — every event the company has
ever filed, most of which have no extractable document at all
(confirmation statements, officer appointments, charges registered or
satisfied, resolutions). `documents` is the subset of filings that have a
downloadable artifact, mainly accounts filings.

They look duplicated today only because the fetch logic narrows to
`category=accounts`, latest filing only. Broadening `filings` ingestion to
the full history is low cost (one more API call already available) and is
the direct source of several free marketing signals: confirmation-statement
lateness, charge registration/satisfaction timing, officer turnover
frequency, dormant-account cadence. None of it requires reading a PDF. This
is scoped as migration step 4 below.

## Design note: EAV vs fixed columns

`financial_period_summaries` (fixed columns) and `vlm_financial_metrics`
(EAV) look like duplication but serve different purposes and both are kept
deliberately:

- Fixed columns are cheap to query for dashboards and the known,
  stable metric set (`turnover`, `cash`, `net_assets`, ...).
- EAV is cheap to extend — adding a new metric (e.g. one of the tier-5
  balance-sheet rows currently discarded as `unclassified`, or a new
  business-profile signal) is an insert, not a migration.

`website_signals` already uses this EAV shape
(`signal_key` / `signal_value_type` / `signal_bool` / `signal_int` /
`signal_real` / `signal_text`). The new `company_signals` table below reuses
it rather than inventing a third pattern.

## New tables — free signals (Companies House API, no AI/PDF cost)

These come from data you already have API access to and are not currently
captured anywhere.

```sql
create table if not exists officers (
    officer_id text primary key,
    company_number text not null,
    name text not null,
    role text,
    appointed_on text,
    resigned_on text,
    nationality text,
    occupation text,
    other_appointments_count integer,
    officer_payload text not null,
    foreign key(company_number) references companies(company_number)
);

create table if not exists psc (
    psc_id text primary key,
    company_number text not null,
    name text,
    kind text,
    notified_on text,
    ceased_on text,
    nature_of_control text,
    psc_payload text not null,
    foreign key(company_number) references companies(company_number)
);

create table if not exists charges (
    charge_id text primary key,
    company_number text not null,
    status text not null,
    classification text,
    created_on text,
    satisfied_on text,
    persons_entitled text,
    charge_payload text not null,
    foreign key(company_number) references companies(company_number)
);

create table if not exists company_signals (
    id integer primary key autoincrement,
    company_number text not null,
    signal_key text not null,
    signal_value_type text not null,
    signal_bool integer,
    signal_int integer,
    signal_real real,
    signal_text text,
    source_scope text not null default 'api',
    created_at text not null,
    updated_at text not null,
    unique(company_number, signal_key),
    foreign key(company_number) references companies(company_number)
);
```

`company_signals` examples: `officer_count_active`, `officer_turnover_1y`,
`psc_has_corporate_entity`, `charges_outstanding_count`,
`previous_name_count`, `accounts_overdue`,
`confirmation_statement_days_overdue`. `officers` / `psc` / `charges` hold
the raw entities (needed for name-level lookups like related-party or
referral-partner surfacing); `company_signals` holds the derived scalars
the MCP layer actually queries against — same split as
`vlm_financial_extraction_runs`/`vlm_financial_metrics` vs
`financial_period_summaries`.

## New tables — AI-derived business profile (separate harness)

Scoped as its own text-only stage reading persisted narrative/vision output,
not folded into the financial VLM prompts — see the reasoning on cost and
benchmark isolation in the prior discussion of this project. Keyed by
`(company_number, financial_year)` because trading/group status can change
year to year, not just by `company_number`.

```sql
create table if not exists company_profiles (
    id integer primary key autoincrement,
    company_number text not null,
    document_id text,
    financial_year integer,
    source text not null,               -- 'xhtml' | 'vlm' | 'api_only'
    trading_status text,                -- dormant | non_trading | holding_only | spv | trading
    trading_status_confidence real,
    group_role text,                    -- parent | subsidiary | standalone | ultimate_parent
    group_role_confidence real,
    principal_activity_verbatim text,
    principal_activity_page integer,
    business_description text,
    sector_tags text,                   -- json list, multi-label
    customer_model text,                -- b2b | b2c | public_sector | mixed
    delivery_model text,                -- product | service | saas | contracting | distribution | ecommerce | retail | wholesale
    revenue_model text,                 -- project | retainer | subscription | transactional | rental
    geography_served text,              -- local | regional | uk | international
    sic_agreement integer,
    sic_agreement_reason text,
    extraction_method text not null,
    extraction_model text,
    generated_at text not null,
    unique(company_number, financial_year),
    foreign key(company_number) references companies(company_number),
    foreign key(document_id) references documents(document_id)
);

create table if not exists company_subsidiaries (
    id integer primary key autoincrement,
    parent_company_number text not null,
    subsidiary_name text not null,
    subsidiary_company_number text,     -- matched later where possible, nullable
    relationship text,                  -- subsidiary | associate | joint_venture
    ownership_percentage real,
    principal_activity text,
    registered_office text,
    source_document_id text,
    source_page integer,
    extraction_method text not null,
    foreign key(parent_company_number) references companies(company_number)
);
```

`company_subsidiaries` operationalises the point from the earlier
discussion: the balance sheet is already transcribed at high resolution by
the existing VLM vision stage, and rows like `Investments in subsidiary
undertakings` or the subsidiaries note are currently discarded as
`unclassified` (tier 5) rather than persisted. Persisting them here is
near-zero marginal cost against the existing pipeline.

## Multi-year history is the actual gap

`financial_period_summaries` has exactly 2 rows per company today — current
and comparative period, from the single latest accounts filing. "One year
isn't a good indicator" isn't a schema problem: none of the tables above
need a `financial_year` array or extra columns to hold history, they're
already shaped as one-row-per-period. The gap is that
`scripts/enrichment` only ever walks to the newest accounts filing. Getting
multi-year history means walking `filing-history?category=accounts` back
further and inserting one `filings` + `documents` +
`financial_period_summaries` row per historical filing — a change to the
enrichment fetch logic, not to the schema.

## Migration path

1. ~~`DROP TABLE ocr_financial_period_summaries`~~ — done.
2. Optional: strip `narrative_runs.ocr_requested` / `ocr_used` /
   `ocr_engine_used`. Low priority, cheap to leave.
3. Drop or repurpose `documents.downloaded_xhtml_path` /
   `downloaded_pdf_path` (currently always null).
4. Broaden `scripts/enrichment` to fetch full filing history, not just
   `category=accounts` — unlocks the free `company_signals` inputs and is a
   prerequisite for step 7.
5. Add `officers`, `psc`, `charges`, `company_signals` — free, API-only, no
   AI cost. Backfill against all 8,169 enriched companies immediately since
   there's no per-document spend involved.
6. Add `company_profiles`, `company_subsidiaries` — AI-derived, own gold
   set, own harness, reads persisted narrative/vision output rather than
   re-rendering PDFs.
7. Extend `scripts/enrichment` to walk filing history beyond the latest
   accounts filing, inserting one row per historical period into `filings`
   / `documents` / `financial_period_summaries`.

## Related

- [../AGENTS.md](../AGENTS.md) — repository conventions and workflow rules.
- [../scripts/vlm/README.md](../scripts/vlm/README.md) — VLM extraction
  pipeline this schema feeds.
- [../core/companies_house_sqlite.py](../core/companies_house_sqlite.py) —
  exact current schema and migrations.
