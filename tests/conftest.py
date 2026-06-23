from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies_house_sqlite import init_db
from scripts.enrichment.ch_batch_enrich import LEADS_SCHEMA


@pytest.fixture()
def seeded_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "companies-house-test.db"
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        conn.executescript(LEADS_SCHEMA)

        conn.execute(
            """
            insert into companies (
                company_number,
                company_name,
                company_status,
                company_type,
                date_of_creation,
                source_mode,
                profile_payload,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "13406761",
                "MESH AI LTD",
                "active",
                "ltd",
                "2021-06-01",
                "api",
                '{"company_number":"13406761","company_name":"MESH AI LTD"}',
                "2026-06-22T10:00:00+00:00",
            ),
        )
        conn.execute(
            """
            insert into companies (
                company_number,
                company_name,
                company_status,
                company_type,
                date_of_creation,
                source_mode,
                profile_payload,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "22222222",
                "MESH DIGITAL LTD",
                "active",
                "ltd",
                "2019-02-11",
                "api",
                '{"company_number":"22222222","company_name":"MESH DIGITAL LTD"}',
                "2026-06-22T10:00:00+00:00",
            ),
        )

        conn.execute(
            """
            insert into leads (
                company_number,
                company_name,
                sic_1,
                incorporation_date,
                last_accounts_date,
                account_category,
                post_town,
                post_code,
                lead_score,
                score_reasons,
                status,
                xhtml_available,
                filing_date,
                filing_type,
                processed_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "13406761",
                "MESH AI LTD",
                "62020",
                "2021-06-01",
                "2025-12-31",
                "FULL",
                "London",
                "EC1A 1AA",
                82,
                "tier-3 SIC; accounts <18mo; non-shell name",
                "done",
                1,
                "2026-03-31",
                "AA",
                "2026-06-22T10:00:00+00:00",
            ),
        )
        conn.execute(
            """
            insert into leads (
                company_number,
                company_name,
                sic_1,
                incorporation_date,
                last_accounts_date,
                account_category,
                post_town,
                post_code,
                lead_score,
                score_reasons,
                status,
                xhtml_available,
                filing_date,
                filing_type,
                processed_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "22222222",
                "MESH DIGITAL LTD",
                "62020",
                "2019-02-11",
                "2025-09-30",
                "FULL",
                "Bristol",
                "BS1 4DJ",
                76,
                "tier-3 SIC; established 7.3yr",
                "pending",
                0,
                "2026-02-28",
                "AA",
                "2026-06-22T10:00:00+00:00",
            ),
        )

        conn.execute(
            """
            insert into filings (
                transaction_id,
                company_number,
                filing_date,
                category,
                type,
                description,
                action_date,
                pages,
                filing_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tx-13406761-aa",
                "13406761",
                "2026-03-31",
                "accounts",
                "AA",
                "accounts-with-accounts-type-full",
                "2025-12-31",
                12,
                '{"transaction_id":"tx-13406761-aa"}',
            ),
        )
        conn.execute(
            """
            insert into documents (
                document_id,
                transaction_id,
                company_number,
                metadata_url,
                xhtml_url,
                pdf_url,
                downloaded_xhtml_path,
                downloaded_pdf_path,
                metadata_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc-13406761-aa",
                "tx-13406761-aa",
                "13406761",
                "https://document-api.example/doc-13406761-aa",
                "https://document-api.example/doc-13406761-aa/content.xhtml",
                "https://document-api.example/doc-13406761-aa/content.pdf",
                None,
                None,
                '{"document_id":"doc-13406761-aa"}',
            ),
        )
        conn.execute(
            """
            insert into financial_period_summaries (
                company_number,
                document_id,
                period_type,
                turnover,
                gross_profit,
                operating_result,
                profit_after_tax,
                cash,
                net_assets,
                employees,
                derived_payload,
                raw_payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "13406761",
                "doc-13406761-aa",
                "current",
                1250000,
                700000,
                180000,
                150000,
                250000,
                410000,
                18,
                '{"gross_margin_pct":56.0}',
                '{"period_type":"current"}',
            ),
        )

        conn.execute(
            """
            insert into narrative_runs (
                document_id,
                company_number,
                pdf_path,
                text_source,
                ocr_requested,
                ocr_used,
                ocr_engine_used,
                text_quality_payload,
                raw_payload,
                created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc-13406761-aa",
                "13406761",
                None,
                "xhtml_visible_text",
                0,
                0,
                None,
                '{"quality":"high"}',
                '{"document_id":"doc-13406761-aa"}',
                "2026-06-22T10:00:00+00:00",
            ),
        )
        narrative_run_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            insert into narrative_sections (
                narrative_run_id,
                section_key,
                section_title,
                page_number,
                section_text,
                section_payload
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                narrative_run_id,
                "strategic_report",
                "Strategic report",
                3,
                "Demand remained strong across enterprise AI services and support retainers.",
                '{"section_key":"strategic_report"}',
            ),
        )

        conn.execute(
            """
            insert into ppc_company_estimates (
                company_number,
                document_id,
                sic_code,
                sic_label,
                annual_ppc_ratio,
                turnover,
                estimated_annual_ppc_spend,
                estimated_monthly_ppc_spend,
                estimate_basis,
                model_version,
                generated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "13406761",
                "doc-13406761-aa",
                "62020",
                "Information technology consultancy activities",
                0.02,
                1250000,
                25000.0,
                2083.33,
                "sic_ratio_times_turnover",
                "test-v1",
                "2026-06-22T10:00:00+00:00",
            ),
        )

        conn.execute(
            """
            insert into website_investigations (
                company_number,
                source_label,
                source_file,
                investigation_type,
                status,
                sic_1,
                sic_label,
                account_category,
                turnover,
                estimated_monthly_ppc_spend,
                search_queries,
                search_results_count,
                candidate_count,
                chosen_result_score,
                chosen_result_title,
                chosen_result_snippet,
                chosen_result_domain,
                chosen_result_url,
                final_url,
                final_domain,
                page_title,
                meta_description,
                og_description,
                business_model,
                business_description,
                raw_payload,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "13406761",
                "pilot-test",
                "evidence.json",
                "browser_pilot",
                "ok",
                "62020",
                "Information technology consultancy activities",
                "FULL",
                1250000,
                2083.33,
                '["mesh ai ltd"]',
                5,
                2,
                0.92,
                "MESH AI",
                "Enterprise AI delivery partner",
                "mesh.ai",
                "https://mesh.ai",
                "https://mesh.ai",
                "mesh.ai",
                "MESH AI | Enterprise AI",
                "Enterprise AI delivery partner",
                None,
                "B2B service",
                "Enterprise AI delivery and consulting",
                '{"company_number":"13406761"}',
                "2026-06-22T10:00:00+00:00",
                "2026-06-22T10:00:00+00:00",
            ),
        )
        investigation_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            insert into website_signals (
                investigation_id,
                signal_key,
                signal_value_type,
                signal_bool,
                signal_int,
                signal_real,
                signal_text,
                source_scope,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                investigation_id,
                "ppc_fit_score",
                "real",
                None,
                None,
                74.5,
                None,
                "derived",
                "2026-06-22T10:00:00+00:00",
                "2026-06-22T10:00:00+00:00",
            ),
        )

        conn.commit()
    finally:
        conn.close()

    return db_path
