# Plan: get the business-profile classifier working properly

Status as of 2026-09-02: **Phases 1, 2, and 3 all done.** `PROMPT_VERSION`
bumped to `v2` to mark the Phase 2 + 3a/3c rewrite, and now registered as a
real, diffable version in MLflow's Prompt Registry (`business_profile_prompt_registry.py`)
so future prompt changes are visible there, not just in a run param.
**3d (the real-model smoke test) passed**: coverage rose to 63-74% (from a
44-47% baseline) and `accuracy_when_committed_on_answerable` held at 89-100%
-- exactly the outcome 3a/3c was meant to produce. Running it surfaced three
previously-unknown extraction/validation bugs that were rejecting genuinely
correct model quotes as if they were hallucinations (a leading article
dropped from three section anchors, case-sensitive quote matching, and a
hyphen-spacing normalization gap) -- all three fixed and verified against
the same paid-for model responses, no extra spend. Phase 1c's result
supports all of this: self-reported confidence separates correct from
incorrect (pooled point-biserial r=+0.64 across 251 field/case pairs). Two
metric bugs that would have made 3d misjudge a working prompt change as a
failure were found and fixed before running it: `accuracy_when_committed`'s
denominator included unanswerable gold-`unclear` cases (capping it below the
90% pass bar regardless of model quality -- fixed by adding
`accuracy_when_committed_on_answerable`), and `macro_f1` scored abstention
as a classification target (fixed by excluding `unclear` from the average).
A live taxonomy-drift bug was also caught and fixed: the MLflow review
queue's label-schema dropdowns for four fields still offered values Phase 2
and an earlier merge had dropped. **Phase 4 (targeted labelling) is next.**
See "What Phase 1 actually did" below
for a plain-English walkthrough of the original code changes.

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
  visibly unmeasurable rather than silently noisy. `unclear` is excluded from
  the macro-F1 average (still reported per-class): it is an abstention, not a
  classification target, and Phase 3 deliberately drives its recall toward
  zero -- scoring that as class damage would report the intended effect of
  the prompt change as a regression. Abstention has its own metric,
  coverage, and belongs there instead.
- **Majority-class baseline** beside each field's accuracy. A field that does
  not beat its baseline is not working, regardless of its headline number.
- **The headline business metric**: collapse `demand_model` to
  search-addressable (`consumer_search`, `local_service`) vs not, and report
  precision/recall/F1 on it. This is the number that actually says whether the
  stage is doing its job.

**1c. Run the confidence-vs-correctness check** ✅ Done
(`scripts/profile/business_profile_confidence_check.py`). Pulled the 57
traces from run `169063b5a3f0406d8e6c3322142f4edd`, extracted each field's
self-reported `confidence` alongside whether it was correct, and tested
whether confidence separates right from wrong.

**Result: yes, and cleanly.** Pooled across 251 field/case pairs (excluding
gold-`unclear` cases, which have no right answer to correlate against):
point-biserial correlation +0.64, mean confidence 0.93 when correct vs 0.48
when wrong. Every field individually correlates positively (weakest:
`trading_status_confirmed` at +0.22, driven by that field's confidence
barely leaving the 0.90-1.00 band in this run rather than by confidence
being unreliable there; strongest: `demand_model` at +0.92). Every wrong
answer with confidence below 0.5 really was wrong (n=31, 0% accuracy); the
`[0.90, 1.00]` and `[0.75, 0.90)` bands both scored 80%+.

**This unblocks Phase 3b** -- confidence-banded filtering has something real
to band on; a different mechanism is not needed.

One data-hygiene issue surfaced and was corrected before trusting the
result: 18 of the 57 `demand_model` payloads in that run predate the
sub-value merge (`considered_b2b`/`tender_framework`/`relationship_repeat`
→ `b2b_relationship`, see Phase 2) and still carried the old values. Scored
naively against today's gold labels, every one of those 18 counted as a
high-confidence miss -- not a confidence failure, but the run being graded
against a taxonomy it predates. The check normalizes the legacy values
before scoring (see `LEGACY_VALUE_REMAP` in the script) and documents why.
Before the fix, `demand_model`'s correlation measured +0.38 with a 45%
accuracy ceiling even at >=0.90 confidence -- a taxonomy-drift artifact, not
a real finding; the corrected number above is the one to trust.

**A second instance of the same drift was caught and fixed in the same
pass, in a different place:** the MLflow review queue's label schemas
(`_label_schemas` in `business_profile_eval.py`) only created a schema when
none existed yet, so the reviewer-facing dropdowns for `demand_model`,
`customer_type`, `delivery_model`, and `trading_status_confirmed` had
drifted back to pre-merge and pre-Phase-2 values (`considered_b2b`,
`wholesale_contract`, `saas`, `b2b2c`, `dormant`) regardless of what
`business_profile_policy.py` currently says. Fixed to reconcile an
existing schema's options against the current taxonomy on every sync, not
just create a missing one. This matters directly for Phase 4: labelling
through the review UI before this fix would have produced new gold labels
in the old taxonomy.

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

## Phase 3 -- Fix over-abstention, commit with confidence ✅ Done -- 3a/3b/3c/3d all complete, 3d passed

**3a. Rewrite the uncertainty instruction.** ✅ Done. The prompt used to say
*"Never guess to avoid saying unclear -- unclear is a correct answer, not a
failure."* It was doing exactly what it said, too well. Now it says: always
give the best supported answer, and express uncertainty through `confidence`
rather than by withholding a value. Reserve `unclear` for genuinely no signal
at all.

**3b. Make `confidence` real.** ✅ Done 2026-09-02.

- **Validate it.** `validate_response` (`business_profile_policy.py`) now
  rejects a response whose confidence is missing, non-numeric, or outside
  0.0-1.0, for every field including `unclear` -- the prompt asks for it
  regardless (Phase 3a made confidence the only way uncertainty gets
  expressed at all), and every real response on hand already includes one,
  so this tightens nothing that was actually in use. One knock-on fix:
  `business_profile_review.py`'s placeholder for a field the reviewer
  hasn't touched yet used `confidence: None`, which the new check would
  have rejected -- changed to `0.0`, matching every real `unclear` label on
  hand.
- **Carry it through scoring.** It already reached `score_case`'s per-field
  output; the gap was that nothing downstream ever looked at it. New
  `confidence_bands` (`business_profile_metrics.py`) buckets committed
  answers into confidence bands (`>=0.90`, `0.75-0.90`, `0.50-0.75`,
  `<0.50` -- the same bands Phase 1c used) and reports accuracy per band,
  restricted to the same population as `accuracy_when_committed_on_answerable`
  (an abstention has no confidence-vs-correctness question to ask; a
  gold-`unclear` case has no correct committed answer to band against).
  Wired into every field's `field_metrics` output and printed in `run`'s
  report. Verified against the real historical run: reproduces Phase 1c's
  numbers exactly (e.g. `demand_model` >=0.90: n=11, 100% accurate;
  0.75-0.90: n=15, 86.7%).
- **Deliberately not logged as MLflow scalars.** Same reasoning as
  per-class precision/recall in `flatten_for_mlflow`: a table to read in
  the report, not a time series worth charting.
- This is what a downstream confidence threshold (the "filtering done
  downstream" half of the plan's opening operating-point decision) would
  actually be chosen from -- that consumer doesn't exist yet; this is the
  measurement it would be built against.

8 new tests (confidence validation, the review-queue placeholder fix,
banding itself), 298 total pass.

**3c. Give the remaining fields the treatment `demand_model` just got.** ✅
Done. All six fields now carry a one-line definition per value in the prompt,
not just `demand_model`. Two are written to target known errors: `mixed`
(customer_type) now has an explicit high bar, and `international`
(geography_served) now requires customers abroad rather than a foreign parent.

**3d. Smoke test on the 19-case sample** (~$0.16) ✅ Done 2026-09-02
(`google/gemini-2.5-flash`, narrative context, run `phase3d-smoke-test-19case`,
corrected re-score logged as `phase3d-smoke-test-19case-corrected`). **Pass.**
Coverage rose to 63-74% across fields (from a historical 44-47% baseline) and
`accuracy_when_committed_on_answerable` held at 89-100% across every field --
both exactly the outcome 3a/3c was meant to produce, well inside "holds near
90%". `mean_field_accuracy` 0.640, search-addressable precision 1.00 (recall
0.33 at n=15 considered, 3 gold positives -- too thin at this sample size to
read, not evidence of a problem).

The raw run initially measured only 13/19 (68.4%) cases accepted -- a
quote-verification pass rate low enough to look like a real problem with 3a/3c
itself. Investigating the 6 rejections found they were not model failures:
three separate, previously-unknown bugs in the shared extraction/validation
path, exposed by this being the first time the rewritten prompt ran against
real filings at all:

1. **Leading-article truncation** (`core/companies_house_pdf_text.py`).
   `principal_activity`, `strategic_report`, and `employee_note` all anchor to
   a phrase that is naturally the object of a leading "The" in the source
   sentence ("The average monthly number of persons...", "...present the
   strategic report..."), but the regex matched only the noun phrase, so the
   stored section text began mid-sentence, missing that first word. A model
   quoting the real, complete sentence then failed verbatim-match validation
   against a source that was missing a word -- rejected as if it had
   hallucinated, when it had quoted correctly. Fixed by extending a match
   backward over an immediately-preceding "The "/"the " when present.
2. **Case-sensitive quote matching** (`business_profile_policy.py`,
   `normalize_quote_text`). A model occasionally re-cases the first letter of
   a quote once it's embedded in a JSON string value ("The..." -> "the...",
   "DoBeDo..." -> "DOBEDO..."), which changed nothing about whether the words
   came from the source but failed verbatim matching regardless. Fixed with
   `.casefold()`.
3. **Hyphen-spacing normalization** (same function). Stripping punctuation
   outright rather than replacing it with a space meant whether the *source*
   happened to space out a hyphen changed the normalized result
   ("long-term" -> "longterm" vs "long - term" -> "long term") independently
   of whether the model quoted correctly. Fixed by replacing punctuation with
   a space and re-collapsing whitespace afterward.

Verified without spending anything further: the three fixes were checked by
re-parsing and re-validating the *same* raw model responses already paid for
(pulled from the run's MLflow traces) against the corrected extraction --
5 of the 6 original rejections now pass. The 6th (`02372641`, `customer_type`)
remains correctly rejected: the model's quote literally contains an ellipsis,
which the prompt explicitly forbids -- a real rule violation, not a bug.
This raised the accepted rate from 13/19 to 18/19 (94.7%), which is the
number the Pass verdict above is read against, not the raw 68.4%.

Read this against `accuracy_when_committed_on_answerable`, not
`accuracy_when_committed`. The latter counts every commitment against a
gold-`unclear` case as wrong even for a perfect model, which caps it at
(answerable / scored) -- on the historical 57-case run that ceiling is 92.3%
for `demand_model` and lower for fields with more gold-`unclear` labels, so
"holds near 90%" was, before this fix, close to unreachable by construction
regardless of how good the prompt change is. `accuracy_when_committed_on_answerable`
excludes gold-`unclear` cases from the denominator the same way
`search_addressable_metrics` already did, so 90% is a real bar again.

`PROMPT_VERSION` bumped `business-profile-v1` → `v2` to mark the Phase 2 +
3a/3c prompt rewrite: it's logged as an MLflow run param and written to
`company_business_profile.prompt_version`, and both were still saying `v1`
after a 162-line prompt rewrite, making pre- and post-change runs
indistinguishable in MLflow.

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

- `python -m pytest` after each phase (282 tests currently pass); tests exist
  for the new metric functions and the pruned taxonomies.
- Phase 1 metrics validated offline against the existing 57-case report before
  any code was wired in -- all six accuracy figures reproduced exactly.
- Phase 3 will be verified by the 19-case smoke test, read against
  `accuracy_when_committed_on_answerable` (see Phase 3d): **coverage should
  rise substantially while precision on committed, answerable cases holds
  near 90%.** If precision collapses instead, the abstention was load-bearing
  and the prompt change should be reverted.
- Every eval run logs per-case traces to the one MLflow server, verified with
  `search_traces` rather than assumed
  (`.claude/skills/mlflow-eval-discipline/SKILL.md`). This was true for the
  model/context comparison harness from the start but not for the gold-set
  `run` command itself -- fixed 2026-09-02 (`_log_gold_eval_case_trace` in
  `business_profile_eval.py`); every case, including a request-level
  failure, now gets its own trace, linked to the run. Verified live against
  the server for both the success and failure paths, not just that it
  compiled.
- Final summary spreadsheet published to the "Projects / companies-house-leads"
  Drive folder via the `publish-google-sheet` skill, per `AGENTS.md`.

## Risks

- **Merging classes inflates accuracy mechanically.** Mitigated by always
  reporting the recomputed majority-class baseline alongside.
- **Self-reported confidence may not be calibrated.** ~~Phase 1c tests this
  before Phase 3 depends on it.~~ Resolved: it is calibrated (pooled
  point-biserial r=+0.64; see Phase 1c).
- **Some residual error is probably gold-label noise, not model error.** Earlier
  review passes on this set found genuine labelling mistakes. Worth adjudicating
  ~15 disagreements during Phase 4 and recording what share were the label's
  fault -- that number determines whether the realistic ceiling is 85% or 95%.
- **57 cases is statistically thin** (95% CI on a 70% measurement is about
  +/-12 points). Differences smaller than ~10 points cannot be trusted until the
  set grows.
- **The section splitter could silently drop the sentence a label most
  needs.** ✅ Fixed 2026-09-02 (`core/companies_house_pdf_text.py`). Found
  while checking whether `10723179`'s `unclear` calls were genuine (they
  were, independent of this bug) or the classifier under-reading available
  signal. Two compounding bugs, both in the shared extraction path every
  stage reads from, not specific to this eval:
  1. `extract_sections` finds a section's end by scanning for the *next*
     heading-pattern match anywhere in the document -- but several heading
     phrases recur inside their own section's body prose, most importantly
     "principal activity", which appears once as the heading and then again
     in the boilerplate sentence pair filings use to distinguish group
     activity from parent-company activity ("The principal activity of the
     group... The principal activity of the company was that of a holding
     company") -- exactly the sentence `trading_status_confirmed` exists to
     read. The second occurrence was mistaken for a new section, fragmenting
     the real one; the longest-fragment tie-break could then keep a fragment
     that omits the decisive sentence entirely. A corpus check found 752 such
     self-adjacent same-key match pairs across 57 of 58 filed reports on
     hand -- routine, not an edge case. Fixed by dropping a match that
     shares its key with the match immediately before it in the merged,
     all-headings list, provided nothing else sits between them (`_drop_self_referential_repeats`)
     -- narrower than merging anything within some character distance, which
     would have broken the existing, correct handling of a bare
     contents-page heading genuinely followed later by its real section
     (`test_a_bare_heading_does_not_beat_a_real_section`).
  2. Fixing (1) exposed a second, previously-masked bug: `_is_inside_auditor_report`
     treated the bare phrase "independent auditor's report" as evidence a
     candidate sits inside the auditor's own text -- but that exact phrase
     is also just a line in every filing's table of contents, printed
     right alongside "Strategic report" and "Directors' report" near the
     very start of the document. That flagged genuine, early narrative
     content as auditor text purely because the contents page happened to
     precede it (55 of 58 filed reports on hand). Fixed by dropping that one
     ambiguous phrase from the auditor-boilerplate pattern -- every other
     phrase in it (`"we have audited"`, `"in our opinion"`, `"ISAs (UK)"`,
     etc.) is specific enough that it never doubles as ordinary heading or
     contents-page text.

  Confirmed on the 3 gold cases that surfaced (1): `10723179` and
  `11380836`'s `principal_activity` now contain the full sentence pair;
  `10622184`'s really is just a bare heading with nothing after it in the
  source filing, not a bug. Verified on the full corpus with a controlled
  before/after comparison on identical input text (isolating the code
  change from the separate whole-document-vs-DB-pipeline discrepancy that a
  naive before/after-on-stored-JSON comparison would have measured
  instead): 164 sections grew, 32 shrank (all but one is `going_concern`
  collapsing from a runaway near-6000-char capture down to a reasonable
  length -- correct, and that field isn't even read by the classifier
  prompt), 282 unchanged. 11 tests in `tests/test_narrative_quality.py`
  (3 new), 285 total pass.

  This fixes the extraction code path, not retroactively: the 57 stored
  gold cases were captured before this fix and are not automatically
  updated by it. Re-running `initialise` against freshly-extracted sections
  would pick up the improvement but also re-open every case for review.

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
