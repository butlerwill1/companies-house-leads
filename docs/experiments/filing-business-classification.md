# Filing-based business classification: experiment review

[Experiment overview](README.md) | Evidence reviewed through 2026-10-05

## Question

Can a company's filed narrative reliably describe what it sells, its
customers and delivery model, and how those customers find it? Can those
classifications identify businesses that paid search could reach?

The experiments exposed two distinct tasks. Accounts often describe a
company's activities and customers well enough to support a business profile.
They are much less consistent about the source of new demand. A correct
description is therefore not sufficient evidence for an acquisition-channel
classification or a lead-exclusion decision.

## Experiments and results

### Model and context comparison

The August comparison used 19 companies, narrative sections versus the whole
filed document, and four models. The historical review reports the following
results after the initial whole-document validation problem was addressed:

| Model | Input | Mean field accuracy | Quote/validation pass rate | Estimated sample cost |
|---|---|---|---|---|
| Gemini 3.7 Flash | Whole document | 81.6% | 100% | $0.183 |
| Gemini 3.7 Flash | Narrative | 73.7% | 89.5% | $0.071 |
| Claude Opus 5 | Narrative | 78.1% | 100% | $0.573 |
| Claude Opus 5 | Whole document | 74.6% | 94.7% | $2.293 |
| Gemini 2.5 Flash Lite | Narrative | 67.5% | 94.7% | $0.008 |
| Gemini 2.5 Flash Lite | Whole document | 31.6% | 42.1% | $0.034 |
| Gemini 2.5 Flash | Narrative | 49.1% | 63.2% | $0.037 |
| Gemini 2.5 Flash | Whole document | 48.2% | 63.2% | $0.113 |

Source: [the August 21 review](../BUSINESS_PROFILE_HARNESS_REVIEW_2026-08-21.md#evidence-every-comparison-run).
Model names and estimated costs are historical records. Mean field accuracy
averages different classification tasks, not commercial lead success.

Whole-document input helped one model and hurt others. A more expensive model
or longer context was not automatically better. The sample was too small to
establish a durable model ranking.

### Taxonomy, definitions and validation

On the same 19 cases, the documented demand-model accuracy moved from 36.8%
under nine classes to 47.4% when the same predictions were rescored under
merged classes, then to 57.9% with a new prompt containing definitions.
The first improvement partly reflects an easier classification task; the
second involved new model responses. Those effects must remain separate.

September's 109-case GPT-5.4-mini runs uncovered another source of apparent
failure. XHTML flattening split words, table columns became interleaved, and
minor quote differences caused otherwise useful responses to be rejected.
The pipeline changed to reject failed fields individually and allow bounded
quote matching while protecting numbers, negations and classification words.

The design reference records that saved-response rescoring reduced dropped
fields from 30 to 16 for the earlier v6 responses and from 20 to 10 for v7.
The reports identify the policy used at rescoring time, so a rescored report's
prompt label alone is not sufficient to identify the original generation.
These were validation improvements without new model calls, not evidence
that the model had learned to produce better quotes.
See [the validation history](../BUSINESS_PROFILE_EXTRACTION.md#prompt-design).

### Business-relevant search results

Later experiments used 124 reviewed cases, including 108 trading businesses,
8 SPVs and 8 investment holdings. For the saved search metric, 121 cases were
considered and 41 were reference positives. The remaining cases were excluded
by the metric's handling of insufficient reference information.

| Saved generation | Search positives found | Precision | Recall |
|---|---|---|---|
| v10, September 24 | 20/41 | 100.00% | 48.78% |
| v11, September 28 | 21/41 | 95.45% | 51.22% |
| v12, September 28 | 23/41 | 92.00% | 56.10% |
| v12 repeat, same date | 25/41 | 96.15% | 60.98% |

Sources: the saved [v10](../../logs/business-profile-eval/report-20260924T151422.json),
[v11](../../logs/business-profile-eval/report-20260928T150555.json),
[v12](../../logs/business-profile-eval/v12-full/report-20260928T155705.json)
and [v12 repeat](../../logs/business-profile-eval/v12-repeat/report-20260928T191720.json)
reports. These are their stored scores before the later mixed-customer floor
rescoring, not scores under every subsequent rule revision.

The floor is a derived rule: when demand is unclear, certain consumer-facing
delivery categories can still count as reachable. Extending it to mixed
customers rescored the same v12 responses to 31/41 found, precision 91.18%
and recall 75.61%; the repeat rescored to 29/41, precision 93.55% and recall
70.73%. The improvement came from lead-selection logic, not new generations.
See the [v12 floor rescore](../../logs/business-profile-eval/rescore-mixed-floor/report-20260928T190659-rescore.json)
and [repeat floor rescore](../../logs/business-profile-eval/rescore-mixed-floor/report-20260928T191801-rescore.json).

The search-screen design records 11 pass/reject changes between identical
v12 runs. That variability was a reason to change the task rather than
attribute every difference between prompt versions to the prompt.

## What we learned

Accuracy needs a baseline and a business interpretation. Always answering
`trading` would score 108/124, or 87.1%, on the expanded status set. A similar
headline accuracy therefore says little about whether the model finds SPVs
and holdings. Each minority class has only eight cases, so its recall remains
a challenge-set result rather than a production guarantee.

Source preparation and validation are part of the experiment. The August
comparison's first saved whole-document results had extensive rejection;
the follow-up and historical review show why those initial results should
not be treated as a model leaderboard. September's quote repairs provided
another example of useful responses being lost in the surrounding pipeline.

Definitions must describe separable facts. Merging `trading_group_parent`
into `trading` removed a distinction that was inconsistently applied and not
used by the search metric. It simplified the task but did not demonstrate a
new ability to identify the correct legal entity within a group.

## Decisions and current position

Keep the richer filing classifier as an analysis tool. Use the separate
search screen for the simpler question of whether a customer could search
for and contact this kind of business, and use websites for the customer
journey and marketing evidence accounts omit.

Retain evidence quotes, field-level validation, explicit taxonomies and
derived-rule versions. A missing or unclear acquisition channel can be an
honest reflection of the filing, not a reason to invent one.

## Limits and next evidence

Model comparisons span different case sets, taxonomies, text preparation and
validators. They cannot be joined into a single improvement curve. Some
references were drafted by a model before human review, which also makes
cross-model adjudication valuable. Minority cases and repeated runs need
more evidence than the headline average supplies.

The next useful evidence concerns stable minority-class recall, repeatability
and independently reviewed search prospects. The derived search-opportunity
review remains experimental; it is not a production selection rule.

## Evidence

- [Business-profile design and derived search metric](../BUSINESS_PROFILE_EXTRACTION.md).
- [Metric, taxonomy and validation improvement history](../BUSINESS_PROFILE_CLASSIFIER_IMPROVEMENT_PLAN.md).
- [August comparison reports](../../logs/business-profile-context-ab/report-20260820T204438.json) and [whole-document follow-up](../../logs/business-profile-context-ab/report-20260820T205603.json).
- [Why the separate search screen was introduced](../SEARCH_SCREEN.md#why-this-exists).

Saved reports are local evidence; see the [overview's source policy](README.md#sources-and-maintenance).
