# Plan: get the business-profile classifier working properly

Status as of 2026-08-27: **Phase 1 done and committed** (`0956891`). Phase 2
and Phase 3a/3c are implemented and unit-tested. The MLflow server is still
needed for the confidence check and the 19-case smoke test in 3d, so those
results remain pending before the new operating point is trusted in
production. See "What Phase 1 actually did" below for a plain-English
walkthrough of the code changes.

## Context

The Gate A2 business-profile classifier currently reads at 70.2% mean field
accuracy (gemini-3.7-flash, whole document, 57-case gold set). That number
looks like "a mediocre classifier that needs to be more accurate", and the
obvious response is "label more data". Diagnosis says that is the wrong read.
Three separate problems are tangled together:

1. **It is not mainly inaccurate -- it is over-abstaining.** On the question
   the stage exists to answer (can paid search reach this company?), when the
   model commits to an answer it is 91.7% correct, but it only commits 44.4%
   of the time. Of 15 genuinely search-addressable companies it found 5, got 1
   wrong, and said `unclear` on 9. That is a recall problem, not an accuracy
   problem, and no amount of extra labelling fixes it.
2. **The metrics hide this.** Accuracy alone cannot distinguish "wrong" from
   "declined to answer", and class imbalance inflates it: `sic_agreement`
   scores 80.7% against a 77.2% always-guess-`agrees` baseline, and
   `demand_model` before the recent prompt fix sat exactly on its baseline
   (contributing nothing).
3. **16 of 36 taxonomy classes have too few examples to measure**, and some of
   them should not exist at all -- Gate A already resolves them for free from
   structured data.

So: yes, more labels are needed, but that is the *third* problem, not the
first, and "more of the same" would mostly add more `b2b_relationship` /
`national_uk` / `agrees`. The cheap, high-yield work comes first.

Ordering rationale: fix the metric before changing behaviour (otherwise you
cannot tell whether a change helped), prune the taxonomy before labelling
(otherwise you label classes you are about to delete), and label last because
it is the only step that costs significant human time.

## Decisions taken

- **Operating point**: model should always commit to a best answer *and* emit a
  confidence, with filtering done downstream. The schema already collects a
  `confidence` field per classification that is currently requested, returned,
  and then silently discarded -- never validated, stored, or used.
- **Labelling appetite**: ~30 targeted cases, combined with taxonomy pruning
  rather than labelling every rare class up to n>=10 (which would need ~138).
- **Field priority**: all six fields matter; sequence by cost-to-fix, not by
  dropping any.

---

## Phase 1 -- Make the metric tell the truth ✅ DONE (committed `0956891`)

No API spend. Nothing here changes model behaviour; it changes what we can see.

**1a. Consolidate the duplicated scoring.** ✅ Done. `score_case` in
`business_profile_eval.py` and `score` in `business_profile_context_ab.py`
were near-identical, and the accuracy-counting code was duplicated too. Both
now import one shared module.

**1b. Add the metrics that expose the real failure modes:** ✅ Done.

- **Coverage** per field: share of cases where the model committed (did not say
  `unclear`). This is the number that reveals the abstention problem.
- **Per-class precision / recall / F1**, and **macro-F1** across classes with
  support. Report support (n) beside every class so unmeasurable classes are
  visibly unmeasurable rather than silently noisy.
- **Majority-class baseline** beside each field's accuracy. A field that does
  not beat its baseline is not working, regardless of its headline number.
- **The headline business metric**: collapse `demand_model` to
  search-addressable (`consumer_search`, `local_service`) vs not, and report
  precision/recall/F1 on it. This is the number that actually says whether the
  stage is doing its job.

**1c. Run the confidence-vs-correctness check** ⏳ Blocked on the MLflow server
being up (it went down mid-check last time; free and offline once it's back).
Pull the 57 traces from run `169063b5a3f0406d8e6c3322142f4edd`, extract each
field's self-reported `confidence` alongside whether it was correct, and test
whether confidence separates right from wrong. **This is a prerequisite for
Phase 3** -- if self-reported confidence turns out to be uncorrelated with
correctness, confidence-banding cannot be built on it and Phase 3 needs a
different mechanism (e.g. a coarse three-level certainty, or sampling
agreement).

## Phase 2 -- Prune the taxonomy ✅ Implemented and unit-tested

No API spend. The rule applied to every value:

> **Drop it** if Gate A already determines it deterministically from structured
> data (it is free there and an LLM call is pure waste).
> **Merge it** if it is rare *and* the distinction does not change what you would
> do with the lead.
> **Keep it and target labels at it** if it is rare but genuinely changes the
> decision.

What actually happened (one deviation from the original plan, explained where
it occurs): dropped `dormant` (Gate A decides it for free; only 1 of 2,960
companies with narrative is dormant) and `b2b2c` (never used by human or
model). Merged `saas`→`product_digital` and `wholesale_contract`→
`b2b_relationship`. The plan also proposed merging `rental_leasing`/`property`,
but checking what the model actually predicts showed `property` gets used
4 times -- it's decision-relevant (equipment/vehicle hire is one of the most
search-driven categories there is), so it was kept for targeted labelling
instead, per the same rule that justified every other merge.

Result: 36 → 33 classes; unmeasurable classes (too few gold examples to
compute a trustworthy number) 16 → 12.

## Phase 3 -- Fix over-abstention, commit with confidence 🟡 3a/3c implemented and unit-tested; 3b/3d not started

**3a. Rewrite the uncertainty instruction.** ✅ Done. The prompt used to say
*"Never guess to avoid saying unclear -- unclear is a correct answer, not a
failure."* It was doing exactly what it said, too well. Now it says: always
give the best supported answer, and express uncertainty through `confidence`
rather than by withholding a value. Reserve `unclear` for genuinely no signal
at all.

**3b. Make `confidence` real.** ⏳ Not started. Validate it is present and in
range, carry it through scoring, and log it per case so coverage/precision can
be reported at several confidence thresholds.

**3c. Give the remaining fields the treatment `demand_model` just got.** ✅
Done. All six fields now carry a one-line definition per value in the prompt,
not just `demand_model`. Two are written to target known errors: `mixed`
(customer_type) now has an explicit high bar, and `international`
(geography_served) now requires customers abroad rather than a foreign parent.

**3d. Smoke test on the 19-case sample** (~$0.16) ⏳ Blocked on the MLflow
server. This is the real test of 3a/3c: coverage should rise substantially
while precision on committed answers holds near 90%. If precision collapses
instead, the abstention was load-bearing and 3a should be reverted.

## Phase 4 -- Targeted labelling (~30 cases) ⏳ Not started

Reuse `select_candidate_companies` (`business_profile_eval.py`), which already
stratifies across Gate A `trading_status` and SIC groups, plus
`initialise_cases` and the review UI in `business_profile_review.py`.

Target the gaps that survive Phase 2, using the "guess from structured signals
first, then read to confirm" approach that worked when the set grew from 47 to
57: Gate A's `holding` bucket (58 companies) for `investment_holding` vs
`trading_group_parent`; consumer-facing SIC groups for `local` geography and
`consumer_search`; public-sector-heavy SIC groups for `public_sector`.

## Phase 5 -- Re-baseline ⏳ Not started

One full run on the expanded set (~$0.55 at current pricing) with the full
metric suite from Phase 1, logged as a single MLflow run with per-case traces.
Compare against majority-class baselines, not against the pre-change numbers.

Also decide, with evidence in hand, whether
`evals/business_profiles/configs/openrouter-gemini.yaml` should switch its
default from gemini-2.5-flash to gemini-3.7-flash -- currently deferred as a
deliberate production decision.

---

## Verification

- `python -m pytest` after each phase (278 tests currently pass); tests exist
  for the new metric functions and the pruned taxonomies.
- Phase 1 metrics validated offline against the existing 57-case report before
  any code was wired in -- all six accuracy figures reproduced exactly.
- Phase 3 will be verified by the 19-case smoke test: **coverage should rise
  substantially while precision on committed answers holds near 90%.** If
  precision collapses instead, the abstention was load-bearing and the prompt
  change should be reverted.
- Every eval run logs per-case traces to the one MLflow server, verified with
  `search_traces` rather than assumed
  (`.claude/skills/mlflow-eval-discipline/SKILL.md`).
- Final summary spreadsheet published to the "Projects / companies-house-leads"
  Drive folder via the `publish-google-sheet` skill, per `AGENTS.md`.

## Risks

- **Merging classes inflates accuracy mechanically.** Mitigated by always
  reporting the recomputed majority-class baseline alongside.
- **Self-reported confidence may not be calibrated.** Phase 1c tests this
  before Phase 3 depends on it.
- **Some residual error is probably gold-label noise, not model error.** Earlier
  review passes on this set found genuine labelling mistakes. Worth adjudicating
  ~15 disagreements during Phase 4 and recording what share were the label's
  fault -- that number determines whether the realistic ceiling is 85% or 95%.
- **57 cases is statistically thin** (95% CI on a 70% measurement is about
  +/-12 points). Differences smaller than ~10 points cannot be trusted until the
  set grows.

---

## What Phase 1 actually did, in plain English

**The problem before Phase 1:** the evaluation scripts only ever calculated
one number per field: *accuracy* -- the percentage of times the model's
answer matched the human-labelled correct answer. That single number hides
two different things:

1. **It can't tell "wrong" apart from "wouldn't say".** If a field is 70%
   accurate, that could mean "answers everything, right 70% of the time" or
   "answers half the time and is always right when it does" -- two completely
   different problems needing two completely different fixes, and a single
   accuracy number can't distinguish them.
2. **It's flattered by a lopsided dataset.** If 44 of 57 companies are
   correctly labelled `agrees`, a model that always answers `agrees` --
   without reading anything -- scores 77% by luck alone. An 80.7% accuracy
   next to that looks decent, but it's really only 3.5 points of actual
   skill.

**What I built:** one new file, `scripts/profile/business_profile_metrics.py`.
It replaces the old single "accuracy" calculation with several numbers that
each answer a different question:

| New number | Question it answers |
|---|---|
| **Coverage** | Out of everything it was asked, how often did it actually give an answer instead of shrugging? |
| **Majority baseline** | What score would you get by always guessing the single most common answer, without reading anything? |
| **Accuracy vs. baseline** | Is the model actually better than a lazy guess, and by how much? |
| **Precision / recall per answer choice** | For one specific answer (e.g. `mixed`), how often is it right when it picks that answer, and how often does it find every real case of it? Catches a model that over-uses one option. |
| **Search-addressable score** | Of the companies that genuinely can be reached through paid-search advertising, what fraction did the model actually find? This is the number that matters for the business, not the technical accuracy number. |

**Where it got plugged in:** two existing scripts run the actual evaluations
-- one runs a single test, one compares different AI models against each
other. Both used to have their own, slightly different, copy of the
scoring code. I deleted the duplicate and pointed both at the one new shared
file, so any future improvement to how results are measured only has to be
made in one place, and both tools stay in sync automatically.

**How I checked it wasn't broken:** before trusting any of this, I ran the
new code against results from a real test that had already happened (the
57-company run from a few days ago) and compared its answers to the accuracy
numbers already on record. All six matched exactly. Only after that did I
wire it into the actual evaluation scripts, and the full automated test suite
(278 checks) still passes.

**What this revealed, immediately:** on that same 57-company run, the
business-relevant number came out to precision 1.00, recall 0.33 -- meaning
when the model says "yes, this company is reachable by paid search," it's
right every time, but it only finds 1 in 3 of the companies that actually are
reachable. Old accuracy numbers had no way to show that at all. That finding
is what the implemented part of Phase 3 tries to fix; the MLflow smoke test is
still pending.
