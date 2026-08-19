#!/usr/bin/env python3
"""Store Companies House extraction outputs in a local SQLite database."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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

    customer_type text,
    customer_type_confidence real,
    customer_type_quote text,
    customer_type_section text,

    delivery_model text,
    delivery_model_confidence real,
    delivery_model_quote text,
    delivery_model_section text,

    geography_served text,
    geography_served_confidence real,
    geography_served_quote text,
    geography_served_section text,

    trading_status_confirmed text,
    trading_status_confirmed_confidence real,
    trading_status_confirmed_quote text,
    trading_status_confirmed_section text,

    sic_agreement text,
    sic_agreement_reason text,

    extraction_model text not null,
    prompt_version text not null,
    generated_at text not null,
    unique(company_number, financial_year),
    foreign key(company_number) references companies(company_number),
    foreign key(narrative_run_id) references narrative_runs(id)
);

create table if not exists website_investigations (
    id integer primary key autoincrement,
    company_number text not null,
    source_label text not null,
    source_file text,
    investigation_type text not null default 'browser_pilot',
    status text not null,
    sic_1 text,
    sic_label text,
    account_category text,
    turnover integer,
    estimated_monthly_ppc_spend real,
    search_queries text,
    search_results_count integer not null default 0,
    candidate_count integer not null default 0,
    chosen_result_score real,
    chosen_result_title text,
    chosen_result_snippet text,
    chosen_result_domain text,
    chosen_result_url text,
    final_url text,
    final_domain text,
    page_title text,
    meta_description text,
    og_description text,
    business_model text,
    business_description text,
    raw_payload text not null,
    created_at text not null,
    updated_at text not null,
    unique(company_number, source_label),
    foreign key(company_number) references companies(company_number)
);

create table if not exists website_signals (
    id integer primary key autoincrement,
    investigation_id integer not null,
    signal_key text not null,
    signal_value_type text not null,
    signal_bool integer,
    signal_int integer,
    signal_real real,
    signal_text text,
    source_scope text not null default 'derived',
    created_at text not null,
    updated_at text not null,
    unique(investigation_id, signal_key),
    foreign key(investigation_id) references website_investigations(id)
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
create index if not exists idx_website_investigations_company_number on website_investigations(company_number);
create index if not exists idx_website_investigations_status on website_investigations(status);
create index if not exists idx_website_signals_investigation_id on website_signals(investigation_id);
create index if not exists idx_website_signals_key on website_signals(signal_key);

create view if not exists website_investigation_metric_view as
select
    wi.id as investigation_id,
    wi.company_number,
    wi.source_label,
    wi.status,
    wi.sic_1,
    wi.sic_label,
    wi.account_category,
    wi.turnover,
    wi.estimated_monthly_ppc_spend,
    wi.business_model,
    wi.business_description,
    wi.chosen_result_domain,
    wi.final_domain,
    wi.final_url,
    wi.page_title,
    max(case when ws.signal_key = 'site_match_confidence_score' then coalesce(ws.signal_real, ws.signal_int) end) as site_match_confidence_score,
    max(case when ws.signal_key = 'ppc_fit_score' then coalesce(ws.signal_real, ws.signal_int) end) as ppc_fit_score,
    max(case when ws.signal_key = 'ecommerce_signal_score' then coalesce(ws.signal_real, ws.signal_int) end) as ecommerce_signal_score,
    max(case when ws.signal_key = 'lead_generation_signal_score' then coalesce(ws.signal_real, ws.signal_int) end) as lead_generation_signal_score,
    max(case when ws.signal_key = 'b2b_service_signal_score' then coalesce(ws.signal_real, ws.signal_int) end) as b2b_service_signal_score,
    max(case when ws.signal_key = 'local_presence_signal_score' then coalesce(ws.signal_real, ws.signal_int) end) as local_presence_signal_score,
    max(case when ws.signal_key = 'search_results_count' then coalesce(ws.signal_real, ws.signal_int) end) as search_results_count,
    max(case when ws.signal_key = 'candidate_count' then coalesce(ws.signal_real, ws.signal_int) end) as candidate_count,
    max(case when ws.signal_key = 'chosen_result_score' then coalesce(ws.signal_real, ws.signal_int) end) as chosen_result_score,
    max(case when ws.signal_key = 'nav_link_count' then coalesce(ws.signal_real, ws.signal_int) end) as nav_link_count,
    max(case when ws.signal_key = 'cta_count' then coalesce(ws.signal_real, ws.signal_int) end) as cta_count,
    max(case when ws.signal_key = 'body_word_count' then coalesce(ws.signal_real, ws.signal_int) end) as body_word_count,
    max(case when ws.signal_key = 'price_mention_count' then coalesce(ws.signal_real, ws.signal_int) end) as price_mention_count,
    max(case when ws.signal_key = 'contact_keyword_count' then coalesce(ws.signal_real, ws.signal_int) end) as contact_keyword_count,
    max(case when ws.signal_key = 'ecommerce_keyword_count' then coalesce(ws.signal_real, ws.signal_int) end) as ecommerce_keyword_count,
    max(case when ws.signal_key = 'service_keyword_count' then coalesce(ws.signal_real, ws.signal_int) end) as service_keyword_count,
    max(case when ws.signal_key = 'b2b_keyword_count' then coalesce(ws.signal_real, ws.signal_int) end) as b2b_keyword_count,
    max(case when ws.signal_key = 'location_keyword_count' then coalesce(ws.signal_real, ws.signal_int) end) as location_keyword_count,
    max(case when ws.signal_key = 'trust_keyword_count' then coalesce(ws.signal_real, ws.signal_int) end) as trust_keyword_count,
    max(case when ws.signal_key = 'has_checkout' then coalesce(ws.signal_bool, ws.signal_int) end) as has_checkout,
    max(case when ws.signal_key = 'has_store_locator' then coalesce(ws.signal_bool, ws.signal_int) end) as has_store_locator,
    max(case when ws.signal_key = 'has_quote_form' then coalesce(ws.signal_bool, ws.signal_int) end) as has_quote_form,
    max(case when ws.signal_key = 'has_booking' then coalesce(ws.signal_bool, ws.signal_int) end) as has_booking,
    max(case when ws.signal_key = 'has_demo' then coalesce(ws.signal_bool, ws.signal_int) end) as has_demo,
    max(case when ws.signal_key = 'has_finance' then coalesce(ws.signal_bool, ws.signal_int) end) as has_finance,
    wi.created_at,
    wi.updated_at
from website_investigations wi
left join website_signals ws on ws.investigation_id = wi.id
group by
    wi.id,
    wi.company_number,
    wi.source_label,
    wi.status,
    wi.sic_1,
    wi.sic_label,
    wi.account_category,
    wi.turnover,
    wi.estimated_monthly_ppc_spend,
    wi.business_model,
    wi.business_description,
    wi.chosen_result_domain,
    wi.final_domain,
    wi.final_url,
    wi.page_title,
    wi.created_at,
    wi.updated_at;
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
    ensure_financial_period_summary_columns(conn)
    ensure_financial_year_columns(conn)
    ensure_vlm_financial_metric_columns(conn)
    ensure_currency_columns(conn)
    ensure_comparative_overlap_columns(conn)
    ensure_company_sic_columns(conn)
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
    tmp/dropped-tables/ before this ran."""
    conn.execute("drop table if exists ppc_company_estimates")
    conn.execute("drop table if exists ppc_ratio_rules")


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


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        hostname = urlparse(url).hostname or ""
    except ValueError:
        return None
    hostname = hostname.lower().strip()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or None


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def count_keyword_hits(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(keyword.lower()) for keyword in keywords)


def company_name_tokens(company_name: str | None) -> list[str]:
    stopwords = {
        "and",
        "company",
        "group",
        "holdco",
        "holdings",
        "limited",
        "ltd",
        "newco",
        "services",
        "solutions",
        "the",
        "uk",
    }
    tokens = re.findall(r"[a-z0-9]+", (company_name or "").lower())
    return [token for token in tokens if len(token) > 2 and token not in stopwords]


def derive_website_metrics(payload: dict[str, Any]) -> dict[str, int | float | str | bool]:
    chosen_result = payload.get("chosen_result") or {}
    website = payload.get("website") or {}
    company_tokens = company_name_tokens(payload.get("company_name"))
    text_parts = [
        website.get("title", ""),
        website.get("meta_description", ""),
        website.get("og_description", ""),
        website.get("body_sample", ""),
        " ".join(website.get("nav_links") or []),
        " ".join(website.get("ctas") or []),
        chosen_result.get("title", ""),
        chosen_result.get("snippet", ""),
    ]
    text = normalize_space(" ".join(text_parts))
    domain_text = " ".join(
        part
        for part in [
            extract_domain(website.get("final_url")) or "",
            chosen_result.get("hostname") or "",
            chosen_result.get("target_url") or "",
        ]
        if part
    ).lower()
    title_text = " ".join(
        part
        for part in [
            chosen_result.get("title") or "",
            chosen_result.get("snippet") or "",
            website.get("title") or "",
            website.get("meta_description") or "",
        ]
        if part
    ).lower()
    ecommerce_keywords = [
        "shop",
        "product",
        "products",
        "buy",
        "basket",
        "cart",
        "checkout",
        "delivery",
        "sale",
        "collection",
    ]
    service_keywords = [
        "service",
        "services",
        "maintenance",
        "installation",
        "contractor",
        "solution",
        "solutions",
        "project",
        "projects",
        "support",
        "refurbishment",
    ]
    b2b_keywords = [
        "client",
        "clients",
        "sector",
        "sectors",
        "commercial",
        "framework",
        "contract",
        "nationwide",
        "public sector",
    ]
    location_keywords = [
        "postcode",
        "find us",
        "find a store",
        "find a dealer",
        "store locator",
        "location",
        "locations",
        "branch",
        "branches",
        "nationwide",
    ]
    trust_keywords = [
        "award",
        "accredited",
        "trusted",
        "established",
        "experience",
        "years",
        "family-run",
        "certified",
    ]
    contact_keywords = [
        "contact",
        "call us",
        "email us",
        "get in touch",
        "enquiry",
        "enquiries",
        "request a quote",
        "book",
    ]
    price_count = len(re.findall(r"[£$€]\s?\d", text))
    ecommerce_keyword_count = count_keyword_hits(text, ecommerce_keywords)
    service_keyword_count = count_keyword_hits(text, service_keywords)
    b2b_keyword_count = count_keyword_hits(text, b2b_keywords)
    location_keyword_count = count_keyword_hits(text, location_keywords)
    trust_keyword_count = count_keyword_hits(text, trust_keywords)
    contact_keyword_count = count_keyword_hits(text, contact_keywords)

    has_checkout = bool(website.get("has_checkout"))
    has_store_locator = bool(website.get("has_store_locator"))
    has_quote_form = bool(website.get("has_quote_form"))
    has_booking = bool(website.get("has_booking"))
    has_demo = bool(website.get("has_demo"))
    has_finance = bool(website.get("has_finance"))
    domain_token_match_count = sum(1 for token in company_tokens if token in domain_text)
    title_token_match_count = sum(1 for token in company_tokens if token in title_text)
    company_token_count = len(company_tokens)
    domain_match_ratio = round(domain_token_match_count / company_token_count, 4) if company_token_count else 0.0
    title_match_ratio = round(title_token_match_count / company_token_count, 4) if company_token_count else 0.0

    ecommerce_signal_score = min(
        100.0,
        (35.0 if has_checkout else 0.0)
        + min(25.0, ecommerce_keyword_count * 3.0)
        + min(20.0, price_count * 2.0)
        + (10.0 if has_store_locator else 0.0),
    )
    lead_generation_signal_score = min(
        100.0,
        (30.0 if has_quote_form else 0.0)
        + (25.0 if has_booking else 0.0)
        + (20.0 if has_demo else 0.0)
        + min(15.0, contact_keyword_count * 2.0)
        + min(10.0, service_keyword_count * 1.5),
    )
    b2b_service_signal_score = min(
        100.0,
        min(35.0, service_keyword_count * 3.0)
        + min(25.0, b2b_keyword_count * 4.0)
        + min(15.0, trust_keyword_count * 3.0)
        + (10.0 if has_quote_form else 0.0),
    )
    local_presence_signal_score = min(
        100.0,
        (25.0 if has_store_locator else 0.0)
        + (15.0 if has_booking else 0.0)
        + min(30.0, location_keyword_count * 4.0)
        + min(20.0, contact_keyword_count * 2.0),
    )
    ppc_fit_score = round(
        min(
            100.0,
            max(
                ecommerce_signal_score,
                lead_generation_signal_score * 0.9 + b2b_service_signal_score * 0.35,
                local_presence_signal_score * 0.7 + lead_generation_signal_score * 0.3,
            ),
        ),
        2,
    )
    site_match_confidence_score = round(
        min(
            100.0,
            (20.0 if payload.get("status") == "ok" else 0.0)
            + (domain_match_ratio * 45.0)
            + (title_match_ratio * 25.0)
            + (10.0 if website.get("final_url") else 0.0),
        ),
        2,
    )

    return {
        "company_token_count": company_token_count,
        "domain_token_match_count": domain_token_match_count,
        "title_token_match_count": title_token_match_count,
        "domain_match_ratio": domain_match_ratio,
        "title_match_ratio": title_match_ratio,
        "site_match_confidence_score": site_match_confidence_score,
        "search_results_count": len(payload.get("search_results") or []),
        "candidate_count": len(payload.get("candidates") or []),
        "chosen_result_score": chosen_result.get("score"),
        "body_char_count": len(website.get("body_sample") or ""),
        "body_word_count": len((website.get("body_sample") or "").split()),
        "nav_link_count": len(website.get("nav_links") or []),
        "cta_count": len(website.get("ctas") or []),
        "h1_count": len(website.get("h1s") or []),
        "price_mention_count": price_count,
        "contact_keyword_count": contact_keyword_count,
        "ecommerce_keyword_count": ecommerce_keyword_count,
        "service_keyword_count": service_keyword_count,
        "b2b_keyword_count": b2b_keyword_count,
        "location_keyword_count": location_keyword_count,
        "trust_keyword_count": trust_keyword_count,
        "has_checkout": has_checkout,
        "has_store_locator": has_store_locator,
        "has_quote_form": has_quote_form,
        "has_booking": has_booking,
        "has_demo": has_demo,
        "has_finance": has_finance,
        "ecommerce_signal_score": round(ecommerce_signal_score, 2),
        "lead_generation_signal_score": round(lead_generation_signal_score, 2),
        "b2b_service_signal_score": round(b2b_service_signal_score, 2),
        "local_presence_signal_score": round(local_presence_signal_score, 2),
        "ppc_fit_score": ppc_fit_score,
    }


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
    one row per (company_number, signal_key). Same shape as
    website_signals: cheap to extend with a new signal without a migration.
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
    section (see scripts/profile/business_profile_policy.py), plus optionally
    "business_description" (str) and "sic_agreement" (dict with value and
    reason). Any field the model declined to call (value "unclear" or
    absent) is still stored -- unclear is a legitimate answer, not a gap."""
    values: dict[str, Any] = {
        "business_description": profile.get("business_description"),
        "sic_agreement": (profile.get("sic_agreement") or {}).get("value"),
        "sic_agreement_reason": (profile.get("sic_agreement") or {}).get("reason"),
    }
    for field in COMPANY_PROFILE_FIELDS:
        entry = profile.get(field) or {}
        values[field] = entry.get("value")
        values[f"{field}_confidence"] = entry.get("confidence")
        values[f"{field}_quote"] = entry.get("quote")
        values[f"{field}_section"] = entry.get("section")

    columns = [
        "company_number", "financial_year", "narrative_run_id",
        "business_description",
        *[c for field in COMPANY_PROFILE_FIELDS for c in (field, f"{field}_confidence", f"{field}_quote", f"{field}_section")],
        "sic_agreement", "sic_agreement_reason",
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


def upsert_website_investigation(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    source_label: str,
    source_file: str | None = None,
) -> int:
    website = payload.get("website") or {}
    chosen_result = payload.get("chosen_result") or {}
    created_at = utc_now()
    raw_payload_text = json_text(payload)

    conn.execute(
        """
        insert into website_investigations (
            company_number, source_label, source_file, investigation_type, status, sic_1, sic_label,
            account_category, turnover, estimated_monthly_ppc_spend, search_queries, search_results_count,
            candidate_count, chosen_result_score, chosen_result_title, chosen_result_snippet,
            chosen_result_domain, chosen_result_url, final_url, final_domain, page_title,
            meta_description, og_description, business_model, business_description, raw_payload,
            created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(company_number, source_label) do update set
            source_file=excluded.source_file,
            investigation_type=excluded.investigation_type,
            status=excluded.status,
            sic_1=excluded.sic_1,
            sic_label=excluded.sic_label,
            account_category=excluded.account_category,
            turnover=excluded.turnover,
            estimated_monthly_ppc_spend=excluded.estimated_monthly_ppc_spend,
            search_queries=excluded.search_queries,
            search_results_count=excluded.search_results_count,
            candidate_count=excluded.candidate_count,
            chosen_result_score=excluded.chosen_result_score,
            chosen_result_title=excluded.chosen_result_title,
            chosen_result_snippet=excluded.chosen_result_snippet,
            chosen_result_domain=excluded.chosen_result_domain,
            chosen_result_url=excluded.chosen_result_url,
            final_url=excluded.final_url,
            final_domain=excluded.final_domain,
            page_title=excluded.page_title,
            meta_description=excluded.meta_description,
            og_description=excluded.og_description,
            business_model=excluded.business_model,
            business_description=excluded.business_description,
            raw_payload=excluded.raw_payload,
            updated_at=excluded.updated_at
        """,
        (
            payload.get("company_number"),
            source_label,
            source_file,
            "browser_pilot",
            payload.get("status") or "unknown",
            payload.get("sic_1"),
            payload.get("sic_label"),
            payload.get("account_category"),
            payload.get("turnover"),
            payload.get("estimated_monthly_ppc_spend"),
            json_text(payload.get("search_queries") or []),
            len(payload.get("search_results") or []),
            len(payload.get("candidates") or []),
            chosen_result.get("score"),
            chosen_result.get("title"),
            chosen_result.get("snippet"),
            chosen_result.get("hostname") or extract_domain(chosen_result.get("target_url")),
            chosen_result.get("target_url"),
            website.get("final_url"),
            extract_domain(website.get("final_url")),
            website.get("title"),
            website.get("meta_description"),
            website.get("og_description"),
            payload.get("business_model"),
            payload.get("business_description"),
            raw_payload_text,
            created_at,
            created_at,
        ),
    )
    investigation_id = conn.execute(
        """
        select id
        from website_investigations
        where company_number = ? and source_label = ?
        """,
        (payload.get("company_number"), source_label),
    ).fetchone()[0]

    metrics = derive_website_metrics(payload)
    for signal_key, signal_value in metrics.items():
        signal_column, typed_value = _signal_columns(signal_value)
        conn.execute(
            f"""
            insert into website_signals (
                investigation_id, signal_key, signal_value_type,
                signal_bool, signal_int, signal_real, signal_text, source_scope,
                created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(investigation_id, signal_key) do update set
                signal_value_type=excluded.signal_value_type,
                signal_bool=excluded.signal_bool,
                signal_int=excluded.signal_int,
                signal_real=excluded.signal_real,
                signal_text=excluded.signal_text,
                source_scope=excluded.source_scope,
                updated_at=excluded.updated_at
            """,
            (
                investigation_id,
                signal_key,
                "boolean" if isinstance(signal_value, bool) else "integer" if isinstance(signal_value, int) else "real" if isinstance(signal_value, float) else "text",
                typed_value if signal_column == "signal_bool" else None,
                typed_value if signal_column == "signal_int" else None,
                typed_value if signal_column == "signal_real" else None,
                typed_value if signal_column == "signal_text" else None,
                "derived",
                created_at,
                created_at,
            ),
        )

    return int(investigation_id)


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
