# XHTML financial recovery: scope and cost

The existing `scripts/pdf_vision_extraction/xhtml_text_financials.py` recovers
**turnover and profit after tax only** from figures visibly present in an XHTML
filing but absent from the extracted cells. It does not recover all seven
financial fields, overwrite populated values, or infer figures never filed.
Each accepted value needs a matching quote, plausible row label and known unit.

## Read-only preflight, 2026-10-12

These are candidate filings with at least one missing turnover/profit cell,
not a count of demonstrably recoverable filings. Counts use the current
database and the script's `documents_needing_recovery` selection.

| Scope | Candidate filings | Companies with gaps | Planning cost, USD |
|---|---:|---:|---:|
| Latest canonical financial year among screened companies | 146 | 146 | $1-3 |
| Existing 1,573-company history cohort, all stored filings | 3,247 | 983 | $10-30 |
| All company API profiles, all stored XHTML filings | 9,185 | 6,908 | $30-110 |

The latest-year scope was counted separately using `company_financial_history`
and an XHTML document join. The original text-recovery command selects every
eligible filing for a companies file. The new `xhtml_recovery_batch` command
freezes the exact latest-year filings and fills only their selected period;
passing company numbers to the original command would include historical gaps.

Of the history cohort's 3,247 candidates, 2,297 were locally cached. Applying
the existing text-window selector offline found 896 non-empty extracts and
1,401 empty ones; 567 extracts contained a recognised profit-and-loss heading.
Empty extracts currently skip the model call. They are not proof that figures
were never filed: unfamiliar headings or layouts can also defeat the selector.
The remaining 950 documents were not downloaded for this preflight.

## Cost assumptions

The current default is `openai/gpt-5.4-mini`. The public OpenRouter models API
and [model page](https://openrouter.ai/openai/gpt-5.4-mini), checked on
2026-10-12, priced standard input at $0.75 and output at $4.50 per million
tokens. The script limits statement text to 9,000 characters; the longest
cached prompt observed was 10,602 characters including instructions.

For planning, approximately 500-4,000 input tokens and 500-2,000 output tokens
per non-empty extract imply roughly $0.003-0.012 per actual model call.
The cohort estimate allows for uncached filings while skipping the 1,401
known empty extracts. These are estimates, not guaranteed spending caps:
response length, reasoning tokens, retries and layout-specific input sizes
can change the cost. Successful recovery rate is unknown.

The preflight made no paid calls. On 2026-10-12 the user authorized the 146
latest-year screened-company gaps, within the quoted $1-3 estimate. The batch
command uses a $3 total cap, bounded output, provider price ceilings and no
automatic paid retries. It logs per-case traces to the existing self-hosted
Langfuse instance and persists each response before changing SQLite.

## Exact latest-year batch command

```powershell
python -m scripts.pdf_vision_extraction.xhtml_recovery_batch plan --run-dir logs/xhtml-recovery/latest-screened-2026-10-12
python -m scripts.pdf_vision_extraction.xhtml_recovery_batch run --run-dir logs/xhtml-recovery/latest-screened-2026-10-12 --max-cost-usd 3
```

`plan` is read-only and writes `manifest.json`; do not recreate it during a
run. `run` requires authorization for its specific paid scope. It reads local
filings or downloads them from Companies House, writes readable Markdown copies
using `to_readable_markdown`, and records validated results in SQLite's existing
financial audit tables. Only empty turnover/profit cells of the frozen latest
period can change. Historical periods and already populated cells are retained.

`checkpoint.jsonl` contains the prompts, raw responses, validated results,
usage, cost and trace identifiers. The same `run` command resumes completed
cases without paying again; an interrupted request whose billing is unknown
retains its reserved cost and is not automatically repeated. `summary.json`
reports outcomes and cell changes relative to the frozen baseline. All these
files are local, gitignored run artifacts, not a spreadsheet deliverable.

Revalidate saved responses without further model calls:

```powershell
python -m scripts.pdf_vision_extraction.xhtml_recovery_batch revalidate --run-dir logs/xhtml-recovery/latest-screened-2026-10-12
```

This uses the original quoted text and the current validator. Identical repeated
JSON keys can be collapsed, but conflicting values remain errors. A literal
financial-table dash validates as reported zero only when it appears as a
separate cell marker in the quoted source. The original paid cost remains in
the checkpoint; additional validation audit rows have zero cost.

## Completed run, 2026-10-12

All 146 frozen latest-year filings were processed. There were 132 model calls;
14 filings had no matching statement extract and skipped the model. The
recorded total was **$0.288695**, below the $3 cap. Every filing has a local
Langfuse case trace, and every model call's prompt and response were verified
through the observations API.

The initial pass repaired 115 companies. Free revalidation repaired six more:
five explicit zero/dash cells and one response with an identically repeated
period-end key. No additional model calls were made for revalidation.

- **121 companies** now have both latest-year turnover and profit after tax in
  their selected summaries.
- **118 turnover cells** and **4 profit-after-tax cells** were filled.
- **25 summaries remain incomplete**: 7 `not_filed` results; 14 with no matching
  statement text; 3 with no usable validated missing figure; and 1 with recovered
  USD figures retained in the audit tables. Legacy integer summary updates
  currently accept whole GBP amounts only, so that USD filing awaits the
  separate currency/storage step.

Scope, responses, trace identifiers and the final counts are in
`logs/xhtml-recovery/latest-screened-2026-10-12/manifest.json`,
`checkpoint.jsonl` and `summary.json`. Existing populated values and historical
summary periods were not changed.
