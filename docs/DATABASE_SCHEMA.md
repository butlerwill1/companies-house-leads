# Database Schema

`companies-house.db` is SQLite today, kept portable for an eventual
PostgreSQL migration per [AGENTS.md](../AGENTS.md). This document replaces
the deleted `FUTURE_SCHEMA.md`, which sketched an aspirational Postgres
schema written before the VLM pipeline existed. This one documents what is
actually in the live database, flags what is dead weight, and lays out the
concrete tables needed for multi-year history and AI-derived business
profiling.

Schema source of truth remains
[companies_house_core/companies_house_sqlite.py](../companies_house_core/companies_house_sqlite.py)
(`SCHEMA_SQL` plus the `ensure_*_columns` additive migrations run from
`init_db`). Read this document for the *why*; read that file for the exact
current column list.

## Current schema, grouped by role

### Ingestion and entity

- **`leads`** (255,921 rows) — the full funnel from `scripts/bulk_data_filtering`,
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
  `scripts/companies_house_enrichment` only walks to the latest accounts filing. See
  [Design note: filings vs documents](#design-note-filings-vs-documents).
- **`documents`** (8,167 rows) — one row per document attached to a filing.
  `xhtml_url` / `pdf_url` are always populated (8,167/8,167);
  `downloaded_xhtml_path` / `downloaded_pdf_path` are **never** populated
  (0/8,167) — nothing in the current pipeline writes to disk through this
  path. The VLM pipeline reads PDFs from `data/raw/pdf-only-accounts-vlm-gold-set/` by filename
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

### Narrative and whole-document text

- **`document_texts`** — the whole filed document as text, one row per
  `(document_id, source, model)`, written by
  [scripts/pdf_vision_extraction/companies_house_pdf_transcribe.py](../scripts/pdf_vision_extraction/companies_house_pdf_transcribe.py)
  (`source = 'vlm_transcription'`: a vision model read each page of a scanned,
  image-only PDF; `model` names it) and reserved for `source = 'xhtml'` rows
  (`filed_report_text()` over the filed XHTML, `model = ''`) when the
  business-profile pipeline moves to whole-document context. `raw_text` is
  every page with `--- page N ---` markers; `filed_report_text` is the same
  minus the auditor's report (`companies_house_core.companies_house_extractor.strip_auditor_report`,
  the one rule both sources share) and no markers -- the text the
  business-profile stage reads as its `filed_report` section. `status`
  (`complete` / `partial` / `error`), `illegible_pages`, `failed_pages`,
  per-page `page_usage_payload`, summed `usage_payload`, the
  `pricing_payload` snapshot and `cost_usd` / `cost_gbp` / `cost_method`
  follow `vlm_financial_extraction_runs`. `created_at` survives an upsert;
  `updated_at` moves. Foreign keys are declarative only (no
  `PRAGMA foreign_keys`), so a document absent from `documents` -- 08029548's
  paper filing, fetched straight from the filing-history API -- still gets a
  row.
- **`narrative_runs`** (2,962 rows), **`narrative_sections`** (17,864 rows),
  **`performance_statements`** (153,283 rows) — active. This is where
  `principal_activity`, `going_concern`, `strategic_report`,
  `directors_report`, `results_and_dividends`, `principal_risks`,
  `future_developments`, `business_review`, `post_balance_sheet` text
  currently comes from, parsed out of XHTML by
  [companies_house_core/companies_house_pdf_text.py](../companies_house_core/companies_house_pdf_text.py).
  `narrative_runs.ocr_requested` / `ocr_used` / `ocr_engine_used` are
  vestigial: `companies_house_core/companies_house_extractor.py` hardcodes
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
  £11k/month on member-acquisition PPC). See `saved_queries/README.md` for the
  reasoning and `data/dropped-tables/` for the exported data.
- **`company_signals`** — Gate A entity-triage output, written by
  `scripts/company_triage_and_fx/ch_company_triage.py` via `companies_house_core/company_triage.py`. EAV
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
- **`company_profiles`** — Gate A2 business-profile output, written by
  `scripts/business_profile_classifier/companies_house_business_profile.py`, keyed
  `(company_number, financial_year)`. Fixed columns, not EAV, since the
  field set is small and stable: `business_description`, then for each of
  `demand_model`, `customer_type`, `delivery_model`, `geography_served`,
  `trading_status_confirmed` a `<field>`/`<field>_confidence`/
  `<field>_quote`/`<field>_section`/`<field>_reason` group, plus
  `sic_agreement` + `sic_agreement_reason`/`sic_agreement_quote`/
  `sic_agreement_section`. The `_reason` columns and the two
  `sic_agreement` evidence columns arrived with prompt v6 and are null on
  rows written under v5 — `prompt_version` is `not null`, so which rows
  those are is always recoverable. Every non-`unclear` value is traceable to a
  verbatim quote in a named narrative section — see
  `docs/BUSINESS_PROFILE_EXTRACTION.md` and `scripts/business_profile_classifier/README.md`.

### Deprecated

- **`ocr_financial_period_summaries`** — dropped. It was dead: the insert in
  `companies_house_core/companies_house_sqlite.py` only fired from
  `payload["ocr_financials"]["by_period"]`, and
  `companies_house_core/companies_house_extractor.py:534` hardcodes that key to `{}`. Its
  1,228 rows were frozen from before local OCR was removed (AGENTS.md: "No
  local OCR runs anywhere in this repository"), exported to
  `data/dropped-tables/ocr_financial_period_summaries.csv` before the drop.
  The dead insert loop, its schema block, its index, and its entry in the
  `financial_year` additive migration were removed from
  `companies_house_core/companies_house_sqlite.py`; the stale comparison-mode helper reading
  it in `scripts/pdf_vision_extraction/ch_vlm_financial_sample.py` was removed too.
- **`ppc_ratio_rules`**, **`ppc_company_estimates`** — dropped (2,322 and
  103 rows exported to `data/dropped-tables/` first). See "Commercial
  scoring" above. The `get_top_ppc_candidates` MCP tool was removed with
  them; `get_company_snapshot`, `explain_lead_score`, and
  `compare_companies` no longer surface a PPC estimate.
- **`website_investigations`**, **`website_signals`**,
  **`website_investigation_metric_view`** — dropped 2026-10-04 (50 and 1,600
  rows exported to `data/dropped-tables/` first). They held the June browser
  pilot (`ppc_pilot_40k_60k_2026_06_16`): keyword-count signals, a PPC-fit
  score and an estimated monthly PPC spend derived from the retired SIC-ratio
  model. The web stage (`company_web_identity`, `web_sites`, `web_pages`,
  `web_technologies`) replaces them. The `get_website_investigation` and
  `find_website_signal_leads` MCP tools, and the website fields in
  `get_company_snapshot`, `explain_lead_score` and `compare_companies`, went
  with them; `scripts/company_triage_and_fx/ch_website_investigations.py` was removed.

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

`website_signals` (since dropped) used this EAV shape
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

### `company_financial_history` (view)

One row per company and financial year, chosen from `financial_period_summaries`.
That table keeps one row per `(company, document, current or previous)`, so a
year that is `current` in one filing and `previous` in the next, or appears in
an amendment, or from two sources (XHTML tags, the text fallback, the VLM), has
several rows. In practice this is rare (232 of 16,591 company-years, 1.4%, at
the time of writing; 13 of those disagreed on turnover), so the base table is
nearly one row per year already; the view exists to make that guarantee and to
carry a status, not to hold new data. It is a view, not a table, so it cannot
drift from the base table.

Selection: the row with the most of `turnover` and `profit_after_tax` filled,
then the filing's own `current` reading over a later filing's comparative, then
the newest row. Columns: the metrics, `currency_code`, `document_id`,
`period_type`, `comparative_overlap_status`, `n_rows` (how many base rows the
year had), `source` (`xhtml`, `xhtml+text` when the text fallback filled a gap,
or `vlm`) and `status` (`ok` both figures, `partial` one, `missing` neither).

Where the gaps are filled from:

| Gap | Fixed by | Written to |
|---|---|---|
| Older filing exists only as PDF or scan | `scripts/pdf_vision_extraction/history_vlm_batch.py` (VLM) | `vlm_financial_extraction_runs`, `vlm_financial_metrics`, and a `financial_period_summaries` row with `data_source = 'vlm'` |
| XHTML filing shows the figures but the tags did not give them | `scripts/pdf_vision_extraction/xhtml_text_financials.py` (text model, quote-validated) | the same audit tables (`vision_model = 'xhtml_text'`); only empty cells of the base row are filled, noted in `derived_payload.text_recovery` |
| No profit and loss statement was filed (small-company accounts) | recorded, not recoverable | the audit row has `status = 'not_filed'` |

### `company_search_screen`

Output of the search screen (docs/SEARCH_SCREEN.md): one row per company per
`(prompt_version, model, input_kind)`, written by
`scripts/search_screen_classifier/search_screen_population.py store`. A later website check reads
`passes` to skip the companies the screen rejected.

```sql
create table if not exists company_search_screen (
    id integer primary key autoincrement,
    company_number text not null,
    prompt_version text not null,    -- e.g. search-screen-v3-balanced-evidence
    model text not null,             -- e.g. openai/gpt-5.4-mini
    input_kind text not null,        -- short (principal activity + report opening) or full
    answer text,                     -- likely / possible / unlikely; null if unparseable or no filing
    passes integer not null,         -- 1 passes the screen, 0 rejected (only a clean `unlikely`)
    quote text,                      -- sentence the model cited
    quote_valid integer,             -- 1 if the quote appears verbatim in the text shown
    reason text,
    problem text,                    -- why the row failed open (unparseable, request failed, no XHTML filing)
    document_id text,                -- the Companies House filing the text came from
    text_chars integer,
    prompt_tokens integer,
    completion_tokens integer,
    screened_at text not null,
    unique(company_number, prompt_version, model, input_kind),
    foreign key(company_number) references companies(company_number)
);
```

The screen **fails open**: a response that cannot be parsed, a failed request,
or a company with no XHTML filing is stored with `passes = 1` and the cause in
`problem`. `answer = 'unlikely'` is the only value that sets `passes = 0`. A
new prompt version adds rows rather than replacing the old ones, so versions can
be compared; filter on `prompt_version` when reading.

### `company_web_identity` and `company_google_listing`

Output of web-stage W1 (docs/WEB_STAGE.md), written by
`scripts/website_analysis/web_population.py store` from the identity checkpoint. Both are
keyed by `resolver_version`, and a re-store replaces that version's rows for
the company, so resolver versions can be compared.

`company_web_identity` holds one row per checked candidate site whose tier is
above `none`. `role = 'main'` marks the chosen site when its tier is
`verified` (the company number is on the site) or `probable` (name and
postcode, or a matching Maps listing links to it). A company where nothing
was found has a single row with `domain` null, `role = 'none'` and
`tier = 'none'`. That's a result ("no website found"), not missing data.
`evidence` is the check's JSON: the pages checked, the page where the number
was found, and whether the name and postcode were found.

W1 ran under more than one resolver version: `identity-v2-trading-names`
(Maps-first, the first 32 companies), `identity-v3-places-first` (the rest)
and `identity-v4-settled`, which re-judges ambiguous companies from the whole
crawled site (`scripts/website_analysis/web_settle.py`; the rule that settled each is in
`evidence.settled_rule`). The view **`company_web_identity_current`** returns
each company's rows from whichever version wrote last; the crawl, market and
findings steps and the browsing queries in `saved_queries/` read it. A website found by
hand (`sources` contains `hand_found`) is never replaced by `store`.

```sql
create table if not exists company_web_identity (
    id integer primary key autoincrement,
    company_number text not null,
    resolver_version text not null,  -- e.g. identity-v1
    domain text,                     -- registrable domain; null on a 'none' row
    role text not null,              -- main / candidate / none
    tier text not null,              -- verified / probable / ambiguous / none
    sources text,                    -- JSON list: maps, organic, guess, number_search, redirect
    final_url text,
    evidence text,
    resolved_at text not null
);
```

`company_google_listing` holds the Google Maps listing matched to the company,
at most one per resolver version. `category` is Google's own business
category, which W3 uses in place of the SIC code. `match` records how the
listing was tied to the company: `name+postcode`, `name+domain` (its website
is the chosen site) or `name_only`.

```sql
create table if not exists company_google_listing (
    id integer primary key autoincrement,
    company_number text not null,
    resolver_version text not null,
    title text, category text,
    categories text,                 -- JSON list, primary first
    rating real, rating_count integer,
    address text, postcode text, phone text, website text, domain text,
    latitude real, longitude real, cid text, place_id text,
    match text not null,
    source text not null,            -- serper_maps / serper_places
    found_at text not null,
    unique(company_number, resolver_version)
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

### Web stage tables (W2 to W5)

Defined in `SCHEMA_SQL` (companies_house_core/companies_house_sqlite.py) and described, with
their design rules, in `docs/WEB_STAGE_PLAN.md`. All are free to recompute from
what the earlier step stored, except the model profile.

- **`company_trading_names`**: names a company sells under (`filing`,
  `website`, `maps_listing`), each with the sentence it came from. Used by W1
  to search and match under the name the business actually uses.
- **`web_pages`**: one row per fetched page, keyed by registrable `domain`
  (two companies can share a site). `page_kind` (home, service, landing,
  privacy...), `fetched_with` (http or browser), the fetch outcome
  (`blocked (403)`, `challenge page`, `parked`), and the facts extracted: SEO
  basics, forms and their providers, `tel:` links, phone numbers, call-to-action
  phrases, schema.org types. The raw page is in the gzipped page cache
  (`data/raw/website-page-snapshots/<host>/<cache_key>.json.gz`), not in SQLite.
- **`web_technologies`**: one row per technology per domain and `rule_version`,
  with `category`, where it was found (`page`, `gtm` or `both`), the evidence
  and `account_ids` (Google Ads, GA4, Tag Manager, pixel and HubSpot ids).
  Shared ids suggest a shared owner or agency.
- **`web_sites`**: one derived summary row per domain, crawl version and rule
  version: crawl status (ok, thin, blocked, unreachable, parked), the
  measurement and conversion tools found, how a customer can convert, platform,
  agency credit.
- **`company_web_profile`**: the W3 model profile per company, prompt version
  and model: summary, customer type, how customers convert, area served,
  urgency, typical sale, channel fit, Google category (`google_listing` or
  `model_assigned`), seed phrases; each quoted label has `*_quote` and
  `*_quote_valid`.
- **`company_market`**, **`company_keyword_market`**, **`serp_observations`**:
  W4. Advertising now (Ads Transparency), demand for the seed phrases (searches
  and cost per click, a ceiling not a spend), organic traffic estimate, and an
  optional live check; `company_market` also carries the rollup written by the
  findings step (`gap_count`, `strength_count`, `setup_level`, `gap_segment`).
- **`company_setup_findings`**: one row per finding per company and version:
  `kind` gap (a pitch point) or strength, a plain-English `detail`, and the
  `evidence` signals behind it.
- **`lead_outcomes`**: what happened to each lead handed over on a sheet
  (contacted, replied, meeting, won). The gold set for the ranking stage.

`company_google_listing` also carries `is_claimed` and `category_ids`; an
existing database gets them from `ensure_google_listing_columns`.

## AI-derived business profile — built

`company_profiles` (schema documented above, under "Commercial scoring")
is live, written by `scripts/business_profile_classifier/companies_house_business_profile.py`.
It is its own text-only stage reading persisted narrative output, not folded into the
financial VLM prompts, for the same reasons as always: cost isolation and a
separate gold set the financial benchmark can't contaminate. Keyed by
`(company_number, financial_year)` because trading status can change year to
year, not just by `company_number` — see `RAPT LEISURE`'s own narrative
recording a shift to consultancy-only within one filing, in
`docs/BUSINESS_PROFILE_EXTRACTION.md`.

Still just an idea, not built: **`company_subsidiaries`**. The balance sheet
is already transcribed at high resolution by the VLM vision stage, and rows
like `Investments in subsidiary undertakings` or the subsidiaries note are
currently discarded as `unclassified` (tier 5) rather than persisted.
Persisting them would be near-zero marginal cost against the existing
pipeline, but no schema or harness exists for it yet.

## Multi-year history — built

`ch_backfill_history.py` (`scripts/companies_house_enrichment/`) walks
`filing-history?category=accounts` back up to `--years` (default 5) /
`--max-filings` (default 4) filings per company and inserts one `filings` +
`documents` + `financial_period_summaries` row per historical period,
deduped by real accounting period rather than calendar year. Each
backfilled period is cross-checked against the adjacent filing's own
reading for the same period (`financial_period_summaries.comparative_
overlap_status`/`_payload`) — a free accuracy signal needing no labelled
data, and the mechanism that caught the employee-count scale bug described
above.

## Migration path

1. ~~`DROP TABLE ocr_financial_period_summaries`~~ — done.
2. Optional: strip `narrative_runs.ocr_requested` / `ocr_used` /
   `ocr_engine_used`. Low priority, cheap to leave.
3. Drop or repurpose `documents.downloaded_xhtml_path` /
   `downloaded_pdf_path` (currently always null).
4. ~~Broaden `scripts/companies_house_enrichment` to fetch full filing history~~ — done for
   accounts (`ch_backfill_history.py`). `officers` / `psc` / `charges` below
   would need the same broadening for non-accounts filing categories;
   neither those tables nor that broadening exist yet.
5. Add `officers`, `psc`, `charges` — free, API-only, no AI cost, not yet
   built. `company_signals` (the table these were meant to feed) already
   exists and is populated by Gate A triage instead
   (`companies_house_core/company_triage.py`), from data already on hand — the officer/PSC
   signals would be additive, not a prerequisite.
6. ~~Add `company_profiles`~~ — done. `company_subsidiaries` still open, see
   above.
7. ~~Extend `scripts/companies_house_enrichment` to walk filing history~~ — done
   (`ch_backfill_history.py`).

## Related

- [../AGENTS.md](../AGENTS.md) — repository conventions and workflow rules.
- [../scripts/pdf_vision_extraction/README.md](../scripts/pdf_vision_extraction/README.md) — VLM extraction
  pipeline this schema feeds.
- [../companies_house_core/companies_house_sqlite.py](../companies_house_core/companies_house_sqlite.py) —
  exact current schema and migrations.
