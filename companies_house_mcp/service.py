from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class CompaniesHouseDataService:
    """Read-only query service for Companies House lead intelligence."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def search_leads(
        self,
        *,
        query: str | None = None,
        min_score: int | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        clauses: list[str] = []
        params: list[Any] = []

        if query:
            like_query = f"%{query.lower()}%"
            clauses.append("(lower(company_name) like ? or lower(company_number) like ?)")
            params.extend([like_query, like_query])

        if min_score is not None:
            clauses.append("lead_score >= ?")
            params.append(min_score)

        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            params.extend(statuses)

        where_sql = f"where {' and '.join(clauses)}" if clauses else ""
        sql = f"""
            select
                company_number,
                company_name,
                sic_1,
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
            from leads
            {where_sql}
            order by lead_score desc, company_name, company_number
            limit ?
        """
        params.append(limit)

        with self._connect() as conn:
            return self._fetch_all(conn, sql, params)

    def get_company_snapshot(self, company_number: str) -> dict[str, Any]:
        with self._connect() as conn:
            company = self._fetch_one(
                conn,
                """
                select
                    company_number,
                    company_name,
                    company_status,
                    company_type,
                    date_of_creation,
                    source_mode,
                    updated_at
                from companies
                where company_number = ?
                """,
                [company_number],
            )
            if company is None:
                raise LookupError(f"Company not found: {company_number}")

            lead = self._fetch_one(conn, "select * from leads where company_number = ?", [company_number])
            latest_filing = self._fetch_one(
                conn,
                """
                select
                    transaction_id,
                    company_number,
                    filing_date,
                    category,
                    type,
                    description,
                    action_date,
                    pages
                from filings
                where company_number = ?
                order by filing_date desc, transaction_id desc
                limit 1
                """,
                [company_number],
            )
            latest_document = self._fetch_one(
                conn,
                """
                select
                    document_id,
                    transaction_id,
                    company_number,
                    metadata_url,
                    xhtml_url,
                    pdf_url,
                    downloaded_xhtml_path,
                    downloaded_pdf_path
                from documents
                where company_number = ?
                order by rowid desc
                limit 1
                """,
                [company_number],
            )
            financial_rows = self._fetch_all(
                conn,
                """
                select
                    s.period_type, s.financial_year, s.turnover, s.gross_profit,
                    s.operating_result, s.profit_after_tax, s.cash, s.net_assets, s.employees,
                    s.currency_code, s.currency_source, s.period_end_on, s.currency_validation_status,
                    s.turnover_reported_value, s.gross_profit_reported_value,
                    s.operating_result_reported_value, s.profit_after_tax_reported_value,
                    s.cash_reported_value, s.net_assets_reported_value,
                    c.conversion_status, c.conversion_basis, c.fx_rate_id,
                    c.turnover_gbp_pence, c.gross_profit_gbp_pence, c.operating_result_gbp_pence,
                    c.profit_after_tax_gbp_pence, c.cash_gbp_pence, c.net_assets_gbp_pence,
                    r.observation_on as fx_observation_on, r.gbp_per_source_unit, r.bank_series_id,
                    r.source_url as fx_source_url
                from financial_period_summaries s
                left join financial_period_conversions c on c.financial_summary_id=s.id
                left join fx_rates r on r.id=c.fx_rate_id
                where s.company_number = ?
                order by s.period_type
                """,
                [company_number],
            )
            return {
                "company": company,
                "lead": lead,
                "latest_filing": latest_filing,
                "latest_document": latest_document,
                "financials": {row["period_type"]: row for row in financial_rows},
                "website_investigation": self.get_website_investigation(
                    company_number, conn=conn
                ),
            }

    def search_narrative_sections(
        self,
        *,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        like_query = f"%{query.lower()}%"
        with self._connect() as conn:
            return self._fetch_all(
                conn,
                """
                select
                    nr.company_number,
                    c.company_name,
                    nr.document_id,
                    ns.section_key,
                    ns.section_title,
                    ns.page_number,
                    ns.section_text
                from narrative_sections ns
                join narrative_runs nr on nr.id = ns.narrative_run_id
                left join companies c on c.company_number = nr.company_number
                where
                    lower(coalesce(ns.section_text, '')) like ?
                    or lower(coalesce(ns.section_title, '')) like ?
                    or lower(ns.section_key) like ?
                order by nr.company_number, ns.page_number, ns.id
                limit ?
                """,
                [like_query, like_query, like_query, limit],
            )

    def get_website_investigation(
        self,
        company_number: str,
        *,
        source_label: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["company_number = ?"]
        params: list[Any] = [company_number]
        if source_label:
            clauses.append("source_label = ?")
            params.append(source_label)

        sql = f"""
            select *
            from website_investigation_metric_view
            where {' and '.join(clauses)}
            order by updated_at desc, investigation_id desc
            limit 1
        """

        if conn is not None:
            return self._fetch_one(conn, sql, params)
        with self._connect() as owned_conn:
            return self._fetch_one(owned_conn, sql, params)

    def get_lead_pipeline_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            return {
                "lead_counts": {
                    "total": self._fetch_scalar(conn, "select count(*) from leads"),
                    "by_status": self._fetch_counts(
                        conn,
                        "select status, count(*) from leads group by status order by status",
                    ),
                    "by_account_category": self._fetch_counts(
                        conn,
                        """
                        select account_category, count(*)
                        from leads
                        group by account_category
                        order by account_category
                        """,
                    ),
                },
                "enrichment_counts": {
                    "companies": self._fetch_scalar(conn, "select count(*) from companies"),
                    "filings": self._fetch_scalar(conn, "select count(*) from filings"),
                    "documents": self._fetch_scalar(conn, "select count(*) from documents"),
                    "financial_period_summaries": self._fetch_scalar(
                        conn,
                        "select count(*) from financial_period_summaries",
                    ),
                    "website_investigations": self._fetch_scalar(
                        conn,
                        "select count(*) from website_investigations",
                    ),
                },
                "text_counts": {
                    "narrative_runs": self._fetch_scalar(conn, "select count(*) from narrative_runs"),
                    "narrative_sections": self._fetch_scalar(conn, "select count(*) from narrative_sections"),
                    "performance_statements": self._fetch_scalar(
                        conn,
                        "select count(*) from performance_statements",
                    ),
                },
            }

    def find_unenriched_high_score_leads(
        self,
        *,
        min_score: int = 80,
        account_categories: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        target_statuses = statuses or ["pending", "error"]
        clauses = ["lead_score >= ?"]
        params: list[Any] = [min_score]

        if target_statuses:
            placeholders = ", ".join("?" for _ in target_statuses)
            clauses.append(f"status in ({placeholders})")
            params.extend(target_statuses)

        if account_categories:
            placeholders = ", ".join("?" for _ in account_categories)
            clauses.append(f"account_category in ({placeholders})")
            params.extend(account_categories)

        sql = f"""
            select
                company_number,
                company_name,
                sic_1,
                account_category,
                post_town,
                post_code,
                lead_score,
                score_reasons,
                status,
                xhtml_available,
                filing_date,
                filing_type,
                error_message,
                processed_at
            from leads
            where {' and '.join(clauses)}
            order by lead_score desc, company_name, company_number
            limit ?
        """
        params.append(limit)

        with self._connect() as conn:
            return self._fetch_all(conn, sql, params)

    def explain_lead_score(self, company_number: str) -> dict[str, Any]:
        with self._connect() as conn:
            lead = self._fetch_one(conn, "select * from leads where company_number = ?", [company_number])
            if lead is None:
                raise LookupError(f"Lead not found: {company_number}")

            financials = self._fetch_one(
                conn,
                """
                select *
                from financial_period_summaries
                where company_number = ? and period_type = 'current'
                limit 1
                """,
                [company_number],
            )
            website = self.get_website_investigation(company_number, conn=conn)

            return {
                "company_number": company_number,
                "company_name": lead["company_name"],
                "lead_score": lead["lead_score"],
                "score_reasons": self._split_score_reasons(lead.get("score_reasons")),
                "lead": lead,
                "financials": financials,
                "website_investigation": website,
                "data_flags": {
                    "has_financials": financials is not None,
                    "has_website_investigation": website is not None,
                },
            }

    def compare_companies(self, company_numbers: list[str]) -> list[dict[str, Any]]:
        if not company_numbers:
            return []
        company_numbers = company_numbers[:100]
        placeholders = ", ".join("?" for _ in company_numbers)
        with self._connect() as conn:
            return self._fetch_all(
                conn,
                f"""
                select
                    l.company_number,
                    coalesce(l.company_name, c.company_name) as company_name,
                    l.lead_score,
                    l.status,
                    l.sic_1,
                    l.account_category,
                    l.post_town,
                    f.turnover,
                    f.net_assets,
                    f.employees,
                    g.sic_label,
                    g.sic_group,
                    w.final_domain,
                    w.ppc_fit_score,
                    w.business_model
                from leads l
                left join companies c on c.company_number = l.company_number
                left join financial_period_summaries f
                    on f.company_number = l.company_number and f.period_type = 'current'
                left join sic_groups g on g.sic_code = substr(l.sic_1, 1, 5)
                left join website_investigation_metric_view w on w.company_number = l.company_number
                where l.company_number in ({placeholders})
                order by l.lead_score desc, l.company_number
                """,
                list(company_numbers),
            )

    def search_performance_statements(
        self,
        *,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        like_query = f"%{query.lower()}%"
        with self._connect() as conn:
            return self._fetch_all(
                conn,
                """
                select
                    nr.company_number,
                    c.company_name,
                    nr.document_id,
                    ps.page_number,
                    ps.statement_text
                from performance_statements ps
                join narrative_runs nr on nr.id = ps.narrative_run_id
                left join companies c on c.company_number = nr.company_number
                where lower(ps.statement_text) like ?
                order by nr.company_number, ps.page_number, ps.id
                limit ?
                """,
                [like_query, limit],
            )

    def get_enrichment_errors(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        with self._connect() as conn:
            return self._fetch_all(
                conn,
                """
                select
                    company_number,
                    company_name,
                    sic_1,
                    account_category,
                    lead_score,
                    status,
                    error_message,
                    processed_at
                from leads
                where status = 'error'
                order by processed_at desc, lead_score desc, company_number
                limit ?
                """,
                [limit],
            )

    def find_website_signal_leads(
        self,
        *,
        min_ppc_fit_score: float = 0.0,
        business_model: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        clauses = ["coalesce(ppc_fit_score, 0) >= ?"]
        params: list[Any] = [min_ppc_fit_score]
        if business_model:
            clauses.append("lower(business_model) = lower(?)")
            params.append(business_model)

        sql = f"""
            select
                company_number,
                source_label,
                status,
                account_category,
                turnover,
                estimated_monthly_ppc_spend,
                business_model,
                business_description,
                final_domain,
                final_url,
                page_title,
                ppc_fit_score,
                ecommerce_signal_score,
                lead_generation_signal_score,
                b2b_service_signal_score
            from website_investigation_metric_view
            where {' and '.join(clauses)}
            order by ppc_fit_score desc, estimated_monthly_ppc_spend desc, company_number
            limit ?
        """
        params.append(limit)

        with self._connect() as conn:
            return self._fetch_all(conn, sql, params)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        return max(1, min(int(limit), 100))

    @staticmethod
    def _split_score_reasons(value: str | None) -> list[str]:
        if not value:
            return []
        reasons = value.replace("|", ";").split(";")
        return [reason.strip() for reason in reasons if reason.strip()]

    @staticmethod
    def _fetch_scalar(conn: sqlite3.Connection, sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    @staticmethod
    def _fetch_counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
        return {
            str(key): int(count)
            for key, count in conn.execute(sql).fetchall()
            if key is not None
        }

    @staticmethod
    def _fetch_one(
        conn: sqlite3.Connection,
        sql: str,
        params: list[Any],
    ) -> dict[str, Any] | None:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _fetch_all(
        conn: sqlite3.Connection,
        sql: str,
        params: list[Any],
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
