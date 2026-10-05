# Companies House Leads

## Purpose

This repository identifies and enriches UK Companies House leads. It contains
Companies House API extraction, local SQLite persistence, PDF/OCR processing,
PPC and website analysis, VLM evaluation, and an MCP server for querying the
resulting data.

## Repository Map

- `core/` contains reusable extraction, PDF-text, and SQLite modules, imported
  as `core.companies_house_extractor`, etc.
- `scripts/ingestion/` filters Companies House bulk data into lead data.
- `scripts/enrichment/` loads and enriches leads through the Companies House API.
- `scripts/analysis/` converts financials to GBP and imports website investigations.
- `scripts/vlm/` contains the VLM PDF financial-extraction pipeline and its
  evaluation harness, plus `companies_house_pdf_transcribe.py`, the
  whole-document transcription harness for scanned, image-only filings (a
  vision model reads each page; the auditor's report is then dropped with the
  same rule the XHTML path uses). No local OCR runs anywhere in this repository.
- `scripts/profile/` contains the business-profile (Gate A2) pipeline: reads
  a company's filed narrative and records demand_model, customer_type,
  delivery_model, and geography_served via one text-only LLM call. See
  `scripts/profile/README.md` and `docs/BUSINESS_PROFILE_EXTRACTION.md`.
- `scripts/screen/` contains the search screen, the cheap first stage of the
  lead funnel (one question over a filing: would a customer look for this
  business online and buy, book or enquire?): gold-set case builder, evidence
  packs, review sheets and verdict import, the Langfuse review queue
  (`search_screen_queue.py`) and draft dataset (`search_screen_publish.py`), the
  paid screen (`search_screen_policy.py`, `search_screen_eval.py`) with its
  results tabs (`search_screen_results_sheet.py`), and the free baseline. See
  `docs/SEARCH_SCREEN.md`, which also holds the pre-registered definition and
  acceptance criteria.
- `scripts/web/` contains the web stage, the second stage of the lead funnel:
  for screen-passing companies, find the website and Google Maps listing
  (W1, `web_identity.py`, `web_trading_names.py`; `web_settle.py` re-judges the
  ambiguous ones from the crawled sites, `web_settle_model.py` asks a model about
  the plausible rest), crawl up to 25 pages and
  detect the marketing technology (W2, `web_crawl.py`, `web_browser.py`,
  `tech_rules.py`, `web_detect.py`), profile the business (W3,
  `web_profile_policy.py`, `web_profile_eval.py`), measure advertising and
  demand (W4, `web_market.py`), derive the talking points
  (`web_findings.py`) and hand over a lead sheet with an outcome log (W5,
  `web_handoff.py`). `web_population.py` is the command layer. Search
  providers (Serper, DataForSEO with a second account for the friend,
  SerpApi) sit behind one cached, allowance-capped client
  (`search_providers.py`). Every run that spends money or a free allowance
  needs the user's go for that run and a stated cap (the friend's account
  has no default cap and needs their agreement); the browser fallback never
  works around a block. See `docs/WEB_STAGE_PLAN.md` for the build plan and
  `docs/WEB_STAGE.md` for the pre-registered definitions and criteria.
- `companies_house_mcp/` exposes the local lead data to MCP clients.
- `evals/vlm_financials/` contains reviewed VLM evaluation cases and configurations.
- `evals/vlm_transcription/` holds the transcription harness's model configs;
  there is no transcription gold set (a second model's reading is the check).
- `evals/business_profiles/` contains business-profile gold-set cases and configs,
  in the same shape, reviewed the same way (`scripts/profile/business_profile_review.py`).
- `evals/search_screen/` contains the search-screen gold set (`cases/`,
  drafted by a model and verified by the reviewer, with a blind subset) and
  `selection.json`, the seeded record of which companies were drawn. It is
  separate from the business-profile gold set on purpose.
- `evals/web_identity/` contains the web-stage identity gold set: a seeded
  draw of 100 queue companies (`selection.json`), 25 of them labelled blind,
  each with the reviewer's true website or `none`. `evals/web_profile/`
  holds the site-profile gold set. Its first 21 cases are the test
  companies: labels and reference search phrases were drafted by Claude in
  chat (`drafts-2026-10-02.json`) and are reviewed in two Langfuse annotation
  queues (`scripts/web/web_profile_gold.py`: `cases`, `sync`, `export`).
  61 more (2026-10-04) are a seeded random draw from the companies with a
  chosen, readable site (`selection-2026-10-04.json`), drafted the same way
  (`drafts-2026-10-04.json`): 80 active cases in all.
- `docs/` holds design and schema references: `DATABASE_SCHEMA.md` for the
  live schema, `BUSINESS_PROFILE_EXTRACTION.md` for the business-profile
  LLM stage design, `SEARCH_SCREEN.md` for the search screen, `WEB_STAGE.md`
  for the web stage.
- `sql/` contains ad hoc `.sql` exploration queries against
  `companies-house.db`, meant to be run in DB Browser for SQLite or the
  `sqlite3` CLI. Not loaded by any Python code; a query that earns a place
  as a standing capability gets ported into `companies_house_mcp/service.py`
  instead. See `sql/README.md`.
- `tests/` contains the automated test suite.
- `data/` is gitignored local working data: `data/raw/` for source material
  (the Companies House bulk CSV dump, cached filing XHTML) and
  `data/processed/` for output derived from it (e.g. `scripts/ingestion/ch_bulk_filter.py`'s
  filtered lead CSVs). Nothing under `data/` is committed. Companies House's
  filed XHTML is a single unbroken line with no newlines -- readable in a
  browser but not in a text editor. Whenever a raw filing (or any similarly
  unreadable single-line document) is saved locally for a human to read,
  render it to Markdown with `to_readable_markdown()` in
  `scripts/profile/save_raw_filings.py` rather than saving the raw markup
  alone or writing a fresh one-off flattening.

## Development Workflow

- Add or update tests before implementing new behaviour where practical.
- Run `python -m pytest` after Python changes.
- Keep changes focused and preserve unrelated changes in a dirty worktree.
- Use `apply_patch` for deliberate source-file edits.
- Do not use emojis in repository content.

## Data And External Services

- SQLite is the current storage implementation; keep persistence code portable
  enough for a future PostgreSQL migration.
- Use parameterised SQL and temporary fixture databases in tests.
- Treat `.env` and API keys as secrets. Do not print or commit them.
- Do not commit generated PDFs, rendered pages, downloaded filings, databases,
  logs, temporary images, or bulk-output files.
- Do not start large enrichment batches, paid model calls, or GPU workloads
  unless the user has explicitly asked for that specific run in their
  current request. An earlier "yes" to a plan does not carry over once the
  plan changes; state the cost and ask. Free work (rescoring saved
  responses, building case stubs, analysis) needs no such approval.
- All eval harnesses share one self-hosted Langfuse instance
  (`http://localhost:3000`, the Docker Compose stack in `~/langfuse-server/`
  -- see `docs/LANGFUSE_SETUP.md`). A new harness gets its own Langfuse
  dataset (and annotation queue, if it needs review) inside that instance,
  never a second instance or a different host. Keys live in `.env`, selected
  by the `langfuse.key_env` field in each config YAML. Every eval/comparison
  run must log a per-case trace, not just aggregate scores -- see
  `.claude/skills/langfuse-eval-discipline/SKILL.md` before writing or
  running one; both rules there come from real mistakes made in this repo,
  not hypothetical risk. (MLflow, the previous tracking backend, was migrated into Langfuse
  and deleted on 2026-10-05.)
- A report or comparison spreadsheet built as a deliverable (eval summaries,
  per-case breakdowns, anything meant to be looked at or shared) belongs in
  Google Drive as a native Sheet, not just a local file -- publish it there
  as the last step, in the same turn it's built, to the
  "Projects / companies-house-leads" Drive folder. For business-profile eval
  reports the whole procedure (report JSON -> workbook -> Sheet) is the
  `publish-eval-sheet` skill in `.claude/skills/`; other spreadsheets go the
  same way, via the Google Drive connector's `create_file`. If the connector
  isn't attached in your session, say the local file is ready and ask the
  user to publish it, rather than leaving publishing unmentioned. Skip this
  only for something clearly scratch or throwaway, and say so.

## MCP Server

- The current MCP server is a read-only query interface over the lead data.
- Keep existing query tools read-only and return compact, bounded results.
- A new tool that changes data is allowed when required, but must make its
  mutation scope, confirmation behaviour, error handling, and tests explicit.
- Keep tool contract definitions, server registration, service behaviour, and
  their tests aligned when changing the MCP surface.

## Documentation

- Update `README.md` or the relevant file under `docs/` when user-facing
  commands, workflows, or data behaviour change.
