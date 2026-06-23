#!/usr/bin/env python3
"""
Snapshot overnight enrichment progress from SQLite and JSONL logs.

This monitor is meant to be run periodically while
`scripts.enrichment.ch_overnight_enrich` is active. It reads the live SQLite
status counts, tails the JSONL worker log, emits a compact progress snapshot,
and can send email alerts when progress stalls or error rates look suspicious.

Usage:
    python -m scripts.enrichment.ch_monitor_progress --db companies-house.db --json-log logs/overnight.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import smtplib
from collections import Counter
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def parse_jsonl_tail(path: Path, max_lines: int = 5000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    tail = lines[-max_lines:]
    events: list[dict[str, Any]] = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def db_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "leads_total": conn.execute("select count(*) from leads").fetchone()[0],
        "pending": conn.execute("select count(*) from leads where status='pending'").fetchone()[0],
        "done": conn.execute("select count(*) from leads where status='done'").fetchone()[0],
        "no_xhtml": conn.execute("select count(*) from leads where status='no_xhtml'").fetchone()[0],
        "error": conn.execute("select count(*) from leads where status='error'").fetchone()[0],
        "pending_full_group": conn.execute(
            "select count(*) from leads where status='pending' and account_category in ('FULL','GROUP')"
        ).fetchone()[0],
        "turnover_companies": conn.execute(
            "select count(distinct company_number) from financial_period_summaries where turnover is not null"
        ).fetchone()[0],
        "profit_companies": conn.execute(
            "select count(distinct company_number) from financial_period_summaries where profit_after_tax is not null"
        ).fetchone()[0],
        "turnover_and_profit_companies": conn.execute(
            """
            select count(distinct company_number)
            from financial_period_summaries
            group by company_number
            having max(turnover is not null)=1 and max(profit_after_tax is not null)=1
            """
        ).fetchall().__len__(),
        "latest_processed_at": conn.execute(
            "select max(processed_at) from leads where processed_at is not null"
        ).fetchone()[0],
    }


def log_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    counter = Counter()
    last_company_done: dict[str, Any] | None = None
    last_idle: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None

    for event in events:
        event_type = event.get("event")
        if event_type:
            counter[event_type] += 1
        if event_type == "company_done":
            last_company_done = event
            status = event.get("status")
            if status:
                counter[f"company_status:{status}"] += 1
        elif event_type == "idle_sleep":
            last_idle = event
        elif event_type == "company_error":
            last_error = event

    return {
        "events_scanned": len(events),
        "event_counts": dict(counter),
        "last_company_done": last_company_done,
        "last_idle_sleep": last_idle,
        "last_error": last_error,
    }


def load_previous_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_delta(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {}
    delta: dict[str, Any] = {}
    prev_db = previous.get("db") or {}
    current_db = current.get("db") or {}
    for key in (
        "pending",
        "done",
        "no_xhtml",
        "error",
        "turnover_companies",
        "profit_companies",
        "turnover_and_profit_companies",
    ):
        if key in prev_db and key in current_db:
            delta[key] = current_db[key] - prev_db[key]
    return delta


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_alerts(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None,
    state: dict[str, Any],
    *,
    stall_minutes: int,
    no_financial_hours: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    db = snapshot.get("db") or {}
    delta = snapshot.get("delta_since_last_snapshot") or {}
    now = parse_iso(snapshot.get("ts")) or datetime.now(timezone.utc)
    latest_processed = parse_iso(db.get("latest_processed_at"))
    pending_full_group = int(db.get("pending_full_group") or 0)

    state = dict(state)
    state.setdefault("no_financial_streak", 0)
    state.setdefault("completion_sent", False)
    state.setdefault("stall_sent_for_ts", "")
    state.setdefault("last_error_count", 0)

    if delta.get("turnover_companies", 0) > 0 or delta.get("profit_companies", 0) > 0 or delta.get("turnover_and_profit_companies", 0) > 0:
        state["no_financial_streak"] = 0
    else:
        if previous is not None:
            state["no_financial_streak"] = int(state.get("no_financial_streak", 0)) + 1

    if latest_processed and pending_full_group > 0:
        stall_cutoff = now - timedelta(minutes=stall_minutes)
        if latest_processed < stall_cutoff and state.get("stall_sent_for_ts") != db.get("latest_processed_at"):
            alerts.append(
                {
                    "level": "error",
                    "kind": "stalled_processing",
                    "message": f"No company has been processed since {db.get('latest_processed_at')} while {pending_full_group} FULL/GROUP leads remain pending.",
                }
            )
            state["stall_sent_for_ts"] = db.get("latest_processed_at")

    if pending_full_group > 0 and int(state.get("no_financial_streak", 0)) >= no_financial_hours:
        alerts.append(
            {
                "level": "warning",
                "kind": "no_new_financial_rows",
                "message": f"No new turnover/profit companies have been added for {state['no_financial_streak']} hourly snapshots while the enrich run is still active.",
            }
        )
        state["no_financial_streak"] = 0

    error_count = int(db.get("error") or 0)
    if previous is not None and error_count > int(state.get("last_error_count", 0)):
        alerts.append(
            {
                "level": "warning",
                "kind": "new_errors",
                "message": f"Lead rows with status=error increased from {state.get('last_error_count', 0)} to {error_count}.",
            }
        )
    state["last_error_count"] = error_count

    if pending_full_group == 0 and not state.get("completion_sent"):
        alerts.append(
            {
                "level": "info",
                "kind": "full_group_complete",
                "message": "The FULL/GROUP pending queue has reached zero.",
            }
        )
        state["completion_sent"] = True

    return alerts, state


def send_email_alert(env: dict[str, str], subject: str, body: str) -> bool:
    smtp_host = env.get("ALERT_SMTP_HOST")
    smtp_port = int(env.get("ALERT_SMTP_PORT", "587"))
    smtp_user = env.get("ALERT_SMTP_USER")
    smtp_password = env.get("ALERT_SMTP_PASSWORD")
    email_to = env.get("ALERT_EMAIL_TO")
    email_from = env.get("ALERT_EMAIL_FROM") or smtp_user
    if not all([smtp_host, smtp_user, smtp_password, email_to, email_from]):
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls(context=context)
        server.login(smtp_user, smtp_password)
        server.send_message(message)
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Snapshot overnight enrichment progress.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--json-log", required=True, help="JSONL activity log path.")
    parser.add_argument("--output-jsonl", required=True, help="Snapshot JSONL output path.")
    parser.add_argument("--alerts-jsonl", help="Optional JSONL file for raised alerts.")
    parser.add_argument("--state-json", help="Optional JSON state file for alert tracking.")
    parser.add_argument("--stall-minutes", type=int, default=90, help="Alert if processing stalls longer than this.")
    parser.add_argument("--no-financial-hours", type=int, default=4, help="Alert if no new financial rows appear for this many hourly snapshots.")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    json_log_path = Path(args.json_log)
    output_path = Path(args.output_jsonl)
    alerts_path = Path(args.alerts_jsonl) if args.alerts_jsonl else None
    state_path = Path(args.state_json) if args.state_json else None
    env = load_dotenv(Path(".env"))

    conn = sqlite3.connect(db_path)
    try:
        db = db_snapshot(conn)
    finally:
        conn.close()

    events = parse_jsonl_tail(json_log_path)
    log = log_snapshot(events)

    previous = load_previous_snapshot(output_path)
    snapshot = {
        "ts": utc_now(),
        "db": db,
        "log": log,
    }
    delta = build_delta(snapshot, previous)
    if delta:
        snapshot["delta_since_last_snapshot"] = delta

    state = load_json_file(state_path) if state_path else {}
    alerts, next_state = build_alerts(
        snapshot,
        previous,
        state,
        stall_minutes=args.stall_minutes,
        no_financial_hours=args.no_financial_hours,
    )
    if alerts:
        snapshot["alerts"] = alerts

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=True) + "\n")

    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(next_state, ensure_ascii=True, indent=2), encoding="utf-8")

    if alerts_path and alerts:
        alerts_path.parent.mkdir(parents=True, exist_ok=True)
        with alerts_path.open("a", encoding="utf-8") as handle:
            for alert in alerts:
                alert_payload = {
                    "ts": snapshot["ts"],
                    **alert,
                    "db": snapshot["db"],
                }
                handle.write(json.dumps(alert_payload, ensure_ascii=True) + "\n")

    if alerts:
        body_lines = [
            f"{alert['level'].upper()}: {alert['kind']}",
            alert["message"],
            "",
            f"Pending FULL/GROUP: {snapshot['db']['pending_full_group']}",
            f"Done: {snapshot['db']['done']}",
            f"Turnover companies: {snapshot['db']['turnover_companies']}",
            f"Profit companies: {snapshot['db']['profit_companies']}",
            f"Latest processed_at: {snapshot['db']['latest_processed_at']}",
        ]
        send_email_alert(
            env,
            subject="Companies House Leads Monitor Alert",
            body="\n".join(body_lines),
        )

    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
