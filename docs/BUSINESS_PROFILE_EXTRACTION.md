# Business profile extraction (Gate A2)

Spec for the text-only LLM stage that reads a company's filed narrative and
records how it acquires customers. Nothing here is built yet.

This sits between Gate A ([core/company_triage.py](../core/company_triage.py),
deterministic, free) and any website stage. It runs second because it is
cheap, and because the filed narrative is **authoritative by construction**:
it is the company's own director-signed statement of what it does. A website
has to be *found* first, and matching can pick the wrong domain — the
existing `website_investigations` data contains probable mismatches
(`PENKETH GROUP HOLDINGS` matched a domain whose business model contradicts
its SIC). Narrative sets the prior; the website confirms or overrides it.

## Why this stage exists

SIC cannot express the thing that matters. `93110` covers both a local gym
and Cambridge United; `62020` covers a staffing agency at 7.5% gross margin
and a product company at 59.4%. Making the industry axis finer cannot fix
a distinction that lives on a different axis — and 5-digit SIC is already
the most granular level UK SIC has.

The narrative can. From the live data:

| Company | SIC says | Narrative says |
|---|---|---|
| `12575756` VIPER GROUP | Software / IT consultancy | "the sale of electrical goods" |
| `12683499` CRANES 2000 | Estate agents / property mgmt | "crane hire ... serves London and the Home Counties" |
| `05529904` RAPT LEISURE | Sport / fitness / gyms | "design, construction and maintenance of leisure facilities" |
| `00482197` CAMBRIDGE UNITED | Operation of sports facilities | "a community focused professional football club" |

Explicit keyword markers are rare (4.4% B2B, 0.7% B2C), which is exactly why
this needs a model rather than regex: none of the above contain the phrase
"B2B" or "B2C", yet all are unambiguous to a reader.

## Input

Per company, the most recent `narrative_run` only (a company has one run per
filing since the history backfill; mixing years would be wrong).

Sections in priority order: `principal_activity`, `business_review`,
`strategic_report`, `directors_report`, `principal_risks`,
`future_developments`.

**Skip any section where `section_payload.is_auditor_text` is true.** That
flag means the text is the auditor describing its audit, not the company
describing itself — about 6% of sections. Feeding it in produces confident
nonsense about "posting inappropriate journal entries".

Median usable text is roughly 750 words per company across 2,330 companies
that have both narrative and turnover.

## Output schema

Four classification fields, each an object with `value`, `confidence`
(0.0–1.0), `evidence_quote` (verbatim), and `evidence_section`.

### `demand_model` — the target

How customers actually arrive. This is the only field that directly
determines whether paid search can work.

| Value | Meaning | Example from live data |
|---|---|---|
| `consumer_search` | individuals search and buy | e-commerce retail |
| `local_service` | individuals search for a nearby provider | `13400880` opticians; `SC390599` restaurant |
| `considered_b2b` | businesses research then enquire | `05898590` "IT services to business customers" |
| `tender_framework` | won by tender, prequalification, framework | `06717844` "main building contractors for construction contracts" |
| `relationship_repeat` | existing accounts, referrals, repeat trade | `12683499` crane hire |
| `platform_intermediated` | demand arrives via marketplace/OTA/aggregator | hotels via OTAs |
| `wholesale_contract` | few large buyers under contract | manufacturing supply |
| `not_customer_facing` | holding vehicle, SPV, investment company | `SC540426` "investment holding company" |
| `unclear` | text does not support a call | — |

### `customer_type`
`b2c` | `b2b` | `b2b2c` | `public_sector` | `mixed` | `unclear`

### `delivery_model`
`product_physical` | `product_digital` | `saas` | `professional_service` |
`trade_service` | `contracting` | `distribution_resale` | `rental_leasing` |
`property` | `unclear`

### `geography_served`
`local` | `regional` | `national_uk` | `international` | `unclear`

### Supporting fields

- `business_description` — one sentence, plain English, what they actually do.
- `trading_status_confirmed` — `trading` | `investment_holding` |
  `trading_group_parent` | `dormant` | `spv` | `unclear`. This exists to
  resolve the **369 companies** Gate A flagged `turnover_without_employees`
  and deliberately refused to guess about. The narrative states it plainly:
  "an investment holding company" versus "the company **and group**
  continued to be that of motor car retailers".
- `sic_agreement` — `agrees` | `disagrees` | `unclear`, plus `reason`.

## Prompt design

**Evidence quotes are mandatory and must be verbatim.** This is the single
most important design decision, because it makes hallucination
*programmatically detectable*: assert `evidence_quote in section_text`
before accepting any field. A quote that does not appear in the source
fails the whole extraction — no model self-reporting required. Track the
pass rate as a headline metric.

**No quote means `unclear`.** `unclear` is a correct, expected answer, not a
failure. The taxonomy exists to be refused.

**Withhold the SIC code until after the business description.** If the model
sees "Sport / fitness / gyms" before describing Cambridge United it will
anchor on it, and the independent read — the entire point of this stage — is
lost. Either ask in two turns, or order the JSON keys so
`business_description` and the four classifications precede `sic_agreement`,
since earlier fields condition later ones in an autoregressive model.

**Ask for observation, not derivation.** The model reports what the filing
says. Anything downstream (advertising vertical, scoring, spend estimates)
is computed deterministically from these fields, so it can be changed
without re-running the model and is never confused with evidence.

**One company per call.** Batching invites cross-contamination between
companies, and per-company calls make retries and partial failures trivial.

## Harness

Mirrors [scripts/vlm/](../scripts/vlm/) rather than inventing a second
pattern — same config shape, same MLflow conventions, same gold-case layout.

```
scripts/profile/
  companies_house_business_profile.py   # pipeline: read narrative -> model -> validate -> persist
  business_profile_policy.py            # taxonomy, validation, quote verification
  business_profile_eval.py              # eval runner, MLflow-tracked
  business_profile_review.py            # human review / gold-case authoring
  README.md                             # behavioural reference

evals/business_profiles/
  cases/<company_number>.json           # {company_number, financial_year, expected: {...}}
  configs/<name>.yaml                   # provider, model, concurrency, mlflow block
```

Config follows the existing format exactly — API keys in `.env`, never in
the file:

```yaml
provider: openrouter
model: <model id>
timeout_seconds: 120
concurrency: 4
mlflow:
  enabled: true
  tracking_uri: http://127.0.0.1:5000
  experiment: companies-house-business-profile-eval
  run_name: <name>
```

## Storage

`company_profiles`, keyed `(company_number, financial_year)` — already
sketched in [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md). Keyed by year because
trading status genuinely changes: `RAPT LEISURE`'s own narrative records the
shift to consultancy-only within one filing.

Store the value, confidence, evidence quote and source section for each
field, plus the model id and prompt version, so any row can be traced back
to the sentence and the model that produced it.

## Validation

Three signals available before any hand-labelling:

1. **Quote verification** — automated, catches fabrication, costs nothing.
2. **Website agreement** — the 50 existing `website_investigations` rows
   carry an independently derived `business_model`. Agreement between
   narrative-derived and website-derived classification tests both.
3. **SIC agreement rate** — should be high but not total. Near-100% would
   mean the model is just reading SIC back; near-0% means something is
   broken.

Then a gold set of ~50 hand-reviewed cases, matching the size and review
discipline of `evals/vlm_financials`. Metrics: per-field accuracy,
quote-verification pass rate, `unclear` rate, and disagreement-with-SIC rate.

## Cost

Text only — no vision, no browser. Roughly 1,000 input and 300 output
tokens per company across ~2,330 companies. Cheaper than the existing VLM
stage by a wide margin, which is why it belongs before the website stage in
the pipeline.

## Open questions

- Should companies with no narrative (5,209 of 8,169 have none) route
  straight to the website stage, or be left unprofiled until their accounts
  are re-parsed? Note the narrative backfill was only ever run over
  companies with turnover, so some of that gap is reach, not absence.
- Is `relationship_repeat` reliably distinguishable from `considered_b2b`
  in filed text, or should they merge until the gold set shows they separate?
