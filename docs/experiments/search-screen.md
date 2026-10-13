# Search screening: experiment review

[Experiment overview](README.md) | Evidence reviewed through 2026-10-05

## Question

Can a cheap first stage remove clearly unsuitable companies while retaining
businesses whose customers could find them online and buy, book or enquire
directly?

This is deliberately narrower than inferring the company's current
acquisition channel from its accounts. Both consumers and business buyers
can search for suppliers. The screen asks whether that route is plausible,
then leaves website research to assess fit and opportunity.

## Experiments and results

The acceptance criteria were recorded before paid tests: at least 95% recall
on `likely`, at least 90% on `likely` plus `possible`, at least 30% removal,
and evidence that a paid model earns its place against the free baseline.
The same-prompt result should also hold on repeated runs.

The reported comparison used 125 labelled random-cohort cases and 26
additional boundary cases; the wider selection also reserved a blind subset.
The following quality scores are from the tested random cohort;
reported model costs cover all 151 calls. Keeping those populations separate
avoids treating challenge cases as a representative removal-rate sample.

### Short extract, whole filing and free baseline

| v1 comparison | Likely retained | Likely + possible retained | Share removed | Cost for 151 cases |
|---|---|---|---|---|
| Short extract | 40/42, 95.2% | 70/72, 97.2% | 23% | $0.13 |
| Whole filing | 41/42, 97.6% | 71/72, 98.6% | 19% | $1.12 |
| Free baseline | 41/42, 97.6% | 62/72, 86.1% | 25% | No model cost |

The model was `openai/gpt-5.4-mini`. Longer context retained one more
positive but removed fewer companies and cost about nine times as much.
The short extract preserved more uncertain prospects than the free baseline,
but neither model variant met the removal target. This was a cost tradeoff,
not proof that the short extract dominated every quality measure.

### Stricter evidence rules and repeatability

Prompt v2 emphasised evidence about tenders, frameworks, contracted buyers
and captive trade. It was run twice on the same cases with short extracts.

| Random-cohort result | v1 | v2 run 1 | v2 run 2 | Reject only when both v2 runs reject |
|---|---|---|---|---|
| Likely retained | 40/42 | 39/42 | 38/42 | 39/42 |
| Likely + possible retained | 70/72 | 63/72 | 61/72 | 66/72 |
| Share removed | 23% | 46% | 46% | 42% |
| Reference-unlikely rejected | 27/53 | 49/53 | 47/53 | Not reported |

Removal improved substantially, but recall fell below the acceptance
thresholds. Requiring two rejections recovered some possible prospects but
still retained only 39/42 likely businesses, below 95%. Between identical v2
runs, 11 cases changed pass/reject and 15 changed label. Each run cost about
$0.15 for the full evaluation set.

Source for both comparisons: [the dated screen results](../SEARCH_SCREEN.md).

### Balanced prompt on the population

The October 1 v3 run used short extracts for 2,268 target companies with
XHTML filings, at a recorded cost of $2.45.

| Result | Companies |
|---|---|
| Likely | 1,168 |
| Possible | 404 |
| Unlikely, rejected | 695 |
| Unparseable, passed under the fail-open policy | 1 |
| Total passing | 1,573 |

The rejection rate was 695/2,268, or 30.6%, meeting the population removal
target. Of 2,267 parsed answers, 2,257 contained a quote found verbatim in
the source. Quote validity is a grounding check, not a correctness score.

A model-led spot-check of 30 randomly selected rejections outside the gold
set classified 24 as correct, 3 as clear misses and 3 as borderline. The
user had not reviewed that spot-check. It is a signal to investigate lost
prospects, not a verified population error rate.

## What we learned

Strictness is a tradeoff. V1 kept real prospects but also many unsuitable
companies. V2 removed more unsuitable businesses and over-corrected on
genuine prospects with institutional customers, public contracts or repeat
relationships. Neither met all the gold-set acceptance criteria.

A broad business label is not enough. A company can serve business buyers
and still be searchable; a contractor can have a public website while most
work arrives through frameworks. The decision needs evidence about the
customer's ability to choose and contact a supplier.

Same-prompt variation was large enough to matter. A one-run score cannot
establish that a prompt revision improved recall. V2 was written after
inspecting v1's errors on the same cases, so those scores are optimistic;
blind and unused cases are the stronger generalisation test.

## Decisions and current position

The population uses v3's balanced-evidence screen, retaining `likely`,
`possible` and unparseable results. The web queue then removes flagged
duplicates. Short extracts are the practical input choice supported by
the initial cost comparison.

The richer filing profile remains separate. Passing the screen means
investigate the prospect, not endorse it as a good PPC customer.

## Limits and next evidence

The population removal target was met, but recall cannot be calculated from
an unlabelled population. V3 was not repeated on the population, and the
reviewed source does not establish that all gold-set acceptance conditions
were satisfied. All companies in this population run had XHTML filings;
the result does not establish screening quality on vision-transcribed PDFs.

Priorities are independent review of rejected companies, a blind evaluation
of the chosen policy, and repeated-run checks. The lost-prospect risk should
be measured alongside the savings from fewer website lookups.

## Evidence

- [Definitions, selection and acceptance criteria](../SEARCH_SCREEN.md).
- [Reference cases and selection record](../../evals/search_screen_gold_set/).
- [Saved comparison tables](../../logs/search-screen/results/).
- [Filing-classifier history that motivated the screen](filing-business-classification.md).

Saved reports are local evidence; see the [overview's source policy](README.md#sources-and-maintenance).
