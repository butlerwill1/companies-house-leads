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
                    period_type,
                    turnover,
                    gross_profit,
                    operating_result,
                    profit_after_tax,
                    cash,
                    net_assets,
                    employees
                from financial_period_summaries
                where company_number = ?
                order by period_type
                """,
                [company_number],
            )
            ppc_estimate = self._fetch_one(
                conn,
                """
                select
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
                from ppc_company_estimates
                where company_number = ?
                """,
                [company_number],
            )

            return {
                "company": company,
                "lead": lead,
                "latest_filing": latest_filing,
                "latest_document": latest_document,
                "financials": {row["period_type"]: row for row in financial_rows},
                "ppc_estimate": ppc_estimate,
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

    def get_top_ppc_candidates(
        self,
        *,
        min_monthly: float = 0.0,
        max_monthly: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        clauses = ["p.estimated_monthly_ppc_spend >= ?"]
        params: list[Any] = [min_monthly]
        if max_monthly is not None:
            clauses.append("p.estimated_monthly_ppc_spend <= ?")
            params.append(max_monthly)

        sql = f"""
            select
                p.company_number,
                coalesce(l.company_name, c.company_name) as company_name,
                l.lead_score,
                l.account_category,
                p.document_id,
                p.sic_code,
                p.sic_label,
                p.turnover,
                p.annual_ppc_ratio,
                p.estimated_annual_ppc_spend,
                p.estimated_monthly_ppc_spend,
                p.estimate_basis,
                p.model_version,
                p.generated_at
            from ppc_company_estimates p
            left join leads l on l.company_number = p.company_number
            left join companies c on c.company_number = p.company_number
            where {' and '.join(clauses)}
            order by p.estimated_monthly_ppc_spend desc, p.company_number
            limit ?
        """
        params.append(limit)

        with self._connect() as conn:
            return self._fetch_all(conn, sql, params)

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        return max(1, min(int(limit), 100))

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
