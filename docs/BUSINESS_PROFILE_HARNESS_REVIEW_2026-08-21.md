# Business profile harness -- progress review, 2026-08-21

A point-in-time review of the Gate A2 business-profile extraction harness
(`scripts/profile/`, `evals/business_profiles/`): what has been built, what
the evaluation evidence actually says, and where the accuracy is worth
spending effort next. Design rationale for the stage itself lives in
[BUSINESS_PROFILE_EXTRACTION.md](BUSINESS_PROFILE_EXTRACTION.md); this
document is about how well it currently works.

All numbers here come from runs in the MLflow experiment
`companies-house-business-profile-eval` (experiment id 2 on
`http://127.0.0.1:5000`), not from estimates.

## Where it stands

The best configuration measured to date is **`google/gemini-3.7-flash` reading
the whole filed document**. On the full 57-case gold set:

| Metric | Value |
|---|---|
| Quote-verification pass rate | 94.7% |
| Mean field accuracy | 70.2% |
| Cost | $0.551 for 57 companies (~$0.0097 each) |
| Wall time | 700s for 57 companies |

Projected to the ~2,330 companies this stage is intended to cover, that is
roughly **$23 per full pass**.

Per-field accuracy on that run, with the merged `demand_model` taxonomy
applied retroactively so the figure is comparable to current code:

| Field | Accuracy | Majority-class baseline | Lift over baseline |
|---|---|---|---|
| `trading_status_confirmed` | 84.2% | 54.4% | +29.8 |
| `sic_agreement` | 80.7% | 77.2% | **+3.5** |
| `delivery_model` | 75.4% | 31.6% | **+43.9** |
| `customer_type` | 70.2% | 38.6% | +31.6 |
| `geography_served` | 70.2% | 47.4% | +22.8 |
| `demand_model` | 49.1% | 49.1% | **+0.0** |

**Read the lift column, not the accuracy column.** Raw accuracy is misleading
here because the gold set is heavily imbalanced. Two findings only become
visible this way:

- `sic_agreement` looks like the second-best field at 80.7%, but 44 of 57 gold
  labels are `agrees`. A model that answered "agrees" every single time would
  score 77.2%. The field as currently measured carries almost no signal.
- `demand_model`, before the prompt work described below, scored *exactly* the
  majority-class baseline. It was contributing nothing over always guessing
  `b2b_relationship`.
- `delivery_model` is quietly the strongest field in the set (+43.9), despite
  sitting mid-table on raw accuracy.

## What was built

The harness now consists of:

- **`scripts/profile/business_profile_policy.py`** -- taxonomy, prompt
  template, and response validation. Shared by production extraction and every
  eval path, so a taxonomy or validator change cannot drift between them.
- **`scripts/profile/business_profile_eval.py`** -- the standing gold-set
  harness and review queue.
- **`scripts/profile/business_profile_context_ab.py`** -- the model/context
  comparison harness (narrative sections vs whole filed document).
- **57 hand-labelled gold cases** in `evals/business_profiles/cases/`, up from
  47, with the additions deliberately targeted at the thinnest categories.
- **Whole-document Markdown renditions** of every gold-set filing, via
  `to_readable_markdown()` in `scripts/profile/save_raw_filings.py`. Companies
  House XHTML is a single unbroken line; this is what makes both human review
  and whole-document prompting possible.
- **Per-case MLflow tracing** on every eval run, so a run can be opened and
  read case by case rather than only summarised. The rules that keep this
  working are in `.claude/skills/mlflow-eval-discipline/SKILL.md`.

### Changes made on 2026-08-21

1. **Quote matching normalises formatting.** `normalize_quote_text()` collapses
   whitespace and strips punctuation that is not between two digits, so
   "22,557,801" stays intact while a dropped full stop or a curly quote does
   not fail a genuine quote. Motivated by three real rejections, none of which
   were fabrications -- two were whitespace differences from a filed HTML table
   flattening to Markdown.
2. **Per-field partial credit.** A failed quote on one field used to null all
   six fields for that company. Now only the field the error names is nulled;
   case-level failures (bad JSON, unattributable errors) still null everything.
3. **`demand_model` taxonomy merged and defined.** `considered_b2b`,
   `tender_framework`, and `relationship_repeat` collapsed into
   `b2b_relationship`, and the prompt now carries a one-line definition and
   example for every value instead of a bare list of enum names.

## Evidence: every comparison run

19-case sample, one row per model and context. These runs predate the
2026-08-21 changes.

| Model | Context | Pass rate | Mean accuracy | Cost (19 cases) | Projected 2,330 |
|---|---|---|---|---|---|
| gemini-3.7-flash | whole document | 100% | 81.6% | $0.183 | $22 |
| claude-opus-5 | narrative | 100% | 78.1% | $0.573 | $70 |
| claude-opus-5 | whole document | 94.7% | 74.6% | $2.293 | $281 |
| gemini-3.7-flash | narrative | 89.5% | 73.7% | $0.071 | $9 |
| gemini-2.5-flash-lite | narrative | 94.7% | 67.5% | $0.008 | **$1** |
| gemini-2.5-flash | whole document | 63.2% | 48.2% | $0.113 | $14 |
| gemini-2.5-flash | narrative | 63.2% | 49.1% | $0.037 | $4 |
| gemini-2.5-flash-lite | whole document | 42.1% | 31.6% | $0.034 | $4 |

Three things worth noting:

- **gemini-3.7-flash beats claude-opus-5 here, at a twelfth of the cost.** That
  is an unusual result and deserves suspicion, but it held across both contexts
  and the whole-document margin was not close.
- **gemini-2.5-flash-lite on narrative sections is the value outlier**: 67.5%
  for about $1 per full pass. If a cheap first pass with escalation is ever
  wanted, this is the candidate.
- **Whole-document context is worth roughly +8 points over narrative sections
  for gemini-3.7-flash, at 2.6x the cost.** For the incumbent gemini-2.5-flash
  the two contexts are indistinguishable.

### Effect of the 2026-08-21 changes

Measured on the same 19 companies, so the comparison is like-for-like:

| Configuration | `demand_model` accuracy |
|---|---|
| Old prompt, old 9-value taxonomy | 36.8% |
| Same predictions, re-scored under the merged taxonomy | 47.4% |
| New prompt with definitions, merged taxonomy | 57.9% |

The merge is worth about +10 points and the definitions about another +10, but
the merge's share is partly mechanical -- collapsing three classes into one
makes the task easier by construction. The honest summary is that
`demand_model` has moved from *at the majority baseline* to *meaningfully above
it* for the first time, and that the prompt-definition half of that is a real
gain rather than a scoring artifact.

Mean accuracy across all six fields on that smoke test was 75.4%, against
80.7% for the same 19 companies before the changes. That drop is not a
regression signal: two of the 19 calls failed on network timeouts and scored
zero, and 19 cases is far too small to resolve a five-point difference.

## What the errors actually look like

From the 57-case run, the dominant confusion in each field:

- **`customer_type`** (14 errors): 9 are the model answering `mixed` when the
  gold label is a clean `b2c` (5) or `b2b` (4). `mixed` is a real category --
  10 of 57 gold labels use it -- but the model reaches for it as a hedge.
- **`geography_served`** (14 errors): 6 are `national_uk` answered as
  `international`. The model appears to treat any foreign mention -- an
  overseas subsidiary, a foreign parent, an export line in the turnover note --
  as evidence of international customers.
- **`delivery_model`** (11 errors): diffuse, no dominant pattern. Consistent
  with this being the field the model already handles best.
- **`trading_status_confirmed`** (6 errors): 3 are `trading` answered as
  `trading_group_parent`, likely triggered by any mention of subsidiaries.
- **`sic_agreement`** (8 errors): bidirectional and unpatterned, which fits a
  field that is barely beating its baseline.

## Recommendations, in priority order

**1. Give every field the treatment `demand_model` just got.**

The two fields whose prompt text already contains per-value glosses --
`trading_status_confirmed` and `sic_agreement` -- rank first and second on raw
accuracy. The one field with no definitions at all ranked last, and adding them
moved it about +10 points. `customer_type`, `delivery_model`, and
`geography_served` still receive nothing but a bare list of enum names. This is
the cheapest remaining change with direct supporting evidence, and it costs
only prompt tokens.

Two definitions should be written to target the specific confusions above:
`mixed` needs an explicit high bar ("only when the text evidences both
segments, not when you are unsure -- prefer the dominant one"), and
`international` needs to require customers abroad rather than corporate
structure or an incidental export line.

**2. Fix or retire the `sic_agreement` metric.**

At +3.5 points over baseline it is currently measuring almost nothing, so it
cannot detect a regression or an improvement. Either rebalance the gold set so
`disagrees` is better represented, or report it as balanced accuracy / F1 on
the `disagrees` class rather than raw accuracy. Reporting it at "80.7%"
alongside genuinely informative fields overstates the harness's health.

**3. Adjudicate a sample of disagreements before chasing the residual.**

Earlier review passes on this gold set turned up genuine labelling mistakes, so
some fraction of the remaining ~30% error is label noise rather than model
error. Before investing in harder modelling work, take 15 disagreements, decide
them properly against the filed text, and record what share were the gold
label's fault. That number determines whether the realistic ceiling here is 85%
or 95%, and nothing else currently tells us which.

**4. Grow the gold set to roughly 120-150 cases.**

At n=57 the 95% confidence interval on a 70% measurement is about +/-12 points;
at n=19 it is about +/-21. Most of the comparisons in this document cannot
distinguish configurations that differ by less than roughly 10 points, which is
larger than most of the differences being compared. This is the main thing
limiting confidence in every other conclusion here.

Two coverage gaps are worth closing specifically: `trading_status_confirmed`
only ever takes three values in the current set (`trading` 31,
`trading_group_parent` 23, `spv` 3) -- no genuine `dormant` or
`investment_holding` case exists, so its 84.2% is measured on an easier problem
than production will present. `demand_model` has just one
`platform_intermediated` case and two each of `wholesale_contract` and
`not_customer_facing`.

**5. Then re-run the full 57 and re-baseline.**

Once (1) is done, a single full-set run at ~$0.55 re-establishes the numbers
against current code. Doing this before (1) mostly re-measures what is already
known.

**6. Deferred, in rough order of expected value:**

- **Few-shot examples** in the prompt. Held back deliberately: definitions are
  cheaper in tokens and the demand_model result suggests they may be sufficient.
  Worth trying if (1) underdelivers.
- **A cheap-first-pass architecture** -- gemini-2.5-flash-lite on narrative
  sections at ~$1 per full pass, escalating only low-confidence or
  quote-rejected cases to gemini-3.7-flash on the whole document. Only worth
  building if the ~$23 full-pass cost ever becomes a constraint; it is not one
  today.
- **Self-consistency** (sampling the same case several times and taking the
  majority) would likely help `demand_model` most, but multiplies cost by the
  sample count and should wait until the gold set is large enough to prove it
  helped.

## What is not yet known

- Whether gemini-3.7-flash's advantage over claude-opus-5 survives a larger
  gold set, or is an artifact of 19 cases.
- The true label-noise floor (see recommendation 3).
- How any of this behaves on companies with thin or missing narrative -- the
  gold set is drawn from companies that have one, and 5,209 of 8,169 companies
  in the database do not.
- Whether `evals/business_profiles/configs/openrouter-gemini.yaml` should be
  switched from gemini-2.5-flash to gemini-3.7-flash. The evidence supports it
  (73.7% vs 49.1% on the same context, at roughly twice the cost), but changing
  the production default is a decision to take deliberately rather than as a
  side effect of an eval run.
