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
  evaluation harness. No local OCR runs anywhere in this repository.
- `companies_house_mcp/` exposes the local lead data to MCP clients.
- `evals/vlm_financials/` contains reviewed VLM evaluation cases and configurations.
- `docs/` holds design and schema references: `DATABASE_SCHEMA.md` for the
  live schema, `BUSINESS_PROFILE_EXTRACTION.md` for the planned business-profile
  LLM stage.
- `sql/` contains ad hoc `.sql` exploration queries against
  `companies-house.db`, meant to be run in DB Browser for SQLite or the
  `sqlite3` CLI. Not loaded by any Python code; a query that earns a place
  as a standing capability gets ported into `companies_house_mcp/service.py`
  instead. See `sql/README.md`.
- `tests/` contains the automated test suite.

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
  unless the task asks for them.

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
