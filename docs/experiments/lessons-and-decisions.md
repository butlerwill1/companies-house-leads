# Lessons and decisions

[Experiment overview](README.md) | Evidence reviewed through 2026-10-05

## Question

Which findings should shape the next stage of the project, and which ideas
remain hypotheses rather than established improvements?

The experiments repeatedly showed that final scores depend on more than
the model. Source preparation, category definitions, validators, identity
matching and the interpretation of search-provider evidence all changed
what the system appeared to know.

## Decisions supported by the experiments

| Decision | Evidence and reasoning | Position at this review |
|---|---|---|
| Separate the search screen from the filing-based business profile. | Acquisition-channel labels missed prospects and varied between reruns; the simpler screen directly asks whether customers could search and contact the business. | Adopted; the richer profile remains an analysis tool. |
| Use short extracts for the first screen. | Whole filings cost about nine times as much in the v1 comparison while retaining only one additional positive and removing fewer companies. | Adopted as a cost tradeoff; not evidence that long context is always worse. |
| Inspect intermediate decisions in PDF extraction. | Correctly found pages still failed when statement type or group/company scope was wrong. | Adopted diagnostic approach; separate financial and employee results. |
| Validate source preparation and individual fields. | Split words, interleaved table columns and ignored input sources rejected useful evidence; whole-response rejection discarded fields that passed. | Adopted, with bounded quote matching and explicit rejection records. |
| Report baselines, coverage and minority classes. | A trading-status classifier can score 87.1% by always choosing the majority class on the 124-case set. | Adopted measurement approach; eight cases per minority class remain limited evidence. |
| Prefer cheaper places-first discovery with identity checks. | Website choices matched Maps-first for 31/32 pilot companies, with thinner listing metadata. | Adopted lookup strategy; independent correctness still needs measurement. |
| Keep identity promotion conservative. | Loose settlement promoted news mentions and namesakes; stronger disclosure and corroboration checks reduced promotions from 71 to 41. | Adopted, accepting unresolved cases rather than counting weak evidence as success. |
| Separate ad activity, site setup and traffic estimates. | Ads Transparency found 11 advertisers; paid-traffic estimates found only 3 of them, and live searches found none in their ads. | Adopted source roles for the tested market; pilot generalisation remains limited. |
| Tighten website definitions and phrase/location policy. | Customer mix, urgency and tender labels disagreed with drafts; wording caused large demand-estimate swings. | Proposed changes; the source records no completed v4 benchmark. |
| Validate lead prioritisation against outcomes. | Current findings and sorting rules combine useful observations, but the reviewed evidence contains no demonstrated sales-prediction score. | Open; contacted, replied, meeting and won outcomes are the intended evidence. |

Sources: the reviews of [financial extraction](financial-extraction.md),
[filing classification](filing-business-classification.md),
[screening](search-screen.md), [identity and technology](website-discovery-and-technology.md)
and [website classification and advertising](website-classification-and-advertising.md).

## What the experiments taught us

### Start with the decision the stage needs to support

A rich profile can be valuable without being a reliable filter. Accounts
may say what a business does but omit its customer-acquisition channel.
The filing classifier and the search screen therefore have different jobs:
describe the business and retain plausible prospects, respectively.

That separation also keeps commercial logic revisable. Extending the
mixed-customer floor changed the search score without changing the model's
answers. It improved retrieval under a new rule, not the model's accuracy at
reading acquisition channels.

### Diagnose failures before buying another model run

The first whole-document comparison had extensive validation rejection;
later responses and review showed that those scores were not a clean model
ranking. Website quotes from a supplied principal-activity line failed a
validator that ignored that source. PDF pages were located correctly but
interpreted under the wrong statement label.

These examples support inspecting inputs, evidence and rejected fields
before attributing a low score to model capability. Saved responses and
crawls make many of those checks possible without another paid call.

### Every improvement has a denominator and a tradeoff

The stricter screen removed more unsuitable businesses and lost more useful
ones. More PDF locator resolution helped employee values in one comparison
while core-financial values fell. Cheaper website discovery returned less
listing metadata. Conservative identity rules reduced apparent coverage.

Those tradeoffs should be recorded beside the benefit. Cell accuracy,
whole-document accuracy, population coverage, removal rate and model-draft
agreement measure different things. None is a substitute for sales outcomes.

### Evidence validity does not settle the interpretation

An exact quote proves that the words were in the supplied source. It does
not prove that `mixed`, `planned` or `considered` was the right label. A site
mention can be accurate yet refer to a client. A company number can verify
a legal entity while the chosen site serves a different sister brand.

Reviewed definitions and identity evidence matter as much as syntactically
valid model output. Disagreements with draft references are opportunities
for adjudication, not automatic model errors.

### Missing observations are not universal negatives

The pilot's live search missed all the advertisers known through Ads
Transparency. One advertiser had no tag visible on the crawled pages.
Blocked and thin sites could not provide the same evidence as readable ones.

Lead findings should preserve those limits. A missing detected tool can
justify a question about measurement; it cannot by itself establish that
the business has no measurement system or that its advertising is ineffective.
Likewise, a strong existing setup is evidence to recognise, not a gap to invent.

## Approaches rejected or left unproven

The evidence did not support the rule that more context always helps, a
blanket promotion of similar-name websites, paid-traffic estimates as the
primary advertiser detector, or a single live search as evidence of no ads.
It also did not establish that stricter rejection is better regardless of
lost prospects, or that the filing-profile average accuracy measures lead fit.

Two-model transcription agreement remains a useful cross-check without a
human-labelled benchmark. Proposed v4 website definitions, location-aware
phrase measurement and outcome-based ranking remain questions for new
evidence. This collection does not mark them complete because the code or
plan exists.

## Evidence discipline to carry forward

Record the generating model and prompt separately from the validator and
scoring rule. Preserve the case set and label status, and distinguish a new
generation from a rescore. Repeat important comparisons because identical
prompts have changed pass/reject answers.

Every evaluated case needs an inspectable trace and durable saved output,
including failed or rejected cases. One shared Langfuse instance now holds
those experiment records; local checkpoints prevent interrupted runs from
losing completed work. These practices arose from actual project failures,
including missing per-case traces and batch work lost before final output.
See [the repository's experiment discipline](../../.claude/skills/langfuse-eval-discipline/SKILL.md).

## Open questions

The strongest next checks are independent blind identity and screen results,
reviewed website labels, phrase quality under a consistent location policy,
and the commercial usefulness of the first lead sheets. Unreadable sites,
group identities and unusual financial statements should stay visible in
those samples rather than being silently excluded from the claimed result.

The eventual test is whether the evidence helps select businesses worth
contacting and produce accurate, relevant conversations. Until outcomes are
available, that remains the project's objective rather than a measured success.
