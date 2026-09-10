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

How customers actually arrive, **as the filing describes it**. It is the
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
| `b2b_relationship` | B2B demand via research-then-enquire, tender/framework/procurement, or ongoing accounts/referrals/repeat trade -- any non-search B2B channel | `05898590` "IT services to business customers"; `06717844` "main building contractors for construction contracts"; `12683499` crane hire |
| `platform_intermediated` | demand arrives via marketplace/OTA/aggregator | hotels via OTAs |
| `not_customer_facing` | holding vehicle, SPV, investment company | `SC540426` "investment holding company" |
| `unclear` | text does not support a call | — |

`considered_b2b`, `tender_framework`, and `relationship_repeat` were originally
separate values -- merged into `b2b_relationship` after a 57-case gold-set run
showed the answer to the open question below was no: filed narrative text
essentially never states whether repeat B2B trade was won by tender, referral,
or research, so the model guessed among the three about as often as it got it
right, and this field's accuracy (37-40%) was worst of all six by a wide
margin. The distinction that actually matters for this field's purpose (can
paid search work) is search vs not-search, not which non-search channel.
`wholesale_contract` was folded into `b2b_relationship` for the same reason:
a small number of large contracted buyers is a non-search B2B channel, which
is exactly what that value already means.

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
when `demand_model` is `unclear`, a `b2c` company whose `delivery_model` is
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
| `national_uk` | serves the UK broadly |
| `international` | sells to CUSTOMERS outside the UK. A foreign parent company, an overseas subsidiary, a foreign shareholder, or an incidental export line is NOT enough on its own -- the text must indicate customers or markets abroad |
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
  World) and `07608360` ELSEWHEN are labelled off it correctly.
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
checking when you see it, though: it is how `06995506` above was caught. Of the
three such pairs remaining in the gold set, `14934831`, `12989408` TOWNHOUSE
(opening US salons) and `02998017` C.P.J. FIELD (international repatriation)
are all genuine.

Gold-set support across 109 cases: `national_uk` 44, `international` 32,
`regional` 16, `local` 9, `unclear` 8. That `international` is 29% of the set
is high for a UK SME population and worth watching -- the counter-error above
inflates it in exactly one direction.

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
   `demand_model` said `unclear` (or was never produced): a `b2c` company
   whose `delivery_model` is `hospitality`, `leisure_venue`,
   `professional_service`, `product_physical` or `trade_service` counts as
   reachable. `property`, `lending`, `product_digital`, `contracting` and
   `rental_leasing` are deliberately outside it — their demand often arrives
   through brokers, portals, storefronts or tenders.

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
- ~~Is `relationship_repeat` reliably distinguishable from `considered_b2b`
  in filed text, or should they merge until the gold set shows they separate?~~
  Resolved: no, they don't separate reliably -- merged into `b2b_relationship`
  along with `tender_framework` (see the `demand_model` table above).
