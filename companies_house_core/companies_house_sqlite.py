#!/usr/bin/env python3
"""Store Companies House extraction outputs in a local SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
create table if not exists companies (
    company_number text primary key,
    company_name text,
    company_status text,
    company_type text,
    date_of_creation text,
    source_mode text,
    profile_payload text not null,
    updated_at text not null
);

create table if not exists filings (
    transaction_id text primary key,
    company_number text not null,
    filing_date text,
    category text,
    type text,
    description text,
    action_date text,
    pages integer,
    filing_payload text not null,
    foreign key(company_number) references companies(company_number)
);

create table if not exists documents (
    document_id text primary key,
    transaction_id text,
    company_number text not null,
    metadata_url text,
    xhtml_url text,
    pdf_url text,
    downloaded_xhtml_path text,
    downloaded_pdf_path text,
    metadata_payload text,
    foreign key(transaction_id) references filings(transaction_id),
    foreign key(company_number) references companies(company_number)
);

create table if not exists financial_period_summaries (
    id integer primary key autoincrement,
    company_number text not null,
    document_id text,
    period_type text not null,
    financial_year integer,
    turnover integer,
    gross_profit integer,
    operating_result integer,
    profit_after_tax integer,
    cash integer,
    net_assets integer,
    employees integer,
    derived_payload text,
    raw_payload text not null,
    data_source text not null default 'xhtml',
    currency_code text,
    currency_source text,
    period_end_on text,
    currency_validation_status text not null default 'unknown',
    turnover_reported_value text,
    gross_profit_reported_value text,
    operating_result_reported_value text,
    profit_after_tax_reported_value text,
    cash_reported_value text,
    net_assets_reported_value text,
    unique(company_number, document_id, period_type),
    foreign key(company_number) references companies(company_number),
    foreign key(document_id) references documents(document_id)
);

create table if not exists narrative_runs (
    id integer primary key autoincrement,
    document_id text,
    company_number text,
    pdf_path text,
    text_source text,
    ocr_requested integer not null default 0,
    ocr_used integer not null default 0,
    ocr_engine_used text,
    text_quality_payload text not null,
    raw_payload text not null,
    created_at text not null default current_timestamp
);

create table if not exists narrative_sections (
    id integer primary key autoincrement,
    narrative_run_id integer not null,
    section_key text not null,
    section_title text,
    page_number integer,
    section_text text,
    section_payload text not null,
    foreign key(narrative_run_id) references narrative_runs(id)
);

create table if not exists performance_statements (
    id integer primary key autoincrement,
    narrative_run_id integer not null,
    page_number integer,
    statement_text text not null,
    foreign key(narrative_run_id) references narrative_runs(id)
);

-- The whole filed document as text, one row per (document, source, model).
-- source 'xhtml' = filed_report_text() over the filed XHTML; 'vlm_transcription'
-- = scripts/pdf_vision_extraction/companies_house_pdf_transcribe.py reading a scanned PDF page
-- by page with a vision model. model is '' (not null) for xhtml rows so the
-- unique key still applies. filed_report_text is the document minus the
-- auditor's report -- what the business-profile stage reads as its
-- `filed_report` section.
create table if not exists document_texts (
    id integer primary key autoincrement,
    company_number text not null,
    document_id text not null,
    source text not null,
    model text not null default '',
    prompt_version text,
    pdf_path text,
    pdf_sha256 text,
    status text not null,
    page_count integer,
    transcribed_pages integer,
    illegible_pages text not null default '[]',
    failed_pages text not null default '[]',
    raw_text text not null,
    filed_report_text text not null,
    page_usage_payload text not null default '[]',
    usage_payload text not null default '{}',
    pricing_payload text not null default '{}',
    cost_usd real,
    cost_gbp real,
    cost_method text not null default 'unavailable',
    created_at text not null,
    updated_at text not null,
    unique(document_id, source, model),
    foreign key(company_number) references companies(company_number),
    foreign key(document_id) references documents(document_id)
);
create index if not exists idx_document_texts_company_number on document_texts(company_number);

-- A run records exactly which models saw the document, while the metric rows
-- retain the displayed source value and the evidence needed to audit a final
-- choice.
create table if not exists vlm_financial_extraction_runs (
    id integer primary key autoincrement,
    company_number text,
    document_id text,
    pdf_path text not null,
    locator_model text not null,
    vision_model text not null,
    rationalisation_model text not null,
    status text not null,
    pages_scanned_payload text not null,
    candidate_pages_payload text not null,
    raw_extraction_payload text not null,
    rationalisation_payload text not null,
    usage_payload text not null,
    pricing_payload text not null,
    cost_usd real,
    cost_gbp real,
    cost_method text not null,
    created_at text not null default current_timestamp,
    foreign key(company_number) references companies(company_number),
    foreign key(document_id) references documents(document_id)
);

create table if not exists vlm_financial_metrics (
    id integer primary key autoincrement,
    extraction_run_id integer not null,
    company_number text,
    period_type text not null,
    financial_year integer,
    metric_name text not null,
    value_pence integer,
    value_count integer,
    displayed_value text,
    unit text,
    currency_code text,
    scale_multiplier integer,
    reported_value text,
    source_page integer,
    source_label text,
    evidence_text text,
    confidence real,
    vision_model text not null,
    rationalisation_model text not null,
    validation_payload text not null,
    unique(extraction_run_id, period_type, metric_name),
    foreign key(extraction_run_id) references vlm_financial_extraction_runs(id)
);

-- Published source observations are immutable.  The normalised rate is GBP
-- per one unit of the source currency, not the inverse as published by BoE.
create table if not exists fx_rates (
    id integer primary key autoincrement,
    source_currency_code text not null,
    target_currency_code text not null default 'GBP',
    observation_on text not null,
    raw_published_rate text not null,
    gbp_per_source_unit text not null,
    bank_series_id text not null,
    retrieved_at text not null,
    source_url text not null,
    payload_hash text not null,
    unique(source_currency_code, target_currency_code, observation_on, bank_series_id, payload_hash)
);

create table if not exists financial_period_conversions (
    id integer primary key autoincrement,
    financial_summary_id integer not null unique,
    fx_rate_id integer,
    conversion_status text not null,
    conversion_basis text not null,
    converted_at text not null,
    turnover_gbp_pence integer,
    gross_profit_gbp_pence integer,
    operating_result_gbp_pence integer,
    profit_after_tax_gbp_pence integer,
    cash_gbp_pence integer,
    net_assets_gbp_pence integer,
    foreign key(financial_summary_id) references financial_period_summaries(id),
    foreign key(fx_rate_id) references fx_rates(id)
);

create table if not exists sic_groups (
    sic_code text primary key,
    sic_label text not null,
    sic_group text not null,
    model_version text not null,
    updated_at text not null
);

create table if not exists company_signals (
    id integer primary key autoincrement,
    company_number text not null,
    signal_key text not null,
    signal_value_type text not null,
    signal_bool integer,
    signal_int integer,
    signal_real real,
    signal_text text,
    source_scope text not null default 'api',
    created_at text not null,
    updated_at text not null,
    unique(company_number, signal_key),
    foreign key(company_number) references companies(company_number)
);

create table if not exists company_search_screen (
    id integer primary key autoincrement,
    company_number text not null,
    prompt_version text not null,
    model text not null,
    input_kind text not null,
    answer text,
    passes integer not null,
    quote text,
    quote_valid integer,
    reason text,
    problem text,
    document_id text,
    text_chars integer,
    prompt_tokens integer,
    completion_tokens integer,
    screened_at text not null,
    unique(company_number, prompt_version, model, input_kind),
    foreign key(company_number) references companies(company_number)
);

create index if not exists idx_company_search_screen_passes on company_search_screen(prompt_version, passes);

-- Web stage W1 (docs/WEB_STAGE.md): each checked candidate site per company and
-- resolver version. role 'main' is the chosen site when its tier is verified or
-- probable; a company with nothing found has one row with domain null and tier
-- 'none'. evidence is the check's JSON (number, name and postcode found, pages).
create table if not exists company_web_identity (
    id integer primary key autoincrement,
    company_number text not null,
    resolver_version text not null,
    domain text,
    role text not null,              -- main / candidate / none
    tier text not null,              -- verified / probable / ambiguous / none
    sources text,                    -- JSON list: maps, organic, guess, number_search, redirect
    final_url text,
    evidence text,
    resolved_at text not null,
    foreign key(company_number) references companies(company_number)
);

create index if not exists idx_company_web_identity_company on company_web_identity(company_number, resolver_version);

-- Each company's newest identity: the rows of whichever resolver version wrote last. W1 ran under
-- more than one version (maps-first, places-first, the settle step), so later steps read this view.
create view if not exists company_web_identity_current as
select i.* from company_web_identity i
where i.resolver_version = (
    select j.resolver_version from company_web_identity j
    where j.company_number = i.company_number order by j.id desc limit 1);

-- The Google Maps listing matched to a company in W1, with Google's own
-- category. match: name+postcode, name+domain (its website is the chosen
-- site) or name_only.
create table if not exists company_google_listing (
    id integer primary key autoincrement,
    company_number text not null,
    resolver_version text not null,
    title text,
    category text,
    categories text,                 -- JSON list, primary first
    rating real,
    rating_count integer,
    address text,
    postcode text,
    phone text,
    website text,
    domain text,
    latitude real,
    longitude real,
    cid text,
    place_id text,
    match text not null,
    source text not null,            -- serper_maps / serper_places
    found_at text not null,
    is_claimed integer,
    category_ids text,               -- JSON list of Google's category ids
    unique(company_number, resolver_version),
    foreign key(company_number) references companies(company_number)
);

-- Web stage (docs/WEB_STAGE_PLAN.md): names a company sells under, found by
-- scripts/website_analysis/web_trading_names.py. source is filing / website / maps_listing.
create table if not exists company_trading_names (
    id integer primary key autoincrement,
    company_number text not null,
    name text not null,
    source text not null,
    evidence text,                   -- the sentence or listing it came from
    found_at text not null,
    unique(company_number, name),
    foreign key(company_number) references companies(company_number)
);

-- W2: one row per fetched page of a website, keyed by registrable domain (two
-- companies can share a site). The raw page lives in the gzipped page cache
-- (data/raw/website-page-snapshots/<host>/<cache_key>.json.gz); this row holds what was
-- extracted from it.
create table if not exists web_pages (
    id integer primary key autoincrement,
    domain text not null,
    url text not null,
    final_url text,
    page_kind text not null,         -- home / about / service / location / landing / product / blog / contact /
                                     -- pricing / privacy / terms / sitemap / gtm_container / other
    crawl_version text not null,
    fetched_with text not null,      -- http / browser
    status_code integer,
    fetch_error text,                -- 'blocked (403)', 'challenge page', 'robots.txt', 'not html', timeout
    cache_key text,
    fetched_at text,
    html_bytes integer,
    in_navigation integer,           -- linked from the homepage menu (landing pages usually are not)
    script_hosts text,               -- JSON: third-party script hosts (browser: hosts actually requested)
    title text,
    meta_description text,
    h1 text,
    word_count integer,
    lang text,
    canonical_url text,
    noindex integer,
    has_viewport integer,
    schema_types text,               -- JSON: LocalBusiness, Product, AggregateRating, FAQPage...
    form_count integer,
    form_providers text,             -- JSON: hubspot, gravity_forms, contact_form_7, typeform...
    tel_link_count integer,
    phone_numbers text,              -- JSON: distinct UK numbers shown
    cta_phrases text,                -- JSON: 'get a quote', 'book now', 'add to basket'...
    price_mentions integer,
    company_number_found integer,
    copyright_year integer,
    unique(domain, url, crawl_version)
);

create index if not exists idx_web_pages_domain on web_pages(domain, crawl_version);

-- W2: one row per technology found per domain (scripts/website_analysis/tech_rules.py).
-- account_ids holds the tag and account identifiers (AW-..., G-..., GTM-...,
-- a HubSpot portal id): shared ids mean a shared owner or agency.
create table if not exists web_technologies (
    id integer primary key autoincrement,
    domain text not null,
    rule_version text not null,
    technology text not null,
    category text not null,          -- tag_manager / analytics / search_ads / social_ads / consent / crm_automation /
                                     -- email / call_tracking / optimisation / landing_pages / chat / booking /
                                     -- ecommerce / cms / reviews
    found_in text not null,          -- page / gtm / both
    account_ids text,                -- JSON list
    evidence text,
    evidence_url text,
    detected_at text not null,
    unique(domain, rule_version, technology)
);

create index if not exists idx_web_technologies_domain on web_technologies(domain, rule_version);

-- W2: one summary row per domain, derived from web_pages and web_technologies
-- (free to recompute under a new rule_version).
create table if not exists web_sites (
    domain text not null,
    crawl_version text not null,
    rule_version text not null,
    crawl_status text not null,      -- ok / blocked / unreachable / parked / thin
    pages_fetched integer,
    pages_by_browser integer,
    https integer,
    mobile_ready integer,
    platform text,
    copyright_year integer,
    sitemap_url_count integer,
    sitemap_newest_lastmod text,
    product_url_count integer,
    service_page_count integer,
    location_page_count integer,
    landing_page_count integer,
    has_blog integer,
    blog_latest_date text,
    schema_types text,
    gtm_ids text,
    ga4_ids text,
    google_ads_ids text,
    has_google_ads_tag integer,
    has_ads_conversion_event integer,
    has_ads_remarketing integer,
    has_consent_mode integer,
    consent_vendor text,
    server_side_tagging integer,
    social_pixels text,
    has_microsoft_ads integer,
    crm_vendors text,
    email_vendors text,
    call_tracking_vendor text,
    optimisation_vendors text,
    landing_page_builder text,
    has_contact_form integer,
    has_click_to_call integer,
    has_booking text,
    has_checkout integer,
    has_live_chat text,
    review_widget text,
    agency_credit text,
    agency_credit_url text,
    summarised_at text not null,
    primary key (domain, crawl_version, rule_version)
);

-- W3: the site profile (scripts/website_analysis/web_profile_policy.py), one row per company,
-- profile version and model. Every label except the summary carries a
-- verbatim quote from the page text the model was shown, and whether it
-- validated.
create table if not exists company_web_profile (
    company_number text not null,
    profile_version text not null,
    model text not null,
    domain text,
    summary text,
    products_services text,          -- JSON list
    customer_type text,
    customer_type_quote text,
    customer_type_quote_valid integer,
    customer_type_quote_match text,  -- exact / table_row / fuzzy / joined; null when not found or no quote
    conversion_action text,          -- buy_online / book / call / enquiry_form / visit / unclear (v1-v2 also quote_form, tender)
    conversion_action_quote text,
    conversion_action_quote_valid integer,
    conversion_action_quote_match text,
    geography text,                  -- local / regional / national / international / unclear
    main_town text,
    geography_quote text,
    geography_quote_valid integer,
    geography_quote_match text,
    wins_by_tender text,             -- yes / no / unclear: much of the work comes through tenders or frameworks
    wins_by_tender_quote text,
    wins_by_tender_quote_valid integer,
    wins_by_tender_quote_match text,
    urgency text,                    -- emergency / planned / considered / unclear
    ticket_band text,
    channel_fit text,                -- search / social / both / unclear
    google_category text,
    category_source text,            -- google_listing / model_assigned
    seed_keywords text,              -- JSON list
    problem text,                    -- why the row is incomplete (unparseable, no site text, request failed)
    attempts integer,                -- 2 when a quote was not found and the model was asked once more
    first_attempt text,              -- JSON: the first answer's quoted fields, kept when a retry replaced it
    prompt_tokens integer,
    completion_tokens integer,
    profiled_at text not null,
    primary key (company_number, profile_version, model)
);

-- W4: search volume and cost per click for each company's seed phrases.
create table if not exists company_keyword_market (
    company_number text not null,
    market_version text not null,
    keyword text not null,
    location_code integer not null,
    search_volume integer,
    cpc_usd real,
    competition text,
    competition_index integer,
    top_of_page_bid_high_usd real,
    fetched_at text not null,
    primary key (company_number, market_version, keyword)
);

-- W4: advertising and demand per company, plus the rollup of its findings.
create table if not exists company_market (
    company_number text not null,
    market_version text not null,
    domain text,
    ads_seen integer,
    advertising_now integer,
    ads_first_shown text,
    ads_last_shown text,
    advertiser_verified integer,
    advertiser_name text,
    phrase_search_volume integer,
    weighted_cpc_usd real,
    monthly_click_value_usd real,
    organic_etv real,
    organic_keywords integer,
    local_pack_etv real,
    gap_count integer,
    strength_count integer,
    setup_level text,                -- none / basic / partial / sophisticated
    gap_segment text,                -- greenfield / advertising_poorly / advertising_well / site_first
    assessed_at text not null,
    primary key (company_number, market_version)
);

-- W4: one live results check from a point (optional, short list only).
create table if not exists serp_observations (
    company_number text not null,
    market_version text not null,
    keyword text not null,
    latitude real,
    longitude real,
    observed_at text not null,
    in_ads integer,
    organic_position integer,
    in_local_pack integer,
    advertisers text,                -- JSON list of domains seen in the ads
    primary key (company_number, market_version, keyword)
);

-- Findings: the talking points (scripts/website_analysis/web_findings.py). A gap is a pitch
-- point, a strength is something the company already does.
create table if not exists company_setup_findings (
    id integer primary key autoincrement,
    company_number text not null,
    domain text,
    finding_version text not null,
    finding text not null,
    kind text not null,              -- gap / strength
    detail text,                     -- the sentence shown on the lead sheet
    evidence text,                   -- JSON: the signals behind it
    unique(company_number, finding_version, finding)
);

-- W5: what happened to each lead handed over; filled in from the friend's feedback.
create table if not exists lead_outcomes (
    company_number text not null,
    sheet_version text not null,
    handed_over_at text not null,
    contacted_at text,
    replied integer,
    meeting integer,
    won integer,
    notes text,
    primary key (company_number, sheet_version)
);

-- One row per company and financial year, chosen from financial_period_summaries.
-- That table keeps one row per (document, current or previous), so a year that
-- appears as `current` in one filing and `previous` in the next, or in an
-- amendment, or from more than one source, has several rows. This view picks
-- one: the row with the most of turnover and profit after tax filled, then the
-- filing's own (`current`) reading over a later filing's comparative, then the
-- newest row. `source` says where the figures came from.
create view if not exists company_financial_history as
with ranked as (
    select f.*,
           row_number() over (
               partition by f.company_number, f.financial_year
               order by ((f.turnover is not null) + (f.profit_after_tax is not null)) desc,
                        (f.period_type = 'current') desc, f.id desc
           ) as rn,
           count(*) over (partition by f.company_number, f.financial_year) as n_rows
    from financial_period_summaries f
    where f.financial_year is not null
)
select company_number, financial_year, period_end_on, turnover, profit_after_tax, gross_profit,
       operating_result, cash, net_assets, employees, currency_code,
       case when json_valid(derived_payload) and json_extract(derived_payload, '$.text_recovery') is not null
            then 'xhtml+text' else data_source end as source,
       document_id, period_type, comparative_overlap_status, n_rows,
       case when turnover is not null and profit_after_tax is not null then 'ok'
            when turnover is not null or profit_after_tax is not null then 'partial'
            else 'missing' end as status
from ranked where rn = 1;

create table if not exists company_profiles (
    id integer primary key autoincrement,
    company_number text not null,
    financial_year integer,
    narrative_run_id integer,

    business_description text,

    demand_model text,
    demand_model_confidence real,
    demand_model_quote text,
    demand_model_section text,
    demand_model_reason text,

    customer_type text,
    customer_type_confidence real,
    customer_type_quote text,
    customer_type_section text,
    customer_type_reason text,

    delivery_model text,
    delivery_model_confidence real,
    delivery_model_quote text,
    delivery_model_section text,
    delivery_model_reason text,

    geography_served text,
    geography_served_confidence real,
    geography_served_quote text,
    geography_served_section text,
    geography_served_reason text,

    trading_status_confirmed text,
    trading_status_confirmed_confidence real,
    trading_status_confirmed_quote text,
    trading_status_confirmed_section text,
    trading_status_confirmed_reason text,

    sic_agreement text,
    sic_agreement_reason text,
    sic_agreement_quote text,
    sic_agreement_section text,

    extraction_model text not null,
    prompt_version text not null,
    generated_at text not null,
    unique(company_number, financial_year),
    foreign key(company_number) references companies(company_number),
    foreign key(narrative_run_id) references narrative_runs(id)
);

create index if not exists idx_filings_company_number on filings(company_number);
create index if not exists idx_documents_company_number on documents(company_number);
create index if not exists idx_financial_company_number on financial_period_summaries(company_number);
create index if not exists idx_narrative_company_number on narrative_runs(company_number);
create index if not exists idx_vlm_financial_runs_company_number on vlm_financial_extraction_runs(company_number);
create index if not exists idx_vlm_financial_metrics_run_id on vlm_financial_metrics(extraction_run_id);
create index if not exists idx_company_signals_company_number on company_signals(company_number);
create index if not exists idx_company_signals_key on company_signals(signal_key);
create index if not exists idx_company_profiles_company_number on company_profiles(company_number);

"""

SIC_GROUP_MODEL_VERSION = "sic1_grouping_v1"

SIC_GROUPS: list[dict[str, Any]] = [
    {
        "sic_group": "ecommerce_online_retail",
        "sic_label": "E-commerce / online retail",
        "codes": ["47910", "47990"],
    },
    {
        "sic_group": "banking_lending_credit",
        "sic_label": "Banking / lending / credit",
        "codes": ["64110", "64191", "64192", "64999"],
    },
    {
        "sic_group": "insurance",
        "sic_label": "Insurance",
        "codes": ["65110", "65120", "65201", "65202"],
    },
    {
        "sic_group": "financial_services_brokers",
        "sic_label": "Financial services / brokers",
        "codes": ["66110", "66120", "66190", "66210", "66220"],
    },
    {
        "sic_group": "legal_services",
        "sic_label": "Legal services",
        "codes": ["69101", "69102"],
    },
    {
        "sic_group": "dental",
        "sic_label": "Dental",
        "codes": ["86230"],
    },
    {
        "sic_group": "general_medical",
        "sic_label": "General medical",
        "codes": ["86210"],
    },
    {
        "sic_group": "other_health",
        "sic_label": "Other health services",
        "codes": ["86900"],
    },
    {
        "sic_group": "education_tutoring_schools",
        "sic_label": "Education / tutoring / schools",
        "codes": ["85100", "85200", "85310", "85320"],
    },
    {
        "sic_group": "property_development",
        "sic_label": "Property development",
        "codes": ["41100", "41201", "41202"],
    },
    {
        "sic_group": "estate_property_management",
        "sic_label": "Estate agents / property management",
        "codes": ["68100", "68201", "68209", "68310", "68320"],
    },
    {
        "sic_group": "car_dealers",
        "sic_label": "Car dealers",
        "codes": ["45111", "45112", "45190"],
    },
    {
        "sic_group": "restaurants_catering",
        "sic_label": "Restaurants / catering",
        "codes": ["56101", "56102", "56103", "56210", "56290"],
    },
    {
        "sic_group": "hotels_bnb",
        "sic_label": "Hotels / B&Bs",
        "codes": ["55100", "55201", "55202", "55209"],
    },
    {
        "sic_group": "personal_care_wellness",
        "sic_label": "Personal care / beauty / wellness",
        "codes": ["96010", "96020", "96030", "96040", "96090"],
    },
    {
        "sic_group": "specialist_construction_trades",
        "sic_label": "Specialist construction / trades",
        "codes": ["43210", "43220", "43290", "43310", "43320", "43341", "43342", "43390"],
    },
    {
        "sic_group": "software_it_consultancy",
        "sic_label": "Software / IT consultancy",
        "codes": ["62011", "62012", "62020", "62090"],
    },
    {
        "sic_group": "management_consultancy_pr",
        "sic_label": "Management consultancy / PR",
        "codes": ["70210", "70221", "70229"],
    },
    {
        "sic_group": "advertising_market_research",
        "sic_label": "Advertising / market research",
        "codes": ["73110", "73200"],
    },
    {
        "sic_group": "design_photography",
        "sic_label": "Design / photography",
        "codes": ["74100", "74201", "74202", "74209"],
    },
    {
        "sic_group": "accountancy_bookkeeping_audit",
        "sic_label": "Accountancy / bookkeeping / audit",
        "codes": ["69201", "69202", "69203"],
    },
    {
        "sic_group": "architecture_engineering",
        "sic_label": "Architecture / engineering",
        "codes": ["71111", "71112", "71121", "71122"],
    },
    {
        "sic_group": "research_biotech",
        "sic_label": "R&D / biotech",
        "codes": ["72110", "72190", "72200"],
    },
    {
        "sic_group": "business_support_services",
        "sic_label": "Business support services",
        "codes": ["82110", "82190", "82990"],
    },
    {
        "sic_group": "educational_support",
        "sic_label": "Educational support",
        "codes": ["85600"],
    },
    {
        "sic_group": "arts_entertainment",
        "sic_label": "Arts / entertainment",
        "codes": ["90010", "90020", "90030", "90040"],
    },
    {
        "sic_group": "sport_fitness_gyms",
        "sic_label": "Sport / fitness / gyms",
        "codes": ["93110", "93120", "93130", "93190"],
    },
    {
        "sic_group": "theme_parks_amusement",
        "sic_label": "Theme parks / amusement",
        "codes": ["93210", "93290"],
    },
    {
        "sic_group": "specialist_retail",
        "sic_label": "Specialist retail",
        "codes": ["47411", "47710", "47730", "47740", "47750"],
    },
    {
        "sic_group": "transport_taxi_logistics",
        "sic_label": "Transport / taxi / logistics",
        "codes": ["49100", "49311", "49319", "49320"],
    },
]

DEFAULT_SIC_GROUPS: list[dict[str, Any]] = [
    {
        "sic_code": sic_code,
        "sic_label": group["sic_label"],
        "sic_group": group["sic_group"],
        "model_version": SIC_GROUP_MODEL_VERSION,
    }
    for group in SIC_GROUPS
    for sic_code in group["codes"]
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    drop_ppc_ratio_and_estimates(conn)
    drop_website_investigations(conn)
    ensure_financial_period_summary_columns(conn)
    ensure_financial_year_columns(conn)
    ensure_vlm_financial_metric_columns(conn)
    ensure_currency_columns(conn)
    ensure_comparative_overlap_columns(conn)
    ensure_company_sic_columns(conn)
    ensure_company_profile_columns(conn)
    ensure_google_listing_columns(conn)
    ensure_web_profile_columns(conn)
    populate_sic_groups(conn)
    conn.commit()


def ensure_company_sic_columns(conn: sqlite3.Connection) -> None:
    """Lift the API's sic_codes out of companies.profile_payload into their
    own columns. The codes were only ever reachable by JSON-parsing the
    payload, or via leads.sic_1 -- which carries the code and its label in
    one string ("45111 - Sale of new cars...") and comes from a bulk
    snapshot rather than the live API. sic_codes holds the full JSON array;
    sic_code_primary is the first entry, which is what joins to
    sic_groups."""
    columns = {row[1] for row in conn.execute("pragma table_info(companies)")}
    for name, definition in (("sic_codes", "text"), ("sic_code_primary", "text")):
        if name not in columns:
            conn.execute(f"alter table companies add column {name} {definition}")
    conn.execute("create index if not exists idx_companies_sic_code_primary on companies(sic_code_primary)")

    pending = conn.execute(
        "select company_number, profile_payload from companies where sic_codes is null"
    ).fetchall()
    for company_number, payload in pending:
        try:
            codes = (json.loads(payload) or {}).get("sic_codes") or []
        except (TypeError, ValueError):
            codes = []
        conn.execute(
            "update companies set sic_codes = ?, sic_code_primary = ? where company_number = ?",
            (json_text(codes), codes[0] if codes else None, company_number),
        )


def drop_ppc_ratio_and_estimates(conn: sqlite3.Connection) -> None:
    """The SIC-ratio PPC estimate (turnover x a flat per-SIC percentage) was
    dropped: it conflated acquisition volume, affordability, and channel fit
    into one number, and produced estimates as absurd as a football club
    spending 2% of its turnover on member-acquisition PPC. sic_groups
    replaces it for the SIC label/group lookup alone, with no ratio.
    2,323 estimates and the 103 old ratio rules were exported to
    data/dropped-tables/ before this ran."""
    conn.execute("drop table if exists ppc_company_estimates")
    conn.execute("drop table if exists ppc_ratio_rules")


def drop_website_investigations(conn: sqlite3.Connection) -> None:
    """The June browser pilot (50 companies, source_label
    ppc_pilot_40k_60k_2026_06_16) and its keyword-count signals, PPC-fit score
    and estimated monthly PPC spend were superseded by the web stage
    (company_web_identity, web_sites, web_pages, web_technologies)."""
    conn.execute("drop view if exists website_investigation_metric_view")
    conn.execute("drop table if exists website_signals")
    conn.execute("drop table if exists website_investigations")


def ensure_google_listing_columns(conn: sqlite3.Connection) -> None:
    """Add is_claimed and category_ids to a company_google_listing created before
    the web-stage plan wanted them (`create table if not exists` never revisits
    an existing table)."""
    columns = {row[1] for row in conn.execute("pragma table_info(company_google_listing)")}
    for name, definition in (("is_claimed", "integer"), ("category_ids", "text")):
        if columns and name not in columns:
            conn.execute(f"alter table company_google_listing add column {name} {definition}")


def ensure_web_profile_columns(conn: sqlite3.Connection) -> None:
    """Add the quote-match, retry (v2) and tender-flag (v3) columns to a
    company_web_profile created before them."""
    columns = {row[1] for row in conn.execute("pragma table_info(company_web_profile)")}
    for name in ("customer_type_quote_match", "conversion_action_quote_match", "geography_quote_match",
                 "wins_by_tender", "wins_by_tender_quote", "wins_by_tender_quote_valid", "wins_by_tender_quote_match",
                 "attempts", "first_attempt"):
        if columns and name not in columns:
            conn.execute(f"alter table company_web_profile add column {name} "
                         + ("integer" if name in ("attempts", "wins_by_tender_quote_valid") else "text"))


def ensure_company_profile_columns(conn: sqlite3.Connection) -> None:
    """Add the v6 evidence columns to an existing database.

    Prompt v6 made each classification field return a `reason` alongside its
    quote, and gave sic_agreement a quote and section of its own -- it had
    been the one field with no verbatim-quote guard. `create table if not
    exists` never revisits an existing table, so without this an upgraded
    database silently drops the new columns on every write."""
    columns = {row[1] for row in conn.execute("pragma table_info(company_profiles)")}
    wanted = [f"{field}_reason" for field in COMPANY_PROFILE_FIELDS]
    wanted += ["sic_agreement_quote", "sic_agreement_section"]
    for name in wanted:
        if name not in columns:
            conn.execute(f"alter table company_profiles add column {name} text")


def ensure_financial_period_summary_columns(conn: sqlite3.Connection) -> None:
    """Mark canonical summaries by source without disturbing legacy XHTML rows."""
    columns = {row[1] for row in conn.execute("pragma table_info(financial_period_summaries)")}
    if "data_source" not in columns:
        conn.execute(
            "alter table financial_period_summaries add column data_source text not null default 'xhtml'"
        )


def ensure_financial_year_columns(conn: sqlite3.Connection) -> None:
    """Apply the additive reporting-year migration to existing databases."""
    for table_name in (
        "financial_period_summaries",
        "vlm_financial_metrics",
    ):
        columns = {row[1] for row in conn.execute(f"pragma table_info({table_name})")}
        if "financial_year" not in columns:
            conn.execute(f"alter table {table_name} add column financial_year integer")


def ensure_vlm_financial_metric_columns(conn: sqlite3.Connection) -> None:
    """Apply the small additive migration needed by existing VLM result tables."""
    columns = {row[1] for row in conn.execute("pragma table_info(vlm_financial_metrics)")}
    if "company_number" not in columns:
        conn.execute("alter table vlm_financial_metrics add column company_number text")
    conn.execute(
        """
        update vlm_financial_metrics
        set company_number = (
            select company_number
            from vlm_financial_extraction_runs
            where vlm_financial_extraction_runs.id = vlm_financial_metrics.extraction_run_id
        )
        where company_number is null
        """
    )
    conn.execute("create index if not exists idx_vlm_financial_metrics_company_number on vlm_financial_metrics(company_number)")


def ensure_currency_columns(conn: sqlite3.Connection) -> None:
    """Add currency provenance without rewriting historical reported figures."""
    summary_columns = {row[1] for row in conn.execute("pragma table_info(financial_period_summaries)")}
    for name, definition in (
        ("currency_code", "text"), ("currency_source", "text"),
        ("period_end_on", "text"), ("currency_validation_status", "text not null default 'unknown'"),
        *[(f"{metric}_reported_value", "text") for metric in (
            "turnover", "gross_profit", "operating_result", "profit_after_tax", "cash", "net_assets"
        )],
    ):
        if name not in summary_columns:
            conn.execute(f"alter table financial_period_summaries add column {name} {definition}")
    # Old summary rows had no retained unit evidence.  Surface the assumption
    # explicitly so a future reprocess can replace it.
    conn.execute(
        """update financial_period_summaries set currency_code='GBP',
           currency_source='legacy_default', currency_validation_status='legacy_default'
           where currency_code is null and currency_source is null"""
    )
    metric_columns = {row[1] for row in conn.execute("pragma table_info(vlm_financial_metrics)")}
    for name, definition in (("currency_code", "text"), ("scale_multiplier", "integer"), ("reported_value", "text")):
        if name not in metric_columns:
            conn.execute(f"alter table vlm_financial_metrics add column {name} {definition}")


COMPARATIVE_OVERLAP_METRICS = (
    "turnover", "gross_profit", "operating_result", "profit_after_tax", "cash", "net_assets", "employees",
)


def ensure_comparative_overlap_columns(conn: sqlite3.Connection) -> None:
    """Free accuracy signal: a filing's "previous" period and the adjacent
    older filing's "current" period describe the same accounting period, so
    they should report the same figures. Recording whether they agree costs
    nothing to collect during a history backfill and catches extraction
    errors (and genuine prior-year restatements) without any gold-set
    labelling."""
    columns = {row[1] for row in conn.execute("pragma table_info(financial_period_summaries)")}
    for name, definition in (
        ("comparative_overlap_status", "text"),
        ("comparative_overlap_payload", "text"),
    ):
        if name not in columns:
            conn.execute(f"alter table financial_period_summaries add column {name} {definition}")


def compute_comparative_overlap(conn: sqlite3.Connection, company_number: str, document_id: str) -> list[str]:
    """After inserting a filing's periods, check each of them against the
    opposite-role period of any other document covering the same
    period_end_on for this company: this document's "previous" against an
    older filing's "current", and this document's "current" against a
    newer filing's "previous" (the usual case when backdating history —
    the newer filing was already inserted and is waiting for this one to
    turn up). The agreement status is always written on the "previous"-role
    row of the matched pair. Returns the statuses found (0, 1, or 2 —
    a filing can have both a newer and an older neighbour already stored)."""
    rows = conn.execute(
        "select id, period_type, period_end_on, turnover, gross_profit, operating_result, profit_after_tax, cash, net_assets, employees "
        "from financial_period_summaries where company_number = ? and document_id = ?",
        (company_number, document_id),
    ).fetchall()

    statuses: list[str] = []
    for row_id, period_type, period_end_on, *metric_values in rows:
        if not period_end_on or period_type not in ("current", "previous"):
            continue
        values = dict(zip(COMPARATIVE_OVERLAP_METRICS, metric_values))
        counterpart_type = "current" if period_type == "previous" else "previous"
        counterpart = conn.execute(
            "select id, turnover, gross_profit, operating_result, profit_after_tax, cash, net_assets, employees "
            "from financial_period_summaries "
            "where company_number = ? and period_type = ? and period_end_on = ? and document_id != ?",
            (company_number, counterpart_type, period_end_on, document_id),
        ).fetchone()
        if not counterpart:
            continue
        counterpart_id = counterpart[0]
        counterpart_values = dict(zip(COMPARATIVE_OVERLAP_METRICS, counterpart[1:]))

        previous_id, previous_values, current_values = (
            (row_id, values, counterpart_values)
            if period_type == "previous"
            else (counterpart_id, counterpart_values, values)
        )
        diffs = {
            metric: {"previous_period_reading": previous_values[metric], "adjacent_filing_reading": current_values[metric]}
            for metric in COMPARATIVE_OVERLAP_METRICS
            if previous_values[metric] is not None
            and current_values[metric] is not None
            and previous_values[metric] != current_values[metric]
        }
        status = "mismatch" if diffs else "match"
        conn.execute(
            "update financial_period_summaries set comparative_overlap_status = ?, comparative_overlap_payload = ? where id = ?",
            (status, json_text(diffs) if diffs else None, previous_id),
        )
        statuses.append(status)
    return statuses


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def populate_sic_groups(conn: sqlite3.Connection) -> None:
    for rule in DEFAULT_SIC_GROUPS:
        conn.execute(
            """
            insert into sic_groups (
                sic_code, sic_label, sic_group, model_version, updated_at
            ) values (?, ?, ?, ?, ?)
            on conflict(sic_code) do update set
                sic_label=excluded.sic_label,
                sic_group=excluded.sic_group,
                model_version=excluded.model_version,
                updated_at=excluded.updated_at
            """,
            (
                rule["sic_code"],
                rule["sic_label"],
                rule["sic_group"],
                rule["model_version"],
                utc_now(),
            ),
        )


def _signal_columns(value: Any) -> tuple[str, int | float | str]:
    if isinstance(value, bool):
        return "signal_bool", int(value)
    if isinstance(value, int):
        return "signal_int", value
    if isinstance(value, float):
        return "signal_real", value
    return "signal_text", str(value)


def upsert_company_signals(
    conn: sqlite3.Connection,
    company_number: str,
    signals: dict[str, Any],
    *,
    source_scope: str = "api",
) -> int:
    """Write derived per-company scalars into the company_signals EAV table,
    one row per (company_number, signal_key): cheap to extend with a new
    signal without a migration.
    A None value clears that signal rather than storing a null row."""
    now = utc_now()
    written = 0
    for signal_key, value in signals.items():
        if value is None:
            conn.execute(
                "delete from company_signals where company_number = ? and signal_key = ?",
                (company_number, signal_key),
            )
            continue
        column, stored = _signal_columns(value)
        value_type = column.removeprefix("signal_")
        conn.execute(
            f"""
            insert into company_signals (
                company_number, signal_key, signal_value_type, {column},
                source_scope, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            on conflict(company_number, signal_key) do update set
                signal_value_type=excluded.signal_value_type,
                signal_bool=null, signal_int=null, signal_real=null, signal_text=null,
                {column}=excluded.{column},
                source_scope=excluded.source_scope,
                updated_at=excluded.updated_at
            """,
            (company_number, signal_key, value_type, stored, source_scope, now, now),
        )
        written += 1
    return written


COMPANY_PROFILE_FIELDS = ("demand_model", "customer_type", "delivery_model", "geography_served", "trading_status_confirmed")


def upsert_company_profile(
    conn: sqlite3.Connection,
    company_number: str,
    financial_year: int | None,
    profile: dict[str, Any],
    *,
    narrative_run_id: int | None,
    extraction_model: str,
    prompt_version: str,
) -> int:
    """Persist a Gate A2 business-profile extraction. `profile` holds, for
    each name in COMPANY_PROFILE_FIELDS, a dict with value/confidence/quote/
    section (see scripts/business_profile_classifier/business_profile_policy.py), plus optionally
    "business_description" (str) and "sic_agreement" (dict with value and
    reason). Any field the model declined to call (value "unclear" or
    absent) is still stored -- unclear is a legitimate answer, not a gap."""
    values: dict[str, Any] = {
        "business_description": profile.get("business_description"),
        "sic_agreement": (profile.get("sic_agreement") or {}).get("value"),
        "sic_agreement_reason": (profile.get("sic_agreement") or {}).get("reason"),
        "sic_agreement_quote": (profile.get("sic_agreement") or {}).get("quote"),
        "sic_agreement_section": (profile.get("sic_agreement") or {}).get("section"),
    }
    for field in COMPANY_PROFILE_FIELDS:
        entry = profile.get(field) or {}
        values[field] = entry.get("value")
        values[f"{field}_confidence"] = entry.get("confidence")
        values[f"{field}_quote"] = entry.get("quote")
        values[f"{field}_section"] = entry.get("section")
        values[f"{field}_reason"] = entry.get("reason")

    columns = [
        "company_number", "financial_year", "narrative_run_id",
        "business_description",
        *[
            c
            for field in COMPANY_PROFILE_FIELDS
            for c in (field, f"{field}_confidence", f"{field}_quote", f"{field}_section", f"{field}_reason")
        ],
        "sic_agreement", "sic_agreement_reason", "sic_agreement_quote", "sic_agreement_section",
        "extraction_model", "prompt_version", "generated_at",
    ]
    row = {
        "company_number": company_number,
        "financial_year": financial_year,
        "narrative_run_id": narrative_run_id,
        "extraction_model": extraction_model,
        "prompt_version": prompt_version,
        "generated_at": utc_now(),
        **values,
    }
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in ("company_number", "financial_year"))
    cursor = conn.execute(
        f"""
        insert into company_profiles ({", ".join(columns)})
        values ({placeholders})
        on conflict(company_number, financial_year) do update set {update_clause}
        """,
        tuple(row[c] for c in columns),
    )
    conn.commit()
    return int(cursor.lastrowid)


def infer_document_id(payload: dict[str, Any]) -> str | None:
    metadata_url = (payload.get("document_urls") or {}).get("metadata")
    if metadata_url:
        return metadata_url.rstrip("/").split("/")[-1]
    latest_filing = payload.get("latest_accounts_filing") or {}
    links = latest_filing.get("links") or {}
    document_metadata = links.get("document_metadata")
    if document_metadata:
        return document_metadata.rstrip("/").split("/")[-1]
    return None


def upsert_extractor_payload(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("company_profile") or {}
    company_number = payload["company_number"]
    latest_filing = payload.get("latest_accounts_filing") or {}
    downloaded_files = payload.get("downloaded_files") or {}
    document_urls = payload.get("document_urls") or {}
    document_id = infer_document_id(payload)

    conn.execute(
        """
        insert into companies (
            company_number, company_name, company_status, company_type,
            date_of_creation, source_mode, profile_payload, updated_at,
            sic_codes, sic_code_primary
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(company_number) do update set
            company_name=excluded.company_name,
            company_status=excluded.company_status,
            company_type=excluded.company_type,
            date_of_creation=excluded.date_of_creation,
            source_mode=excluded.source_mode,
            profile_payload=excluded.profile_payload,
            updated_at=excluded.updated_at,
            sic_codes=excluded.sic_codes,
            sic_code_primary=excluded.sic_code_primary
        """,
        (
            company_number,
            profile.get("company_name"),
            profile.get("company_status"),
            profile.get("type"),
            profile.get("date_of_creation"),
            payload.get("source_mode"),
            json_text(profile),
            payload.get("generated_at"),
            json_text(profile.get("sic_codes") or []),
            (profile.get("sic_codes") or [None])[0],
        ),
    )

    transaction_id = latest_filing.get("transaction_id")
    if transaction_id:
        conn.execute(
            """
            insert into filings (
                transaction_id, company_number, filing_date, category, type,
                description, action_date, pages, filing_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(transaction_id) do update set
                company_number=excluded.company_number,
                filing_date=excluded.filing_date,
                category=excluded.category,
                type=excluded.type,
                description=excluded.description,
                action_date=excluded.action_date,
                pages=excluded.pages,
                filing_payload=excluded.filing_payload
            """,
            (
                transaction_id,
                company_number,
                latest_filing.get("date"),
                latest_filing.get("category"),
                latest_filing.get("type"),
                latest_filing.get("description"),
                latest_filing.get("action_date"),
                latest_filing.get("pages"),
                json_text(latest_filing),
            ),
        )

    if document_id:
        conn.execute(
            """
            insert into documents (
                document_id, transaction_id, company_number, metadata_url, xhtml_url,
                pdf_url, downloaded_xhtml_path, downloaded_pdf_path, metadata_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(document_id) do update set
                transaction_id=excluded.transaction_id,
                company_number=excluded.company_number,
                metadata_url=excluded.metadata_url,
                xhtml_url=excluded.xhtml_url,
                pdf_url=excluded.pdf_url,
                downloaded_xhtml_path=excluded.downloaded_xhtml_path,
                downloaded_pdf_path=excluded.downloaded_pdf_path,
                metadata_payload=excluded.metadata_payload
            """,
            (
                document_id,
                transaction_id,
                company_number,
                document_urls.get("metadata"),
                document_urls.get("xhtml"),
                document_urls.get("pdf"),
                downloaded_files.get("xhtml"),
                downloaded_files.get("pdf"),
                json_text(document_urls),
            ),
        )

    accounts_extract = payload.get("accounts_extract") or {}
    years = accounts_extract.get("years") or {}
    derived = accounts_extract.get("derived") or {}
    for period_type, raw_period in years.items():
        conn.execute(
            """
            insert into financial_period_summaries (
                company_number, document_id, period_type, financial_year, turnover, gross_profit,
                operating_result, profit_after_tax, cash, net_assets, employees,
                derived_payload, raw_payload, currency_code, currency_source, period_end_on,
                currency_validation_status, turnover_reported_value, gross_profit_reported_value,
                operating_result_reported_value, profit_after_tax_reported_value, cash_reported_value,
                net_assets_reported_value
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(company_number, document_id, period_type) do update set
                financial_year=excluded.financial_year,
                turnover=excluded.turnover,
                gross_profit=excluded.gross_profit,
                operating_result=excluded.operating_result,
                profit_after_tax=excluded.profit_after_tax,
                cash=excluded.cash,
                net_assets=excluded.net_assets,
                employees=excluded.employees,
                derived_payload=excluded.derived_payload,
                raw_payload=excluded.raw_payload
                ,currency_code=excluded.currency_code, currency_source=excluded.currency_source,
                period_end_on=excluded.period_end_on, currency_validation_status=excluded.currency_validation_status,
                turnover_reported_value=excluded.turnover_reported_value, gross_profit_reported_value=excluded.gross_profit_reported_value,
                operating_result_reported_value=excluded.operating_result_reported_value, profit_after_tax_reported_value=excluded.profit_after_tax_reported_value,
                cash_reported_value=excluded.cash_reported_value, net_assets_reported_value=excluded.net_assets_reported_value
            """,
            (
                company_number,
                document_id,
                period_type,
                raw_period.get("financial_year"),
                raw_period.get("turnover"),
                raw_period.get("gross_profit"),
                raw_period.get("operating_result"),
                raw_period.get("profit_after_tax"),
                raw_period.get("cash"),
                raw_period.get("net_assets"),
                raw_period.get("employees"),
                json_text(derived),
                json_text(raw_period),
                raw_period.get("currency_code"), raw_period.get("currency_source"), raw_period.get("period_end_on"),
                raw_period.get("currency_validation_status", "unknown"),
                *[str(raw_period[metric]) if raw_period.get(metric) is not None else None for metric in (
                    "turnover", "gross_profit", "operating_result", "profit_after_tax", "cash", "net_assets"
                )],
            ),
        )

    conn.commit()
    return {"company_number": company_number, "document_id": document_id, "transaction_id": transaction_id}


def upsert_document_text(
    conn: sqlite3.Connection,
    *,
    company_number: str,
    document_id: str,
    source: str,
    model: str,
    prompt_version: str | None,
    raw_text: str,
    filed_report_text: str,
    payload: dict[str, Any],
) -> int:
    """One document_texts row per (document, source, model); a re-run
    replaces the text and bookkeeping but keeps the original created_at.
    ``payload`` is the transcription harness's per-document result (status,
    page counts, usage, cost); only the keys read here are persisted as
    columns, the per-page detail goes into page_usage_payload."""
    now = utc_now()
    cost = payload.get("cost") or {}
    cursor = conn.execute(
        """
        insert into document_texts (
            company_number, document_id, source, model, prompt_version,
            pdf_path, pdf_sha256, status, page_count, transcribed_pages,
            illegible_pages, failed_pages, raw_text, filed_report_text,
            page_usage_payload, usage_payload, pricing_payload,
            cost_usd, cost_gbp, cost_method, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(document_id, source, model) do update set
            company_number = excluded.company_number,
            prompt_version = excluded.prompt_version,
            pdf_path = excluded.pdf_path,
            pdf_sha256 = excluded.pdf_sha256,
            status = excluded.status,
            page_count = excluded.page_count,
            transcribed_pages = excluded.transcribed_pages,
            illegible_pages = excluded.illegible_pages,
            failed_pages = excluded.failed_pages,
            raw_text = excluded.raw_text,
            filed_report_text = excluded.filed_report_text,
            page_usage_payload = excluded.page_usage_payload,
            usage_payload = excluded.usage_payload,
            pricing_payload = excluded.pricing_payload,
            cost_usd = excluded.cost_usd,
            cost_gbp = excluded.cost_gbp,
            cost_method = excluded.cost_method,
            updated_at = excluded.updated_at
        """,
        (
            company_number,
            document_id,
            source,
            model or "",
            prompt_version,
            payload.get("pdf_path"),
            payload.get("pdf_sha256"),
            payload.get("status") or "complete",
            payload.get("page_count"),
            payload.get("transcribed_pages"),
            json_text(payload.get("illegible_pages") or []),
            json_text(payload.get("failed_pages") or []),
            raw_text,
            filed_report_text,
            json_text(payload.get("pages") or []),
            json_text(payload.get("usage") or {}),
            json_text(cost.get("pricing") or {}),
            cost.get("usd"),
            cost.get("gbp"),
            cost.get("method") or "unavailable",
            now,
            now,
        ),
    )
    conn.commit()
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = conn.execute(
        "select id from document_texts where document_id = ? and source = ? and model = ?",
        (document_id, source, model or ""),
    ).fetchone()
    return int(row[0])


def insert_narrative_payload(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    company_number: str | None,
    document_id: str | None,
) -> int:
    cursor = conn.execute(
        """
        insert into narrative_runs (
            document_id, company_number, pdf_path, text_source, ocr_requested,
            ocr_used, ocr_engine_used, text_quality_payload, raw_payload
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            company_number,
            payload.get("pdf_path"),
            payload.get("text_source"),
            int(bool(payload.get("ocr_requested"))),
            int(bool(payload.get("ocr_used"))),
            payload.get("ocr_engine_used"),
            json_text(payload.get("text_quality") or {}),
            json_text(payload),
        ),
    )
    run_id = int(cursor.lastrowid)

    for section_key, section in (payload.get("sections") or {}).items():
        conn.execute(
            """
            insert into narrative_sections (
                narrative_run_id, section_key, section_title, page_number, section_text, section_payload
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                section_key,
                section.get("heading"),
                section.get("page"),
                section.get("text"),
                json_text(section),
            ),
        )

    for statement in payload.get("performance_statements") or []:
        conn.execute(
            """
            insert into performance_statements (
                narrative_run_id, page_number, statement_text
            ) values (?, ?, ?)
            """,
            (run_id, statement.get("page"), statement.get("text")),
        )

    conn.commit()
    return run_id


def insert_vlm_financial_payload(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    company_number: str | None,
    document_id: str | None,
) -> int:
    """Store a hosted-vision financial extraction and its auditable metric rows."""
    models = payload.get("models") or {}
    cost = payload.get("cost") or {}
    cursor = conn.execute(
        """
        insert into vlm_financial_extraction_runs (
            company_number, document_id, pdf_path, locator_model, vision_model,
            rationalisation_model, status, pages_scanned_payload,
            candidate_pages_payload, raw_extraction_payload, rationalisation_payload,
            usage_payload, pricing_payload, cost_usd, cost_gbp, cost_method
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_number,
            document_id,
            payload.get("pdf_path"),
            models.get("locator"),
            models.get("vision"),
            models.get("rationalisation"),
            payload.get("status", "complete"),
            json_text(payload.get("pages_scanned") or []),
            json_text(payload.get("candidate_pages") or []),
            json_text(payload.get("raw_extraction") or {}),
            json_text(payload.get("rationalisation") or {}),
            json_text(payload.get("usage") or {}),
            json_text(cost.get("pricing") or {}),
            cost.get("usd"),
            cost.get("gbp"),
            cost.get("method", "estimated"),
        ),
    )
    run_id = int(cursor.lastrowid)

    metrics_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for metric in payload.get("metrics") or []:
        key = (metric.get("period_type"), metric.get("metric_name"))
        existing = metrics_by_key.get(key)
        score = (
            int(bool((metric.get("validation") or {}).get("unit_known"))),
            float(metric.get("confidence") or 0),
        )
        existing_score = (
            int(bool((existing.get("validation") or {}).get("unit_known"))),
            float(existing.get("confidence") or 0),
        ) if existing else None
        if existing is None or score > existing_score:
            metrics_by_key[key] = metric

    for metric in metrics_by_key.values():
        conn.execute(
            """
            insert into vlm_financial_metrics (
                extraction_run_id, company_number, period_type, financial_year, metric_name, value_pence,
                value_count,
                displayed_value, unit, currency_code, scale_multiplier, reported_value,
                source_page, source_label, evidence_text,
                confidence, vision_model, rationalisation_model, validation_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                company_number,
                metric.get("period_type"),
                metric.get("financial_year"),
                metric.get("metric_name"),
                metric.get("value_pence"),
                metric.get("value_count"),
                metric.get("displayed_value"),
                metric.get("unit"),
                metric.get("currency_code"),
                metric.get("scale_multiplier"),
                metric.get("reported_value"),
                metric.get("source_page"),
                metric.get("source_label"),
                metric.get("evidence_text"),
                metric.get("confidence"),
                models.get("vision"),
                models.get("rationalisation"),
                json_text(metric.get("validation") or {}),
            ),
        )

    canonical_metric_names = (
        "turnover", "gross_profit", "operating_result", "profit_after_tax",
        "cash", "net_assets", "employees",
    )
    canonical_by_period: dict[str, dict[str, Any]] = {}
    years_by_period: dict[str, set[int]] = {}
    for (period_type, metric_name), metric in metrics_by_key.items():
        if metric_name not in canonical_metric_names:
            continue
        period = canonical_by_period.setdefault(period_type, {})
        financial_year = metric.get("financial_year")
        if isinstance(financial_year, int) and not isinstance(financial_year, bool):
            years_by_period.setdefault(period_type, set()).add(financial_year)
        if metric_name == "employees":
            period[metric_name] = metric.get("value_count")
        else:
            value = metric.get("reported_value")
            if value is None and metric.get("currency_code") in (None, "GBP") and metric.get("value_pence") is not None:
                value = str(Decimal(int(metric["value_pence"])) / Decimal(100))
            period[metric_name] = value

    for period_type, period in canonical_by_period.items():
        period_years = years_by_period.get(period_type, set())
        financial_year = next(iter(period_years)) if len(period_years) == 1 else None
        money_metrics = [metric for metric in canonical_metric_names if metric != "employees" and period.get(metric) is not None]
        currencies = {
            metrics_by_key[(period_type, metric)].get("currency_code")
            or ("GBP" if metrics_by_key[(period_type, metric)].get("value_pence") is not None else None)
            for metric in money_metrics
        }
        currencies.discard(None)
        currency_code = next(iter(currencies)) if len(currencies) == 1 else None
        currency_status = "valid" if currency_code and len(currencies) == 1 else ("mixed" if len(currencies) > 1 else "unknown")
        reported = {metric: period.get(metric) for metric in money_metrics}
        def legacy_amount(metric: str) -> int | None:
            value = reported.get(metric)
            if value is None:
                return None
            try:
                return int(value) if str(value).split(".", 1)[-1] == "0" or "." not in str(value) else None
            except ValueError:
                return None
        conn.execute(
            """
            insert into financial_period_summaries (
                company_number, document_id, period_type, financial_year, turnover, gross_profit,
                operating_result, profit_after_tax, cash, net_assets, employees,
                derived_payload, raw_payload, data_source, currency_code, currency_source,
                currency_validation_status, turnover_reported_value, gross_profit_reported_value,
                operating_result_reported_value, profit_after_tax_reported_value, cash_reported_value,
                net_assets_reported_value
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'vlm', ?, 'vlm_statement', ?, ?, ?, ?, ?, ?, ?)
            on conflict(company_number, document_id, period_type) do update set
                financial_year=excluded.financial_year,
                turnover=excluded.turnover,
                gross_profit=excluded.gross_profit,
                operating_result=excluded.operating_result,
                profit_after_tax=excluded.profit_after_tax,
                cash=excluded.cash,
                net_assets=excluded.net_assets,
                employees=excluded.employees,
                derived_payload=excluded.derived_payload,
                raw_payload=excluded.raw_payload,
                data_source='vlm',
                currency_code=excluded.currency_code,
                currency_source=excluded.currency_source,
                currency_validation_status=excluded.currency_validation_status,
                turnover_reported_value=excluded.turnover_reported_value,
                gross_profit_reported_value=excluded.gross_profit_reported_value,
                operating_result_reported_value=excluded.operating_result_reported_value,
                profit_after_tax_reported_value=excluded.profit_after_tax_reported_value,
                cash_reported_value=excluded.cash_reported_value,
                net_assets_reported_value=excluded.net_assets_reported_value
            where financial_period_summaries.data_source = 'vlm'
            """,
            (
                company_number,
                document_id,
                period_type,
                financial_year,
                legacy_amount("turnover"), legacy_amount("gross_profit"), legacy_amount("operating_result"),
                legacy_amount("profit_after_tax"), legacy_amount("cash"), legacy_amount("net_assets"),
                period.get("employees"),
                json_text({"source": "vlm", "extraction_run_id": run_id}),
                json_text({"source": "vlm", "extraction_run_id": run_id, "metrics": list(period)}),
                currency_code, currency_status,
                reported.get("turnover"), reported.get("gross_profit"), reported.get("operating_result"),
                reported.get("profit_after_tax"), reported.get("cash"), reported.get("net_assets"),
            ),
        )
    conn.commit()
    return run_id


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Store Companies House extraction outputs in SQLite.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--extract-json", help="Extractor output JSON path.")
    parser.add_argument("--narrative-json", help="Narrative OCR output JSON path.")
    parser.add_argument("--company-number", help="Override company number for narrative-only import.")
    parser.add_argument("--document-id", help="Override document id for narrative-only import.")
    args = parser.parse_args(argv)

    if not args.extract_json and not args.narrative_json:
        parser.error("Pass at least one of --extract-json or --narrative-json.")

    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        company_number = args.company_number
        document_id = args.document_id

        if args.extract_json:
            extract_payload = load_json(Path(args.extract_json))
            refs = upsert_extractor_payload(conn, extract_payload)
            company_number = refs["company_number"]
            document_id = refs["document_id"]

        narrative_run_id = None
        if args.narrative_json:
            narrative_payload = load_json(Path(args.narrative_json))
            narrative_run_id = insert_narrative_payload(conn, narrative_payload, company_number, document_id)

        print(
            json.dumps(
                {
                    "db": args.db,
                    "company_number": company_number,
                    "document_id": document_id,
                    "narrative_run_id": narrative_run_id,
                },
                indent=2,
            )
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
