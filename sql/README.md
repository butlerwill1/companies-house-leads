# Ad hoc exploration queries

Plain `.sql` files against `companies-house.db`, meant to be opened directly
in [DB Browser for SQLite](https://sqlitebrowser.org/) (Execute SQL tab) or
run via `sqlite3 companies-house.db < sql/some_query.sql`.

`company_financial_history.sql` and `turnover_band_candidates.sql` have a
literal value near the top (a company number, a turnover range) — edit it
in place before running.

## Files

- `company_financial_history.sql` — one company's full multi-year trajectory
  (turnover, margins, net assets, year-over-year % change), plus its
  comparative-overlap status per period. Start here when sizing up a lead.
- `multi_year_companies.sql` — the same trajectory shape as
  `company_financial_history.sql`, but for every company with at least 3
  years of history at once (one row per company-year), to browse examples
  rather than pull up a single company.
- `comparative_overlap_review.sql` — every period flagged `mismatch` by the
  history backfill, with the disagreement itself. Use this to triage: a
  mismatch where turnover/gross_profit/operating_result all move together by
  a plausible amount is usually a genuine prior-year restatement (a fact
  about the company); a mismatch on one field alone by a suspiciously round
  factor (100x, 1000x) is usually an extraction bug worth reporting.
- `comparative_overlap_summary.sql` — match/mismatch counts, overall and by
  account category, to gauge how much of the backfilled data needs a look.
- `turnover_band_candidates.sql` — companies with turnover and profit data
  whose current-period turnover falls in a range, mirroring the selection
  logic in `scripts/enrichment/ch_backfill_history.py`. Useful to preview a
  cohort's size before running a real backfill.
- `backfill_coverage.sql` — how many distinct financial years each company
  has on record, to see who still only has one year of history.
- `company_triage_review.sql` — Gate A results
  (`scripts/analysis/ch_company_triage.py`) pivoted out of the
  `company_signals` EAV table, filtered to everything not classified plain
  `trading`. Read `trading_status` as evidence, not a verdict.

`sic_groups` (sic_code -> sic_label, sic_group) is what's left of the old
`ppc_ratio_rules` table — the SIC labelling is still useful context, but the
flat annual_ppc_ratio percentage it used to carry was removed: it conflated
acquisition volume, affordability, and channel fit into one number and
produced estimates that didn't survive contact with real companies (see
`tmp/dropped-tables/` for the exported data). `ppc_company_estimates` is
gone entirely.

## Relationship to the MCP server

These are exploration tools, not part of `companies_house_mcp/`. The MCP
server's read-only tools are deliberately bounded and contract-tested
(`companies_house_mcp/contract.py` + `service.py` + `tests/test_mcp_service.py`)
in a way ad hoc SQL isn't. If a query here proves useful enough to be a
standing capability (e.g. `company_financial_history.sql` as a
`get_company_financial_history` tool), port its logic into a proper
`service.py` function with a contract and a test — don't have the server
load `.sql` files directly.
