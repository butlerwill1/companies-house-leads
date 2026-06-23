#!/usr/bin/env python3
"""Store Companies House extraction outputs in a local SQLite database."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
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
    turnover integer,
    gross_profit integer,
    operating_result integer,
    profit_after_tax integer,
    cash integer,
    net_assets integer,
    employees integer,
    derived_payload text,
    raw_payload text not null,
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

create table if not exists ocr_financial_period_summaries (
    id integer primary key autoincrement,
    narrative_run_id integer not null,
    company_number text,
    document_id text,
    period_type text not null,
    turnover integer,
    cost_of_sales integer,
    gross_profit integer,
    administrative_expenses integer,
    operating_result integer,
    profit_before_tax integer,
    tax integer,
    profit_after_tax integer,
    current_assets integer,
    cash integer,
    net_current_assets integer,
    net_assets integer,
    employees integer,
    raw_payload text not null,
    unique(narrative_run_id, period_type),
    foreign key(narrative_run_id) references narrative_runs(id),
    foreign key(company_number) references companies(company_number),
    foreign key(document_id) references documents(document_id)
);

create table if not exists ppc_ratio_rules (
    sic_code text primary key,
    sic_label text not null,
    sic_group text not null,
    annual_ppc_ratio real not null,
    rationale text not null,
    model_version text not null,
    updated_at text not null
);

create table if not exists ppc_company_estimates (
    company_number text primary key,
    document_id text,
    sic_code text not null,
    sic_label text not null,
    annual_ppc_ratio real not null,
    turnover integer not null,
    estimated_annual_ppc_spend real not null,
    estimated_monthly_ppc_spend real not null,
    estimate_basis text not null,
    model_version text not null,
    generated_at text not null,
    foreign key(company_number) references companies(company_number),
    foreign key(document_id) references documents(document_id),
    foreign key(sic_code) references ppc_ratio_rules(sic_code)
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
create index if not exists idx_ocr_financial_company_number on ocr_financial_period_summaries(company_number);
create index if not exists idx_ppc_estimates_monthly on ppc_company_estimates(estimated_monthly_ppc_spend desc);
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

PPC_MODEL_VERSION = "sic1_turnover_ratio_v1"

PPC_RULE_GROUPS: list[dict[str, Any]] = [
    {
        "sic_group": "ecommerce_online_retail",
        "sic_label": "E-commerce / online retail",
        "annual_ppc_ratio": 0.045,
        "rationale": "E-commerce businesses commonly buy measurable search traffic at scale.",
        "codes": ["47910", "47990"],
    },
    {
        "sic_group": "banking_lending_credit",
        "sic_label": "Banking / lending / credit",
        "annual_ppc_ratio": 0.015,
        "rationale": "Finance products can be search-led but acquisition is moderated by regulation and brand effects.",
        "codes": ["64110", "64191", "64192", "64999"],
    },
    {
        "sic_group": "insurance",
        "sic_label": "Insurance",
        "annual_ppc_ratio": 0.015,
        "rationale": "Insurance is competitive in search, though spend is often spread across comparison sites and brand.",
        "codes": ["65110", "65120", "65201", "65202"],
    },
    {
        "sic_group": "financial_services_brokers",
        "sic_label": "Financial services / brokers",
        "annual_ppc_ratio": 0.012,
        "rationale": "Financial advisers and brokers often use PPC, but many remain relationship-led rather than purely search-led.",
        "codes": ["66110", "66120", "66190", "66210", "66220"],
    },
    {
        "sic_group": "legal_services",
        "sic_label": "Legal services",
        "annual_ppc_ratio": 0.03,
        "rationale": "Legal services often compete in high-intent search markets with strong enquiry economics.",
        "codes": ["69101", "69102"],
    },
    {
        "sic_group": "dental",
        "sic_label": "Dental",
        "annual_ppc_ratio": 0.035,
        "rationale": "Dental practices often rely on local patient acquisition via paid search.",
        "codes": ["86230"],
    },
    {
        "sic_group": "general_medical",
        "sic_label": "General medical",
        "annual_ppc_ratio": 0.015,
        "rationale": "General medical businesses can use PPC, but search-led acquisition is usually narrower than dental.",
        "codes": ["86210"],
    },
    {
        "sic_group": "other_health",
        "sic_label": "Other health services",
        "annual_ppc_ratio": 0.025,
        "rationale": "Private health and wellness operators often generate enquiries from high-intent search.",
        "codes": ["86900"],
    },
    {
        "sic_group": "education_tutoring_schools",
        "sic_label": "Education / tutoring / schools",
        "annual_ppc_ratio": 0.02,
        "rationale": "Education providers frequently rely on search to attract parents, students, and course enquiries.",
        "codes": ["85100", "85200", "85310", "85320"],
    },
    {
        "sic_group": "property_development",
        "sic_label": "Property development",
        "annual_ppc_ratio": 0.012,
        "rationale": "Property developers can use PPC, but acquisition is often project-based and mixed with offline channels.",
        "codes": ["41100", "41201", "41202"],
    },
    {
        "sic_group": "estate_property_management",
        "sic_label": "Estate agents / property management",
        "annual_ppc_ratio": 0.018,
        "rationale": "Property services often compete for local search demand and valuation or listing leads.",
        "codes": ["68100", "68201", "68209", "68310", "68320"],
    },
    {
        "sic_group": "car_dealers",
        "sic_label": "Car dealers",
        "annual_ppc_ratio": 0.022,
        "rationale": "Vehicle retailers often buy search traffic against strong model and location intent.",
        "codes": ["45111", "45112", "45190"],
    },
    {
        "sic_group": "restaurants_catering",
        "sic_label": "Restaurants / catering",
        "annual_ppc_ratio": 0.015,
        "rationale": "Hospitality operators use PPC selectively for discovery, but not all demand is paid-search-driven.",
        "codes": ["56101", "56102", "56103", "56210", "56290"],
    },
    {
        "sic_group": "hotels_bnb",
        "sic_label": "Hotels / B&Bs",
        "annual_ppc_ratio": 0.0175,
        "rationale": "Hotels often buy paid search, though spend is moderated by OTAs and brand/direct traffic.",
        "codes": ["55100", "55201", "55202", "55209"],
    },
    {
        "sic_group": "personal_care_wellness",
        "sic_label": "Personal care / beauty / wellness",
        "annual_ppc_ratio": 0.025,
        "rationale": "Beauty and wellness providers often convert local search demand into bookings and enquiries.",
        "codes": ["96010", "96020", "96030", "96040", "96090"],
    },
    {
        "sic_group": "specialist_construction_trades",
        "sic_label": "Specialist construction / trades",
        "annual_ppc_ratio": 0.03,
        "rationale": "Trades and installation services often buy search traffic for quote-driven local demand.",
        "codes": ["43210", "43220", "43290", "43310", "43320", "43341", "43342", "43390"],
    },
    {
        "sic_group": "software_it_consultancy",
        "sic_label": "Software / IT consultancy",
        "annual_ppc_ratio": 0.006,
        "rationale": "Software and IT consultancies are more often referral- and outbound-led than PPC-led.",
        "codes": ["62011", "62012", "62020", "62090"],
    },
    {
        "sic_group": "management_consultancy_pr",
        "sic_label": "Management consultancy / PR",
        "annual_ppc_ratio": 0.006,
        "rationale": "Consultancy and PR firms usually rely more on networks and direct sales than search acquisition.",
        "codes": ["70210", "70221", "70229"],
    },
    {
        "sic_group": "advertising_market_research",
        "sic_label": "Advertising / market research",
        "annual_ppc_ratio": 0.01,
        "rationale": "Agencies may use PPC for lead generation, but often lean on referrals, reputation, and outbound.",
        "codes": ["73110", "73200"],
    },
    {
        "sic_group": "design_photography",
        "sic_label": "Design / photography",
        "annual_ppc_ratio": 0.012,
        "rationale": "Creative services can use PPC, especially where demand is local or productized.",
        "codes": ["74100", "74201", "74202", "74209"],
    },
    {
        "sic_group": "accountancy_bookkeeping_audit",
        "sic_label": "Accountancy / bookkeeping / audit",
        "annual_ppc_ratio": 0.015,
        "rationale": "Accountancy firms often compete in search for business-owner enquiries and tax-led demand.",
        "codes": ["69201", "69202", "69203"],
    },
    {
        "sic_group": "architecture_engineering",
        "sic_label": "Architecture / engineering",
        "annual_ppc_ratio": 0.005,
        "rationale": "Architecture and engineering services are usually relationship- and tender-led rather than PPC-led.",
        "codes": ["71111", "71112", "71121", "71122"],
    },
    {
        "sic_group": "research_biotech",
        "sic_label": "R&D / biotech",
        "annual_ppc_ratio": 0.004,
        "rationale": "R&D-led businesses are generally not heavy paid-search buyers relative to turnover.",
        "codes": ["72110", "72190", "72200"],
    },
    {
        "sic_group": "business_support_services",
        "sic_label": "Business support services",
        "annual_ppc_ratio": 0.012,
        "rationale": "Business support firms can use PPC, but channel mix varies widely and is rarely pure search.",
        "codes": ["82110", "82190", "82990"],
    },
    {
        "sic_group": "educational_support",
        "sic_label": "Educational support",
        "annual_ppc_ratio": 0.022,
        "rationale": "Educational support services often target parent and student search demand directly.",
        "codes": ["85600"],
    },
    {
        "sic_group": "arts_entertainment",
        "sic_label": "Arts / entertainment",
        "annual_ppc_ratio": 0.012,
        "rationale": "Arts and entertainment operators can use PPC, though event and brand demand often dominate.",
        "codes": ["90010", "90020", "90030", "90040"],
    },
    {
        "sic_group": "sport_fitness_gyms",
        "sic_label": "Sport / fitness / gyms",
        "annual_ppc_ratio": 0.02,
        "rationale": "Fitness businesses often acquire members and bookings through local search demand.",
        "codes": ["93110", "93120", "93130", "93190"],
    },
    {
        "sic_group": "theme_parks_amusement",
        "sic_label": "Theme parks / amusement",
        "annual_ppc_ratio": 0.015,
        "rationale": "Amusement businesses can use PPC, but demand often overlaps with seasonality and organic discovery.",
        "codes": ["93210", "93290"],
    },
    {
        "sic_group": "specialist_retail",
        "sic_label": "Specialist retail",
        "annual_ppc_ratio": 0.025,
        "rationale": "Specialist retailers often buy intent-driven traffic to stores or online catalogues.",
        "codes": ["47411", "47710", "47730", "47740", "47750"],
    },
    {
        "sic_group": "transport_taxi_logistics",
        "sic_label": "Transport / taxi / logistics",
        "annual_ppc_ratio": 0.012,
        "rationale": "Transport businesses sometimes use PPC, though much demand is repeat, contractual, or platform-driven.",
        "codes": ["49100", "49311", "49319", "49320"],
    },
]

DEFAULT_PPC_RATIO_RULES: list[dict[str, Any]] = [
    {
        "sic_code": sic_code,
        "sic_label": group["sic_label"],
        "sic_group": group["sic_group"],
        "annual_ppc_ratio": group["annual_ppc_ratio"],
        "rationale": group["rationale"],
        "model_version": PPC_MODEL_VERSION,
    }
    for group in PPC_RULE_GROUPS
    for sic_code in group["codes"]
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    populate_ppc_ratio_rules(conn)
    conn.commit()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def normalize_sic_code(sic_text: str | None) -> str | None:
    if not sic_text:
        return None
    sic_text = sic_text.strip()
    if len(sic_text) >= 5 and sic_text[:5].isdigit():
        return sic_text[:5]
    return None


def populate_ppc_ratio_rules(conn: sqlite3.Connection) -> None:
    for rule in DEFAULT_PPC_RATIO_RULES:
        conn.execute(
            """
            insert into ppc_ratio_rules (
                sic_code, sic_label, sic_group, annual_ppc_ratio, rationale, model_version, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            on conflict(sic_code) do update set
                sic_label=excluded.sic_label,
                sic_group=excluded.sic_group,
                annual_ppc_ratio=excluded.annual_ppc_ratio,
                rationale=excluded.rationale,
                model_version=excluded.model_version,
                updated_at=excluded.updated_at
            """,
            (
                rule["sic_code"],
                rule["sic_label"],
                rule["sic_group"],
                rule["annual_ppc_ratio"],
                rule["rationale"],
                rule["model_version"],
                utc_now(),
            ),
        )


def refresh_company_ppc_estimate(
    conn: sqlite3.Connection,
    company_number: str,
    *,
    document_id: str | None = None,
) -> None:
    if not table_exists(conn, "leads"):
        return

    lead = conn.execute(
        "select sic_1 from leads where company_number = ?",
        (company_number,),
    ).fetchone()
    if not lead:
        return

    sic_text = lead[0]
    sic_code = normalize_sic_code(sic_text)
    if not sic_code:
        conn.execute("delete from ppc_company_estimates where company_number = ?", (company_number,))
        return

    rule = conn.execute(
        """
        select sic_label, annual_ppc_ratio, model_version
        from ppc_ratio_rules
        where sic_code = ?
        """,
        (sic_code,),
    ).fetchone()
    if not rule:
        conn.execute("delete from ppc_company_estimates where company_number = ?", (company_number,))
        return

    financial = conn.execute(
        """
        select document_id, turnover
        from financial_period_summaries
        where company_number = ? and period_type = 'current' and turnover is not null and turnover > 0
        order by id desc
        limit 1
        """,
        (company_number,),
    ).fetchone()
    if not financial:
        conn.execute("delete from ppc_company_estimates where company_number = ?", (company_number,))
        return

    effective_document_id = document_id or financial[0]
    turnover = int(financial[1])
    annual_ratio = float(rule[1])
    estimated_annual = turnover * annual_ratio
    estimated_monthly = estimated_annual / 12.0

    conn.execute(
        """
        insert into ppc_company_estimates (
            company_number, document_id, sic_code, sic_label, annual_ppc_ratio, turnover,
            estimated_annual_ppc_spend, estimated_monthly_ppc_spend, estimate_basis,
            model_version, generated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(company_number) do update set
            document_id=excluded.document_id,
            sic_code=excluded.sic_code,
            sic_label=excluded.sic_label,
            annual_ppc_ratio=excluded.annual_ppc_ratio,
            turnover=excluded.turnover,
            estimated_annual_ppc_spend=excluded.estimated_annual_ppc_spend,
            estimated_monthly_ppc_spend=excluded.estimated_monthly_ppc_spend,
            estimate_basis=excluded.estimate_basis,
            model_version=excluded.model_version,
            generated_at=excluded.generated_at
        """,
        (
            company_number,
            effective_document_id,
            sic_code,
            rule[0],
            annual_ratio,
            turnover,
            estimated_annual,
            estimated_monthly,
            "turnover * sic_1 annual PPC ratio / 12",
            rule[2],
            utc_now(),
        ),
    )


def refresh_all_ppc_estimates(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "leads"):
        return 0
    company_numbers = [
        row[0]
        for row in conn.execute(
            """
            select distinct company_number
            from financial_period_summaries
            where period_type = 'current'
            """
        ).fetchall()
    ]
    for company_number in company_numbers:
        refresh_company_ppc_estimate(conn, company_number)
    conn.commit()
    return len(company_numbers)


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
            date_of_creation, source_mode, profile_payload, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(company_number) do update set
            company_name=excluded.company_name,
            company_status=excluded.company_status,
            company_type=excluded.company_type,
            date_of_creation=excluded.date_of_creation,
            source_mode=excluded.source_mode,
            profile_payload=excluded.profile_payload,
            updated_at=excluded.updated_at
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
                company_number, document_id, period_type, turnover, gross_profit,
                operating_result, profit_after_tax, cash, net_assets, employees,
                derived_payload, raw_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(company_number, document_id, period_type) do update set
                turnover=excluded.turnover,
                gross_profit=excluded.gross_profit,
                operating_result=excluded.operating_result,
                profit_after_tax=excluded.profit_after_tax,
                cash=excluded.cash,
                net_assets=excluded.net_assets,
                employees=excluded.employees,
                derived_payload=excluded.derived_payload,
                raw_payload=excluded.raw_payload
            """,
            (
                company_number,
                document_id,
                period_type,
                raw_period.get("turnover"),
                raw_period.get("gross_profit"),
                raw_period.get("operating_result"),
                raw_period.get("profit_after_tax"),
                raw_period.get("cash"),
                raw_period.get("net_assets"),
                raw_period.get("employees"),
                json_text(derived),
                json_text(raw_period),
            ),
        )

    conn.commit()
    refresh_company_ppc_estimate(conn, company_number, document_id=document_id)
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

    ocr_financials = payload.get("ocr_financials") or {}
    for period_type, period_payload in (ocr_financials.get("by_period") or {}).items():
        conn.execute(
            """
            insert into ocr_financial_period_summaries (
                narrative_run_id, company_number, document_id, period_type, turnover,
                cost_of_sales, gross_profit, administrative_expenses, operating_result,
                profit_before_tax, tax, profit_after_tax, current_assets, cash,
                net_current_assets, net_assets, employees, raw_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(narrative_run_id, period_type) do update set
                company_number=excluded.company_number,
                document_id=excluded.document_id,
                turnover=excluded.turnover,
                cost_of_sales=excluded.cost_of_sales,
                gross_profit=excluded.gross_profit,
                administrative_expenses=excluded.administrative_expenses,
                operating_result=excluded.operating_result,
                profit_before_tax=excluded.profit_before_tax,
                tax=excluded.tax,
                profit_after_tax=excluded.profit_after_tax,
                current_assets=excluded.current_assets,
                cash=excluded.cash,
                net_current_assets=excluded.net_current_assets,
                net_assets=excluded.net_assets,
                employees=excluded.employees,
                raw_payload=excluded.raw_payload
            """,
            (
                run_id,
                company_number,
                document_id,
                period_type,
                period_payload.get("turnover"),
                period_payload.get("cost_of_sales"),
                period_payload.get("gross_profit"),
                period_payload.get("administrative_expenses"),
                period_payload.get("operating_result"),
                period_payload.get("profit_before_tax"),
                period_payload.get("tax"),
                period_payload.get("profit_after_tax"),
                period_payload.get("current_assets"),
                period_payload.get("cash"),
                period_payload.get("net_current_assets"),
                period_payload.get("net_assets"),
                period_payload.get("employees"),
                json_text(period_payload),
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
