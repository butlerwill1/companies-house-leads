# Search screen (stage 1 of the lead funnel)

A cheap, generous first stage that removes the clearly unsuitable companies
before anything expensive runs. It is deliberately not the Gate A2 business
profile (`docs/BUSINESS_PROFILE_EXTRACTION.md`), which stays frozen as an
analysis tool. This document was written before any labelling or paid model
run, so the definition and the acceptance criteria below are a
pre-registration: change them only by adding a dated note, never silently.

## Why this exists

Prompt versions v10 to v12 of the business profile showed that a filed annual
report rarely says *how* customers arrive, so `demand_model` answers
`unclear` most of the time and the search-addressable metric leans on a
fallback rule. A same-prompt rerun of v12 flipped 11 search answers, as many
as a prompt change did, so prompt iteration on that field is lost in noise.
Filings are good at saying what a business is and who it sells to. The screen
asks only that.

The stage sits after Gate A (`scripts/analysis/ch_company_triage.py`, free)
and before the website check. It optimises **recall**: it may keep a weak lead,
it must not drop a good one. Precision comes later, from the website check and
from ranking on the extracted financials (precision at k, where k is the number
of leads one person can work in a week).

## The question

> Would a potential customer, consumer or business, look for a business like
> this online and then buy, book or enquire directly?

## Labels

| Label | Meaning |
|---|---|
| `likely` | A trading business selling to customers who choose suppliers themselves: shops, e-commerce, hospitality, clinics, dentists, pharmacies, trades, local services, and business-to-business products or services that buyers search for (accountants, IT, parts suppliers). |
| `possible` | Trading, and the text gives no evidence either way about how customers choose or find the business. Not a default for "the report points towards `unlikely`". |
| `unlikely` | Holding or investment company with no named trade, SPV, financing or concession vehicle, captive or intragroup supply, investment property, or a trading business whose report shows its work comes through tenders, frameworks, contractor or client lists, long-term or repeat contracts with a few large clients, or a handful of contracted buyers. |

*Definition clarified 2026-09-30 (prompt `search-screen-v2-contractor-evidence`).*
The first wording said "tender- or framework-only" and "possible when in doubt".
A contractor is never tender-only, and "in doubt" was read as "when the evidence
points towards `unlikely`", so the v1 screen let about half of the gold
`unlikely` cases through. The labels were always written with the evidence rule
above; the prompt now says so.

Policy rules, agreed 2026-09-29:

- A group parent follows the group's trade. A holding company whose
  subsidiaries run a named trade is judged on that trade. This holds even when
  the company's own accounts show only rent or investment income, as in an
  operating-company / property-company split (clarified 2026-09-30 after
  Westover Holdings, 00714373: its turnover is rent, its subsidiary runs a
  hotel, spa and golf club). Passing the parent is how the screen keeps the
  subsidiary's trade, which may not be in the population on its own; the
  parent's turnover and profit then describe the property side, which matters
  for ranking, not for the screen.
- An NHS-funded service the patient chooses (GP, dentist, pharmacy) is
  `likely`. Council-placed care, where the council chooses the provider, is
  `possible`.
- Searchable business-to-business supply counts. Only tender- or
  framework-only, captive and holding businesses are `unlikely`.

The screen **passes** `likely` and `possible` and **rejects** `unlikely`. It
**fails open**: a rejected, empty or unparseable response counts as a pass,
because a screen must never drop a lead over a formatting error.

The screen does **not** judge lead quality. It asks only whether search-driven
demand plausibly exists. How good a lead is (how much of the revenue depends on
search, whether the company already does well online, whether it is worth a
call) is judged later, by the ranking stage, from the website check's evidence
plus the filing's figures, and scored by precision@k on its own gold set.

## Gold set

`evals/search_screen/cases/`, separate from `evals/business_profiles/`. The
business-profile gold set answers a different question with a different schema
and stays a clean historical record.

- **random cohort (150):** a seeded draw from the target population, which is
  enriched companies with turnover and profit figures, a `principal_activity`
  section, and not in the business-profile gold set. This is the cohort the
  headline numbers come from, because the old gold set was deliberately packed
  with boundary cases and would flatter neither recall nor removal.
- **hard cohort (about 30):** copied from the business-profile gold set:
  holding parents, SPVs and investment holdings, NHS-funded services,
  multi-channel retailers, tender-only contractors. Reported separately as a
  stress test, never mixed into the headline.
- **blind subset (25 of the random cohort):** labelled by the reviewer before
  seeing any draft, so the agreement between the reviewer and the drafter is
  measurable. A model draft that a person merely confirms is anchored; the
  earlier business-profile review changed only 6 of 109 drafts.

**Labelling protocol.** The drafter (Claude, a different model family from the
gpt-5.4-mini being evaluated) reads each case's stored narrative sections and
writes the label, a verbatim quote and a one-sentence reason. The reviewer then
verifies every case. Provenance (`drafted_by`, review status, changed-from) is
recorded per case.

Finding, 2026-09-29: the narrative sections stored in `companies-house.db`
are not usable as labelling text or as screen input. Of the 176 cases first
built from them, 71 had auditor boilerplate under `strategic_report` and 41
had iXBRL context junk (`bus:Director1 2024-01-01 ...`) as their
`principal_activity`. This is the stale-extraction bug described in
`scripts/profile/business_profile_refresh_sections.py`: the rows predate the
fix. The business-profile gold set avoids it by reading the whole filed
document minus the auditor's report
(`core.companies_house_extractor.filed_report_text`) from archived raw XHTML.
This gold set does the same: raw filings are fetched with
`scripts/profile/save_raw_filings.py` (free Companies House document API, no
model calls), each case's `sections` is rebuilt as a single `filed_report`, and
labels are judged from that. The screen's short input is then extracted from
the same clean text, so the gap between the two still measures what the short
input loses. A company whose only filing is a scanned PDF is replaced by the
next company in the seeded draw rather than transcribed, because transcription
is a paid vision-model call.

## Acceptance criteria (recorded before any paid run)

1. Recall on gold `likely` of at least 95%, and on `likely` plus `possible` of
   at least 90%, on the random cohort.
2. The screen removes at least 30% of the target population; otherwise the
   stage is not earning its cost.
3. It beats the free baseline (`scripts/screen/search_screen_baseline.py`) on
   recall or on removal by a clear margin; otherwise the baseline ships.
4. Criterion 1 holds on two separate runs of the same prompt; label flips
   between the runs are reported.

## Planned Phase 2 variants (recorded 2026-09-30, before any paid run)

Each variant is run on the gold set twice (run-to-run noise on the business
profile was as large as a prompt change) and scored on the criteria above.

**A. Input length.** Short extract (principal activity plus the start of the
strategic report, about 2,000 characters) against the whole filed report minus
the auditor's report (median 39,000 characters). About $0.35 against $2.50 per
gold run. The full filing is used in production only if it clearly improves
recall on gold `likely`; at 2,268 companies it costs about $30 a run against $4.

**B. Output shape: single quote against typed evidence.** The baseline output is
`{"answer", "quote", "reason"}`. The variant asks for evidence first, typed from
a closed list, and derives the label from it:

```json
{
  "evidence_for":     [{"quote": "...", "signal": "online_or_direct_channel"}],
  "evidence_against": [{"quote": "...", "signal": "tender_or_framework"}],
  "answer": "likely | possible | unlikely",
  "reason": "may only restate what the quotes say"
}
```

- Zero to three quotes on each side, each validated verbatim against the filing.
- Reject signals: `tender_or_framework`, `captive_or_intragroup`,
  `holding_or_spv_no_trade`, `few_named_contracted_buyers`,
  `investment_property`.
- Pass signals: `consumer_facing_type`, `online_or_direct_channel`,
  `marketing_or_acquisition`, `customers_choose_suppliers`.
- The label follows from the evidence by rule, in code: `unlikely` needs at
  least one valid reject-signal quote and no `online_or_direct_channel` quote;
  `likely` needs a pass-signal quote; conflicting or no evidence gives
  `possible`. The model's answer is recorded and compared with the derived
  label, but the derived label is what is scored.
- Fail open at the evidence level: an `unlikely` whose reject quotes all fail
  validation counts as a pass.

Why: in the gold review, the drafter (a model of the same kind) rejected
business-to-business service companies on evidence that is not on the reject
list: relationship language ("pillar clients", "key clients"), a sector
("defence"), and once on a quote that argued the other way ("clients in their
selection of agencies", Seen Group) plus an unquoted false claim about revenue
geography. A closed signal list blocks each of those, and it makes a reviewer's
correction specific: the tag is wrong, not "the answer is wrong".

## Cost

gpt-5.4-mini through OpenRouter, roughly 2,000 characters in and a short JSON
answer out, about $0.002 a company. One pass over the 2,300 enriched companies
with turnover and profit figures is about $4. Each paid run needs its own
explicit go.

## Status (2026-09-29)

Phase 1 (free), done except the human review:

- Case set built: 150 random-cohort and 26 hard-cohort companies (the hard
  cohort is 26, not 30, because the business-profile gold set has only two
  multi-channel retailers). Filings fetched from Companies House
  (`scripts/profile/save_raw_filings.py`, free document API): 150 of 150 as
  XHTML, none PDF-only, so no replacements were needed.
- Every case has a draft label, a verbatim quote validated against the filing,
  and a reason, written by `claude-sonnet-5-5`. Nothing is verified yet; the
  reviewer has not seen any draft.
- Draft distribution, random cohort: 57 likely, 37 possible, 56 unlikely
  (37% would be removed). Hard cohort: 7 likely, 7 possible, 12 unlikely.
- The blind sheet (25 random-cohort cases, no draft columns) is published to
  Drive. The review sheet, which shows drafts, is generated only after blind
  labels are imported: `review_rows` refuses to show a blind case that has no
  `blind_review` yet.
- Free baseline (`scripts/screen/search_screen_baseline.py`), scored against
  the **provisional draft labels**: on the random cohort it keeps 55 of 57
  likely (96.5%) and 83 of 94 likely-or-possible (88.3%), and removes 22.7% of
  the cohort and 24.9% of the 2,268-company target population. Its misses are
  two rules misfiring on real businesses: "holdings" in a company name drops
  trading group parents (a car-dealership group among them), and the primary
  SIC group is unreliable (housebuilders and fit-out contractors are filed as
  property development). The baseline was written before any label was scored
  and has not been tuned to these results.

Known limits of the drafts, to weigh during review:

- The drafter read an evidence pack (median 1.8 thousand characters), not the
  whole filing, and read the key-lines section clipped to 700 to 1,500
  characters. The published blind sheet trims a few long packs further to fit
  the connector; the case files hold the full pack.
- One filing spells it "principle activity" and its pack lacked the sentence
  until the builder was patched; that case was labelled from the filing text.
- The drafter's `possible` label is used whenever the text is silent on how
  customers find the business. About a quarter of the random cohort landed
  there, so the acceptance criterion on `likely` alone is the easier of the two.

Shortened-text re-draft (2026-09-29). Because the first drafts came from an
evidence pack, all 176 cases were re-drafted from a shortened filing
(`clean_full_text`), with no reference to the pack drafts. **This is not the
full filing.** Besides the auditor's report it drops the front page, statutory
boilerplate, number rows and most of the notes, keeping 19% of the characters
(1.3M of 7.0M; median 6.8k of 39k per filing). Correction 2026-09-30: this pass
was first reported as a full-text read, which it was not; the notes filter also
dropped some business-describing lines (turnover definitions in about 20
cases, related-party trading in 10). The key name `draft_full` is a misnomer
for this pass.
The result is stored beside the first draft as `draft_full`; neither is
verified and nothing was promoted. Comparison:

- 158 of 176 labels agree (89.8%). The 18 changes are all one step along the
  scale: 6 likely to possible, 4 unlikely to possible, 6 possible to unlikely,
  2 possible to likely. No case moved between likely and unlikely.
- 10 cases change pass or reject (6 pass to reject, 4 reject to pass). The
  random-cohort reject rate barely moves (37% to 38%).
- So the short pack was a fair proxy for the shortened filing; whether either
  matches the true full filing is untested. The
  shortened-text pass mostly made `possible` cases firmer where the filing shows
  tender, framework or contract-list revenue (rejects) or names a search
  channel or marketing spend (passes).
- Both drafts are one model, not blind to its own habits, so agreement between
  them is not the same as agreement with a human. The blind sheet is the real
  test.
- Labelling rules used in the shortened-text pass: `likely` for a consumer-facing
  type or explicit channel evidence; `possible` for a B2B trade or service
  filing silent about how customers choose; `unlikely` where the filing
  evidences tender, framework, repeat-client, contractor-list, PFI, captive,
  property-holding or platform-royalty revenue.

Review in Langfuse (2026-09-30). `search_screen_publish.py` exports the filings
(whole filed report, only the independent auditor's report removed) as text files (`logs/search-screen/full-text/`) and publishes the
dataset `search-screen-gold-draft`. `search_screen_queue.py sync` builds the
annotation queue "Search screen gold-label review" in the business-profile
Langfuse project: one trace per case (input: the whole filed report minus the auditor's report;
output: the draft, which was written from the shortened text), with the `search_screen` score pre-filled from the full-text draft.
Reviewer corrects the dropdown, may add `review_notes`, and presses Complete;
`search_screen_queue.py export --reviewer NAME` writes COMPLETED items back
into the case files as verified. The 25 blind cases stay out of both until
their blind labels are imported.

Phase 2, variant A result (2026-09-30). All 151 cases verified in the queue
(125 random, 26 hard) became the gold; the 25 blind cases are still unlabelled
and are not in it. The reviewer changed 5 of the 151 drafts. One run each of
`openai/gpt-5.4-mini`, prompt `search-screen-v1-single-quote`, temperature 0.
Random cohort, against the acceptance criteria:

| | short extract | full filing | free baseline |
|---|---|---|---|
| recall on likely (target 95%) | 40/42 (95%) | 41/42 (98%) | 41/42 (98%) |
| recall on likely+possible (target 90%) | 70/72 (97%) | 71/72 (99%) | 62/72 (86%) |
| share removed (target 30%) | 23% | 19% | 25% |
| cost, 151 cases | $0.13 | $1.12 | none |

Recall passes; removal fails criterion 2, and the model does not clearly beat
the free baseline (criterion 3). The full filing is no better than the short
extract: slightly higher recall, lower removal, about 9 times the cost. Use the
short extract. The screen passes about half of gold `unlikely` (34/65 short,
27/65 full): it treats almost any trading business, including construction
contractors and B2B service firms the gold marks `unlikely` on tender,
contract-list or captive evidence, as searchable. It also says `likely` for
most gold `possible` cases, which is harmless for pass or reject. This is the
failure variant B (typed evidence) is meant to fix. No run-to-run noise check
yet. Sheets: see `logs/search-screen/results/` and Drive.

Prompt v2 result (2026-09-30). Prompt `search-screen-v2-contractor-evidence`
(registered in Langfuse as `search-screen` entry 2; v1 is entry 1) changes the
definition only: evidence of how work is won decides, `possible` means no
evidence either way, and the "tender- or framework-only" wording is gone. Short
extract, same model, run twice (runs 1 and 2). Random cohort, 125 cases:

| | v1 | v2 run 1 | v2 run 2 | v2, reject only if both runs reject |
|---|---|---|---|---|
| recall on likely (target 95%) | 40/42 (95%) | 39/42 (93%) | 38/42 (90%) | 39/42 (93%) |
| recall on likely+possible (target 90%) | 70/72 (97%) | 63/72 (88%) | 61/72 (85%) | 66/72 (92%) |
| share removed (target 30%) | 23% | 46% | 46% | 42% |
| gold unlikely rejected | 27/53 | 49/53 | 47/53 | |

The rule works: removal doubles and almost every gold `unlikely` is rejected.
It over-corrects: the model now rejects real leads whose report mentions public
contracts, institutional buyers or relationships (an NHS GP practice despite the
explicit NHS rule, a recruitment firm that also has SEO, a coach operator with
private hire, a trade platform, care and consultancy firms). Neither v1 nor v2
meets both the recall and removal targets. Run-to-run noise is real: 11 cases
flip between pass and reject and 15 change label between two identical v2
runs, so a single run is not a number to trust. v2 was written after reading
v1's errors on these same cases, so its scores here are optimistic; the blind
25 and the unlabelled reserve are the honest test. Cost: $0.15 a run.

Population run (2026-10-01). Prompt `search-screen-v3-balanced-evidence`, short
extract, `openai/gpt-5.4-mini`, one run, all 2,268 target companies (every one
had an XHTML filing; 2,000 were fetched for this). Cost $2.45. Result stored in
`company_search_screen` and logged to Langfuse as a dataset run in
`search-screen-population`:

| answer | companies |
|---|---|
| likely | 1,168 |
| possible | 404 |
| unlikely (rejected) | 695 (30.6%) |
| unparseable (passed, fails open) | 1 |

The population removal rate, 30.6%, meets the 30% target (the gold cases gave
27 to 29%). 2,257 of 2,267 parsed responses cite a quote found verbatim in the
text. Spot-check of 30 random rejected companies outside the gold set, read by
Claude: 24 correct rejections (property investment, holding, PFI vehicles,
construction contractors showing tender or framework work); 3 clear misses
(a chemicals group's holding company, which should follow its group, a
recruitment firm, a B2B manufacturer with no evidence either way) and 3
borderline (two contractors rejected on assumption rather than evidence, a
property group with a care subsidiary). That suggests roughly one rejected
company in ten may be a lead, higher than on the gold cases; the sample is
small and unreviewed by the user. v3 was not rerun for noise on the population.

Phase 2 (paid, each run needs an explicit go): screen module, context
comparison, noise check, population run. Results are recorded here when the
run is accepted.
