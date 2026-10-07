# Website discovery and technology: experiment review

[Experiment overview](README.md) | Evidence reviewed through 2026-10-05

## Question

Can a legal company be connected to the website its customers actually use,
and can a crawl provide useful evidence about that site's marketing setup?

There are two tests here: identity and observation. Detecting a CRM perfectly
on the wrong company's website is still a failed enrichment. Equally, a
correct website match does not imply that every tool will be visible to a
crawler.

## Experiments and results

### Lookup cost and identity

The first population lookup used Maps-first searches. After 32 companies,
a places-first option was compared on the same companies.

| Observation | Result |
|---|---|
| Maps-first mean allowance use | 4.1 Serper credits per company |
| First Maps search versus first places search | 3 credits versus 1 credit |
| Same website choice under places-first | 31/32 companies |
| Places versus Maps category availability | 23/32 versus 26/32 |
| Places versus Maps rating availability | 21/32 versus 23/32 |

Places-first preserved most website choices and used a cheaper initial call,
but returned thinner listing metadata. The 31/32 agreement is between two
resolver choices, not accuracy against 32 independently labelled websites.

The October 3 initial run processed 1,113 companies before the 2,500-credit
allowance was exhausted. Its tiers were 353 verified, 330 probable, 266
ambiguous, 122 blocked and 42 none. The stage note calls these companies
resolved, but that includes unsuccessful and uncertain results: it does not
mean 1,113 websites were verified.

The stored snapshot had 690 companies with a chosen site, including
hand-found sites. On 21 hand-found test companies, the automatic choices
agreed with 17. Failures included a nursery directory, a sister brand using
the same company number, and plausible legal-name domains without sufficient
identity evidence. Processing the 100 identity reference cases was recorded;
the source notes do not supply a completed blind accuracy score.

Source: [the W1 population and pilot notes](../WEB_STAGE_PLAN.md#w1-website-and-maps-listing-built-including-the-three-fixes).

### Resolving ambiguity from wider evidence

The next step reread crawled pages without new searches. A loose first pass
promoted 71 of the 266 ambiguous companies. After examining news mentions,
client case studies and namesakes, stricter rules promoted only 41: 4 by
company number, 35 by legal-name disclosure or corroboration, and 2 by name
and postcode.

A subsequent GPT-5.4-mini identity check was applied to 177 plausible
candidates. It returned 69 `same_business`, 97 `different_business` and 11
`cannot_tell`, at a documented cost of $0.16. Twenty positive verdicts lacked
a UK corroborating sign and were held for review; 49 were promoted.
Sister-company sites sometimes received inconsistent verdicts.

The browser fallback read 20 of 92 refused domains. It resolved only 2 of
the 122 blocked companies; 120 remained blocked. It did not bypass challenges.
After these follow-ups, the stage note reported 780 companies with chosen
websites across 738 domains, with 176 ambiguous, 120 blocked and 42 none.
These are successive working snapshots, not additive totals for the whole
1,538-company queue.

Source: [the ambiguity and model-check history](../WEB_STAGE_PLAN.md).

### Crawl and technology evidence

The initial 20-site pilot crawled 336 pages. Google Ads tag presence agreed
with Ads Transparency activity on 19/20 sites. The exception, Rettie, had
advertising evidence but no tag visible on crawled pages. Two false positives
were diagnosed in Tag Manager code: a link-click trigger mistaken for
LinkedIn and a runtime string mistaken for an advertising signal.
See [the pilot findings](../WEB_STAGE.md#status-2026-10-02-later).

The October 3 wider crawl recorded 1,023 sites and 14,136 pages, including
unproven candidates. Detection's documented snapshot covered 691 chosen
sites: 653 readable, 17 thin and 21 refusing downloads.

| Signal in the 653 readable sites | Sites |
|---|---|
| Google Ads tag | 251 |
| Ads conversion event | 178 |
| GA4 | 468 |
| Tag Manager | 351 |
| CRM | 96 |
| Call tracking | 21 |
| Booking feature | 37 |
| Checkout feature | 143 |

Source: [the W2 population observations](../WEB_STAGE_PLAN.md#w2-site-crawl-and-technology-detection-built-free).
These are detection frequencies, not sensitivity or accuracy scores. A
completed crawl record does not imply that every site was readable.

## What we learned

Identity evidence must tie the trade to the legal company. A legal name in
a news story or a matched phone belonging to a namesake can look persuasive
without proving the link. The stricter settlement pass deliberately gave up
apparent coverage to remove unsupported promotions.

Company-number matches are strong evidence but can still point to a sister
brand rather than the customer-facing trade of interest. Trading names and
group relationships need interpretation alongside legal identity.

Technology rules need negative examples. Shared JavaScript runtimes contain
names and strings unrelated to the site's actual tools. Tags are evidence
of setup, not proof of campaign activity or effective measurement.

Caching creates an important experimental advantage: identity checks and
technology rules can be revised against saved pages without repeating
searches or downloads. Domain-level storage also avoids treating companies
sharing a website as independent crawls.

## Decisions and current position

Use places-first lookup with identity checks, retain distinct verified,
probable, ambiguous, blocked and none outcomes, and add trading-name evidence.
Apply conservative settlement rules and hold uncertain model promotions for
review. A model's positive identity verdict is not automatically a verified
registration match.

Keep raw crawl evidence separate from versioned detection rules. Treat
unreadable sites as missing evidence and avoid turning absent detections into
confident assertions that a business lacks the tool.

## Limits and next evidence

The hand-found pilot was selected, and the model check targeted difficult
cases. Neither establishes accuracy on a random population. The promising
places-first agreement needs an independent correctness check, and model
promotions need review for group and sister-brand cases.

A reviewed blind identity result and independent technology checks would
strengthen the evidence. Coverage should be reported per company and per
domain, with denominators tied to the relevant snapshot.

## Evidence

- [Identity definitions and acceptance criteria](../WEB_STAGE.md#w1-identity).
- [Dated identity, settlement and crawl results](../WEB_STAGE_PLAN.md).
- [Reference-case selection](../../evals/website_identity_gold_set/selection.json).
- [Saved lookup pilot](../../logs/web/identity-places-trial.jsonl).
- [Model-check decisions](../../logs/web/settle-model-checkpoint.jsonl) and [held matches](../../logs/web/settle-model-review.csv).
- [Wider crawl record](../../logs/web/crawl-run-2026-10-03.json).

Saved reports are local evidence; see the [overview's source policy](README.md#sources-and-maintenance).
