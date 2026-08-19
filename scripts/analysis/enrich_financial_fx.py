#!/usr/bin/env python3
"""Import immutable Bank of England daily spots and derive GBP financial values.

The Bank publishes currency units per GBP.  This command stores both that
published value and its Decimal inverse (GBP per source unit).  Rates are
indicative, not an official settlement rate.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.companies_house_sqlite import init_db

BOE_SERIES = {"USD": "XUDLGBD", "EUR": "XUDLERD"}
BOE_URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
MONEY_COLUMNS = ("turnover", "gross_profit", "operating_result", "profit_after_tax", "cash", "net_assets")


def boe_csv_url(series: str, start: date, end: date) -> str:
    return BOE_URL + "?" + urlencode({"csv.x": "yes", "Datefrom": start.strftime("%d/%b/%Y"), "Dateto": end.strftime("%d/%b/%Y"), "SeriesCodes": series, "CSVF": "TN", "UsingCodes": "Y", "VPD": "Y", "VFD": "N"})


def parse_boe_csv(payload: bytes, series: str) -> list[tuple[str, Decimal]]:
    rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    result: list[tuple[str, Decimal]] = []
    for row in rows:
        raw_date = row.get("DATE") or row.get("Date")
        raw_rate = row.get(series) or next((v for k, v in row.items() if k and series in k), None)
        if not raw_date or not raw_rate:
            continue
        try:
            observed = datetime.strptime(raw_date.strip(), "%d %b %Y").date().isoformat()
            rate = Decimal(raw_rate.replace(",", "").strip())
            if rate > 0:
                result.append((observed, rate))
        except (ValueError, InvalidOperation):
            continue
    return result


def import_rates(conn: sqlite3.Connection, currency: str, series: str, payload: bytes, source_url: str) -> int:
    payload_hash = hashlib.sha256(payload).hexdigest()
    count = 0
    for observed, raw_rate in parse_boe_csv(payload, series):
        cursor = conn.execute(
            """insert into fx_rates (source_currency_code,target_currency_code,observation_on,
               raw_published_rate,gbp_per_source_unit,bank_series_id,retrieved_at,source_url,payload_hash)
               values (?, 'GBP', ?, ?, ?, ?, ?, ?, ?) on conflict do nothing""",
            (currency, observed, str(raw_rate), str(Decimal(1) / raw_rate), series,
             datetime.now(timezone.utc).replace(microsecond=0).isoformat(), source_url, payload_hash),
        )
        count += cursor.rowcount
    return count


def _pence(value: str | None, rate: Decimal) -> int | None:
    if value is None:
        return None
    return int((Decimal(value) * rate * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def convert_pending(conn: sqlite3.Connection) -> int:
    summaries = conn.execute(
        """select * from financial_period_summaries where currency_validation_status='valid'"""
    ).fetchall()
    converted = 0
    for summary in summaries:
        row = dict(summary)
        currency = row.get("currency_code")
        status, rate_id, rate, basis = "pending", None, None, "missing_period_end"
        if currency == "GBP":
            status, rate, basis = "converted", Decimal(1), "GBP identity conversion; no external rate"
        elif not row.get("period_end_on"):
            pass
        else:
            fx = conn.execute(
                """select id, gbp_per_source_unit, observation_on from fx_rates
                   where source_currency_code=? and target_currency_code='GBP'
                     and observation_on <= ? and observation_on >= date(?, '-10 days')
                   order by observation_on desc limit 1""",
                (currency, row["period_end_on"], row["period_end_on"]),
            ).fetchone()
            if fx:
                status, rate_id, rate, basis = "converted", fx[0], Decimal(fx[1]), f"BoE nearest prior published rate on {fx[2]}; indicative"
            else:
                basis = "no Bank of England preceding rate within ten calendar days"
        amounts = [_pence(row.get(f"{metric}_reported_value") or (str(row.get(metric)) if row.get(metric) is not None else None), rate) if rate else None for metric in MONEY_COLUMNS]
        conn.execute(
            """insert into financial_period_conversions (financial_summary_id,fx_rate_id,conversion_status,conversion_basis,converted_at,
               turnover_gbp_pence,gross_profit_gbp_pence,operating_result_gbp_pence,profit_after_tax_gbp_pence,cash_gbp_pence,net_assets_gbp_pence)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               on conflict(financial_summary_id) do update set fx_rate_id=excluded.fx_rate_id, conversion_status=excluded.conversion_status,
               conversion_basis=excluded.conversion_basis, converted_at=excluded.converted_at, turnover_gbp_pence=excluded.turnover_gbp_pence,
               gross_profit_gbp_pence=excluded.gross_profit_gbp_pence, operating_result_gbp_pence=excluded.operating_result_gbp_pence,
               profit_after_tax_gbp_pence=excluded.profit_after_tax_gbp_pence, cash_gbp_pence=excluded.cash_gbp_pence, net_assets_gbp_pence=excluded.net_assets_gbp_pence""",
            (row["id"], rate_id, status, basis, datetime.now(timezone.utc).replace(microsecond=0).isoformat(), *amounts),
        )
        if status == "converted":
            converted += 1
    conn.commit()
    return converted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import BoE indicative FX rates and derive GBP financial values.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--currency", action="append", default=[])
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--series-code", help="Use with one --currency for a BoE series not in the built-in map.")
    parser.add_argument("--convert-only", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        imported = 0
        if not args.convert_only:
            if not args.start or not args.end:
                parser.error("--from and --to are required unless --convert-only is used")
            currencies = [item.upper() for item in args.currency]
            for currency in currencies:
                series = args.series_code or BOE_SERIES.get(currency)
                if not series:
                    continue
                url = boe_csv_url(series, date.fromisoformat(args.start), date.fromisoformat(args.end))
                request = Request(url, headers={"User-Agent": "companies-house-leads/1.0"})
                with urlopen(request, timeout=30) as response:
                    imported += import_rates(conn, currency, series, response.read(), url)
            conn.commit()
        print({"rates_imported": imported, "periods_converted": convert_pending(conn)})
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
