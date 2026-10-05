# Website classification and advertising: experiment review

[Experiment overview](README.md) | Evidence reviewed through 2026-10-05

## Question

Can website text and search-provider evidence describe a company's customer
journey, assess whether search suits its offer, and identify a useful
advertising conversation?

The work combines several tests: the quality of website classifications,
the usefulness of generated search phrases, and the reliability of different
advertising observations. These should not be reduced to one score.

## Experiments and results

### Advertising and market-data pilot

The October 2 pilot used 23 user-chosen companies across 20 distinct,
hand-found websites, verified by company number. The documented DataForSEO
cost was $0.27. The sample was selected, not random.

| Advertising source | Observation on the pilot |
|---|---|
| Ads Transparency | Found recent Google advertising for 11/20 websites. |
| Labs paid-traffic estimate | Found paid traffic for only 3 of those 11 advertisers. |
| One live search per company | Did not show any of the 11 known active advertisers in its ads. |

Ads Transparency therefore supplied much stronger evidence of activity in
this sample. Live searches still added organic position and map visibility.
The failure to see an ad once did not demonstrate that a company was not
advertising. Source: [the market-data pilot](../WEB_STAGE.md#w4-findings-2026-10-02-23-user-chosen-companies).

Maps linked 17/20 sites to a listing. Categories were commercially useful
but sometimes misleading, such as an investment-company category for a
finance business or a cafe category for a wider venue operation. The site
description remained necessary alongside the listing category.

### Website-profile prompt and evidence checks

GPT-5.4-mini profiled 21 pilot companies with usable site text. The model
saw the website, the filing's principal activity and any Maps category.

V1 had nine quote-check failures. Five were exact principal-activity quotes
from an input the validator had ignored. Rechecking against all supplied
sources reduced nine failures to two. V2 added corrected source validation
and a targeted quote retry; two companies needed a retry and both were
fixed. All quotes then passed, with two fields answered `unclear`.

This established better evidence handling. It did not establish correct
classification. Against model-drafted reference labels, v2 agreed on customer
type for 13/21, conversion for 16/21, geography for 16/21, urgency for 15/21,
sale-value band for 11/21 and channel fit for 18/21. Those labels required
human review. Source: [the W3 pilot history](../WEB_STAGE_PLAN.md).

### Phrase quality and category assignment

Of the model's search phrases, 248/379 in v1 and 259/378 in v2 had reported
volume, compared with 104/110 hand-drafted phrases. The phrase sets differed,
so this is an indication of phrase usefulness, not a controlled accuracy
comparison.

For SDC Clinics, the aggregate swung from 337,170 monthly searches in v1
to 3,860 in v2: the first included a very broad national phrase, while the
second repeatedly inserted Scotland into the wording. The demand threshold
changed side for only 1/21 companies, suggesting it separated little in this
pilot even though the volume estimates moved substantially.

The word-overlap category shortlist included the known Maps category for
only 6/19 companies. That is a shortlist-recall diagnostic, not final category
accuracy. It showed that model-assigned categories without a listing needed
stronger evidence.

### Expanded references and population profile

The references grew to 80 active cases: 19 pilot cases plus 61 seeded draws
from companies with chosen, readable sites. The draw excluded very short
site text and used one company per website. Claude drafted labels and phrases
for review, separately from GPT-5.4-mini's answers.

The October 4 v3 run covered 768 company entries: 686 in queue order plus
82 reference-case entries, including inactive cases. Of these, 759 had site
text and 9 did not. The documented total cost was $3.30; 35 required a quote
retry and 8 still had a failed quote. The stage note reports 751 entries
without a problem. This is a processing result, not 751 correct profiles.

| Field | Agreement with 80 draft references |
|---|---|
| Customer type | 52/80, 65.0% |
| Tender dependence | 50/80, 62.5% |
| Purchase urgency | 45/80, 56.3% |
| Conversion route | 57/80, 71.3% |
| Geography | 56/80, 70.0% |
| Typical sale value | 55/80, 68.8% |
| Search/social fit | 65/80, 81.3% |

The dominant disagreements were over `mixed` customers (26 of 28 customer
misses), tender `unclear` versus reference `no` (26 cases), and `planned`
versus reference `considered` urgency (28 cases). The records propose v4
definition changes; they do not report a completed v4 evaluation.
Source: [the October 4 population and draft comparison](../WEB_STAGE_PLAN.md).

## What we learned

Grounded quotes and correct labels are different checks. A model can quote
the site accurately while interpreting an ambiguous category differently
from the reference drafter. The reference may also need correction.

The conversion field needed to describe the main route offered by the
website, not the eventual contract or purchase mechanism. V3 separated
tender dependence from conversion and replaced `quote_form` with
`enquiry_form`. Urgency, customer mix and sale value still needed clearer
boundaries.

Demand estimation depends on phrase selection as well as the API. Broad
national terms and heavily localised wording do not measure the same market.
A threshold over their summed volumes cannot be interpreted as stable
commercial opportunity without consistent phrase and location rules.

## Decisions and current position

Use Ads Transparency for advertising activity, Labs for organic estimates,
and optional live searches for point-in-time visibility. Keep observed ads,
detected tags and estimated traffic distinct. Search volume multiplied by
cost per click is a market ceiling, not company spend.

Keep Maps categories beside business descriptions and distinguish listing
categories from model assignments. V3 is the recorded population profile;
the draft disagreements inform proposed improvements, not proven fixes.

The proposed phrase policy removes incidental location wording and measures
demand for the business's area, with destination-business exceptions. The
notes describe this policy as proposed; this review does not claim that its
full location-aware measurement has been validated on the population.

## Limits and next evidence

The advertising pilot is small and selected. Ads Transparency itself is
an observation source, not exhaustive knowledge of every campaign. Website
references were drafts at the reported comparison, and the expanded draw
excluded unreadable and very short sites. No score here establishes
performance on those missing sites or predicts sales outcomes.

Priorities are human adjudication, a repeatable phrase benchmark, independent
category checks and a controlled comparison of the proposed definitions.
The resulting lead findings also need review against real contact outcomes.

## Evidence

- [Advertising pilot results](../WEB_STAGE.md#w4-findings-2026-10-02-23-user-chosen-companies).
- [Website-profile experiments, proposed changes and findings design](../WEB_STAGE_PLAN.md).
- [Pilot label drafts](../../evals/web_profile/drafts-2026-10-02.json) and [expanded drafts](../../evals/web_profile/drafts-2026-10-04.json).
- [Seeded reference selection](../../evals/web_profile/selection-2026-10-04.json).
- [Saved market observations](../../logs/web/research-report.json) and [website-profile checkpoint](../../logs/web/profile-checkpoint.jsonl).

Saved reports are local evidence; see the [overview's source policy](README.md#sources-and-maintenance).
