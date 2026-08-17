# Benchmark improvement plan: 96.3% → 99%

Target: raise core-financial expected-value accuracy on the 50-case gold-label
set from **96.33%** to **99%**.

Baseline is the run of 2026-08-15, `openrouter-qwen3-vl-235b-gold-review-50-combined-20260815`
(run id `09c516c12f534fde8bee5e8dd9e98031`, gold snapshot
`f97e42db…6b3cf`), raw data in `logs/comparison-50-20260815/`.

## Baseline

| Metric group | Accuracy | Correct | Errors |
|---|---|---|---|
| Core financial (expected values) | **96.33%** | 551 / 572 | 21 |
| Core financial (all cells) | 96.5% | 579 / 600 | — |
| Core financial (missing-cell) | 100% | 28 / 28 | 0 |
| Employees (expected values) | **85.88%** | 73 / 85 | 14 |
| Employees (missing-cell) | 86.67% | 13 / 15 | 2 |
| Statement pages | precision 0.639 · recall 0.759 · F1 0.635 | | |

Cost: $2.138 / 50 docs = **$0.0428 per document**.

**Error budget to reach 99% core financial:** ≤ 6 errors out of 572. From 21,
that means eliminating **15 errors**.

## Where the errors are

35 error rows total (21 core financial + 14 employees), concentrated in
**16 of 50 documents**. The top 5 documents produce 49% of all errors; the top
12 produce 89%.

By metric:

| Metric | Errors | Share |
|---|---|---|
| employees | 14 | 40% |
| cash | 9 | 26% |
| net_assets | 5 | 14% |
| operating_result | 3 | 9% |
| profit_after_tax | 3 | 9% |
| gross_profit | 1 | 3% |
| turnover | 0 | 0% |

By outcome: `missing_prediction` 23 (66%), `wrong_value` 10 (29%),
`unexpected_prediction` 2 (6%). **The dominant failure is recall, not
hallucination** — the pipeline stays correctly silent when a value is genuinely
absent (100% missing-cell accuracy on core financial).

## Page numbering: verified, not a confound

Before any root-cause work: it was checked whether gold pages are *printed*
page numbers while predictions are *raw PDF indices*, which would invalidate
every "was the page located" comparison below.

They are not. Three failing documents were rendered and inspected directly.
Gold matches the raw 1-based PDF index in all three, and each has a
**different** printed-vs-PDF offset, so gold cannot be printed numbers:

| Document | Gold page | Actual PDF page (verified) | Printed number on that page |
|---|---|---|---|
| 14328191 | 13 | 13 — Consolidated Balance Sheet | 12 |
| 14719690 | 13 | 13 — Statement of Financial Position | 11 |
| 11796392 | 22 | 22 — Statement of Financial Position | 21 |

Predicted pages use the same convention: 14328191 predicted page 18 is PDF
index 18, the Consolidated Statement of Cash Flows (printed "17"). Both sides
are raw PDF index, so the comparisons are valid.

## Root cause

The locator is **not** the bottleneck:

- Documents *with* core-financial errors have **higher** mean statement-page
  recall (0.905) than documents *without* errors (0.727).
- In every failing core-financial case inspected, the gold source page **was
  present** in `predicted_statement_pages`, and visual inspection confirms the
  expected values are plainly legible on that page.

The failures are therefore in *what is read from a correctly located page*, and
there are three distinct mechanisms — not one.

### Mode A — competing primary statements, no tie-break

*14328191, 14523269 — 4 errors.*

Not a transcription failure. On 14328191 the model read
`Cash and cash equivalents at end of year` = **3,688** from the Cash Flow
Statement (PDF p18); gold wants `Cash at bank and in hand`, **Total** column,
from the Balance Sheet (PDF p13) = **144,337**.

Both sources are **tier 1** — `_CANONICAL_PRIMARY_STATEMENTS` allows `cash`
from `balance_sheet` *or* `cash_flow` — so nothing deterministic separates
them and the tie falls through to unit/period-count/confidence.

For Lloyd's and insurance structures the two figures legitimately differ by
orders of magnitude, because the cash flow statement reflects only corporate
funds and excludes syndicate participation. This is not model error so much as
an underspecified policy.

### Mode B — multi-column statements

*Same two documents, same errors as Mode A.*

These balance sheets carry three value columns per period —
`Syndicate participation | Corporate | Total`. The predicted 3,688 is exactly
the **Corporate** column on the balance sheet, and 105,579 exactly the prior-year
Corporate figure. Whether the value came from the cash-flow statement or the
balance sheet's Corporate column, the same defect applies: **there is no rule
requiring the `Total` column** on a multi-column primary statement.

### Mode C — genuine extraction miss on a located page

*11796392, 14719690 — 8 errors.*

Here the value really is absent from the output despite a clean, legible page:

- **14719690** (PDF p13): `Cash at bank and in hand 5,826 | -` and
  `Net (liabilities)/assets (26) | -`. Note the label variants —
  `Net (liabilities)/assets` rather than `Net assets`, and `Shareholder's funds`
  with a singular possessive — plus a parenthesised negative and dash-zeroes.
- **11796392** (PDF p22): `Cash at bank and in hand 61,823 | 118,563` and
  `Total equity 2,170,816 | 1,975,972`, reported in **$000** (USD).

Both are plausible label-variant or unit-handling failures, but that is
inference from the rendered pages, not proof. Traces must be read before code
is written.

Completeness recovery contains a rule for exactly this shape ("a balance sheet
with a visible `Current assets` row but no cash row is re-read"). Establishing
why it did not fire or did not succeed here is the first task.

Employees are a **largely independent** problem: only 2 of 9 employee-error
documents overlap with the 9 core-financial-error documents.

| Employee failure | Count | Page located? |
|---|---|---|
| Narrative zero not detected | 6 | 5 of 6 not located |
| Numeric count on a located page | 4 | yes |
| Numeric count on an unlocated page | 2 | no |
| False positive (`unexpected_prediction`) | 2 | n/a |

Narrative zeroes are mostly a *discovery* failure — the text backstop is not
finding "no employees" wording on pages the visual locator never selected
(pages 4, 5, 19, 22).

## Result of the 2026-08-17 run

Core financial **96.33% -> 97.73%** (21 -> 13 errors: 14 fixed, 6 new
regressions). Employees **85.88% -> 83.53%** (14 -> 16). Overall 94.98% ->
95.89%. Target not met: 99% needs <=6 core errors.

What landed: `corrected_statement_type()`, which repairs balance sheets the
locator labelled `income_statement`. That was the dominant cause and was not in
the original plan.

What backfired: the cash tie-break. It fixed 14523269 but regressed 13941271,
where the `statement_scope` labels are wrong and the cash-flow row held the
correct value. Net negative in isolation; recommend reverting.

## Attribution: the locator is the dominant cause after all

The earlier conclusion "the locator is not the bottleneck" was drawn from page
*recall* alone. Recall is fine. Page *classification* is not, and classification
gates everything downstream.

| Decision | Made by | At what resolution | Errors traced to it |
|---|---|---|---|
| `statement_type` | locator | 384 px | 14 of the original 21 core errors |
| `statement_scope` (Group vs Company) | locator | 384 px | 6 of the 13 remaining core errors |
| `contains_employee_count` | locator | 384 px | 7 of the 16 employee errors |

Roughly two thirds of all observed errors trace to a locator decision taken on a
384 px thumbnail, on which statement headings are a few pixels tall.

The remaining 13 core errors break down as:

| Cause | Errors | Fixable by locator resolution? |
|---|---|---|
| Group/Company scope confusion (13941271, 14523269) | 6 | likely |
| Wrong row on the correct page (11506103, 14383643) | 3 | no |
| Prior-period zero not reported (14550816) | 3 | no |
| Digit misread: 1,908 read as 1,906 (14848333) | 1 | no - extraction resolution |

## Next: raise locator resolution

`long_edge=384` is hardcoded at the locator call site. Making it configurable is
a prerequisite for testing it.

Expected effect, with wide error bars (n=13 and n=16 error samples, and
14523269 alone produced three different error patterns across three runs):

| Group | Now | Plausible after | Reasoning |
|---|---|---|---|
| Core financial | 97.73% | ~98.4-98.8% | Addresses the 6 scope errors; cannot touch the other 7 |
| Employees | 83.53% | ~87-89% | Addresses ~7 discovery errors; cannot touch the 7 extraction failures |

**Resolution alone will not reach 99% on core financial.** It plausibly closes
6 of 13, leaving ~7 that are extraction-side: row selection, prior-period zero
handling and one digit misread. Those need extraction work, not locator work.

Cost: the locator is $0.44 of $2.14 per 50 documents. Moving 384 -> 768 roughly
quadruples image area, so expect the run to land near $3.50 (~$0.07/document).
Hold `locator_batch_size` at 4 so resolution is the single variable; batching
has already been A/B tested by `run_vlm_batching_ab_test.ps1`, resolution never
has.

## Workstreams

Ordered by expected error reduction per unit of effort.

### 1. Close the gaps in completeness recovery

*Targets Mode C — est. 6–8 core errors.*

`statement_completeness_recovery_pages()` already re-reads a partially
transcribed primary page, but its triggers have two gaps that match our
failures exactly.

**Gap 1 — the balance-sheet cash re-read is gated on `current_assets`:**

```python
if "current_assets" in metrics and "cash" not in metrics:
    triggers.append("balance_sheet_current_assets_without_cash")
```

Insurance and investment balance sheets have no `Current assets` heading at
all — 11796392 (Convex) and 14328191 (Ednal) both group assets as
`Investments / Debtors / Other assets`. So on exactly the filings where cash is
hardest, **a missing cash row triggers nothing**. 14719690 does have a
`Current assets` heading, which is why it behaves differently.

Fix: trigger on a balance sheet that yielded money rows but no `cash` row,
regardless of whether `current_assets` was seen.

**Gap 2 — the income-statement trigger needs two metrics already present:**

```python
if len(present) >= 2 and not complete_family:
    triggers.append("income_statement_partial_core_family")
```

A page that returned only *one* core metric — turnover but no gross profit, for
example — never qualifies. That is the single most likely page to be worth
re-reading, and it is excluded by the `>= 2` guard. Lower it to `>= 1`.

**Two secondary issues in the same function:**

- The page cap selects by page order, not severity:
  `recovery_pages = sorted(eligible_pages)[:max_pages]` with `max_pages = 3`.
  A balance sheet at p22 loses to three earlier eligible pages. Rank by trigger
  severity (missing net assets / missing cash first) before truncating, and
  check `skipped_due_to_page_cap` in the traces to see how often the cap binds.
- The gate `confidence < 0.80` uses the **locator's** confidence in the page's
  statement type. A correctly located but hesitantly classified page gets no
  completeness recovery at all.

### 2. Deterministic tie-break between competing primary statements

*Targets Modes A and B — est. 4–6 core errors.*

Two changes, both in the policy rather than the prompt:

1. **Prefer the balance sheet for `cash`.** `_CANONICAL_PRIMARY_STATEMENTS`
   currently allows `cash` from `balance_sheet` *or* `cash_flow`, both tier 1,
   with no separation. Where both exist, the balance-sheet row must win; the
   cash-flow row stays as fallback. This alone fixes 14328191 and 14523269.
2. **Require the `Total` column on multi-column statements.** A
   `Syndicate participation | Corporate | Total` balance sheet must yield the
   `Total` figure. Selecting a component column should be rejected the same way
   a component *row* already is.

Confirm against traces before implementing — establish whether the
extractor emitted both candidates and rationalisation chose wrongly, or only
ever saw the cash-flow row:

```powershell
python .\scripts\vlm\vlm_financial_eval.py run `
  --config .\evals\vlm_financials\configs\openrouter-qwen3-vl-235b.yaml `
  --company-numbers 11796392,14719690,14328191,14523269 `
  --output-dir .\logs\diag-completeness-recovery
```

### 3. Whole-document narrative-zero backstop for employees

*Targets 5–6 employee errors.*

The embedded-text backstop currently only helps on pages that were otherwise in
scope. Run the conservative narrative-zero regex over the **full embedded text
layer of the document**, independent of locator output, and treat a single
unambiguous match as employee evidence with its page recorded.

Keep the existing strictness: `no employees other than directors` and
staff-cost disclosures must still be rejected (`ambiguous_narrative_zero_evidence`).
This only applies to text-layer PDFs; scanned-only documents still need the
visual path.

### 4. Small-value, dash and negative handling on balance sheets

*Targets ~2–4 core errors.*

14719690 expects `(26)` and `-` for net assets and cash and returns neither.
Confirm whether the row is transcribed and then dropped by row validation, or
never transcribed. A parenthesised small negative and a dash-zero on the same
row are the two cases most likely to be discarded as noise.

### 5. Locator precision (cost, not accuracy)

*Est. 0 accuracy gain; ~30–40% vision cost reduction.*

Precision 0.639 means roughly a third of read pages are unnecessary — 14523269
had 30 pages selected against 9 gold. Vision is the largest cost line ($0.81 of
$2.14). Worth doing **after** the accuracy work: while Mode C is unfixed, some
correct answers are currently coming from surplus pages that a tighter locator
would drop, so doing this first risks a regression that looks like a locator
problem but is not one.

## Realistic outcome

| Group | Now | After 1–4 | Notes |
|---|---|---|---|
| Core financial | 96.33% | **~99%** | Needs workstreams 1+2 to land nearly fully; they address ~15 of 21 errors. |
| Employees | 85.88% | ~94–96% | 99% would mean ≤1 error in 85 — not realistic in one pass. |

**99% on core financial is achievable but has no slack.** It needs Modes A, B
and C essentially eliminated — workstreams 1 and 2 together address roughly 12
of the 21 core errors, so the remaining few must come from workstream 4 and
from whatever the traces reveal. Employees should be treated as a separate
target; quoting a single blended number will hide the fact that the two have
unrelated causes.

## How to verify

Re-run the full 50 with the same config and gold snapshot, then compare:

```powershell
python .\scripts\vlm\vlm_financial_eval.py run `
  --config .\evals\vlm_financials\configs\openrouter-qwen3-vl-235b.yaml `
  --output-dir .\logs\vlm-eval-50-<change>

python .\scripts\vlm\vlm_financial_eval.py report-cell-errors `
  --results-dir .\logs\vlm-eval-50-<change> --log-mlflow
```

Guard rails:

- Gold labels are never edited to make a run pass. If a label is genuinely
  wrong, fix it deliberately and publish a new dataset version.
- Watch `missing_cell_accuracy` (currently 100% on core financial). Every
  workstream here increases recall, so the risk is trading silent misses for
  false positives. A drop there is a regression even if headline accuracy rises.
- The 15 holdout cases exist for this reason — check development and holdout
  splits separately before believing an improvement.

## Caveats

This analysis is one run, n=50, single model (`qwen/qwen3-vl-235b-a22b-instruct`).
Per-metric error counts in the single digits are not statistically robust —
`gross_profit` at 1 error could be noise. The root-cause claims are inferred
from `errors.csv` plus `predicted_statement_pages`, not from reading the
extraction traces; workstream 2 exists to confirm that inference before code
is written.
