---
name: export-shortlist
description: Prepare and publish a shortlist of Companies House leads to Google Sheets with source links and evidence caveats; keep contact notes and outcomes outside SQLite.
---

Build rows from the user's selection using the read-only MCP tools. Include
company number as text, registered name, trading name where supported,
website, Companies House link, financial year, turnover/profit, business
description, identity confidence, fit reason, evidence URLs/dates and missing
research. Keep identifiers with leading zeros and unknown values blank.
Keep observed facts separate from model judgements and proposed talking points.

Add notes or contact/outcome columns in the Sheet when useful to the user;
do not write them to SQLite. Updating an existing Sheet must preserve its
notes and outcomes, match by company number, and target explicit ranges.

Follow AGENTS.md: a shared report/shortlist deliverable belongs in native
Google Sheets in `Projects / companies-house-leads`. Use an available
Google Drive/Sheets connector and verify publication by reading back a small
range. Access to Sheets is separate from the local MCP server; do not assume
the friend's account has the original owner's folder or connector permissions.
If the connector is unavailable, prepare CSV/TSV, say the local file is ready,
and ask the user to import/publish it or connect Sheets. Do not claim publishing
succeeded. For an explicitly scratch export, say it is scratch.

Use plain cell values for external text so source strings are not interpreted
as spreadsheet formulas. Preserve numeric measurements as numbers and source
URLs as usable links. Publish only the requested shortlist, not the database.
