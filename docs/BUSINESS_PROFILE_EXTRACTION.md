# Business profile extraction (Gate A2)

Spec for the text-only LLM stage that reads a company's filed narrative and
records how it acquires customers. Built and running -- pipeline, harness,
and a 57-case hand-labelled gold set (`scripts/profile/`,
`evals/business_profiles/`); see `scripts/profile/README.md` for how to run
it.

For how well it currently performs, which model and context to use, and where
the accuracy work should go next, see
[BUSINESS_PROFILE_HARNESS_REVIEW_2026-08-21.md](BUSINESS_PROFILE_HARNESS_REVIEW_2026-08-21.md).

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

Five classification fields, each an object with these keys **in this order**:
`quote` (verbatim), `section`, `reason`, `value`, `confidence` (0.0–1.0).
`sic_agreement` follows the same order minus `confidence`, which it has never
carried.

**The order is load-bearing, not cosmetic.** Until v6 the shape was
`value, confidence, quote, section`, so an autoregressive model committed to
an answer and then went looking for a quote to justify it — the quote was a
rationalisation, not the basis of the call. The 2026-09-09 gold review found
exactly the pattern that produces: `09406074` PSR EQUITIES cited a sentence
about its *suppliers* as evidence of who its *customers* are; `10930289`
BENNETTS used one sentence as evidence for three deliberately orthogonal
fields; `06995506` SIZE GROUP read a geography statement as a demand channel;
`13181834` EARTHAVE recorded `delivery_model: unclear` at 0.95 confidence with
an empty quote. This is the same argument that already places `sic_label` last
in the prompt, applied inside the field object.

`reason` is validated for presence on every field including `unclear`, where
it must name the specific fact the text does not give ("the filing never says
who the borrowers are") rather than restate that the model was unsure. Only
presence is checked: whether the sentence really names the missing fact is a
semantic property, and this pipeline gates persistence on checkable things.

### `demand_model` — the target

The evidenced acquisition channel to use from the filing. It is the
primary input to whether paid search can work, but no longer the only one:
where it answers `unclear`, the search-addressable metric falls back to a
category floor over `delivery_model` + `customer_type` (see
[`is_search_addressable`](../scripts/profile/business_profile_metrics.py) and
the note under `delivery_model` below). Keep this field strictly evidentiary
— what the text says — and let that rule carry the commercial judgement.

| Value | Meaning | Example from live data |
|---|---|---|
| `consumer_search` | individuals search and buy | e-commerce retail |
| `local_service` | individuals search for a nearby provider | `13400880` opticians; `SC390599` restaurant |
| `relationship_or_contract` | demand evidenced by ongoing accounts, referrals, repeat relationships, commissions, tenders, frameworks or procurement; it can apply to B2C, B2B or public-sector customers | a tendered public-sector service; a relationship-led B2C business |
| `platform_intermediated` | demand arrives via marketplace/OTA/aggregator | hotels via OTAs |
| `not_customer_facing` | holding vehicle, SPV, investment company | `06698313` CAUDWELL PROPERTIES (101) "holding Investment Property" |
| `unclear` | text does not support a call | — |

`considered_b2b`, `tender_framework`, and `relationship_repeat` were originally
separate values -- merged into `relationship_or_contract` after a 57-case gold-set run
showed the answer to the open question below was no: filed narrative text
essentially never states whether repeat B2B trade was won by tender, referral,
or research, so the model guessed among the three about as often as it got it
right, and this field's accuracy (37-40%) was worst of all six by a wide
margin. The distinction that actually matters for this field's purpose (can
paid search work) is search vs not-search, not which non-search channel.
`wholesale_contract` was folded into `relationship_or_contract` for the same
reason. The old name, `b2b_relationship`, was retired in v10 because it made
the acquisition channel sound like a customer type. A named customer, an
ordinary sales contract, intragroup property letting or a revenue-recognition
policy does not establish how the principal business was won. Where the filing
describes the business but does not describe acquisition, the honest answer is
`unclear`.

Prompt v11 adds a narrow tie-break for recall. When the filing explicitly
describes a substantial direct-search or local-service channel, record that
channel even where wholesale, repeat relationships, marketplaces or another
channel contributes more revenue. It is an evidence-priority rule, not an
inference from business type: a bare website, location, industry, payer or
ordinary sales contract is still insufficient. The rule applies only to
`demand_model`; `delivery_model` continues to describe the principal
deliverable.

Prompt v12 retains that demand priority and makes `relationship_or_contract`
require positive evidence of winning or receiving customer business. In the
saved v11 results, four lost leads used a franchise arrangement, delivered
services, organic growth or student interest as acquisition evidence. These
facts alone establish neither relationship-led demand nor search. V12 permits
`unclear` when the mechanism is absent, including at low confidence, and
distinguishes organic growth from organic search. A ticket or appointment
alone also does not establish local search. Generic examples replace repeated
priority instructions; a core or substantial existing line qualifies, an
incidental mention or future aspiration does not.

V12 restores the v10 customer/delivery field instructions and delivery
tie-break to limit the experiment's scope. It asks for short contiguous
quotes, retaining word boundaries and source order, after a fifth lost lead
failed quote validation. The validator, response schema, gold labels and
`is_search_addressable` rule are unchanged. The saved v11 run does not
establish which prompt clause caused each change, and software tests do not
establish that v12 recovers the leads. V12 remains a candidate for a separately
approved paid evaluation against the same frozen cases; V10 and V11 saved
responses are the baselines. Review lost/recovered leads, the Viper label
boundary, non-search negatives, quote failures and trading-status errors
before considering rollout.

### `customer_type`
`b2c` | `b2b` | `public_sector` | `mixed` | `unclear`

**Decided by what the contract buys and who consumes it, not by who pays or
who chooses the provider.** The operative test:

> Name the person whose consumption this contract pays for. If you can name
> them (each patient, each resident, each placement), the customer is that
> individual (`b2c`). If the answer is the buying organisation itself, or the
> public generally, the customer is that organisation (`b2b` /
> `public_sector`).

This exists because the original definitions (`b2c` = "sells to individual
consumers", `public_sector` = "sells to ... public bodies") had no answer for
the large class of UK businesses where the payer and the consumer are
different parties, and the model resolved that ambiguity by hedging to
`mixed`. Worked examples:

| Business | What the contract buys | Label |
|---|---|---|
| NHS dental practice (`06020611`) | one patient's course of treatment | `b2c` |
| Community pharmacy (`12500354`) | one patient's prescription or consultation | `b2c` |
| Care provider on council placements (`05940625`) | one named person's care placement | `b2c` |
| Care homes, block-booked beds + self-funders (`03143947`) | one resident's bed-week either way | `b2c` |
| Main contractor building for councils and universities (`02343739`) | a building the buyer will own | `public_sector` |
| Digital supplier to central government (`02755304`) | a system the department will run | `public_sector` |

Note what the rule deliberately discards: that 62% of the dental group's fees
and 100% of the care provider's income come from a public body is *not*
recorded by this field. That is a payer-concentration fact, and if it turns
out to matter commercially it needs its own field rather than being folded
back in here -- overloading one field with two orthogonal facts is what
produced the hedging in the first place.

Two edges the definitions state explicitly:

- Services an organisation buys **for its own staff** (occupational health,
  training, employee benefits) are `b2b`, not `b2c`. The individual consumes
  them, but the buyer is procuring an input to its own operations.
- `mixed` was widened from "consumers **and businesses**" to any two of
  `b2c` / `b2b` / `public_sector`. Under the old wording an agency serving
  both commercial and public-sector clients had nowhere to go. It is
  explicitly *not* for one customer base with two payers -- that is the hedge
  the rule above removes.

`b2b2c` was dropped: across 57 hand-labelled cases no human ever chose it and
the model never once predicted it. An unused option is not free -- it is
another near-synonym to hedge between, and hedging is already this field's
dominant error (9 of 14 were `mixed` chosen over a clean `b2c` or `b2b`).

#### The second `mixed` hedge: a mention is not a proportion

The payer-vs-consumer rule above kills one hedge. A second one survived it,
and all three football clubs in the gold set fell to it: a venue business
whose core product is sold to individuals also sells sponsorship, so the
filing mentions sponsors somewhere, so the label came back `mixed`.

What made it a hedge rather than a judgement is the **quality of the text
cited**. Compare what the three clubs were labelled from:

| Case | Quote it was labelled `mixed` from | What that text actually evidences | b2b share of turnover |
|---|---|---|---|
| `09858599` NORTHAMPTON | "Thanks also goes to the club's sponsors, partners and any other person who gave their support…" | a directors' acknowledgements line | Commercial 741,678 / 4,333,831 = **17.1%** |
| `14934831` THE WILLOWS | "Sponsorship, advertising and other similar commercial income is recognised over the duration of the contracts…" | an accounting policy on *timing* | Digital advertising 14,415 / 9,056,185 = **0.2%** |
| `SC007629` ST JOHNSTONE | "Turnover represents the income arising from football … gate receipts, advertising boards, sponsorships and corporate hospitality" | the revenue composition itself | gate 42.6% vs broadcasting, prize and sponsorship 55.6% |

Only the third is evidence. The first two name a second kind of customer
without saying it is worth anything, and in one case it was worth 0.2%.
Northampton and The Willows are now `b2c`; St Johnstone stays `mixed`, and the
three clubs differing is the right outcome -- it follows disclosed revenue
rather than a hedge.

**The test to apply:** proportion is answered by the *turnover analysed by
class of business* note, which nearly every filing carries. A sentence that
merely mentions sponsors, partners or advertisers is not a substitute for it.
This was written into the `mixed` gloss in v5, which had not been run at the
time, so it cost no comparability.

Note this is the same shape of error as the pharmacy case in the payer rule
above, one level along: there the model read a *payer* as a customer, here it
reads a *mention* as a customer base. Both are answered by asking what the
turnover note actually attributes revenue to.

### `delivery_model` — what actually changes hands

The "what" axis, and deliberately orthogonal to the other three:
`demand_model` is how customers arrive, `customer_type` is who they are, this
is what they receive. The first two do not constrain it -- an online retailer
is `consumer_search` + `b2c` + `product_physical`, a private dental practice
is `consumer_search` + `b2c` + `professional_service`.

It earns its place downstream because what is delivered sets the shape of the
ad account: a stockholding retailer needs product feeds and shopping
campaigns, a contractor needs a handful of high-value lead forms, a hire
business bids on availability-and-location terms. Two companies with identical
`demand_model` and `customer_type` still get different work.

The operative test, in order:

> **1. Is the thing handed over a good, someone's labour, the temporary use of
> an asset, or money?** Goods are `product_physical` / `product_digital`;
> labour is `professional_service` / `trade_service` / `contracting`; an asset
> that comes back at the end is `rental_leasing` or `property`; money advanced
> as principal is `lending`.
>
> **2. Two sector values take precedence over the labour test**, because what
> they imply downstream is nothing like a consultancy's: feeding or housing
> guests is `hospitality`; running a venue or club people pay to attend or
> belong to is `leisure_venue`.
>
> **3. Within the remaining labour:** work organised as *projects under
> contract* is `contracting`; *hands-on work at the customer's premises* is
> `trade_service`; everything else people-delivered is `professional_service`.

Note what step 1 does **not** ask: who made the goods. That question used to
be folded in here as `distribution_resale` and has been removed -- see below.

Two tie-breaks, both added in v5 because the new values create new seams:

- **`hospitality` vs `leisure_venue`** -- if the customer is buying a bed or a
  meal, `hospitality`; if buying admission, membership or participation,
  `leisure_venue`. A business doing both (a golf club with a hotel, a hotel
  with a wedding venue) follows whichever the narrative names as dominant.
- **`lending` vs `professional_service`** -- lending your own money is
  `lending`; advising on, broking or intermediating someone else's is
  `professional_service`. This is the line that keeps `09081062` PARADIGM
  NORTON and `11306657` SALAMANDER out of `lending`.

| Value | What it means here | Gold examples |
|---|---|---|
| `product_physical` | physical goods are what the customer receives, however the company got them | `03121306` IRONMONGERYDIRECT; `05332212` ONLINE 4 BABY; `10017661` KINGSTON MODULAR (manufactures); `03453603` RICHARDSONS (car dealership); `03784836` PILL BOX (pharmacies plus wholesale); `11428791` DLS (NE) (food wholesale) |
| `product_digital` | software, digital products, SaaS, licensed content | `09922859` GUMGUM ad platform; `03228491` EMBARCADERO software licences; `10713956` NINJA TUNE recordings |
| `professional_service` | any people-delivered service that is not site work, project contracting, hospitality, lending or a leisure venue | `08248223` PSG LAW; `09081062` PARADIGM NORTON financial planning; `09644619` SSP HEALTH medical services; `02998017` C.P.J. FIELD funeral directors; `11879464` SKYLINE TAXIS; `12989408` TOWNHOUSE beauty salons |
| `trade_service` | hands-on skilled work at the customer's premises, not organised as projects | `02372641` JOHNSONS removals and fit-out -- the only gold case, see below |
| `contracting` | projects delivered under contract, typically construction or engineering | `01185592` ARDMORE main contractor; `00310690` H.E. SIMM M&E; `04420880` HEALTHCARE SUPPORT PFI |
| `hospitality` | places where guests eat, drink or stay | `07187799` THE VILLA (hotel and restaurant); `08120464` WARWICK HOTEL; `13043443` VENTRESS (hotel accommodation); `01576582` LYONS HOLIDAY PARK; `00485994` WILTONS (licensed restaurants); `10036243` REDMILL (McDonald's franchisee) |
| `lending` | lends its own money or provides credit as principal | `11168447` ZIRCON BRIDGING; `13664578` AMBER BRIDGING; `11168409` ZIRCON GROUP; `09406074` PSR EQUITIES |
| `leisure_venue` | a venue or club people pay to attend or belong to | `02535759` STOCK BROOK MANOR golf and country club; `09858599` NORTHAMPTON TOWN; `SC007629` ST JOHNSTONE FC; `14934831` THE WILLOWS 96 |
| `rental_leasing` | assets are hired out and come back | none -- see below |
| `property` | owns, develops, or lets property as the business | `04334155` KITEWOOD "property investment" |
| `unclear` | the text never says what is delivered | `13181834` EARTHAVE (securitisation vehicle); holding and financing vehicles generally |

#### Why `distribution_resale` was removed (v4)

The field used to carry a ninth value, `distribution_resale` -- "buys and
resells others' goods (wholesale, distribution, dealership)" -- alongside
`product_physical`, then defined as "makes or sells physical goods". Those two
are not alternatives on one axis. `product_physical` answers *what form does
the deliverable take*; `distribution_resale` answers *did you make it or buy
it in*. Those questions are orthogonal, so **every reseller of physical goods
satisfied both by construction**, and the enum forced a single choice between
answers to two different questions.

The gold set showed exactly that. Three companies that plainly do not make
what they sell were split across the two values by human reviewers, with no
rule separating them:

| Company | Filing says | Old label |
|---|---|---|
| `03121306` IRONMONGERYDIRECT | "sells door furniture and associated ironmongery products through the Company's website, call centre and on-site trade counter" | `product_physical` |
| `05332212` ONLINE 4 BABY | "retail sale of nursery goods" | `product_physical` |
| `05900590` BELL TRUCKS | "commercial vehicle sales and servicing and the supply of replacement parts" | `distribution_resale` |

No tie-break repairs this, because **the deciding fact is almost never in a
filed narrative.** None of those three filings says who made the goods, and
the prompt forbids supplying it from elsewhere: "Base every answer only on the
text given below. Do not use outside knowledge of the company or the
industry." The best textual proxy available -- does the filing happen to
describe itself as a distributor, wholesaler or dealership -- labels the
*wording* rather than the business, and two verified cases did not even
satisfy that: BELL TRUCKS (labelled from the quote "Vehicle Sales
136,360,988"; the only "dealer" anywhere in the case is the SIC label `Car
dealers`) and `10622184` PENKETH GROUP (quote "Sale of goods 12,985,535",
empty `principal_activity`, SIC of `E-commerce / online retail`).

This is the same call already made for `considered_b2b` / `tender_framework` /
`relationship_repeat`, and for the same reason: a distinction the narrative
does not carry is one the model guesses at. Nothing downstream distinguished
the two values either -- at the time, `delivery_model` was a stored text
column ([core/companies_house_sqlite.py](../core/companies_house_sqlite.py))
that no code branched on, and the headline search-addressable metric keyed off
`demand_model` alone.

**That second half is no longer true, and the change is worth knowing about
when reading anything below.** `delivery_model` now feeds the headline metric
through the category floor in
[`is_search_addressable`](../scripts/profile/business_profile_metrics.py):
when `demand_model` is `unclear`, a `b2c` or `mixed` company whose `delivery_model` is
`hospitality`, `leisure_venue`, `professional_service`, `product_physical` or
`trade_service` still counts as reachable by paid search. It rescues only --
it never overrides a `demand_model` that committed to a non-search answer.

The floor exists because the gold-`unclear` exclusion was deleting real leads:
`07538544` BIRD OVERSEAS, a hotel group whose filing never says how guests
arrive, was dropping out of the metric entirely. Three of the five gold
`unclear` cases are rescued by it (`07538544`, `04479650` SKYBOUND WEALTH,
`09081062` PARADIGM NORTON); the two that remain excluded are excluded because
their `customer_type` is also `unclear`. Note the consequence for the merge
argument above: a `delivery_model` distinction now *can* have downstream
effect, so "nothing branches on it" is no longer a reason to merge values.

So sourcing is now explicitly **out of scope** for this field, and
`product_physical`'s definition says so. The 13 gold cases that carried
`distribution_resale` were relabelled `product_physical`; a v3 run's
`distribution_resale` predictions have no v4 equivalent to score against.

If make-vs-buy turns out to matter commercially, it needs its own field fed by
the website stage that runs after this one -- a website says immediately
whether a company sells third-party brands. The filed accounts do not.

#### `professional_service` vs `trade_service` vs `contracting`

`professional_service` is 36 of 109 gold cases, by far the largest class. Until
v5 the prompt gloss ("advisory or expert services delivered by people --
consultancy, legal, accountancy, agency work") described only part of what it
was being used for, and this doc said the opposite -- that it was the residual
bucket. That contradiction was live for three versions and had a cost: the
model only ever saw the narrow gloss, so people-delivered services that are
not advisory had nowhere to go and hedged to `unclear`. Transport (`10930289`
BENNETTS COACHES, `11879464` SKYLINE TAXIS) and personal care (`12989408`
TOWNHOUSE) sat in `unclear` for exactly that reason.

v5 settles it in favour of this doc's reading: it is the **default for all
people-delivered services** once the sector values are ruled out. Care homes,
schools, funeral directors, insurance agents, taxi firms, salons and umbrella
payroll all sit there, none of which are advisory. Read it as "a service,
delivered by people, that is not site work, not a project, and not one of the
three sector values".

The other two are the narrow exceptions:

- `contracting` -- the deliverable is a *project* with a start, an end and a
  contract. Installation work counts when it is organised that way:
  `00310690` H.E. SIMM ("design, manufacture, installation, commissioning and
  maintenance") is `contracting`, not `trade_service`, because it is delivered
  as construction-industry projects.
- `trade_service` -- recurring hands-on work at the customer's premises with
  no project framing. **Only 1 gold case**, `02372641` JOHNSONS (workplace
  relocations, storage and fit-out); a case that looks borderline against
  `contracting` is almost certainly `contracting`.

  This passage previously named `03744489` M.C.P. as the `trade_service`
  exemplar and claimed 2 verified cases. Both were wrong: M.C.P. is
  `contracting` and says so itself ("We act as a **main contractor** to the
  social housing sector"), and no case in the set is verified. Treat the
  remaining single example with care too -- JOHNSONS' own quote says it
  "provides a full in house, project management service", which is project
  language in the one case the value rests on. If that one goes to
  `contracting` on review, `trade_service` joins `rental_leasing` at zero
  support and should be merged rather than defended.

**The discriminator is project framing, not trade.** `trade_service` names
plumbing and electrical in its prompt gloss, so any M&E business looks like a
candidate at first glance; what settles it is whether the filing describes
work that is tendered for, awarded, and run to completion. `SC712711`
HUTCHEON is the worked example: "general electrical, mechanical and plumbing
**contractors**", "activities based around the **construction industry**",
"efficiencies being made in its **tendering** ... processes", "projections of
future works accepted for and those yet to be **awarded**", "labour costs and
**project profitability**", and turnover recognised on "the **stage of
completion**". Tender, award, project, percentage-of-completion is the full
`contracting` signature, and it sits behind a business whose trades are
exactly the ones `trade_service` lists.

#### `rental_leasing` and `property`

Both are kept on decision-relevance rather than gold-set support (**0** and 6
cases respectively). `rental_leasing` is for assets that are hired and
returned -- equipment, vehicles, plant. `property` is for owning, developing
or letting land and buildings as the business itself, not for a trading
company that happens to own its premises.

`rental_leasing` having *zero* gold examples is worth stating plainly rather
than defending. The argument for keeping it is a bet on the addressable
population -- equipment and vehicle hire are among the most search-driven
categories there are -- not a claim about the gold set, which currently offers
no evidence either way. If the next review pass still finds nothing to put in
it, it should be merged rather than defended a third time.

#### How the v4 disagreements were resolved (v5)

Both of the disagreements recorded here in v4 are now settled by giving them
values rather than tie-breaks:

1. **Hospitality** had no consistent home -- restaurants were
   `product_physical` while hotels split across `unclear` (`07187799`,
   `08120464`), `property` (`13043443`, `01576582`) and one
   `professional_service`. All nine now sit in `hospitality`. Note the fix was
   *not* the `rental_leasing` "a bed-night is an asset hired and returned"
   reading floated in v4: that is a clever description of a hotel and a poor
   description of a restaurant, and it would have put a wedding venue in the
   same class as plant hire.
2. **Lending** had no home at all; all four bridging lenders were `unclear`.
   They are now `lending`, and the `lending` / `professional_service`
   tie-break above keeps the four broker and financial-planning cases where
   they were.

The `product_physical` / `distribution_resale` disagreements that used to sit
here are gone -- not resolved case by case, but removed with the value itself
(above).

Two boundary calls made during the v5 relabel, recorded because they are the
ones most likely to be re-litigated:

- `11825450` FLOUR POWER (Patisserie Valerie) stays `product_physical`. It
  operates retail cafes, but the narrative describes a vertically integrated
  bakery producing cakes sold through cafes, e-commerce *and* wholesale -- the
  cafes are one channel, not the business.
- `13181834` EARTHAVE stays `unclear`, and is now the only case in that class.
  It is a securitisation vehicle issuing loan notes to hold a loan portfolio,
  not a lender originating to customers -- which makes it a genuine example of
  what `unclear` is for.

#### Merges and keeps

`saas` was merged into `product_digital` -- SaaS is a digital product, each had
one gold example, and nothing downstream treats them differently.
`distribution_resale` was merged into `product_physical` in v4, for the
different reason set out above: not thin support, but a value that was never
exclusive of the one beside it.
`rental_leasing` and `property` are deliberately kept despite thin support:
equipment and vehicle hire are among the most search-driven categories there
are, so the distinction changes the decision this stage exists to make. Thin
classes that are decision-relevant get targeted labels; thin classes that are
not get merged.

`hospitality`, `lending` and `leisure_venue` were added in v5 on that same
rule. All three were decision-relevant and all three were homeless: an audit
of the 14 v4 `unclear` cases found **not one** where the filing failed to say
what was delivered -- every one was a missing enum slot. Mean `delivery_model`
confidence by SIC group made the clusters obvious: banking and lending 0.41,
sport and fitness 0.20, hotels 0.68, against 0.85-0.95 everywhere else. Sized
against the 2,325 companies that actually have a filed narrative (not the full
8,169 -- the classifier only runs where there is text), hotels are 6.5%,
restaurants 3.9%, lending 4.3% and sport 0.9%.

Gold-set support across 109 cases: `professional_service` 36,
`product_physical` 26, `contracting` 18, `hospitality` 9, `property` 6,
`lending` 4, `leisure_venue` 4, `product_digital` 4, `trade_service` 1,
`unclear` 1, `rental_leasing` 0.

`lending`, `leisure_venue` and `unclear` fall below the
`MIN_RELIABLE_SUPPORT = 5` threshold in `scripts/profile/business_profile_metrics.py`
and will show up in `classes_below_min_support`. That is expected: it argues
for adding targeted gold cases in those categories, not for withholding the
values. This tally is maintained by hand -- no script emits it, which is why
it had drifted by v4.

### `geography_served` — how far the customers are

`local` | `regional` | `national_uk` | `international` | `unclear`

The prompt glosses, verbatim from `FIELD_DEFINITIONS` in
[business_profile_policy.py](../scripts/profile/business_profile_policy.py):

| Value | Gloss the model is shown |
|---|---|
| `local` | serves one town, city, or immediate area |
| `regional` | serves a region of the UK |
| `national_uk` | serves the UK broadly. This INCLUDES a company whose geographic turnover split shows less than 5% of turnover from outside the UK -- that is an incidental export line, not an international business |
| `international` | a material share of turnover comes from CUSTOMERS outside the UK. Where the notes give a turnover split by geographical market, material means 5% or more of turnover from outside the UK -- read the current-year column. Where no split is given, the narrative must name an overseas market the company actively serves. A foreign parent company, an overseas subsidiary, a foreign shareholder, an overseas client win, or an export line under 5% is NOT enough on its own |
| `unclear` | the text does not indicate geographic reach |

**The field records where the customers are, not where the company is.** A
Bolton-registered company with customers across the UK is `national_uk`; a
London company serving one borough is `local`. Registered office, incorporation
jurisdiction and the location of subsidiaries are all evidence *about the
company*, and only weak proxies for its customers.

#### The turnover-by-geographical-market note cuts both ways

Almost every filing carries a "Turnover analysed by geographical market" note.
It is the single most-cited quote for this field, and it is only sometimes
evidence:

- **Non-UK rows with material amounts are good evidence for `international`.**
  That note reports revenue by where customers are, which is exactly the
  question. `01552102` WILSON LEARNING (UK 71,144 / Europe 159,572 / Rest of
  World 522,567 -- 90.6% overseas) is labelled off it correctly. What counts
  as material is now a number, not a judgement: see the 5% threshold below.
- **A note listing only "United Kingdom" is NOT evidence for `national_uk`.**
  That line distinguishes UK from overseas, not local from national, and it is
  present whether the company serves one street or the whole country. This is
  the mirror image of the counter-error below and it produced a real mislabel:
  `12886017` ABBEYDALE, a Bolton-and-Cheshire pharmacy group, was labelled
  `national_uk` on nothing but that line. It is now `regional`.

When the only geographic statement in a filing is a UK-only turnover note, the
answer comes from the rest of the narrative -- named towns, the spread of
trading subsidiaries, whether the business model is inherently footfall-based
-- or it is `unclear`.

#### Counter-error: foreign mentions read as international customers

Recorded in the prompt itself: 6 of 14 geography mistakes in an early
evaluation were `national_uk` answered as `international`, because the model
treated any foreign mention -- an overseas parent, a subsidiary, a foreign
shareholder, an incidental export line -- as proof of customers abroad. Hence
the unusually emphatic gloss above.

The word to watch is *occasional*. `06995506` SIZE GROUP was labelled
`international` on "as well as carrying out occasional international projects
for certain key clients" -- the incidental export line the gloss excludes,
almost verbatim. Its turnover-by-geographical-market note shows United Kingdom
92,941,394 and no foreign rows at all, so that work is not separately material.
It is now `regional`, on a quote that states the market directly: "focused on
continuing our growth and development, in London and the surrounding
countryside, and, gradually, into a wider geographical area."

That case is also a warning about the opposite over-correction. The obvious
repair for a bad `international` is `national_uk`, and it would have been wrong
here: the filing says the market is London and the home counties and that wider
reach is still aspirational. `national_uk` means *serves the UK broadly*, which
is a positive claim needing positive evidence -- it is not the default landing
place for a company that turns out not to be international.

#### The 5% threshold (v9)

The counter-error clause said an "incidental export line" was not enough, but
never said what incidental meant -- so it could not decide `SC757671` BOOTH
WELSH NEXUS. Its turnover note reads UK 28,915,404 / Australia 763,737 /
Europe 60,893 / USA 14,058 / Rest of World 150: **97.2% UK**. Gold called it
`international` (named overseas markets with real revenue); the v8
gpt-5.4-mini run called it `national_uk` (an export line). Both were
defensible readings of the same words, which is a definition failing, not a
model failing.

The field exists to say where to point advertising, and for that purpose a
company earning 97% of its revenue in the UK is a UK company. So v9 makes
"incidental" a number: **where a geographic turnover split is given,
`international` means 5% or more of turnover from outside the UK.** Below that
the answer is `national_uk`. Where no split is given, the narrative rule stands
unchanged.

The threshold comes from the gold set, not from the air. Of 124 cases, 59 carry
a geographic split, and their non-UK share is sharply bimodal:

| Non-UK share | Cases | Reading |
|---|---|---|
| 12.2% -- 100% | 24 | genuine international; smallest is `10358376` ADP ARCHITECTURE at 12.2% |
| **3.6% -- 12.2%** | **0** | -- |
| 0.0% -- 3.6% | 6 | export lines |
| exactly 0% | 29 | UK only |

There is nothing between 3.6% and 12.2%, so any threshold in that gap -- 5%,
10% -- gives identical answers on this data. 5% was chosen; it sits in empty
space.

Six gold labels moved from `international` to `national_uk` on 2026-09-14,
each recorded in the case's `review.taxonomy_migrations`:

| Company | Non-UK | Why it had been `international` |
|---|---|---|
| `07047520` DOMU BRANDS | 3.6% | Europe / North America rows, all small |
| `SC757671` BOOTH WELSH NEXUS | 2.8% | the Australia row, a legacy of the Clough acquisition |
| `12989408` TOWNHOUSE GROUP | 1.7% | "opening US salons" -- expansion, not yet revenue |
| `07608360` ELSEWHEN | 1.2% | a small overseas row |
| `02755304` INFORMED SOLUTIONS | **0.0%** | a Malaysian client win named in the narrative |
| `03465435` THE INFORMED GROUP | **0.0%** | same group, same client win |

The last two are the reason a number was needed rather than a better adjective:
their turnover note reads **"United Kingdom 21,001,909"** and nothing else. The
`international` label rested on "Notable client wins have included ... Thompson
Hospital Group in Malaysia" -- a client that earned nothing in the period. That
is a foreign mention read as international customers, exactly the counter-error
above, and the wording alone did not stop it; the threshold does. Nothing moved
the other way: no `national_uk` case has 5% or more overseas.

Two consequences to keep in mind. The 65 cases without a geographic split are
untouched by this rule -- for them the narrative still has to name an overseas
market actively served, and a client, parent or subsidiary abroad still is not
enough. And v8 and v9 `geography_served` numbers are not comparable, though no
value was retired so older saved responses need no mapping.

#### What `international` does not mean

It means "has customers outside the UK", nothing more. It is **not** a claim
about scale, sophistication, or global operations, and the value name oversells
it.

The clearest illustration is `14934831` THE WILLOWS 96, a holding company for
two football clubs: Fleetwood Town in England and Waterford FC in Ireland. Each
club's customers are the people who live near its ground; neither is
"international" in any ordinary sense. But Waterford's supporters are outside
the UK, so the group's customer footprint crosses a border and the label is
`international` -- correctly, per the rule as written.

Note what that implies: this field measures the **span of the customer
footprint**, not reach per customer. A group of purely local businesses in two
countries reads as `international`, and the `international` + `local_service`
pairing that produces is expected rather than contradictory. It is worth
checking when you see it, though: it is how `06995506` above was caught, and
how `12989408` TOWNHOUSE was re-examined -- "opening US salons" had read as
genuine, but its turnover split shows 1.7% overseas, so under the v9 threshold
it is `national_uk` until those salons earn something. Of the pairs remaining,
`14934831` and `02998017` C.P.J. FIELD (international repatriation, 35.8%
overseas) are genuine.

Gold-set support across 124 cases: `national_uk` 52, `international` 26,
`local` 17, `regional` 16, `unclear` 13. `international` was 29% of the set
before the v9 threshold and is 21% after it -- still worth watching for a UK
SME population, but six of the labels the counter-error had inflated are now
gone, and the remaining 26 all clear 12% overseas or rest on a named market.

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
| `trading` | A real business selling to customers outside its own group — run by this company itself, or by its subsidiaries with this company filing as head of the group | Turnover with employees either in the filer's own column or, for a group parent, in the Group column with the Company column empty | Yes (a group parent may need the subsidiary's number looked up — see below) |
| `investment_holding` | Owns shares/property, generates no trading revenue of its own | Turnover (often large) against zero employees, with **no trade named** in the text | No — the entity itself isn't a business; a named subsidiary might be |
| `spv` | A dedicated financing, securitisation or concession vehicle, or a captive subsidiary serving only its group | "Turnover" is often interest income or concession fee income rather than sales revenue; captive activity may be billed to the parent, with staff recharged in and profit at or near nil | No — the entity does not compete for ordinary trade |
| `unclear` | Narrative doesn't say enough to place it confidently | — | Needs a human look before use or discard |

`dormant` was removed from this taxonomy. Gate A already decides dormancy
deterministically and for free from structured data
([core/company_triage.py](../core/company_triage.py): no turnover and no
employees), and only 1 of the 2,960 companies that reach this stage with a
filed narrative is dormant at all. Paying for an LLM call to re-derive a
decision the free deterministic gate has already made is waste, and the extra
option only gives the model somewhere else to hedge. The general rule this
follows: **the LLM should only be asked to make distinctions Gate A cannot
make from structured data.**

**`trading_group_parent` was retired in prompt v8 (2026-09-14).** It was a
sub-case of `trading` — a real trade, filed by the parent while the
subsidiaries hold the payroll — and the distinction was being applied
inconsistently in both directions: the v7 gpt-5.4-mini run answered `trading`
for 12 of the 53 gold group parents, and reading the employee notes of those
twelve found three gold labels wrong the other way (C.P.J. FIELD, PILL BOX
CHEMISTS, ADP ARCHITECTURE: the parent itself employs the staff). The only
evidence that separates the two is the Group/Company employee table, which
the XHTML flattener turns into one number per line. Nothing downstream read
the value (`is_search_addressable` ignores this field), so the 53 gold
labels were merged into `trading` mechanically (`review.taxonomy_migrations`
in each case file), scoring maps the retired value to `trading` in older
saved responses (`RETIRED_VALUES`), and the group-parent fact, if a
subsidiary-lookup stage ever needs it, can be derived from the accounts.
After the September SPV and investment-holding review, the 124-case gold set is
108 `trading` / 8 `spv` / 8 `investment_holding`. The majority baseline is
0.871, so raw accuracy remains weak evidence of quality; what matters is recall
on the two minority classes, whose support remains too small for a reliable
production claim.

A group parent vs. `investment_holding` is decided by whether the
narrative **names an actual trade**: WILTONS HOLDINGS (£10.2m turnover, zero
employees) reads "the subsidiaries operate restaurants"; `06698313` CAUDWELL
PROPERTIES (101) reads "the principal activity of the company continued to be
that of holding Investment Property" — no trade named, nothing to sell.
Financial shape alone cannot make this call, which is exactly why this field
exists as a narrative read rather than a Gate A rule.

Read the **whole** principal-activities paragraph, not the company sentence
alone. `SC540426` J. W. JOHNSTON used to be this section's example of an
investment holding company, quoted as "the principal activity of the company
continued to be that of an investment holding company". The sentence
immediately before it reads "the principal activity of the group continued to
be the supply and distribution of oil and gas, supply and fitting of tyres,
industrial services and wind turbine maintenance" — a £254m group with around
336 staff across 25 depots. It is `trading` (a group parent) and is now in the
gold set as exactly that trap: a parent whose company-level sentence says
"investment holding" while the group trades.

`spv` needs the same care for a different reason: EARTHAVE BRIDGING reports
£18.1m turnover that is bridge-loan interest receivable on a securitised
book, not sales revenue — a real number that would badly mislead any
spend estimate if treated like ordinary trading turnover, which is exactly
the failure mode the old SIC-ratio model had no way to catch.

`spv` was widened in v6 from "a special-purpose financing, concession, or
securitisation vehicle" — three financial structures — to the structural test
in the table above, because the old wording had nowhere to put a captive
trading subsidiary. `09202205` NORTHERN BALLET PRODUCTIONS is the case that
found it: £6,861,645 of turnover against £8,922,046 of cost of sales, zero
employees ("there were no employees during the current or prior year", with
£2,071,735 of staff costs recharged from the parent), and a loss wiped out to
exactly nil by a £2,060,401 Theatre Tax Relief credit. It is commissioned by,
and sells only to, its charitable parent Northern Ballet Limited.

Under the old gloss none of the five values fitted — `trading` requires "its
own staff", `investment_holding` requires no trade named, the (since retired)
`trading_group_parent` required it to be the parent, and it is none of the three structures `spv`
listed — leaving `unclear` as the only defensible answer for a filing that
says plenty. That is a missing enum slot rather than genuine ambiguity, and it
falls squarely inside the turnover-without-employees population this field
exists to resolve.

Prompt v10 makes the distinction explicit: an external NHS or council
counterparty does not disqualify a filing from `spv` when the text identifies
a dedicated PFI or concession structure. A public-sector contract by itself
does not establish `spv`; an ordinary contractor serving the same body is
`trading`. Zero employees, outsourcing, tax relief and group membership also
remain insufficient without one of the two structural routes.

The gloss covers two different things. Financing, concession and
securitisation vehicles are **named outright** because they are special-purpose
by construction. Their counterparty is usually external: a PFI vehicle bills a
public body, and EARTHAVE earns interest from outside borrowers. The captive
subsidiary is the addition, and for it the test is deliberately the
**counterparty, not the motive**: its trade is with its own group.

That test cuts both ways. A charity trading subsidiary is not `spv` just
because it is one. `07306464` ST ANTONY'S COLLEGE TRADING hires out college
conference facilities to outside customers (2024 trade debtors £170,905,
nothing owed by the group). It has no employees and gift-aids its whole profit
to the college, and it is `trading`. Only a subsidiary whose customer is its
parent, like Northern Ballet Productions, is `spv`. An earlier version of this
section listed "charity trading subsidiaries" as an `spv` shape, which was
wrong.

Naming the motive instead would repeat the
v5 `platform_intermediated` mistake — "tax relief production company" pulls in
any theatre or film business, and "tax relief company" pulls in every R&D
claimant, which is the more damaging direction because it demotes real trading
companies out of the leads. The gloss therefore carries an explicit negative
clause against tax relief, low employee counts and group membership as
standalone evidence.

It was sized before it was written: 377 of the 2,350 companies with turnover
report zero employees, 30 of those match this shape, and of those roughly 17
are PFI concessions the old wording already served (`COMMUNITY 1ST`,
`EDUCATION SUPPORT`, `STOBHILL HEALTHCARE FACILITIES`) and 5 are group
parents. So the wording decides perhaps 8 companies, of which `09202205`
NORTHERN BALLET PRODUCTIONS and `09837639` STORYWORKS PRODUCTIONS (£35.4m
turnover, "film production", zero employees) are the two clear ones. That
support level is why this is a gloss rewrite and **not** a sixth enum value:
a new class would sit below `MIN_RELIABLE_SUPPORT` from the outset and add
another near-synonym to hedge between, the argument that removed `b2b2c` and
`distribution_resale`. Every company in this group is non-search-addressable
under any of these labels, so the headline metric does not move either way.

**None of the 47 gold cases currently carry `investment_holding` or
`dormant`** — both are real categories a live run will hit, just not
represented in the hand-labelled set yet. Treat any future per-category
accuracy number for those two values as unmeasured, not zero-error.

**The open gap:** a group parent (a `trading` filer whose employee note puts
the staff in the Group column only) is lead-worthy, but nothing says *which*
company number to actually contact. For a small, simple
group (RICHARDSONS (HOLDINGS): one dealership brand, two sites) the parent
is fine to target directly. For a larger one, the filed narrative sometimes
*names* the subsidiary doing the work (AMIRY & GILBRIDE's filing names
"LP North Fourteen Limited and LP North Fifteen Limited") — but there is no
structured subsidiary-lookup step today. A group-parent lead may
need a human, or a future stage, to resolve to the right company number.

### Supporting fields

- `business_description` — one sentence, plain English, what they actually do.
- `sic_agreement` — `agrees` | `disagrees` | `unclear`, plus `reason`.

## Prompt design

**Evidence quotes are mandatory and must be verbatim.** This is the single
most important design decision, because it makes hallucination
*programmatically detectable*: assert `evidence_quote in section_text`
before accepting any field. A quote that does not appear in the source
fails *that field* — it is stored null with the rejection as its reason —
and no model self-reporting is required. (Until 2026-09-14 it failed the
whole extraction; see below for why that was dropped.) Track the field pass
rate as a headline metric.

The match is on normalized text (`business_profile_policy.normalize_quote_text`):
whitespace collapsed, and punctuation not directly between two digits
stripped (so "22,557,801" stays intact -- that's a real number, not
cosmetic -- while a dropped trailing full stop or a curly vs straight quote
doesn't fail a genuine quote). This came from three real rejections in a
57-case run: two were pure whitespace/line-break differences from a filed
HTML table collapsing to markdown, one was a model quoting only the current
year's column out of an interleaved two-year table -- a real number, just
not contiguous in the flattened text. None were fabrications. Only
formatting differences are forgiven; a quote whose actual words or numbers
differ from the source still fails, by design.

Two more forgivenesses landed on 2026-09-13 after the first gpt-5.4-mini
run over the 109-case set rejected 18 responses, none of them fabrications:

- **A quote may read one column of a table**
  (`business_profile_policy.quote_reads_table_row`): the quote's tokens must
  appear in the source in the same order with nothing skipped except numeric
  tokens and cell separators, so "United Kingdom 13,026,917 North America
  2,363,493" passes against the two-column turnover note it was read from,
  while a quote that skips a word or invents a number still fails.
- **Our own text had to stop splitting words.** The accounts software wraps
  letters and word fragments in adjacent `<span>`s, and the flattener turned
  every tag into a space, so 94 of 109 gold texts contained "T he company"
  and "C ompany" -- a model quoting the sentence correctly was rejected
  for not matching our broken copy, and a model quoting our broken copy
  faithfully passed. `strip_tags_preserving_blocks` now removes inline tags
  without inserting a space (measured over the 108 cached filings: every
  adjacent-span join was mid-word, none joined two real words). The gold
  texts were refreshed and the 11 draft quotes that carried the artefact
  repaired (`review.evidence_repaired`).

**Per-field rejection (2026-09-14).** The whole-response rule was chosen as
a signal before anyone had looked at what fails. Two 109-case gpt-5.4-mini
runs later, every rejection was a one-letter drift in an otherwise honest
quote, and the other fields in those responses had passed the same verbatim
check -- the only grounding guarantee the design offers. Discarding them cost
16% of cases under v6 and 11% under v7 and bought nothing. A failing field
is now dropped on its own (`validate_fields` + `reject_failed_fields`); a
response is rejected outright only when it is not JSON. Rescoring the saved
responses under the new rule moved every field's accuracy up by 4-12 points
with no model call, and the run report now carries three grades:
`responses_rejected_outright`, `responses_with_dropped_fields`,
`fields_rejected`. The signal the old rule was meant to carry survives as the
second of those.

Of the 18, those two changes recover four. The other fourteen are the
model's habit, not the harness's: eleven quotes had their opening words
regularised ("the principal activity of the group continues" quoted as "The
group's principal activity continues", a filing's "principle" corrected to
"principal", "the directors have identified" turned into "The company has
identified") and three borrowed customer_type's `mixed` for a field that has
no such value. The enum errors were what a prompt revision addressed (v7
took them from three to none); the tidied quotes persisted through v7, and
are handled by the bounded fuzzy match below.

**Bounded fuzzy match (2026-09-14,
`business_profile_policy.quote_matches_fuzzily`).** The v7 run still lost
20 fields across 12 companies to quotes that were not exact. Diffing each
against its nearest passage: eleven were a faithful reading with the
wording tidied -- one word of morphology ("manages"/"managed",
"activities"/"activity"), the filing's own typo corrected ("main
principle" -> "principal"), a pronoun for its antecedent ("they" for "the
subsidiary"), a sentence reordered into canonical form -- and one, Johnsons
1871's "The turnover is generated entirely in the UK", was not in the
document at all. The rule that lets the first kind through and not the
second: the quote must align to one passage of the source with at most
ceil(words / 8) differing words (one for a 6-13 word quote, two for 14-21,
and so on), quotes under six words must match exactly, and no differing word
may carry a digit, be a negation, or be one of the words the taxonomy turns
on (`_CLAIM_TOKENS`: "consumers", "overseas", "ceased", ...). Words at the
quote's own edges that the source has instead of the quote's cost one edit
per quote word, since where a quote starts is the model's choice and the
exact check already lets it cut anywhere. Reordering costs more than the
budget on purpose: that is a rewrite, and the prompt asks for a quote.

The match is tried strictest-first (exact, then table column, then fuzzy)
and the kind is written onto the field as `quote_match`, so a fuzzy
acceptance is visible in the Langfuse output, the saved response file and
the run report (`fields_fuzzy_matched`) rather than indistinguishable from a
correct quote. The count should stay small; a jump means the model has
stopped quoting. Rescoring the saved runs: v6 went from 30 dropped fields to
16 (16 fuzzy), v7 from 20 to 10 (10 fuzzy), and every field's accuracy rose
or held -- none of the recovered quotes backed a wrong label. What still
fails under v7 is Johnsons 1871 (fabricated), STM 360 (the quote dropped the
inline company name "STM 360", a digit-bearing token), and two reordered
sentences. The `company_profiles` table has no column for `quote_match` yet;
the mark reaches the database only if a column is added.

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
pattern — same config shape, same Langfuse conventions
([docs/LANGFUSE_SETUP.md](LANGFUSE_SETUP.md)), same gold-case layout.

```
scripts/profile/
  companies_house_business_profile.py   # pipeline: read narrative -> model -> validate -> persist
  business_profile_policy.py            # taxonomy, validation, quote verification
  business_profile_eval.py              # eval runner, Langfuse dataset runs
  business_profile_review.py            # human review / gold-case authoring
  README.md                             # behavioural reference

evals/business_profiles/
  cases/<company_number>.json           # {company_number, financial_year, expected: {...}}
  configs/<name>.yaml                   # provider, model, concurrency, langfuse block
```

Config follows the existing format exactly — API keys in `.env`, never in
the file:

```yaml
provider: openrouter
model: <model id>
timeout_seconds: 120
langfuse:
  enabled: true
  key_env: BUSINESS_PROFILE
  dataset: business-profile-gold
  run_name: <name>
  deepeval:
    enabled: true
    judge_model: google/gemini-2.5-flash
```

## Metrics

Per-field accuracy is reported for all six scored fields, but the number that
says whether this stage is doing its job is a single binary: **can paid search
reach this company?** It lives in
[`search_addressable_metrics`](../scripts/profile/business_profile_metrics.py)
and is reported as precision / recall / F1.

### The rule

One function, `is_search_addressable(demand, delivery, customer)`, decides it,
in two steps:

1. **`demand_model` answers it directly** when it committed to an answer:
   `consumer_search` and `local_service` are positive, every other value is
   negative.
2. **A category floor answers it from what the business *is*** when
   `demand_model` said `unclear` (or was never produced): a `b2c` or `mixed`
   company whose `delivery_model` is `hospitality`, `leisure_venue`,
   `professional_service`, `product_physical` or `trade_service` counts as
   reachable. `property`, `lending`, `product_digital`, `contracting` and
   `rental_leasing` are deliberately outside it — their demand often arrives
   through brokers, portals, storefronts or tenders. `b2b`, `public_sector`
   and `unclear` customers are outside it too.

The floor accepted `b2c` only until 2026-09-28. Under that rule 8 of prompt
v12's 18 missed leads were `unclear` demand + a floor delivery model +
`mixed` customer, and in four of them the gold customer was `mixed` as well,
so a correct answer was shut out. The b2c/mixed boundary is also the least
stable `customer_type` answer between runs. Widening the floor changes both
sides of the metric (gold and predicted go through the same rule), so search
numbers from before this date are comparable only after a `rescore`.

**The floor rescues; it never overrides.** A `demand_model` that committed to
a non-search answer stands. That distinction is the whole design: overriding
would promote `13043443` VENTRESS correctly, but at the cost of `10713956`
NINJA TUNE, whose demand genuinely does arrive through streaming platforms,
and two care providers whose work arrives through council commissioning. Where
a label is wrong — as Ventress's `platform_intermediated` was — the fix is the
label, not a rule that routes around it.

### Why the floor exists

`demand_model: unclear` used to remove a case from the metric altogether,
since there was no ground truth to score against. That silently deleted real
leads. `07538544` BIRD OVERSEAS is the case that found it: a hotel group whose
filing never once says how guests arrive — no "platform", "booking", "online",
"website" or "agent" anywhere in 54,711 characters — so `unclear` is the
honest label, and the company then vanished from the number the stage exists
to produce. That is worse than a wrong label, which at least surfaces the
company for someone to correct.

The floor lets `demand_model` stay strictly evidentiary (what the filing says)
while the commercial judgement lives in code that can be unit-tested and
changed without a prompt version bump or any relabelling. Two questions that
had been conflated in one field are now separate.

Of the six gold cases whose `demand_model` is `unclear`, four are rescued
(`07538544`, `13043443`, `04479650` SKYBOUND WEALTH, `09081062` PARADIGM
NORTON). The two that stay excluded — `00765538`, `08248223` — are excluded
because their `customer_type` is `unclear` too, so the floor cannot resolve
them either.

### Where the numbers go

| Destination | What lands there |
|---|---|
| `logs/business-profile-eval/report-<UTCstamp>.json` | the full block under `metrics.search_addressable` — `precision`, `recall`, `f1`, `tp`, `fp`, `fn`, `considered`, `gold_positives`, `missed_by_abstention`, `floor_rescued_gold`, `floor_rescued_predicted` — plus the raw per-case `results` |
| console | the one-line headline printed by `_print_summary` |
| Langfuse run-level scores | three scalars only: `search_addressable_precision`, `_recall`, `_f1`, via `flatten_metrics` |

The floor counts are deliberately kept out of Langfuse: they are diagnostics
for reading a report, not a series worth charting, and adding score names
churns the run-comparison view.

Because the reports persist the raw `results`, a change to the metric can be
re-scored against historical runs without spending anything on model calls.
Adding the floor moved `report-20260902T224723`'s recall from 0.600 to 0.625
(considered 48 → 49); the other two reports with a `metrics` block were
unchanged.

### Not yet persisted per company

`is_search_addressable` runs **only at eval time**. Nothing in the pipeline
calls it and no column records it, so today this is a measurement improvement
rather than a change to which companies get surfaced. When lead selection is
built it should import that function rather than re-deriving the rule.

Persisting it to `company_profiles` later is an additive migration, in the
shape of `ensure_currency_columns` in
[core/companies_house_sqlite.py](../core/companies_house_sqlite.py) — a
`pragma table_info` check, then `alter table ... add column` for anything
missing, so existing rows survive untouched. The things to decide before
writing it:

- **Store the verdict, the reason, or both?** A bare `search_addressable`
  boolean loses *why*. A companion `search_addressable_basis` recording
  `demand_model` vs `category_floor` keeps a row explainable, matching the
  quote-and-section discipline every other field already follows.
- **It is derived, not extracted.** Every other column in the table is
  something the model said about the filing. A derived column is a different
  kind of thing, and it goes stale whenever the rule changes — unlike
  `prompt_version`, which pins how a row was produced. It would need its own
  provenance (a rule version, or a `computed_at`) or a documented rule that it
  is recomputed rather than trusted.
- **Or do not store it at all.** The rule is cheap, and the five inputs are
  already on the row, so a view or a query-time call to
  `is_search_addressable` cannot go stale by construction. That is probably
  the right default unless something needs to filter on it in SQL at scale.

### Experimental search-opportunity review

The saved profile fields also support a broader, **experimental** question:
whether a material external business line could be independently discovered
through search even when its present acquisition channel is a relationship,
framework, repeat customer, or tender. This is intentionally derived in
[`search_opportunity_from_profile`](../scripts/profile/business_profile_metrics.py),
not requested from the model. It keeps the LLM response small and leaves
`is_search_addressable` and its historical metrics unchanged.

`python -m scripts.profile.business_profile_search_recall --report <report>`
creates a separate review snapshot at
`evals/business_profiles/search_opportunity_review.json` and two CSVs for
native-Sheet publication. The snapshot uses the existing human-reviewed
business fields only to make proposals. A human must set its independent
verdict before it becomes an evaluation label. It never edits demand model or
trading-status gold labels, and it is not a production lead-selection rule.

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

## Evaluating prompt v10

Run prompt v10 on the frozen 124 reviewed cases, using the configured model,
filing text and generation settings. First run two deliberately chosen boundary
cases to prove per-case Langfuse traces, durable checkpoints and recovery. Then
resume the same evaluation for the remaining 122 cases: the two saved responses
are replayed into the complete Langfuse run without another model call. The
checkpoint is JSONL, keyed by prompt version, model and company number; deleting
it deliberately forces a clean run. Confirm current model pricing and obtain
approval before a paid run.

Report exact counts and percentages for the original 109 cases and the 15
newly reviewed cases, the 8 SPVs, 8 investment holdings, 108 trading cases,
and the 17 previously missed search-addressable leads. The acceptance test is
better SPV and search-addressable recall, no increase in genuine trading
businesses excluded as SPVs/holdings, and search precision at least 95%.
Publish the report and per-case evidence to Projects / companies-house-leads
as native Google Sheets. Compare the result to the previously rescored v8
responses as historical context only; a fresh v9 call is not needed. Eight
examples per minority class remain a challenge set, not enough evidence of
production reliability.

## Open questions

- Should companies with no narrative (5,209 of 8,169 have none) route
  straight to the website stage, or be left unprofiled until their accounts
  are re-parsed? Note the narrative backfill was only ever run over
  companies with turnover, so some of that gap is reach, not absence.
- ~~Is `relationship_repeat` reliably distinguishable from `considered_b2b`
  in filed text, or should they merge until the gold set shows they separate?~~
  Resolved: no, they don't separate reliably -- merged into
  `relationship_or_contract` along with `tender_framework` (see the
  `demand_model` table above).
