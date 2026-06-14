#!/usr/bin/env python3
"""Store Companies House extraction outputs in a local SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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

create index if not exists idx_filings_company_number on filings(company_number);
create index if not exists idx_documents_company_number on documents(company_number);
create index if not exists idx_financial_company_number on financial_period_summaries(company_number);
create index if not exists idx_narrative_company_number on narrative_runs(company_number);
create index if not exists idx_ocr_financial_company_number on ocr_financial_period_summaries(company_number);
"""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


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
