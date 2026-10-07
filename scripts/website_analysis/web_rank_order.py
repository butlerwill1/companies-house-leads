#!/usr/bin/env python3
"""Queue order for the web stage: which screen-passing companies get the
limited free search allowance first. Free; reads only SQLite.

This is not the lead ranking (that is a separate stage, scored by precision
at k on the friend's outcomes). It only decides who is looked up first, by a
rule simple enough to state in one line (docs/WEB_STAGE.md):

    core turnover band first (default GBP 1m to 50m), then above it, then
    below it; within a band, profitable before loss-making, growing before
    not, then larger turnover first.

Above the band, companies usually run marketing in house or through an
agency; below it, a paid-search budget is small. Both stay in the queue, later.
Companies Gate A flags as duplicates of another entity are left out.

The order is frozen to `logs/web/queue-order.json` so a later history
backfill cannot reshuffle a run half way through; `--refresh` rebuilds it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from scripts.search_screen_classifier.search_screen_policy import PROMPT_VERSION as SCREEN_VERSION

QUEUE_FILE = Path("logs/web/queue-order.json")
CORE_LOW = 1_000_000
CORE_HIGH = 50_000_000


def passing_companies(conn: sqlite3.Connection, screen_version: str = SCREEN_VERSION) -> list[str]:
    rows = conn.execute(
        "select distinct company_number from company_search_screen where prompt_version = ? and passes = 1 "
        "order by company_number", (screen_version,)).fetchall()
    return [number for (number,) in rows]


def duplicates(conn: sqlite3.Connection) -> set[str]:
    """Companies Gate A flags as the same business consolidated twice
    (`company_signals.duplicate_of`): the entity they duplicate is looked up
    instead, since both would resolve to one website."""
    rows = conn.execute("select company_number from company_signals where signal_key = 'duplicate_of' "
                        "and coalesce(signal_text, '') != ''").fetchall()
    return {number for (number,) in rows}


def latest_financials(conn: sqlite3.Connection, numbers: list[str]) -> dict[str, dict[str, Any]]:
    """Newest year with a turnover, and the year before it, from company_financial_history."""
    wanted = set(numbers)
    by_company: dict[str, list[tuple[int, float | None, float | None]]] = {}
    for number, year, turnover, profit in conn.execute(
            "select company_number, financial_year, turnover, profit_after_tax from company_financial_history "
            "where turnover is not null order by company_number, financial_year desc"):
        if number in wanted:
            by_company.setdefault(number, []).append((year, turnover, profit))
    out: dict[str, dict[str, Any]] = {}
    for number, years in by_company.items():
        year, turnover, profit = years[0]
        previous = next((t for y, t, _ in years[1:] if y == year - 1), None)
        out[number] = {"financial_year": year, "turnover": turnover, "profit_after_tax": profit,
                       "previous_turnover": previous}
    return out


def band(turnover: float | None, low: float = CORE_LOW, high: float = CORE_HIGH) -> int:
    """0 core, 1 above the core band, 2 below it or unknown."""
    if turnover is None:
        return 2
    if low <= turnover <= high:
        return 0
    return 1 if turnover > high else 2


def sort_key(row: dict[str, Any], low: float = CORE_LOW, high: float = CORE_HIGH) -> tuple:
    profitable = (row.get("profit_after_tax") or 0) > 0
    previous = row.get("previous_turnover")
    growing = previous is not None and row.get("turnover") is not None and row["turnover"] > previous
    return (band(row.get("turnover"), low, high), not profitable, not growing, -(row.get("turnover") or 0),
            row["company_number"])


def build_queue(conn: sqlite3.Connection, *, low: float = CORE_LOW, high: float = CORE_HIGH,
                screen_version: str = SCREEN_VERSION) -> list[dict[str, Any]]:
    skip = duplicates(conn)
    numbers = [number for number in passing_companies(conn, screen_version) if number not in skip]
    figures = latest_financials(conn, numbers)
    rows = [{"company_number": number, **figures.get(number, {})} for number in numbers]
    rows.sort(key=lambda row: sort_key(row, low, high))
    for position, row in enumerate(rows, 1):
        row["position"] = position
        row["band"] = ("core", "above", "below")[band(row.get("turnover"), low, high)]
    return rows


def load_queue(conn: sqlite3.Connection, *, refresh: bool = False, path: Path = QUEUE_FILE,
               low: float = CORE_LOW, high: float = CORE_HIGH) -> list[dict[str, Any]]:
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))["queue"]
    queue = build_queue(conn, low=low, high=high)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"screen_version": SCREEN_VERSION, "core_low": low, "core_high": high,
                                "queue": queue}, indent=1), encoding="utf-8")
    return queue
