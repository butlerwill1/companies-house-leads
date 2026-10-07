"""Taxonomy, prompt, and response validation for the business-profile stage
(Gate A2). See docs/BUSINESS_PROFILE_EXTRACTION.md for the design rationale.

The policy deliberately makes hallucination checkable rather than trusted:
every classification must carry a verbatim quote from the source text, and
a quote that does not appear in the section it claims to come from is
rejected here, before anything is persisted. No field is ever accepted on
the model's self-reported confidence alone.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from typing import Any

from pydantic import Field

from companies_house_core.llm_validation import (
    StrictResponseModel,
    JsonResponseError,
    merge_validation,
    parse_json_object,
    validate_object,
)

# v2: Phase 2's taxonomy prune (36 -> 33 classes) and Phase 3a/3c's rewrite
# of the uncertainty instruction and per-value field definitions -- a
# different prompt and a different response schema (unclear now means "no
# signal at all" instead of "the default safe answer"), so runs against v1
# should not be compared against v2 as if they measured the same thing.
#
# v3: customer_type is now decided by what the contract buys and who consumes
# it, not by who pays or who chooses the provider -- so an NHS-funded dental
# practice or a council-placed care provider is b2c, while a school builder or
# a government digital supplier is public_sector. `mixed` was widened from
# "consumers and businesses" to any two of b2c/b2b/public_sector, because the
# old wording had no way to express a contractor serving both private
# developers and councils. Same response schema as v2, different labelling
# rule: v2 and v3 customer_type numbers are not comparable.
#
# v4: delivery_model lost `distribution_resale`, merged into
# `product_physical` (see the comment above DELIVERY_MODEL_VALUES). The two
# were never alternatives on one axis, so a reseller of physical goods
# satisfied both and the field could not be applied consistently.
# `product_physical` now says explicitly that sourcing is irrelevant to it.
# Same response schema as v3, one fewer allowed value: v3 and v4
# delivery_model numbers are not comparable, and a v3 run's
# `distribution_resale` predictions have no v4 equivalent to score against.
#
# v5: delivery_model gains `hospitality`, `lending` and `leisure_venue`, and
# `professional_service` is redefined from "advisory or expert services" to
# the residual "any people-delivered service that is not site work, project
# contracting, hospitality, lending or a leisure venue". The prompt gloss and
# docs/BUSINESS_PROFILE_EXTRACTION.md had disagreed on that value since v2 --
# the doc called it the residual bucket, the prompt called it advisory work --
# so people-delivered services that are neither advisory nor site work had
# nowhere to go and hedged to `unclear`. An audit of all 14 v4 gold `unclear`
# cases found not one where the text genuinely fails to say what is delivered;
# every one was a missing enum slot. Hotels, lenders and sports clubs were the
# three clusters, at mean confidences of 0.68, 0.41 and 0.20 against 0.85-0.95
# everywhere else. Same response schema as v4, three more allowed values and
# one redefined: v4 and v5 delivery_model numbers are not comparable in either
# direction, and v4 `unclear` / `professional_service` / `property` /
# `product_physical` predictions all have v5 equivalents that mean something
# narrower.
#
# v5 also amends the customer_type `mixed` gloss to name where the proportion
# test is answered -- the turnover-by-class-of-business note -- and to say
# that a passing mention of sponsors or partners is not evidence of a second
# customer base. Folded into v5 rather than taken as a v6 because v5 had not
# been run when it was made, so there were no v5 customer_type numbers for it
# to invalidate. Anything registered as v5 from this point differs from the
# first v5 registration in that gloss.
#
# That sentence used to end "the Langfuse entry carries the exact text of each
# registration if it ever matters". It did not. Until the registry was fixed
# on 2026-09-10 it stored only the prompt skeleton, with the option blocks
# left as `{{<field>_options}}` placeholders -- so every gloss amendment,
# including this one, was invisible there, and Langfuse registrations 3
# through 7 are byte-identical across semantic v4 and v5 despite v5 adding
# three delivery_model values and rewriting three glosses. Registrations from
# v6 onward bake the blocks in and are diffable; earlier entries are not, and
# the code history is the only record of what those versions said.
#
# v5 likewise rewrites the demand_model `platform_intermediated` gloss, which
# had the same defect in a purer form: its example ("e.g. hotels booked
# through platforms") named an industry, and the model then applied the value
# to any hotel. Three of its four gold cases had no platform named anywhere in
# the filing, and were the three lowest-confidence in the class. Same reason
# for folding it into v5 rather than taking a v6: no v5 run exists.
#
# v6 changes the response schema, so v5 and v6 numbers are not comparable in
# either direction. Each classification field now returns quote, section,
# reason, value, confidence -- in that order -- and sic_agreement gains a
# quote and section.
#
# The order is the change; the reason field only works because of it. With
# value emitted first the model committed to an answer and then went looking
# for a quote to justify it, and the 2026-09-09 gold review found exactly the
# pattern that produces: 09406074 PSR EQUITIES cited a sentence about its
# suppliers as evidence of who its customers are, 10930289 BENNETTS used one
# sentence as evidence for three deliberately orthogonal fields, 06995506
# SIZE GROUP read a geography statement as a demand channel, and 13181834
# EARTHAVE recorded delivery_model "unclear" at 0.95 confidence with an empty
# quote. This is the same autoregressive argument already used to put
# sic_label last (see the note above PROMPT_TEMPLATE), applied inside the
# field object rather than only to the order of the prompt.
#
# reason is required and validated non-empty for every value including
# "unclear", where it must name the specific fact the text does not give
# rather than restate that the model was unsure. Only presence is checked --
# a lexical test for negation words would certify a property it cannot
# measure, and this module gates persistence on checkable things only.
#
# sic_agreement gains a quote because it was the one field with no verbatim
# quote guard at all: validation checked its value against the taxonomy and
# nothing else. Its quote anchors only the narrative half of the comparison
# -- the SIC code is handed to the model in the prompt, not found in the
# sections -- so the same principal-activity sentence is often cited whether
# the verdict is agrees or disagrees. It stops the model inventing what the
# business does; it does not discriminate between the two verdicts.
#
# No A/B was run before landing this. The published evidence is mixed and
# task-dependent: generating an explanation before the label underperforms
# label-first on short intuitive classification, but forcing evidence-backed
# support gains several points on long-document tasks, and gains more the
# longer the document. This task is the second kind -- 26k-59k characters of
# filed accounts where the answer turns on locating one sentence.
# v6 also rewrites the trading_status_confirmed `spv` gloss, which named three
# financial structures ("a special-purpose financing, concession, or
# securitisation vehicle") and so had no room for a captive trading
# subsidiary. 09202205 NORTHERN BALLET PRODUCTIONS is the case that found it:
# 6,861,645 turnover, zero employees, all staff recharged from its charitable
# parent, profit engineered to exactly nil by a 2,060,401 Theatre Tax Relief
# credit, and its only customer the parent that commissioned it. None of the
# five values fit -- `trading` requires "its own staff", `investment_holding`
# requires no trade named, `trading_group_parent` requires it to be the
# parent, and it is none of the three structures `spv` listed -- leaving
# `unclear` as the only defensible answer for a filing that says plenty. That
# is a missing enum slot, the same defect the v5 delivery_model audit found,
# and it lands in exactly the turnover-without-employees population this field
# exists to resolve.
#
# The new gloss covers two different things, and they get in for different
# reasons. Financing, concession and securitisation vehicles are named
# outright: they are special-purpose by construction, and their counterparty
# is usually external (a PFI vehicle bills a public body, EARTHAVE earns
# interest from outside borrowers). The captive subsidiary is the addition,
# and for it the test is the counterparty rather than the motive: its trade
# is with its own group. The motive is not what the filing evidences.
#
# That test cuts both ways. A charity trading subsidiary is NOT spv by
# virtue of being one -- 07306464 ST ANTONY'S COLLEGE TRADING hires out
# college conference rooms to outside customers (2024 trade debtors
# 170,905, nothing owed by the group), has no employees and gift-aids every
# penny to the college, and is `trading`. Only the ones whose customer is the
# parent, like Northern Ballet Productions, are spv. An earlier version of
# this note listed "charity trading subsidiaries" as an spv shape; that was
# wrong. Naming the motive would also repeat
# the v5 platform_intermediated mistake in a new place: "tax relief production
# company" would pull in any theatre or film business, and "tax relief
# company" would pull in every R&D claimant in the corpus, which is the more
# damaging direction -- it demotes real trading companies out of the leads.
# Hence the explicit negative clause.
#
# Sized before writing: 377 of 2,350 companies with turnover report zero
# employees, 30 of those match this shape, and of those roughly 17 are PFI
# concessions the old wording already served and 5 are group parents. So the
# wording decides perhaps 8 companies. Deliberately NOT a new enum value at
# that support level -- a sixth class would sit below MIN_RELIABLE_SUPPORT
# from the outset and add another near-synonym to hedge between, which is the
# argument that removed b2b2c and distribution_resale. Folded into v6 rather
# than taken as a v7 for the same reason the v5 gloss amendments were folded
# in: no v6 run exists, so there are no v6 trading_status_confirmed numbers
# for it to invalidate. Every company in this group is non-search-addressable
# under any of these labels, so the headline metric does not move either way.
#
# v6 also amends the delivery_model `product_physical` gloss to say that
# ownership matters even though sourcing does not: a business that auctions or
# brokers goods it never owns and books only commission is
# professional_service. 04304063 RAW2K found it -- an agent whose turnover is
# commission on used-car auctions, labelled product_physical because the gloss
# named "dealership" and the company is registered under SIC 45112 "Car
# dealers". `lending` already carried exactly this tie-break against
# professional_service; goods were missing their half of it, and FRS 102
# forces filings to state the agent/principal answer, so it is one of the more
# checkable distinctions in the taxonomy. Folded into v6 rather than taken as
# a v7 because v6 has not been run, so there are no v6 delivery_model numbers
# for it to invalidate. Recorded honestly: only one of the three gold filings
# carrying agent/principal language was mislabelled, so this closes a wording
# asymmetry rather than a frequent failure.
#
# v7: same schema, same taxonomy, same glosses as v6 -- only the quoting and
# value instructions change, so v6 and v7 field numbers are comparable and
# the thing expected to move is the rejection count. The 2026-09-13
# gpt-5.4-mini run over the 109-case set rejected 18 responses; after the two
# harness fixes of the same day (inline tags no longer split words in our
# text; a quote may read one column of a table) 14 remained, and none were
# fabrications. Eleven were quotes with their opening words regularised --
# "the principal activity of the group continues" quoted as "The group's
# principal activity continues", "the directors have identified" as "The
# company has identified", a filing's "principle activity" corrected to
# "principal" -- and three were demand_model / delivery_model answered
# "mixed", a value only customer_type has. v6 already said "character for
# character" and "a substring you could find with Ctrl-F"; the model still
# tidied, so v7 names the specific edits it must not make and tells it to
# shorten the quote rather than edit it. Gemini-3.7-flash passed 100% of
# quotes under v6, so this is a cross-model robustness change, not a fix
# for the model the labels were drafted with.
#
# v8: trading_status_confirmed loses `trading_group_parent`; a parent whose
# subsidiaries do the work is now `trading`. The v7 gpt-5.4-mini run got the
# field wrong 20 times and 12 of those were gold `trading_group_parent`
# answered `trading` -- the model reads "the group's principal activity is
# retail pharmacy" and calls it trading, and only the employee note (staff in
# the Group column, none in the Company column, flattened to one number per
# line) says which legal entity holds the payroll. Looking at those twelve
# with the note in hand also found three gold labels wrong the other way
# (C.P.J. FIELD, PILL BOX CHEMISTS, ADP ARCHITECTURE: the parent itself
# employs the staff), so the distinction was being applied inconsistently by
# the drafting model *and* by review. Nothing downstream consumes it:
# is_search_addressable never reads this field, and the design doc already
# calls a group parent lead-worthy, flagging only that the subsidiary's
# company number may need looking up -- a lookup that a future stage can do
# from the accounts (consolidated, Company column empty) without asking an
# LLM to read a flattened table. What the field was created for -- deciding
# whether a real trade stands behind Gate A's turnover-without-employees
# companies, or only shareholdings -- is the `trading` / `investment_holding`
# split, and that survives intact. The gold set is migrated mechanically
# (`review.taxonomy_migrations`), and scoring maps the retired value to
# `trading` in older saved responses so v6/v7 numbers stay comparable.
#
# v9: geography_served gets a threshold. `international` now means 5% or
# more of turnover from outside the UK where the notes give a geographic
# split; below that is an incidental export line and the answer is
# national_uk. The v8 definition said an "incidental export line" was not
# enough but never said what incidental meant, so it could not decide
# SC757671 BOOTH WELSH (97.2% UK): gold said international, the v8 model
# said national_uk, and both were defensible readings of the same words.
# The field exists to say where to point advertising, and for that a
# company earning 97% of its revenue in the UK is a UK company. 5% comes
# from the data, not from the air: across the 59 gold cases with a split,
# the smallest genuine international is 12.2% and the largest export line
# 3.6%, so anything in that gap gives the same answer. Six gold labels
# move (DOMU 3.6%, BOOTH WELSH 2.8%, TOWNHOUSE 1.7%, ELSEWHEN 1.2%, and
# INFORMED SOLUTIONS / THE INFORMED GROUP, whose split reads 100% United
# Kingdom and whose `international` rested on a Malaysian client win that
# earned nothing in the period -- exactly the error the v-earlier
# counter-error clause was written against). Same values, one rule
# changed: v8 and v9 geography_served numbers are not comparable, and no
# value is retired so nothing needs mapping in older responses.
# v10: two taxonomy clarifications target the errors revealed by the expanded
# 124-case gold set. `b2b_relationship` became `relationship_or_contract`:
# demand channel and customer type are independent, so a council-commissioned
# care service or a B2C business with repeat trade must not be forced into a
# B2B-labelled demand class. The old value maps mechanically to the new one.
# `spv` now distinguishes dedicated financing/securitisation/concession
# vehicles from ordinary contractors: a PFI vehicle may serve an NHS trust or
# council and still be an SPV, but a public-sector contract by itself is not
# enough. The taxonomy values for trading status are unchanged.
#
# v11: demand_model keeps its existing values, but changes its tie-break. A
# substantial, explicitly evidenced direct-search or local-service channel is
# now selected even where wholesale, repeat trade, a marketplace, or another
# channel contributes more revenue. This repairs an evidence-reading error:
# FLOUR POWER's owned ecommerce channel was explicitly described, yet the
# model selected wholesale relationships because they were also described.
# This is deliberately not business-type inference. A company type, a bare
# website mention, a payer, and an ordinary contract still do not establish a
# search channel. The rule applies only to demand_model; delivery_model still
# records the business's principal deliverable.
# v12: retain evidenced search-channel priority, but require positive evidence
# for relationship_or_contract rather than using it as a residual category.
# V11's lost leads cited a franchise agreement, the service delivered, organic
# growth, and student interest as acquisition mechanisms. One further loss
# came from rewritten quotes. Restore V10 customer/delivery instructions and
# ask for short contiguous evidence without changing the quote validator.
# These are testable hypotheses, not a claim of improved model performance.
PROMPT_VERSION = "business-profile-v12"

# Values retired from a field, and what they became. Applied to a model
# answer before it is scored, so responses saved under an older prompt score
# against the current gold set on the current taxonomy rather than being
# marked wrong for a value that no longer exists.
RETIRED_VALUES: dict[str, dict[str, str]] = {
    "demand_model": {"b2b_relationship": "relationship_or_contract"},
    "trading_status_confirmed": {"trading_group_parent": "trading"},
}


def normalise_retired_values(payload: dict[str, Any]) -> list[str]:
    """Rewrite retired values in a response to what they became, in place,
    and return the fields touched. For rescoring responses saved under an
    older prompt: without this the retired value fails validation as
    "not one of the allowed values" and the field is dropped, which would
    make a taxonomy change look like a model regression."""
    touched: list[str] = []
    for field, mapping in RETIRED_VALUES.items():
        entry = payload.get(field)
        if isinstance(entry, dict) and entry.get("value") in mapping:
            entry["value"] = mapping[entry["value"]]
            touched.append(field)
    return touched

# Sections read in priority order. Sections flagged is_auditor_text by
# companies_house_core/companies_house_pdf_text.py are excluded by the caller before this
# module ever sees them -- that text is the auditor describing its audit,
# not the company describing itself.
# The section key holding the whole filed document minus the auditor's report.
# Its presence is what puts validate_response into whole-document mode.
WHOLE_DOCUMENT_SECTION = "filed_report"

NARRATIVE_SECTION_PRIORITY = (
    # The whole filed document minus the auditor's report
    # (companies_house_core.companies_house_extractor.filed_report_text). Ranked first, and in
    # practice the only section present when a case is built this way: the
    # named windows below cannot be widened without evicting each other, so
    # they carry 417 of the 457 quotes the gold labels rest on, against 452
    # here. The named keys are kept for cases captured before the switch and
    # for other consumers of the stored sections.
    "filed_report",
    # Financial notes, not qualitative narrative -- but the decisive evidence
    # for geography_served and customer_type in practice: the turnover note's
    # geographic/class-of-business split settles calls the prose sections
    # often leave unclear (14 of 47 gold-set geography_served labels turned
    # on this note alone). Ranked first for exactly that reason.
    "turnover_note",
    "employee_note",
    "principal_activity",
    "business_review",
    "strategic_report",
    "directors_report",
    "principal_risks",
    "future_developments",
)

DEMAND_MODEL_VALUES = (
    "consumer_search",
    "local_service",
    "relationship_or_contract",
    "platform_intermediated",
    "not_customer_facing",
    "unclear",
)

# considered_b2b, tender_framework, and relationship_repeat were originally
# separate values (docs/BUSINESS_PROFILE_EXTRACTION.md once posed exactly
# this as an open question: "is relationship_repeat reliably distinguishable
# from considered_b2b in filed text?"). A 57-case gold-set run showed the
# answer is no -- narrative text rarely states whether repeat B2B trade was
# won by tender, referral, or research, so the model guessed among them
# about as often as it guessed right, and demand_model's accuracy (37-40%)
# was the worst of every field by a wide margin. Merged into one value; the
# distinction that actually matters for this field's purpose (is demand
# search-driven, i.e. can paid search work) is search vs not, not which
# non-search channel.
DEMAND_MODEL_DEFINITIONS: dict[str, str] = {
    "consumer_search": (
        "individuals search online and buy directly. Use this where the filing explicitly evidences "
        "a substantial direct online sales or acquisition channel -- for example owned ecommerce, "
        "direct-to-consumer online sales, organic search, or website search/traffic/conversion work. "
        "A bare mention that the company has "
        "a website, digital systems, or marketing is NOT enough"
    ),
    "local_service": (
        "individuals search for a nearby provider. The filing must support how customers find or "
        "choose the provider; a business type, location, appointment, or ticket sale alone does not "
        "establish the acquisition channel"
    ),
    "relationship_or_contract": (
        "the filing positively identifies how customer business is won: through referrals, repeat "
        "orders or ongoing customer accounts, commissioned work, tenders, frameworks or procurement. "
        "This is a CHANNEL, not a customer type: it can apply to b2c, b2b or public_sector. The quote "
        "must link the mechanism to winning or receiving customer business. A description of services "
        "delivered, customer satisfaction, organic growth or customer interest alone does not do this. "
        "A supplier, franchisor, licensing or intragroup agreement does not explain how external "
        "customers arrive. Neither do payment terms, a named payer, wholesale activity, ordinary sales "
        "contracts or revenue recognition alone. Absence of search evidence is NOT evidence of "
        "relationship_or_contract; use unclear when no acquisition mechanism is supported"
    ),
    # Counter-error (v5): the gloss used to end "(e.g. hotels booked through
    # platforms)", and that example was doing the classifying. Of the four
    # gold cases carrying this value, only 10713956 NINJA TUNE cited real
    # evidence ("consumption on key digital streaming services", confidence
    # 0.85). The other three were labelled off industry association with no
    # platform named anywhere in the filing: 07538544 BIRD OVERSEAS (0.5) and
    # 13043443 VENTRESS (0.6), both quoted from sentences that say only that
    # the business is a hotel, and 12861236 PHOENIX GAMES (0.4) from
    # "royalties earned from the sale of video games". The three lowest
    # confidences in the class were the three without evidence. Naming the
    # trigger industry inside the gloss is what caused it, so the example is
    # gone and the evidence requirement is explicit.
    "platform_intermediated": (
        "demand arrives via a marketplace, online travel agent, or aggregator. The text must NAME "
        "the platform, marketplace, aggregator, or streaming service the customers come through -- "
        "operating in an industry where such platforms are common (hotels, taxis, takeaways, games, "
        "music) is NOT evidence that this company's demand arrives that way"
    ),
    "not_customer_facing": "a holding vehicle, SPV, or investment company with no customer-facing trade of its own",
    "unclear": "the text does not support a confident call",
}



# b2b2c dropped: in 57 hand-labelled cases no human ever chose it and the
# model never predicted it once. An option nobody uses is not free -- it is
# one more near-synonym for the model to hedge between, and this field's
# dominant error is already hedging (9 of 14 errors were `mixed` chosen over
# a clean b2c or b2b).
CUSTOMER_TYPE_VALUES = ("b2c", "b2b", "public_sector", "mixed", "unclear")

# saas merged into product_digital: SaaS is a digital product, the split had
# one gold example each, and nothing downstream treats them differently.
#
# distribution_resale merged into product_physical (v4). The two were not
# alternatives on one axis -- product_physical answers "what form does the
# deliverable take", distribution_resale answers "did you make it or buy it
# in". Those are orthogonal, so every reseller of physical goods satisfied
# BOTH by construction, and the old gloss ("makes or sells physical goods")
# swallowed the other value whole. The gold set shows exactly that: three
# companies that plainly do not make what they sell -- 03121306
# IRONMONGERYDIRECT, 05332212 ONLINE 4 BABY, 05900590 BELL TRUCKS -- were
# split two-to-one across the two values by human reviewers, with no rule
# that separates them. No tie-break fixes this, because the deciding fact
# (who made the goods) is almost never in a filed narrative; the best
# available proxy was whether the filing happens to call itself a
# distributor, which labels the wording rather than the business. Same
# reasoning that merged considered_b2b / tender_framework /
# relationship_repeat into b2b_relationship. Nothing downstream distinguished
# them either: delivery_model is a stored text column
# (companies_house_core/companies_house_sqlite.py) that nothing branches on, and the headline
# search-addressable metric keys off demand_model alone. If make-vs-buy ever
# matters commercially it needs its own field fed by the website stage, not a
# second value on this one.
#
# rental_leasing and property are deliberately KEPT despite thin support --
# equipment and vehicle hire are among the most paid-search-driven categories
# there are, so the distinction changes the decision this stage exists to
# make. They get targeted labels instead of being merged away. NOTE:
# rental_leasing now has zero gold examples (it had one before the v4 redraft);
# the argument above is a bet on the addressable population, not a claim about
# the gold set, and it should be revisited if the next review pass still finds
# nothing to put in it.
#
# hospitality, lending and leisure_venue added in v5, each for the same reason
# rental_leasing is kept: the ad account they imply is not the one any
# neighbouring value implies. A hotel bids on dated availability through OTAs,
# a bridging lender bids on some of the most expensive keywords in UK search,
# a members' club sells renewals and ticketed events -- none of which look like
# a retailer's product feed or a consultancy's lead form. Before v5 all three
# fell to `unclear`, which is why that value had 14 members and not one of them
# was a case where the filing failed to say what was delivered.
DELIVERY_MODEL_VALUES = (
    "product_physical",
    "product_digital",
    "professional_service",
    "trade_service",
    "contracting",
    "hospitality",
    "lending",
    "leisure_venue",
    "rental_leasing",
    "property",
    "unclear",
)

GEOGRAPHY_SERVED_VALUES = ("local", "regional", "national_uk", "international", "unclear")

# dormant dropped: Gate A already decides it deterministically and for free
# from structured data (companies_house_core/company_triage.py, "no turnover and no
# employees"), and only 1 of the 2,960 companies that reach this stage with a
# filed narrative is dormant at all. Asking an LLM to re-derive a decision the
# free deterministic gate already made is pure waste. The field exists to
# separate a real trade from mere shareholdings among the 369
# turnover-without-employees companies Gate A explicitly refuses to guess
# about: `trading` covers the trade whether this entity or its subsidiaries
# carry it out (v8 retired `trading_group_parent`, see the changelog),
# `investment_holding` is no trade at all.
TRADING_STATUS_VALUES = (
    "trading",
    "investment_holding",
    "spv",
    "unclear",
)

SIC_AGREEMENT_VALUES = ("agrees", "disagrees", "unclear")

FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "demand_model": DEMAND_MODEL_VALUES,
    "customer_type": CUSTOMER_TYPE_VALUES,
    "delivery_model": DELIVERY_MODEL_VALUES,
    "geography_served": GEOGRAPHY_SERVED_VALUES,
    "trading_status_confirmed": TRADING_STATUS_VALUES,
}

# A one-line meaning for every value of every field. This exists because a
# bare list of enum names ("Allowed values: consumer_search, local_service,
# ...") gives the model nothing to reason from: these are our coinages, not
# terms it was trained to define the way we mean them. The evidence that this
# matters is direct -- the two fields whose prompts already carried per-value
# glosses (trading_status_confirmed, sic_agreement) ranked first and second on
# accuracy, the one field with none ranked last, and adding definitions to it
# moved it about +10 points.
#
# Several definitions below are written to counter a specific observed error
# rather than merely to describe the value; those carry a note saying so.
FIELD_DEFINITIONS: dict[str, dict[str, str]] = {
    "demand_model": DEMAND_MODEL_DEFINITIONS,
    # Decided by what the contract buys and who consumes it, NOT by who pays
    # or who chooses the provider. Counter-error: the old definitions ("sells
    # to individual consumers" / "sells to ... public bodies") gave no way to
    # place an NHS-funded dentist, a council-placed care provider, or a
    # community pharmacy, because the payer and the consumer differ -- the
    # model resolved that split by hedging to `mixed`. The split is now named
    # explicitly and resolved in favour of the consumer.
    "customer_type": {
        "b2c": (
            "what is sold is one individual's own consumption -- a course of treatment, a "
            "prescription, a care placement, a bed-week, a lesson, a meal. Choose this EVEN IF "
            "a public body chooses the provider, holds the contract, and pays the whole bill: "
            "public funding of a named person's care or treatment does not make the funder the "
            "customer"
        ),
        "b2b": (
            "what is sold is delivered to another business for its own use -- goods it will "
            "resell, a system it will run, work on its premises or operations. Services an "
            "organisation buys for its own staff (occupational health, training, employee "
            "benefits) are b2b, not b2c"
        ),
        "public_sector": (
            "what is sold is delivered to a government body, council, NHS body, school or "
            "similar for that body's own use -- a building it will own, a system it will run, "
            "work on its estate or operations. A public body merely paying for an individual's "
            "care or treatment is NOT enough; that is b2c"
        ),
        # Counter-error: 9 of 14 customer_type mistakes were `mixed` chosen
        # over a clean b2c or b2b. The model was using it as a hedge, so the
        # bar for it is stated explicitly rather than left to inference. The
        # second sentence is aimed at the specific hedge the rule above
        # removes: one customer base with two payers is not "mixed".
        #
        # v5 addition: stating the bar was not enough, because the gloss named
        # no way to MEASURE "significant proportion" and the model settled for
        # any sentence mentioning a second kind of customer. All three football
        # clubs in the gold set came back `mixed`, two of them on text that
        # evidences no proportion at all -- 09858599 NORTHAMPTON on a
        # directors' acknowledgements line thanking "the club's sponsors,
        # partners and any other person", 14934831 THE WILLOWS on an accounting
        # policy saying when sponsorship income is recognised. Their turnover
        # notes put the b2b share at 17.1% and 0.2%. The last sentence gives
        # the test an address: the turnover-by-class-of-business note, which
        # nearly every filing carries and which answers the proportion question
        # directly.
        "mixed": (
            "two of b2c / b2b / public_sector are served in significant proportion and the text "
            "evidences BOTH -- e.g. a contractor working for both private developers and "
            "councils, or a retailer with both a consumer shop and a trade counter. Do NOT "
            "choose this because one customer base has more than one source of payment, and do "
            "not choose it because you are unsure which dominates -- if the text points mainly "
            "at one, choose that one. Judge the proportion from the turnover note ('turnover "
            "analysed by class of business') where there is one, NOT from a passing mention: a "
            "sentence that merely names sponsors, partners or advertisers -- an acknowledgements "
            "line, or an accounting policy stating when such income is recognised -- is not "
            "evidence that a second customer base is significant"
        ),
        "unclear": "the text says nothing about who the customers are",
    },
    "delivery_model": {
        # Counter-error: the old gloss was "makes or sells physical goods",
        # which read as a manufacturer label and left every retailer,
        # wholesaler and dealer hedging against the since-removed
        # distribution_resale. Whether the company made the goods is
        # deliberately irrelevant here -- this field records the FORM of what
        # is delivered, nothing about how it was sourced.
        #
        # Counter-error: sourcing is irrelevant but OWNERSHIP is not, and the
        # gloss did not say so. 04304063 RAW2K, an agent auctioning used cars
        # whose turnover is commission only ("the sales value of the vehicles
        # being sold is not included in turnover as the Company is acting as
        # an agent not a principal"), came back product_physical -- the word
        # "dealership" here matched a business registered under SIC 45112
        # "Car dealers". This mirrors the tie-break `lending` already carries
        # against professional_service; goods were simply missing their half
        # of it.
        "product_physical": (
            "physical goods are what the customer receives -- retail, wholesale, distribution, "
            "dealership, or manufacture alike. It does NOT matter whether the company made the "
            "goods or bought them in to resell, but it does matter whether it owned them: a "
            "business that auctions, brokers or sells goods it never owns and books only "
            "commission is professional_service, not this"
        ),
        "product_digital": "sells software, digital products, or software-as-a-service",
        # Counter-error: this was "advisory or expert services delivered by
        # people (consultancy, legal, accountancy, agency work)", which the
        # design doc had always described as the residual people-delivered
        # bucket. The narrow reading left transport, personal care, health and
        # every other non-advisory service with no home, and they hedged to
        # `unclear`. It is the residual value, and it says so -- but only after
        # hospitality, lending and leisure_venue have been ruled out, which is
        # why they are named in it.
        "professional_service": (
            "any service delivered by people that is not site work, project contracting, "
            "hospitality, lending, or a leisure venue -- consultancy, legal, accountancy, "
            "agency work, transport, personal and health services"
        ),
        "trade_service": "hands-on skilled work at a customer's site (plumbing, electrical, installation, repair)",
        "contracting": "delivers projects under contract, typically construction or engineering",
        # Tie-break against leisure_venue: a bed or a meal is hospitality;
        # admission, membership or participation is leisure_venue. A business
        # doing both follows whichever the narrative names as dominant.
        "hospitality": (
            "operates places where guests eat, drink, or stay -- hotels, B&Bs, holiday parks, "
            "restaurants, cafes, pubs, event catering"
        ),
        # Tie-break against professional_service: lending your own money is
        # lending; advising on, broking, or intermediating someone else's money
        # is professional_service.
        "lending": (
            "lends its own money or provides credit as principal (bridging, mortgages, "
            "asset finance, invoice finance)"
        ),
        "leisure_venue": (
            "runs a venue or club people pay to attend or belong to -- sports clubs, gyms, "
            "golf clubs, theme parks, stadia, visitor attractions"
        ),
        "rental_leasing": "rents or leases assets to customers rather than selling them (equipment, vehicles, plant hire)",
        "property": "owns, develops, or lets property as its business",
        # This means the filing genuinely does not say -- a holding company
        # with no described trade, a financing vehicle. It is NOT the answer
        # for a business whose activity is stated but fits no value above; if
        # that happens the taxonomy has a hole and should get a value.
        "unclear": "the text does not say what is actually delivered",
    },
    "geography_served": {
        "local": "serves one town, city, or immediate area",
        "regional": "serves a region of the UK",
        "national_uk": (
            "serves the UK broadly. This INCLUDES a company whose geographic turnover split shows "
            "less than 5% of turnover from outside the UK -- that is an incidental export line, "
            "not an international business"
        ),
        # Counter-error: 6 of 14 geography mistakes were national_uk answered
        # as international. The model treated any foreign mention -- an
        # overseas parent, a subsidiary, an incidental export line -- as
        # evidence of international customers. v9 turns "incidental" into a
        # number because the word alone could not decide a case: 97.2% UK
        # (SC757671 BOOTH WELSH) was called international by gold and
        # national_uk by the v8 model, and the definition supported both.
        # 5% is set from the gold set's own distribution -- of 59 cases with a
        # geographic split, the smallest genuine international is 12.2% and
        # the largest incidental export line is 3.6%, so any threshold in
        # that gap gives the same answer.
        "international": (
            "a material share of turnover comes from CUSTOMERS outside the UK. Where the notes "
            "give a turnover split by geographical market, material means 5% or more of turnover "
            "from outside the UK -- read the current-year column. Where no split is given, the "
            "narrative must name an overseas market the company actively serves. A foreign parent "
            "company, an overseas subsidiary, a foreign shareholder, an overseas client win, or an "
            "export line under 5% is NOT enough on its own"
        ),
        "unclear": "the text does not indicate geographic reach",
    },
    "trading_status_confirmed": {
        "trading": (
            "a real business selling to customers outside its own group -- run by this company "
            "itself, or by its subsidiaries with this company filing as the head of the group; "
            "either way the narrative names an actual trade"
        ),
        "investment_holding": "owns shares or property and names no trade of its own",
        # Counter-error: the old gloss named three financial structures
        # ("financing, concession, or securitisation") and so had nowhere to
        # put a captive trading subsidiary -- see the v6 note at the top of
        # this module. What every company in this bucket shares is not a
        # motive but a counterparty: its own group.
        "spv": (
            "the company exists to sit inside a fixed structure rather than to compete for ordinary trade. "
            "There are two routes: (1) the text identifies a dedicated financing, securitisation or concession "
            "vehicle, including a PFI project company; it remains spv even when its fixed contractual counterparty "
            "is a council, NHS trust or other external public body; or (2) a captive subsidiary performs work only "
            "for its parent or group. A public-sector contract alone is NOT enough: an ordinary contractor serving "
            "a council is trading. Do NOT choose this merely because a company claims a tax relief, reports few or "
            "no employees, outsources work or belongs to a group: a subsidiary selling to external customers is trading"
        ),
        "unclear": "the narrative does not say enough to place it",
    },
    "sic_agreement": {
        "agrees": "the business described is consistent with the registered SIC classification",
        "disagrees": "the business described does not match the registered SIC classification",
        "unclear": "there is not enough description to judge against the SIC code",
    },
}


def format_field_options(field: str) -> str:
    """The allowed values for one field, each with its one-line meaning."""
    definitions = FIELD_DEFINITIONS[field]
    values = FIELD_VALUES.get(field) or SIC_AGREEMENT_VALUES
    return "\n".join(f"  {value} -- {definitions[value]}" for value in values)

# The classification fields precede sic_agreement in every prompt and
# response ordering in this module. In an autoregressive model, tokens
# generated earlier condition tokens generated later: if the SIC label were
# shown or asked about before the model describes the business in its own
# words, the description anchors on it and the independent read -- the
# entire purpose of this stage -- is lost. sic_label is deliberately the
# last thing given to the model, after it has already made its calls.
PROMPT_TEMPLATE = """You are reading a UK company's filed annual report to record how it \
acquires customers. Base every answer only on the text given below. Do not use outside \
knowledge of the company or the industry.

Company name: {company_name}

Filed narrative sections:
{sections_block}

For each field below, work in the order the JSON shape shows. First find a short quote copied \
EXACTLY (character for character) from the section text above, and name the section it came from. \
Then say in one sentence what that quote tells you about the question. Only then commit to a \
value and a confidence. Do not choose a value first and then go looking for a quote that fits it: \
the quote is what produces the answer, not a justification added afterwards.

Express uncertainty through the confidence number, not by withholding an answer. A call \
the text states outright gets high confidence; a reasonable inference from indirect \
evidence gets low confidence. Both are more useful than "unclear", because a low-confidence \
answer can be filtered later while a missing one cannot be recovered. Reserve "unclear" for \
when the text genuinely says nothing bearing on the question -- not for when the answer is \
merely implicit, or when you had to reason to reach it. When you do answer "unclear", leave \
the quote empty and use the reason to name the specific fact the text does not give -- "the \
filing never says who the borrowers are", not "insufficient information to determine". For \
demand_model, this does not permit guessing a channel from business type: if the filing describes \
the activity but gives no acquisition evidence, use unclear. Low confidence cannot replace that evidence.

Keep the questions separate. customer_type is who consumes what is sold; demand_model records \
how external customer business arrives, subject to the channel priority below. A council commissioning an individual's care can be b2c and \
relationship_or_contract. A hotel group can have intragroup property letting while its principal \
external business serves guests. A PFI concession can be spv while its counterparty is a public \
body. A college subsidiary hiring facilities to outside customers is trading. Do not let one \
field's answer determine another.

reason -- one sentence connecting the quote to the answer. Write it as the step that produces \
the value, not as a justification for a value you had already chosen.

confidence -- a number from 0.0 to 1.0. Use the range honestly: it is what decides whether \
your answer is relied on, so a confident-sounding number on a weak inference is worse than \
a low one.

demand_model -- the evidenced acquisition channel to use for this business:
{demand_model_options}

Demand channel priority: scan the whole filing for all substantial external business lines before \
selecting the quote. If consumer_search or local_service is evidenced for one of those lines, \
select it even when relationships, wholesale or marketplaces contribute more revenue. A core \
business line or actual sales described as substantial can establish materiality; an incidental \
mention or a future aspiration cannot. If both search categories qualify, choose the one with \
the clearest acquisition evidence. Otherwise choose the best-supported channel, or unclear if \
none is supported. Do not infer that a channel exists from the business type or location.

Evidence boundaries: a retailer's established owned online shop can coexist with marketplace \
sales; marketplace use does not erase that direct channel. A franchise licence describes the \
relationship with the brand owner, not how diners arrive. "Organic growth" does not mean organic \
search or referrals. Services provided to clients and expressions of customer interest describe \
activity or demand, not necessarily an acquisition mechanism.

customer_type -- who the customers are. Decide by what the contract buys and who consumes it, not by who pays or who picks the provider. Name the person whose consumption the contract pays for: if you can name them (each patient, each resident, each placement), the customer is that individual; if the answer is the buying organisation itself, or the public generally, the customer is that organisation:
{customer_type_options}
delivery_model -- what is delivered and how:
{delivery_model_options}
geography_served -- geographic reach:
{geography_served_options}

Also provide:
business_description -- one plain sentence describing what the company actually does, in \
your own words based on the text.

trading_status_confirmed -- is there a real trade here (this company's own, or its \
group's), only shareholdings, or a vehicle that exists for its own group:
{trading_status_confirmed_options}

The company's registered SIC classification is: {sic_label} ({sic_code}).
sic_agreement -- does the text you read describe a business consistent with that \
classification. Quote the sentence saying what the business actually does, name its section, \
then give a one-sentence reason, then the verdict. Answer "unclear" with an empty quote only if \
the text never says what the business does.
{sic_agreement_options}

Each field's "value" must be one of the options listed for THAT field. "mixed" is a \
customer_type option only; demand_model and delivery_model have no "mixed". For demand_model \
use the channel priority above. For delivery_model, if two values apply, choose the one the text \
supports most directly and lower the confidence.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{
  "business_description": "...",
  "demand_model": {{"quote": "...", "section": "...", "reason": "...", "value": "...", "confidence": 0.0}},
  "customer_type": {{"quote": "...", "section": "...", "reason": "...", "value": "...", "confidence": 0.0}},
  "delivery_model": {{"quote": "...", "section": "...", "reason": "...", "value": "...", "confidence": 0.0}},
  "geography_served": {{"quote": "...", "section": "...", "reason": "...", "value": "...", "confidence": 0.0}},
  "trading_status_confirmed": {{"quote": "...", "section": "...", "reason": "...", "value": "...", "confidence": 0.0}},
  "sic_agreement": {{"quote": "...", "section": "...", "reason": "...", "value": "..."}}
}}

"section" must be one of the section names shown above (e.g. "principal_activity"). \
"quote" must be a substring you could find with Ctrl-F in the text above. Copy it character \
for character, including the filing's own spelling, grammar and punctuation, even where they \
are wrong. Do not replace the sentence's subject ("the directors", "Nantwich Ltd", "the group") \
with "the company", do not correct a typo, do not merge two sentences, and do not tidy the \
opening words. If a sentence starts with words you would otherwise want to change, start the \
quote after them: a shorter exact quote beats a longer edited one, and an edited quote is \
rejected outright. Prefer the shortest contiguous passage that supports the answer. You may \
read the whole table to reason, but copy the supporting row or passage in its original order \
rather than reconstructing a table or joining separated cells. Keep word boundaries, subjects \
and numbers exactly as supplied. Re-check every quote against the source before returning \
the JSON; the reason may interpret the evidence, the quote must only copy it. Do not paraphrase, \
summarise, or add ellipses."""


def build_sections_block(sections: dict[str, str]) -> str:
    parts = []
    for key in NARRATIVE_SECTION_PRIORITY:
        text = sections.get(key)
        if text:
            parts.append(f"[{key}]\n{text}")
    return "\n\n".join(parts) if parts else "(no usable narrative text available)"


def build_prompt(*, company_name: str, sections: dict[str, str], sic_label: str | None, sic_code: str | None) -> str:
    return PROMPT_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        sections_block=build_sections_block(sections),
        **prompt_option_blocks(),
        sic_label=sic_label or "(none declared)",
        sic_code=sic_code or "(none)",
    )


def prompt_option_blocks() -> dict[str, str]:
    """The `{<field>_options}` substitutions PROMPT_TEMPLATE expects.

    Kept separate from build_prompt so the whole-document comparison harness,
    which formats PROMPT_TEMPLATE itself against a different sections_block,
    cannot drift out of sync with the field definitions here."""
    return {f"{field}_options": format_field_options(field) for field in FIELD_DEFINITIONS}


def parse_json_response(text: str) -> dict[str, Any]:
    """Backward-compatible JSON parser shared by all classifier policies."""
    return parse_json_object(text).payload


def parse_json_response_with_validation(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the parsed object and the repair record for run diagnostics."""
    parsed = parse_json_object(text)
    return parsed.payload, parsed.validation


class _ProfileFieldResponse(StrictResponseModel):
    value: str
    quote: str | None = None
    section: str | None = None
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class _SicAgreementResponse(StrictResponseModel):
    value: str
    quote: str | None = None
    section: str | None = None
    reason: str = Field(min_length=1)


def structure_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """Pydantic type checks, kept separate from taxonomy and quote evidence."""
    from companies_house_core.llm_validation import JsonResponse

    base = JsonResponse(payload, {"version": "classifier-validation-v1", "status": "valid",
                                  "parse_method": "already_parsed", "repairs": [], "normalisations": [],
                                  "errors": [], "warnings": []})
    reports: list[dict[str, Any]] = []
    description = payload.get("business_description")
    if not isinstance(description, str) or not description.strip():
        reports.append({"errors": [{"path": "business_description", "code": "string_type",
                                     "message": "Input should be a non-empty string"}]})
    for field in FIELD_VALUES:
        value = payload.get(field)
        if not isinstance(value, dict):
            reports.append({"errors": [{"path": field, "code": "model_type",
                                         "message": "Input should be an object"}]})
            continue
        _, report = validate_object(JsonResponse(value, base.validation), _ProfileFieldResponse)
        report["errors"] = [{**issue, "path": f"{field}.{issue['path']}"} for issue in report["errors"]]
        report["warnings"] = [{**issue, "path": f"{field}.{issue['path']}"} for issue in report["warnings"]]
        reports.append(report)
    sic = payload.get("sic_agreement")
    if not isinstance(sic, dict):
        reports.append({"errors": [{"path": "sic_agreement", "code": "model_type",
                                     "message": "Input should be an object"}]})
    else:
        _, report = validate_object(JsonResponse(sic, base.validation), _SicAgreementResponse)
        report["errors"] = [{**issue, "path": f"sic_agreement.{issue['path']}"} for issue in report["errors"]]
        report["warnings"] = [{**issue, "path": f"sic_agreement.{issue['path']}"} for issue in report["warnings"]]
        reports.append(report)
    return merge_validation(base.validation, *reports)


_QUOTE_WHITESPACE_RE = re.compile(r"\s+")
# Punctuation not directly between two digits -- so "22,557,801" and "12.5%"
# stay intact (a comma/period there changes which number is being claimed),
# while a sentence's trailing full stop, a stray semicolon, or a straight
# vs curly quote does not (that's formatting, not a different claim).
_QUOTE_SOFT_PUNCT_RE = re.compile(r"(?<!\d)[.,;:!?\"'‘’“”()\[\]{}\-–—]+(?!\d)")


def normalize_quote_text(text: str) -> str:
    """Normalize a quote (or the source text it's checked against) before
    the verbatim-match check. Filed HTML tables collapse to markdown with
    inconsistent line breaks and spacing, and a model occasionally drops a
    trailing period, reformats a quotation mark, or re-cases the first
    letter of a sentence it is quoting mid-JSON-string ("The..." -> "the...",
    "DoBeDo..." -> "DOBEDO...") without changing what it is actually
    claiming -- that should not fail the hallucination check that this
    exists to run, which cares whether the words came from the source, not
    whether their capitalization survived being embedded in a JSON value.
    Confirmed live in the 2026-09-02 Phase 3d smoke test: two rejections
    were exactly this. See docs/BUSINESS_PROFILE_EXTRACTION.md for the real
    rejected quotes that motivated the rest of this normalization.

    Punctuation is replaced with a space, not deleted outright, and
    whitespace is re-collapsed afterward -- deleting it outright means
    whether a hyphen originally had spaces around it changes the result:
    "long-term" -> "longterm" but "long - term" -> "long term", two
    different strings for the same two words. Also confirmed live the same
    day: a model's quote used spaced-out hyphens where the source had none,
    for otherwise identical, correctly-quoted text."""
    text = text.casefold()
    text = _QUOTE_WHITESPACE_RE.sub(" ", text)
    text = _QUOTE_SOFT_PUNCT_RE.sub(" ", text)
    return _QUOTE_WHITESPACE_RE.sub(" ", text).strip()


_NUMERIC_TOKEN_RE = re.compile(r"^[£$€(]?[\d,]+(?:\.\d+)?%?\)?$")


def quote_reads_table_row(quote: str, text: str) -> bool:
    """Accept a quote that reads one column of a multi-column table.

    A turnover-by-geography note is a table: "United Kingdom | 13,026,917 |
    12,787,696 | North America | 2,363,493 | 1,667,002". A model quoting the
    current-year column writes "United Kingdom 13,026,917 North America
    2,363,493", which is a faithful reading of the table but not a contiguous
    substring of it, and the 2026-09-13 gpt-5.4-mini run lost two cases that
    way. The quote's tokens must appear in the same order in the source with
    nothing skipped except numeric tokens, so a fabricated sentence still
    cannot pass: every word has to be there, in order.
    """
    qt = normalize_quote_text(quote).split()
    tt = normalize_quote_text(text).split()
    if len(qt) < 2 or len(tt) < len(qt):
        return False
    first = qt[0]
    for start in (i for i, tok in enumerate(tt) if tok == first):
        qi, ti, skipped = 0, start, 0
        while qi < len(qt) and ti < len(tt):
            if tt[ti] == qt[qi]:
                qi += 1
            elif _NUMERIC_TOKEN_RE.match(tt[ti]) or not any(ch.isalnum() for ch in tt[ti]):
                # a number, or table furniture such as the "|" cell separator
                skipped += 1
                if skipped > 2 * len(qt):
                    break
            else:
                break
            ti += 1
        if qi == len(qt):
            return True
    return False


# A quote this short has too little text for a one-word difference to be
# distinguishable from a different sentence, so it must match exactly.
FUZZY_MIN_WORDS = 6
# One differing word allowed per this many quoted words, rounded up: a
# 6-13 word quote may differ in one word, 14-21 in two, and so on.
FUZZY_WORDS_PER_EDIT = 8
# A differing word from this set flips the meaning of a sentence, so it is
# never absorbed by the budget, however long the quote.
_NEGATION_TOKENS = frozenset({
    "no", "not", "never", "none", "nor", "neither", "without", "cannot", "nothing", "nobody",
})
# Words that by themselves decide one of the labels. A quote that swaps,
# drops or adds one of these is making a different claim about the thing
# being classified even when every other word matches, so it gets no
# tolerance either. Kept to the vocabulary the taxonomy definitions turn on;
# it is not a general synonym list.
_CLAIM_TOKENS = frozenset({
    # customer_type
    "consumer", "consumers", "public", "individual", "individuals", "household", "households",
    "business", "businesses", "corporate", "commercial", "trade",
    # geography_served
    "uk", "overseas", "international", "internationally", "export", "exports", "exported",
    "europe", "european", "worldwide", "global", "globally", "abroad", "domestic",
    "local", "locally", "national", "nationally", "regional",
    # delivery_model
    "online", "digital", "physical", "manufacture", "manufactures", "manufacturing",
    "contracting", "contractor", "contractors", "software", "platform",
    # trading_status_confirmed
    "ceased", "cease", "dormant", "discontinued", "sold", "disposal", "disposed", "wound",
})


def _protected_token(token: str) -> bool:
    """A token whose change is a different claim, not a different spelling:
    anything carrying a digit (an amount, a year, a count, a name such as
    "STM 360"), a negation, or a word the taxonomy turns on."""
    return any(ch.isdigit() for ch in token) or token in _NEGATION_TOKENS or token in _CLAIM_TOKENS


def quote_matches_fuzzily(quote: str, text: str) -> bool:
    """Accept a quote that differs from the source by a bounded number of
    ordinary words.

    Two 109-case gpt-5.4-mini runs (2026-09-13) lost 20 fields to quotes the
    model had tidied on the way out -- "manages" written as "managed",
    "activities" as "activity", the filing's own typo "main principle" silently
    corrected to "principal", a pronoun swapped for its antecedent. Every one
    was a faithful reading of the passage; the check rejected them because it
    only knew exact substrings. This is the tolerance that lets those through
    while a fabricated sentence still fails: the quote must align to one
    passage of the source with no more than ceil(words / 8) differing words,
    and no differing word may carry a digit, be a negation, or be one of the
    words the taxonomy turns on ("consumers", "overseas", "ceased") -- those
    are changes of claim, not of wording. Reordering a sentence ("the group's
    principal activity" for "the principal activity of the group") costs more
    than the budget on purpose: that is a rewrite, and the prompt asks the
    model to quote, not rewrite.

    Words at either end of the quote that the source has instead of the
    quote's own count as one edit per quote word, not per source word: where
    a quote starts and stops is the model's choice, and the exact check
    already lets it cut a sentence anywhere.
    """
    qw = normalize_quote_text(quote).split()
    if len(qw) < FUZZY_MIN_WORDS:
        return False
    tw = normalize_quote_text(text).split()
    if len(tw) < len(qw):
        return False
    budget = math.ceil(len(qw) / FUZZY_WORDS_PER_EDIT)
    anchor = difflib.SequenceMatcher(None, tw, qw, autojunk=False).find_longest_match(0, len(tw), 0, len(qw))
    if anchor.size == 0:
        return False
    run = qw[anchor.b:anchor.b + anchor.size]
    # The longest shared run may occur more than once in a filing ("the
    # principal activity of the company" usually does); try each occurrence.
    for start in (i for i in range(len(tw) - anchor.size + 1) if tw[i:i + anchor.size] == run):
        lo = max(0, start - anchor.b - budget)
        hi = min(len(tw), start + (len(qw) - anchor.b) + budget)
        if _aligned_within_budget(qw, tw[lo:hi], budget):
            return True
    return False


def _aligned_within_budget(qw: list[str], window: list[str], budget: int) -> bool:
    ops = difflib.SequenceMatcher(None, qw, window, autojunk=False).get_opcodes()
    # Source words before the quote begins or after it ends are the window's
    # slack, not differences.
    if ops and ops[0][0] == "insert":
        ops = ops[1:]
    if ops and ops[-1][0] == "insert":
        ops = ops[:-1]
    edits = 0
    for index, (op, a1, a2, b1, b2) in enumerate(ops):
        if op == "equal":
            continue
        at_edge = index == 0 or index == len(ops) - 1
        changed = qw[a1:a2] + ([] if at_edge else window[b1:b2])
        if any(_protected_token(token) for token in changed):
            return False
        edits += (a2 - a1) if at_edge else max(a2 - a1, b2 - b1)
        if edits > budget:
            return False
    return True


QUOTE_MATCH_EXACT = "exact"
QUOTE_MATCH_TABLE_ROW = "table_row"
QUOTE_MATCH_FUZZY = "fuzzy"


def quote_match_kind(quote: str, text: str) -> str | None:
    """How the quote was found in the text, or None if it was not: exact
    substring, one column of a table row, or a bounded fuzzy match. The
    kinds are tried cheapest and strictest first, so a quote is only ever
    reported as fuzzy when nothing stricter accepted it."""
    if normalize_quote_text(quote) in normalize_quote_text(text):
        return QUOTE_MATCH_EXACT
    if quote_reads_table_row(quote, text):
        return QUOTE_MATCH_TABLE_ROW
    if quote_matches_fuzzily(quote, text):
        return QUOTE_MATCH_FUZZY
    return None


def _quote_errors(label: str, quote: str, section_name: Any, sections: dict[str, str]) -> list[str]:
    """The verbatim-quote check, shared by the classification fields and by
    sic_agreement.

    Whole-document mode: the model is shown one blob of text with the filing's
    own headings still visible inside it, so it cites those ("Strategic
    report", "Notes to the financial statements") rather than the synthetic
    wrapper key it was never told to use. Rejecting that is pedantry -- it
    costs a correct extraction over a label, which is exactly what happened on
    the first two cases of the 2026-09-07 smoke run. The hallucination check
    that actually matters, that the quote is verbatim in what the model was
    given, is kept in full."""
    if WHOLE_DOCUMENT_SECTION in sections:
        if quote_match_kind(quote, sections[WHOLE_DOCUMENT_SECTION]) is None:
            return [f"{label}.quote does not appear verbatim in the filed document: {quote!r}"]
        return []
    section_text = sections.get(section_name) if section_name else None
    if section_text is None:
        return [f"{label}.section {section_name!r} is not one of the sections given to the model"]
    if quote_match_kind(quote, section_text) is None:
        return [f"{label}.quote does not appear verbatim in section {section_name!r}: {quote!r}"]
    return []


def _quote_source(section_name: Any, sections: dict[str, str]) -> str | None:
    if WHOLE_DOCUMENT_SECTION in sections:
        return sections[WHOLE_DOCUMENT_SECTION]
    return sections.get(section_name) if section_name else None


def mark_quote_matches(payload: dict[str, Any], sections: dict[str, str]) -> dict[str, str]:
    """Record on each quoted field how its quote was found -- ``quote_match``
    of "exact", "table_row" or "fuzzy" -- and return ``{field: kind}``. A
    tolerance that leaves no trace is indistinguishable from the model
    having quoted correctly; this is what keeps the fuzzy path visible in
    the eval report, the Langfuse output and the saved response. Fields with
    no quote, or whose quote was not found at all, are left untouched (the
    latter are rejected by :func:`validate_fields`)."""
    kinds: dict[str, str] = {}
    for field in (*FIELD_VALUES, "sic_agreement"):
        entry = payload.get(field)
        if not isinstance(entry, dict):
            continue
        quote = entry.get("quote")
        source = _quote_source(entry.get("section"), sections)
        if not quote or source is None:
            continue
        kind = quote_match_kind(quote, source)
        if kind is not None:
            entry["quote_match"] = kind
            kinds[field] = kind
    return kinds


def validate_response(
    payload: dict[str, Any], sections: dict[str, str], *, require_sic_quote: bool = True
) -> list[str]:
    """Every problem with the response, flattened. Empty means fully valid.

    Until 2026-09-14 any problem here rejected the whole response. That was
    chosen as a signal -- one invented quote, trust nothing -- before anyone
    had looked at what actually fails. Two 109-case runs later, every
    rejection was a one-letter drift in an otherwise honest quote, and the
    other fields in those responses had passed the same verbatim check,
    which is the only grounding guarantee this module ever offered. Discarding
    them bought nothing. Callers now use :func:`validate_fields` and
    :func:`reject_failed_fields` to drop only the failing field; this
    flattened form is kept for the gold-set shape check and for tests."""
    return [error for errors in validate_fields(payload, sections, require_sic_quote=require_sic_quote).values() for error in errors]


def validate_fields(
    payload: dict[str, Any], sections: dict[str, str], *, require_sic_quote: bool = True
) -> dict[str, list[str]]:
    """Problems per field: ``{field_name: [errors]}``, only fields with a
    problem present. ``business_description`` and ``sic_agreement`` are
    fields here like any other."""
    errors: dict[str, list[str]] = {}

    def add(field: str, message: str) -> None:
        errors.setdefault(field, []).append(message)

    description = payload.get("business_description")
    if not isinstance(description, str) or not description.strip():
        add("business_description", "business_description is missing or empty")

    for field, allowed in FIELD_VALUES.items():
        entry = payload.get(field)
        if not isinstance(entry, dict):
            add(field, f"{field} is missing or not an object")
            continue
        value = entry.get("value")
        if value not in allowed:
            add(field, f"{field}.value {value!r} is not one of {allowed}")
            continue
        # Confidence is requested and returned but was, until now, never
        # checked -- a response with confidence 1.5, "high", or missing
        # entirely passed validation exactly like a real one. Checked for
        # every value including "unclear": the prompt asks for it
        # regardless (Phase 3a made confidence the way uncertainty gets
        # expressed at all), and every real response on hand already
        # includes 0.0 there, so this tightens nothing that was actually
        # in use. bool is excluded explicitly because Python's bool is an
        # int subclass -- True would otherwise silently pass as 1.0.
        confidence = entry.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            add(field, f"{field}.confidence {confidence!r} must be a number between 0.0 and 1.0")
        # Checked before the "unclear" short-circuit below, and that
        # placement is the point: an "unclear" answer has no quote to
        # inspect, so the reason is the only record of what was looked for
        # and not found. Presence and non-emptiness only -- whether the
        # sentence really names the missing fact is a semantic property, and
        # this module gates persistence on checkable things (see the note at
        # the top of the file). A lexical test for negation words would
        # certify a property it cannot measure.
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            add(field, f"{field}.reason is missing or empty")
        quote = entry.get("quote") or ""
        if value == "unclear":
            continue
        if not quote:
            add(field, f"{field} has value {value!r} but no supporting quote")
            continue
        for problem in _quote_errors(field, quote, entry.get("section"), sections):
            add(field, problem)

    sic = payload.get("sic_agreement")
    if not isinstance(sic, dict) or sic.get("value") not in SIC_AGREEMENT_VALUES:
        add("sic_agreement", f"sic_agreement.value must be one of {SIC_AGREEMENT_VALUES}")
    else:
        sic_reason = sic.get("reason")
        if not isinstance(sic_reason, str) or not sic_reason.strip():
            add("sic_agreement", "sic_agreement.reason is missing or empty")
        # require_sic_quote is False only when validating a gold `expected`
        # block: those were written before sic_agreement had a quote at all,
        # and cannot be given one without re-reading 109 filings. Model
        # responses are held to the same evidence standard as every other
        # field.
        sic_quote = sic.get("quote") or ""
        if require_sic_quote and sic.get("value") != "unclear":
            if not sic_quote:
                add("sic_agreement", "sic_agreement has a verdict but no supporting quote")
            else:
                for problem in _quote_errors("sic_agreement", sic_quote, sic.get("section"), sections):
                    add("sic_agreement", problem)

    return errors


def reject_failed_fields(payload: dict[str, Any], field_errors: dict[str, list[str]]) -> dict[str, Any]:
    """A copy of the response with each failing field replaced by a null
    entry that records why. The value, quote, section and confidence go --
    a quote that failed the verbatim check is exactly the evidence that
    must not be persisted -- and the reason carries the rejection so the
    row explains itself. Fields that passed are untouched."""
    out = json.loads(json.dumps(payload))
    for field, errors in field_errors.items():
        note = "rejected: " + "; ".join(errors)
        if field == "business_description":
            out[field] = None
        elif field == "sic_agreement":
            out[field] = {"value": None, "quote": None, "section": None, "reason": note}
        else:
            out[field] = {"value": None, "quote": None, "section": None, "confidence": None, "reason": note}
    return out


def select_narrative_sections(all_sections: dict[str, dict[str, Any]]) -> dict[str, str]:
    """From the full stored section payload (section_key -> {text,
    is_auditor_text, ...}), keep only company-authored text in the priority
    list this stage reads."""
    return {
        key: entry["text"]
        for key, entry in all_sections.items()
        if key in NARRATIVE_SECTION_PRIORITY and entry.get("text") and not entry.get("is_auditor_text")
    }
