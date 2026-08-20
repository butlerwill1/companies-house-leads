# Business profile extraction (Gate A2)

Spec for the text-only LLM stage that reads a company's filed narrative and
records how it acquires customers. Built and running -- pipeline, harness,
and a 47-case hand-labelled gold set (`scripts/profile/`,
`evals/business_profiles/`); see `scripts/profile/README.md` for how to run
it.

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

### `trading_status_confirmed` — who to actually contact

Resolves the **369 companies** Gate A flagged `turnover_without_employees`
and deliberately refused to guess about (`core/company_triage.py`). The
question it answers is not "is this company real" but **"is the company
number in front of me the right one to advertise to, or does the real
business sit somewhere else in the group"** — Gate A's structured data
cannot tell a holding vehicle with genuine trading subsidiaries
(HEDIN AUTOMOTIVE: zero employees, £412m turnover, "motor car retailers and
repairers") from a pure investment shell (MTALX GLOBAL HOLDINGS: zero
employees, £423m turnover, no trade named at all) — both look identical in
structured fields. Only the narrative separates them.

| Value | Meaning | Financial signature | Lead-worthy? |
|---|---|---|---|
| `trading` | Operates its own business with its own staff | Turnover and employees both belong to the same entity | Yes, directly |
| `trading_group_parent` | Real trade, filed through the top-of-group holding entity; subsidiaries do the work | Turnover with **zero direct employees** — staff sit in subsidiaries, not the filer | Yes, but see below |
| `investment_holding` | Owns shares/property, generates no trading revenue of its own | Turnover (often large) against zero employees, with **no trade named** in the text | No — the entity itself isn't a business; a named subsidiary might be |
| `spv` | Special-purpose financing/securitisation vehicle (a concession, a securitisation, a single-asset structure) | "Turnover" is often interest income or concession fee income, not sales revenue | No — no customer-facing trade exists |
| `dormant` | Filed accounts, currently does nothing | No turnover, no employees | No, unless investigating a related active entity |
| `unclear` | Narrative doesn't say enough to place it confidently | — | Needs a human look before use or discard |

`trading_group_parent` vs. `investment_holding` is decided by whether the
narrative **names an actual trade**: WILTONS HOLDINGS (£10.2m turnover, zero
employees) reads "the subsidiaries operate restaurants"; `SC540426` (see the
`demand_model` table above) reads "the principal activity of the company
continued to be that of an investment holding company" — no activity named,
nothing to sell. Financial shape alone cannot make this call, which is
exactly why this field exists as a narrative read rather than a Gate A rule.

`spv` needs the same care for a different reason: EARTHAVE BRIDGING reports
£18.1m turnover that is bridge-loan interest receivable on a securitised
book, not sales revenue — a real number that would badly mislead any
spend estimate if treated like ordinary trading turnover, which is exactly
the failure mode the old SIC-ratio model had no way to catch.

**None of the 47 gold cases currently carry `investment_holding` or
`dormant`** — both are real categories a live run will hit, just not
represented in the hand-labelled set yet. Treat any future per-category
accuracy number for those two values as unmeasured, not zero-error.

**The open gap:** `trading_group_parent` is lead-worthy, but the field
doesn't say *which* company number to actually contact. For a small, simple
group (RICHARDSONS (HOLDINGS): one dealership brand, two sites) the parent
is fine to target directly. For a larger one, the filed narrative sometimes
*names* the subsidiary doing the work (AMIRY & GILBRIDE's filing names
"LP North Fourteen Limited and LP North Fifteen Limited") — but there is no
structured subsidiary-lookup step today. A `trading_group_parent` lead may
need a human, or a future stage, to resolve to the right company number.

### Supporting fields

- `business_description` — one sentence, plain English, what they actually do.
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
