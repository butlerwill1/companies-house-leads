# Web stage plan, W0 to W5 (2026-10-02)

The build plan for the web stage: what each step does, what it stores, in
what order, with which provider, and at what cost. It was agreed with the
user on 2026-10-02 and has not been implemented yet beyond what is marked
"built". Implement it one step at a time from "Order of work", following
the notes at the end.

## Context

The search screen leaves 1,538 companies (after removing duplicates) whose
customers could plausibly find them online. The web stage turns each one
into a lead your friend can judge:

- what the business actually does: Google's category and a description,
  instead of the SIC code
- whether people search for it
- whether it advertises on Google now
- how good its advertising setup is: HubSpot, conversion tracking, call
  tracking and so on, which your friend asked for

The output is concrete talking points per company, then a short list sized
to your friend's weekly capacity.

This plan replaces the first draft of this document (also dated
2026-10-02). The definitions and pre-registered acceptance criteria stay in
`docs/WEB_STAGE.md`.

## Decisions so far

- **Providers, one job each, swappable** (`scripts/web/search_providers.py`):
  - Serper for finding websites (2,500 free searches)
  - Google Places as the pay-as-you-go fallback (adapter built only if
    needed)
  - DataForSEO for Ads Transparency, keyword volume and cost per click, and
    organic traffic
  - OpenRouter for the site profile
- **Two DataForSEO accounts:** yours ($0.73 left) for tests, your friend's
  for full runs. Separate ledger and cap per account, one shared cache.
- **Advertising signal:** Ads Transparency plus the Google Ads tag on the
  site. Labs' paid estimate is dropped (it caught 3 of 11 advertisers). One
  live search is used only for organic position and map presence.
- **Technology detection:** our own curated rules (about 60 tools), up to
  25 pages per site.
- **Browser fallback (Playwright), built now,** only for pages that fail or
  come back thin. It identifies as this project and honours robots.txt. A
  block, challenge or CAPTCHA is recorded as `blocked`, never worked around.
- **Every paid run needs your go and a cap.** Runs on your friend's account
  also need their agreement to the cap.

## Order of work

Everything downstream needs the right website for each company, so W1's
gold-set run is the critical path. It waits on two things from you: a
Serper key and the 25 blind labels. Until then, the free building happens
in two parallel tracks.

| Step | Track A (critical path) | Track B (free, no waiting) | Spend |
|---|---|---|---|
| 1 | W1 fixes: trading names, PDF policies, blocked sites | W2 technology rules and detection over the 20 test sites' saved pages | none |
| 2 | Two-account DataForSEO support; trim W4 runner | W2 crawl (25 pages), browser fallback | none |
| 3 | **You:** Serper key + 25 blind labels. Then the W1 gold run (about 250 Serper credits) and its score against the criteria | W2 crawl pilot on the 20 test sites | Serper free |
| 4 | W1 on the whole queue (about 1,000-1,300 companies) | | Serper free |
| 5 | W2 crawl and detection on every found website | | none |
| 6 | W3 profile: gold set of about 60, then the full run | | about $3 OpenRouter |
| 7 | W4 market on a pilot of 20, then the whole list (friend's account) | | about $3.50 DataForSEO |
| 8 | Findings and gap segments; W5 lead sheet and outcome log | | none |

So: start with Track B's W2 detection and Track A's W1 fixes together,
since neither waits on anything. W2 detection on the cached test pages also
shows your friend results quickly.

## Stages, with what each stores

### W0: setup (built)

- **Queue order** (`scripts/web/web_rank_order.py`): 1,538 companies, core
  turnover band first.
- **Provider adapters, cache, ledger, caps** (`search_providers.py`):
  Serper, DataForSEO (Maps, organic, live results with ads, Ads
  Transparency, keyword volume, Labs traffic, ranked keywords, account
  check, category list), SerpApi, postcodes.io.
- **Two-account DataForSEO support (built):**
  - `.env` keys `DATAFORSEO_FRIEND_LOGIN` and `DATAFORSEO_FRIEND_PASSWORD`
  - a `--dataforseo-account mine|friend` flag
  - each account's ledger under its own provider key (`dataforseo` and
    `dataforseo:friend`)
  - the friend's account with no default cap
  - `check` showing both balances

### W1: website and Maps listing (built, including the three fixes)

**Population run (2026-10-03, Serper's 2,500 free credits, no DataForSEO):**

- **Cost of the first lookup.** A Serper Maps search costs 3 credits, so
  Maps-first averaged 4.1 credits a company. After 32 companies the resolver
  gained a places-first option (`--first-lookup places`, version
  `identity-v3-places-first`): Serper places search near the postcode's
  district, 1 credit. On the same 32 companies it chose the same website for
  31, with no tier worse, and found the same number of listings.
- **What places-first gives up.** It returns a thinner listing:
  - one category, sometimes not Google's official name ("Property Developer");
  - no secondary categories, place id or opening hours;
  - a category or rating missing slightly more often (category 23/32 vs 26,
    rating 21 vs 23).
- **Speed.** `--workers 4` resolves companies in parallel, about 450 an hour.
  The client reserves each call's cost under a lock, so parallel calls cannot
  pass the cap. The credits ended at exactly 2,500.
- **Result: 1,113 of 1,538 companies resolved.**

  | Group | Resolved |
  |---|---|
  | Core turnover band | 1,088 of 1,133 |
  | Above the band | 7 of 140 |
  | Below the band | 18 of 265 |
  | The 100 identity gold cases | all |

  | Tier | Companies |
  |---|---|
  | Verified | 353 |
  | Probable | 330 |
  | Ambiguous | 266 |
  | Blocked | 122 |
  | None | 42 |

  - 690 companies have a chosen site (verified, probable or hand-found).
  - 895 have a Maps listing (787 with a category, 661 with a rating).
- **Against the 21 hand-found test companies: 17 agree.** The misses:
  - a directory chosen (daynurseries.co.uk for Millie's House);
  - a sister site verified by the same company number (jancettplaysafe.co.uk);
  - two ambiguous picks of a legal-name domain (ara-concept.com, paystore.com).

  Hand-found rows are kept when storing; the resolver's answer for those
  companies stays in the checkpoint.
- **Follow-ups:**
  - add listing directories (daynurseries.co.uk and similar) to the blocklist;
  - prefer the listing's own site when a number-verified candidate is a
    different brand;
  - settle the 266 ambiguous companies, for example from the listing's
    website.

Built: `scripts/web/web_identity.py`, `web_population.py identity`, and the
gold-set tool `web_review.py`. 100 companies are drawn, with the 25 blind
ones waiting in Drive.

Existing tables: `company_web_identity` (each candidate site with tier and
evidence) and `company_google_listing` (Google category, rating, reviews,
claimed, match). Fixes:

1. **Trading names**, from three free sources:
   - "trading as" lines in the filed report (`filed_report_text` in
     `core/companies_house_extractor.py`)
   - the "X is a trading name of Y Limited" line on verified sites
   - the title of a Maps listing that links to a verified site

   They're also searched when the registered name finds nothing. New
   table:

   ```sql
   create table if not exists company_trading_names (
       company_number text not null,
       name text not null,
       source text not null,          -- filing / website / maps_listing
       evidence text,                 -- the sentence or listing it came from
       found_at text not null,
       unique(company_number, name)
   );
   ```
2. **PDF privacy and terms pages:** read with PyMuPDF, already in
   `requirements-eval.txt`.
3. **Blocked sites:** recorded as `blocked`, not `none`. A Maps listing
   matched on name and postcode can still make the site `probable`.

**Settling the ambiguous companies, then the W2 crawl (2026-10-03, no paid calls):**

- **Why they were ambiguous.** W1 reads a candidate's home page and up to three
  legal pages. A company is ambiguous when a candidate shows its name, or its
  domain spells it, but nothing in those pages ties the site to this company.
- **The settle step (`scripts/web/web_settle.py`).** W2's crawl reads up to 25
  pages, including privacy, terms and contact pages, so it re-judges each
  ambiguous company's two best candidates against the whole crawled site, with
  no new searches. The rules are in the module docstring. Two were tightened
  after reading the first pass's results:
  - the full legal name must appear as a disclosure (a privacy, terms,
    contact or about page, or next to "(c)", "registered", "data controller"
    or a postcode): a news story naming a company (Gilks) or a client case
    study (Pikrevni) is not one;
  - name plus the Maps listing's phone only counts when the listing matched
    the registered postcode: otherwise a namesake's listing and its own
    site simply agree with each other (Lotus Homes (UK), London, was matched
    to a Peterborough company).
- **Result: 41 of 266 settled** (4 by company number, 35 by legal name with
  corroboration or disclosure, 2 by name and postcode). A first pass with
  looser rules settled 71; about 30 of those rested on evidence that does not
  prove the site is this company's, and some were plainly wrong, so they were
  dropped.
- **The other 225 stay ambiguous:**

  | Why | Companies |
  |---|---|
  | The name is not on the crawled pages (a wrong site, or an unrelated business) | 71 |
  | A short name is on a site whose listing matches elsewhere; nothing ties it to this company | 69 |
  | A short name on the site, and no listing | 42 |
  | Name plus the listing's phone, but the listing is not at the registered postcode (possible namesake) | 27 |
  | The legal name only in body text (news, case study, partner list) | 11 |
  | The site is unreadable | 5 |

  `logs/web/ambiguous-remaining.json` has each one's best candidate and its
  evidence. About 150 of them are plausible and could be confirmed by comparing
  the filing's principal activity with the site (a cheap model call), or by eye.
- **Later steps read every resolver version.** The crawl, market and findings
  steps had been tied to one resolver version, which would have crawled 42
  companies. They now read `company_web_identity_current`.
- **Directories.** 36 domains that appeared as a "candidate" for many unrelated
  companies (company-data mirrors, academic sites, marketplaces) joined the
  blocklist.

**Model check for the unproven ones, and the refused sites (2026-10-03):**

- **Why a model.** About 150 of the 225 companies that stayed ambiguous had a
  plausible site that no rule could prove: it shows a shorter brand ("Heaton
  Group" for Heaton Group Developments Limited), or its footer names a sister
  company with another number (Heaton's footer says it is a trading style of
  Heaton Group Manchester Limited, 08480568). The earlier label for the group
  "name not found on the crawled pages" was too blunt: it meant the full
  registered name, and it also covered brand-only and sister-company sites.
- **The check (`scripts/web/web_settle_model.py`, prompt `web-settle-check-v2`,
  registered in Langfuse).** One OpenRouter call per company, on its best
  crawled candidate: the filing's principal activity, SIC, Maps listing, the
  site's title, home-page text and footer text, and facts (registered postcode
  shown, listing phone shown, other registration numbers printed). The verdict
  is `same_business`, `different_business` or `cannot_tell`. A same_business
  verdict must quote the website's own words, checked by `quote_match`: the
  trial caught a verdict that quoted the Maps listing, so the listing and the
  filing no longer count as site text.
- **Who was checked.** 177 companies, those with a crawled candidate that shows
  the name or brand, the registered postcode or the listing's phone: 140 others
  had nothing on the site tying it to the company, and 28 had no crawled
  candidate. Cost $0.16 (141k prompt tokens).
- **Verdicts:** 69 same_business, 97 different_business, 11 cannot_tell.
  - The rejections held up on reading: a Peterborough lettings firm for Lotus
    Homes (UK), a news site for Gilks (Nantwich), a client case study for
    Pikrevni, a New Zealand supplier for Pacific Health.
  - Sister companies on one site got mixed verdicts (Hartwood Care Limited and
    (4) same_business, (2) cannot_tell), so it is not perfectly consistent.
- **A UK sign is required to promote.** 20 of the 69 had none (no UK domain,
  UK phone or registered postcode): mostly global brand sites for UK
  subsidiaries (Jamf, Larian, Neil Patel) and a doubtful match (Lapa Holdings to
  jeffersonhealth.org). They are held in `logs/web/settle-model-review.csv`.
  49 were settled (rule `model:same_business`).
- **Refused sites, browser fallback.** The 122 refused companies share 92
  sites. The fallback (honours robots.txt, the project's user agent, never
  works around a challenge; 12 pages a site) read 20; 72 still refuse (37 HTTP
  403, 30 challenge pages, 5 other errors). Only 2 of the 122 companies settled
  (by company number); 120 stay `blocked`. The list included directories
  blocklisted after W1 ran, which the settle step now skips.
- **Result.** 780 companies now have a chosen website (347 verified, 433
  probable), on 738 sites, all crawled and detected. Still unresolved:
  176 ambiguous, 120 blocked and 42 with no site found.

### W2: site crawl and technology detection (built, free)

**Population run (2026-10-03):** 1,023 sites crawled (the chosen sites and the
ambiguous companies' candidates), 14,136 pages, none failed, in about 45
minutes at 8 sites at a time. Detection then ran on the 691 chosen sites,
which cover 729 companies (some share a site): 653 readable, 17 too thin to
read and 21 that refused our download. Of the 653 readable:

| Signal | Sites |
|---|---|
| Google Ads tag | 251 (38%) |
| Ads conversion event | 178 (27%) |
| Remarketing | 69 (11%) |
| GA4 | 468 (72%) |
| Tag Manager | 351 (54%) |
| A CRM (HubSpot 75, Salesforce/Pardot 14, Marketo 4) | 96 (15%) |
| Call tracking | 21 (3%) |
| Social pixels | 229 (35%) |
| Microsoft Ads | 99 (15%) |
| Contact form / click-to-call | 540 / 444 |
| Booking / checkout | 37 / 143 |
| An agency credit | 92 (14%) |
| WordPress | 348 |

Detection is single-threaded (about 12 sites a minute), so the last 507 sites
ran as four processes.

Design rules:

- **Keyed by domain, not company:** Pay Store and Store First share one
  site.
- **Fetch once, detect many times.** Raw pages live in the gzipped page
  cache (`data/raw/web-pages/`, about 1.5 GB for 1,000 sites). The database
  stores what was extracted. Changing a rule means re-running detection,
  free, under a new `rule_version`.
- **Every detection keeps its evidence:** the matched script or snippet,
  the page, and whether it was found on the page or inside Tag Manager.
- **Pages:** up to 25 per site, in priority order:
  1. the homepage
  2. the sitemap
  3. contact, about, services, pricing, privacy and terms
  4. service and location pages
  5. landing-style URLs
  6. one product and one blog page
  7. each Tag Manager container (`gtm.js`)
- **Pace:** about 8 sites in parallel, with a pause per host. 1,000 sites
  take roughly an hour.

```sql
-- One row per fetched page.
create table if not exists web_pages (
    id integer primary key autoincrement,
    domain text not null,
    url text not null,
    final_url text,
    page_kind text not null,       -- home / about / service / location / landing / product / blog / contact /
                                   -- pricing / privacy / terms / sitemap / gtm_container / other
    crawl_version text not null,
    fetched_with text not null,    -- http / browser
    status_code integer,
    fetch_error text,              -- 'blocked (403)', 'challenge page', 'robots.txt', 'not html', timeout
    cache_key text,
    fetched_at text,
    html_bytes integer,
    in_navigation integer,         -- linked from the homepage menu (landing pages usually aren't)
    script_hosts text,             -- JSON: third-party script hosts (browser: hosts actually requested)
    title text, meta_description text, h1 text, word_count integer, lang text,
    canonical_url text, noindex integer, has_viewport integer,
    schema_types text,             -- JSON: LocalBusiness, Product, AggregateRating, FAQPage...
    form_count integer,
    form_providers text,           -- JSON: hubspot, gravity_forms, contact_form_7, typeform...
    tel_link_count integer,
    phone_numbers text,            -- JSON: distinct UK numbers shown
    cta_phrases text,              -- JSON: 'get a quote', 'book now', 'add to basket'...
    price_mentions integer,
    company_number_found integer,
    copyright_year integer,
    unique(domain, url, crawl_version)
);

-- One row per technology found per domain.
create table if not exists web_technologies (
    id integer primary key autoincrement,
    domain text not null,
    rule_version text not null,
    technology text not null,      -- HubSpot, Google Ads, CallRail, Cookiebot...
    category text not null,        -- tag_manager / analytics / search_ads / social_ads / consent / crm_automation /
                                   -- email / call_tracking / optimisation / landing_pages / chat / booking /
                                   -- ecommerce / cms / reviews
    found_in text not null,        -- page / gtm / both
    account_ids text,              -- JSON: AW-..., G-..., GTM-..., HubSpot portal id (shared IDs = shared owner/agency)
    evidence text,
    evidence_url text,
    detected_at text not null,
    unique(domain, rule_version, technology)
);

-- One summary row per domain (derived, free to recompute).
create table if not exists web_sites (
    domain text not null,
    crawl_version text not null,
    rule_version text not null,
    crawl_status text not null,    -- ok / blocked / unreachable / parked / thin
    pages_fetched integer, pages_by_browser integer,
    https integer, mobile_ready integer, platform text, copyright_year integer,
    sitemap_url_count integer, sitemap_newest_lastmod text, product_url_count integer,
    service_page_count integer, location_page_count integer, landing_page_count integer,
    has_blog integer, blog_latest_date text, schema_types text,
    gtm_ids text, ga4_ids text, google_ads_ids text,
    has_google_ads_tag integer, has_ads_conversion_event integer, has_ads_remarketing integer,
    has_consent_mode integer, consent_vendor text, server_side_tagging integer,
    social_pixels text, has_microsoft_ads integer,
    crm_vendors text, email_vendors text, call_tracking_vendor text,
    optimisation_vendors text, landing_page_builder text,
    has_contact_form integer, has_click_to_call integer, has_booking text, has_checkout integer,
    has_live_chat text, review_widget text,
    agency_credit text, agency_credit_url text,
    summarised_at text not null,
    primary key (domain, crawl_version, rule_version)
);
```

Code:

- **`scripts/web/tech_rules.py`:** each rule has a name, a category, where
  it may match (`page` / `gtm`; platform rules are page-only, which fixes
  the preview's Wix/WooCommerce false positives), patterns, and a pattern
  that captures account IDs.
- **`scripts/web/web_crawl.py`:** page selection and fetching through the
  existing `Fetcher` (`scripts/web/web_fetch.py`). `Fetcher` changes:
  - allow JavaScript for `gtm.js`
  - gzip the cache
  - record `fetched_with`
  - recognise challenge pages as blocked
- **`scripts/web/web_browser.py`:** the Playwright fallback. It records the
  hosts a page actually requested. Needs `playwright` in
  `requirements-eval.txt` and a one-off `playwright install chromium`
  (about 150 MB, run with your OK).
- **`scripts/web/web_detect.py`:**
  - page facts, extending `parse_html`
  - technologies, from the rules
  - the site summary

### W3: site profile and category (built, not yet run; about $3 for 1,000)

One OpenRouter call per company. It reads the W2 page text, the filing's
principal activity and the Maps category. Every label except the summary
quotes the page, checked against the text the model was shown. The
pattern follows `company_profiles`, reusing `call_model`, the checkpoint
and the Langfuse replay from `scripts/screen/search_screen_eval.py`.

```sql
create table if not exists company_web_profile (
    company_number text not null,
    profile_version text not null,
    model text not null,
    domain text,
    summary text,
    products_services text,                 -- JSON list
    customer_type text, customer_type_quote text, customer_type_quote_valid integer,
    conversion_action text, conversion_action_quote text, conversion_action_quote_valid integer,
                                            -- buy online / book / call / quote form / visit / tender
    geography text, main_town text, geography_quote text, geography_quote_valid integer,
    urgency text,                           -- emergency / planned / considered
    ticket_band text,
    channel_fit text,                       -- search / social / both
    google_category text,
    category_source text,                   -- google_listing / model_assigned (from the free category list)
    seed_keywords text,                     -- JSON: 10-20 phrases customers type
    prompt_tokens integer, completion_tokens integer,
    profiled_at text not null,
    primary key (company_number, profile_version, model)
);
```

Gold set: about 60 companies, blind subset first, scored per field, with a
same-prompt rerun to measure noise. Each run gets a Langfuse trace.

**Test runs on the 23 hand-found companies (2026-10-02, gpt-5.4-mini, 21 sent):**

- **v1 (`web-profile-v1-quoted`, $0.08):** 9 quotes failed the check. Five of
  them were exact copies of the principal activity, which the prompt shows
  but the check ignored. Re-checked offline against everything shown, with
  the business-profile stage's bounded tolerance (`quote_match_kind`) and
  "..." joins, 9 became 2.
- **v2 (`web-profile-v2-sources`, $0.09):** quotes are checked against the site
  text, principal activity and Maps category, and any failure gets one retry
  that names the failed quotes.
  - 2 companies needed the retry, and both were fixed.
  - Every quote is now valid (2 fields answered `unclear`, 0 failed).
  - v1 and v2 agree on customer_type 20/21, conversion 18/21, geography 18/21
    and urgency 17/21. The prompt changed between them, so this is an upper
    bound on noise, not a clean rerun.
- **Category shortlist:** for the 19 companies with a Maps category, that
  category was in the 25-category word-overlap shortlist only 6 times. Rare
  categories outrank common ones ("business card design" for a cafe), so
  `model_assigned` categories are not yet trustworthy.
- **Conversion vs crawl:** 12 of 20 checkable answers match a crawl signal.
  Most mismatches are `book` on sites with no booking widget, where booking
  goes through a form or phone.
- **Phrases:** 248/379 (v1) and 259/378 (v2) of the model's phrases have
  search volume, against 104/110 for hand-drafted ones. The totals swing with
  phrase style:
  - SDC Clinics: 337,170 a month in v1 ("dentist near me" alone is 246,000
    nationally), 3,860 in v2 (every phrase suffixed "scotland", 12 with no
    volume);
  - only 1 of 21 companies changed side of the 500 threshold, because nearly
    all are far above it.

  So the 500 threshold separates very little, and the phrases need a style
  rule: generic service terms with no place names, with location applied in
  W4 rather than written into the phrase. The phrase check (gold-set step 4)
  decides this.

**Phrase rule (proposed, for prompt v3 after the gold review):**

- **Prompt.** Write each phrase as a customer types it, naming the product or
  service only. Leave out where the customer is: no town, county, region or
  country, and no "near me", "nearby" or "local". The one exception is a
  destination business (hotel, venue, attraction), where the place is what is
  sold: "spa hotel lake district" stays.
- **Code check, after the model.** Drop and count any phrase that contains
  "near me", "nearby" or "local", or a place word, unless the Google category
  is a destination category. Place words come from the company's own main town
  and site locations, plus a fixed list of UK nations, regions, counties and
  large towns. A phrase is dropped, never rewritten. The count is stored so
  the rule's effect is visible.
- **W4 applies location instead.** Volume is requested for the company's
  area (Google Ads location codes go down to city and county) for local and
  regional businesses, and for the UK for national ones. So "dentist" in
  Glasgow is measured, rather than the phrase "dentist glasgow".

**Gold set (2026-10-02).**

- **Drafts.** Claude, in chat, drafted labels and reference phrases for the
  21 test companies with site text (`evals/web_profile/drafts-2026-10-02.json`),
  from the model's inputs and before reading its answers.
- **Review.** The drafts are reviewed in Langfuse, in the "Web profile labels
  review" and "Web profile phrases review" queues
  (`scripts/web/web_profile_gold.py`).
- **Draft vs gpt-5.4-mini v2:** customer_type 13/21, conversion 16/21,
  geography 16/21, urgency 15/21, ticket_band 11/21 and channel_fit 18/21
  agree.
- **Two definitions are behind most disagreements:**
  - the model answers `mixed` for 12 of 21 (the draft for 4), because "mixed"
    is not defined;
  - ticket_band leans high, because "one sale" is undefined for contracts and
    subscriptions.

  Both get a definition in v3.
- **The draft's verdict on the model's v2 phrases:** 3 good, 13 partly good,
  5 poor.

**Prompt v3 (`web-profile-v3-enquiry-tender`, 2026-10-03), from review of
the drafts:**

- **conversion_action is now the main route the website offers**, not how
  customers end up buying.
  - `quote_form` is renamed `enquiry_form`: any form, callback requests
    included.
  - A form wins a tie with a phone number.
  - `tender` is no longer a value.
- **New flag, `wins_by_tender` (yes/no/unclear):** much of the work comes
  through public tenders, frameworks or contracted public programmes, which
  advertising does not reach. A yes must be quoted; a no needs no quote.
  - Endurance (ESPO and other frameworks) and Aran (council and housing
    programmes) are yes.
  - It is a negative signal for leads.
- **Old values in Langfuse were migrated in place:** quote_form to
  enquiry_form everywhere, and per-case changes for Endurance, Aran, PSG Law
  and Davisons. Items already marked Completed were set back to pending so
  the new field gets a look.
- **Prompt management:** the prompt is registered in Langfuse as `web-profile`
  (`scripts/web/web_profile_prompt_registry.py register`, to re-run after
  every bump), and each run records which version it used. v1 and v2 were
  edited in place before the registry existed, so only v3 onwards is there.

### W4: market, advertising and gap (built)

Per company:

- Ads Transparency
- search volume and cost per click for its W3 phrases (batched, 1,000 per
  call)
- Labs organic traffic (batched, 1,000 websites per call)

On the short list only: one live search for organic position and map
presence. Trimming: drop the Labs paid-keyword call, make the live check
optional, store to SQLite, and checkpoint per company.

```sql
create table if not exists company_keyword_market (
    company_number text not null,
    market_version text not null,
    keyword text not null,
    location_code integer not null,
    search_volume integer, cpc_usd real, competition text, competition_index integer,
    top_of_page_bid_high_usd real,
    fetched_at text not null,
    primary key (company_number, market_version, keyword)
);

create table if not exists company_market (
    company_number text not null,
    market_version text not null,
    domain text,
    -- Ads Transparency
    ads_seen integer, advertising_now integer, ads_first_shown text, ads_last_shown text,
    advertiser_verified integer, advertiser_name text,
    -- demand
    phrase_search_volume integer, weighted_cpc_usd real, monthly_click_value_usd real,
    -- organic (Labs)
    organic_etv real, organic_keywords integer, local_pack_etv real,
    -- rollup of the findings (below)
    gap_count integer, strength_count integer,
    setup_level text,              -- none / basic / partial / sophisticated
    gap_segment text,              -- greenfield / advertising_poorly / advertising_well / site_first
    assessed_at text not null,
    primary key (company_number, market_version)
);

create table if not exists serp_observations (
    company_number text not null,
    market_version text not null,
    keyword text not null,
    latitude real, longitude real,
    observed_at text not null,
    in_ads integer, organic_position integer, in_local_pack integer,
    advertisers text,              -- JSON list of domains in the ads
    primary key (company_number, market_version, keyword)
);
```

### Findings: the talking points (after W4, free)

These are rules over `web_sites` plus `company_market`
(`scripts/web/web_findings.py`). Each finding is either a gap (a pitch
point) or a strength (something they already do). They are stored in
plain English for the lead sheet.

```sql
create table if not exists company_setup_findings (
    id integer primary key autoincrement,
    company_number text not null,
    domain text,
    finding_version text not null,
    finding text not null,
    kind text not null,            -- gap / strength
    detail text,                   -- the sentence shown on the lead sheet
    evidence text,                 -- JSON: the signals behind it
    unique(company_number, finding_version, finding)
);
```

| Finding | Kind | Rule |
|---|---|---|
| `ads_without_conversion_tracking` | gap | Ads in last 30 days, no Ads conversion event |
| `ads_without_tag` | gap | Advertising, no Google Ads tag found |
| `no_consent_mode` | gap | Google Ads tag, no Consent Mode |
| `phone_leads_untracked` | gap | Click-to-call or phone shown, no call tracking |
| `no_remarketing` | gap | Advertising, no remarketing tag |
| `no_landing_pages` | gap | Advertising, no landing-style pages |
| `no_crm` | gap | Enquiry forms, no CRM or marketing automation |
| `search_demand_not_advertising` | gap | Phrase volume over a threshold, no ads in 90 days |
| `no_analytics` | gap | No analytics at all |
| `multi_channel_advertiser` | strength | Two or more ad pixels besides Google |
| `call_tracking_in_place` | strength | Call tracking found |
| `crm_in_place` | strength | CRM found |
| `agency_managed` | strength | Agency credit, or call tracking plus server-side tagging |

### W5: hand-off and outcomes (built)

- **A lead sheet in Drive, one row per company:**
  - financials
  - Google category and description
  - advertising status
  - gap segment
  - the gap findings as sentences
  - links to the website and listing
- **An outcome table your friend fills in:**

```sql
create table if not exists lead_outcomes (
    company_number text not null,
    sheet_version text not null,
    handed_over_at text not null,
    contacted_at text, replied integer, meeting integer, won integer,
    notes text,
    primary key (company_number, sheet_version)
);
```

These outcomes become the gold set for the ranking stage and the
precision@k claim. Ranking itself is a separate plan.

## Files

- **New:**
  - `scripts/web/{tech_rules,web_crawl,web_browser,web_detect,web_profile_policy,web_profile_eval,web_findings,web_handoff}.py`
  - `evals/web_profile/`
  - `tests/test_web_{crawl,detect,browser,findings,profile,handoff}.py`
- **Changed:**
  - `scripts/web/{search_providers,web_fetch,web_identity,web_market,web_population}.py`
  - `core/companies_house_sqlite.py`: all tables above in `SCHEMA_SQL`
  - `docs/DATABASE_SCHEMA.md`, `docs/WEB_STAGE.md`,
    `docs/WEB_STAGE_PLAN.md`
  - `AGENTS.md` repository map
  - `requirements-eval.txt` (playwright)

## Cost for about 1,000 companies

| Stage | Cost |
|---|---|
| W1 (Serper free searches) | $0 |
| W2 (own crawl) | $0 |
| W3 (OpenRouter) | about $3 |
| W4 (DataForSEO, friend's account) | about $3.50 |
| Findings, W5 | $0 |
| **Total** | **about $6.50** |

## Verification

- **W0:** tests that the two accounts' ledgers and caps stay separate, and
  that `check` shows both balances.
- **W1:** fixtures for trading-name extraction, PDF reading and blocked
  sites. Then the gold-set score against the pre-registered criteria, plus
  the 23 hand-found sites as an extra check.
- **W2:**
  - **Rules:** fixture HTML and `gtm.js` for each rule, positive and
    negative, including the preview's false positives.
  - **Re-detection:** a free run over the 20 test sites. The Ads tag must
    agree with Ads Transparency on 19 of 20 or better, with Rettie
    investigated.
  - **Hand check:** 10 sites against BuiltWith's free lookup.
  - **Browser:** a stubbed browser for the challenge, thin and robots.txt
    cases.
  - **Crawl pilot report:** pages per site, browser share, blocked share,
    storage and time per site.
- **W3:** gold-set scores per field, quote validity, rerun noise, and
  Langfuse traces.
- **W4:** pilot of 20, with the ledger matching the live balance after
  each run.
- **Findings and W5:** rule tests on fixture rows. Your friend reviews the
  first lead sheet for whether the talking points are right.
- **Everywhere:** `python -m pytest` after each step, and every table
  tested on a temporary database.

## Waiting on you

- Serper key, and the 25 blind labels in Drive (unblocks step 3)
- Whether your friend agrees to their DataForSEO key being used, and the
  cap
- Does your friend only manage PPC, or build websites too? That decides
  whether `site_first` companies are leads or rejects.
- Any trade or area specialism, which would become a free filter before
  W4

## Notes for whoever implements this

- **Read first:** `AGENTS.md`, `docs/WEB_STAGE.md` (definitions,
  pre-registered criteria, test findings), and this plan. Repo conventions
  apply: tests before or with the code, no emojis, and keep unrelated
  changes in a dirty worktree untouched.
- **Run tests with** `.venv-claude/Scripts/python.exe -m pytest`. Tests use
  hand-written or recorded fixtures only, never files under `data/` or
  `logs/`, which are gitignored and machine-local.
- **One step at a time.** Do one row of "Order of work", run the full test
  suite, and update the Status section of `docs/WEB_STAGE.md`. Then stop
  and report before starting the next step.
- **Spending rules (from `AGENTS.md`; not optional):**
  - Never run a command that spends money or a free allowance (Serper,
    DataForSEO, SerpApi, OpenRouter) without the user's explicit go for
    that specific run. State the expected cost first.
  - Do a `--cache-only` dry run first.
  - Every DataForSEO run states `--dataforseo-allowance`.
  - After each paid run, `python -m scripts.web.search_providers check`
    must show the ledger matching the live balance.
  - Runs on the friend's account also need the friend's agreement to the
    cap.
- **Secrets:** keys live only in `.env`. Never print them, log them, or
  put them in a cache key, URL or test.
- **Reuse, don't rebuild:**
  - `SearchClient` (cache, ledger, caps) in `scripts/web/search_providers.py`
  - `Fetcher` and `parse_html` in `scripts/web/web_fetch.py`
  - `check_site`, `_legal_links`, `names_similar` and `number_found` in
    `scripts/web/web_identity.py`
  - the checkpoint helpers in `scripts/web/web_population.py`
  - `research` and `summary_rows` in `scripts/web/web_market.py`
  - `call_model` and the Langfuse replay in
    `scripts/screen/search_screen_eval.py`
  - `utc_now`, `json_text` and `_signal_columns` in
    `core/companies_house_sqlite.py`
- **Model runs (W3):** follow
  `.claude/skills/langfuse-eval-discipline/SKILL.md`: one trace per case,
  and an fsync'd checkpoint per case.
- **Known gotchas:**
  - DataForSEO bills "40102 No Search Results" answers. They're handled as
    empty results and must stay ledgered.
  - Serper reports credits per call in the response's `credits` field;
    check what a Maps search really costs on the first pilot.
  - DataForSEO's live-results `location_coordinate` radius is in
    millimetres.
  - Google Tag Manager's own code mentions Wix and WooCommerce, so platform
    rules must be page-only.
  - The tag-scan preview (`logs/web/test-tag-scan.json`) used rough rules.
    Treat it as a reference result, not as rules to copy.
- **Reference data from the 2026-10-02 test:**
  - `logs/web/test-companies.json`: 23 companies with hand-found,
    company-number-checked domains
  - `logs/web/test-seeds.json`: hand-drafted search phrases
  - `logs/web/research-report.json`: the DataForSEO results
  - the "DataForSEO research - 23 test companies" sheet in Drive
- **The shell is Windows:** PowerShell 5.1 by default, with Git Bash
  available. Write multi-line scripts to a scratch file rather than an
  inline heredoc.
- **Commit only when the user asks.**

## Implementation status (2026-10-02)

Built, with tests, and no paid call made: everything above. Differences from
the plan as drafted:

- `company_google_listing` gained `is_claimed` and `category_ids`.
- The resolver version is `identity-v2-trading-names`; there is a new
  `blocked` tier (a site that refused automated requests), below `ambiguous`.
- W4's `bulk_traffic` requests organic and map traffic only; the paid-keyword
  lookup was removed. `web_population research` is kept as the harness for a
  hand-supplied list, and `market` is the production command.
- The gap segment has one added value, `low_demand` (not advertising and little
  search demand), defined in `scripts/web/web_findings.py`.
- Playwright is optional (`requirements-eval.txt`); `playwright install
  chromium` has not been run.
- Not done: a Google Places adapter (only if Serper's free credits run out),
  and registering the W3 prompt in Langfuse (per-company traces and the
  checkpoint are done; `log-langfuse` is untested against a live Langfuse).
- Detection speed: about 1.4 seconds per megabyte of HTML, single-threaded
  (roughly an hour and a half for 1,000 sites). Parallelise `detect` if that matters.

Next runs, each needing a go: a Serper key and the 25 blind labels, then
`identity --gold`; then `identity --limit N`; `crawl`; `detect`;
`web_profile_eval run`; `market` (with a stated cap and account); `findings`;
`web_handoff build`.
