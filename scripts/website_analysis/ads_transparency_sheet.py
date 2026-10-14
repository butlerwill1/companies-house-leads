"""Prepare bounded native-Sheets updates for an already published lead scan.

No network calls. The Drive connector applies the values to the verified
sheet IDs in publishing.json; existing formats, formulas and filters remain.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def date_value(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp() / 86400 + 25569


def build(output: Path):
    companies = json.loads((output / "companies.json").read_text(encoding="utf-8"))
    domains = {r["domain"]: r for r in (json.loads(line) for line in (output / "domains.jsonl").read_text(encoding="utf-8").splitlines())}
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    def label(value):
        return "Yes" if value is True else "No" if value is False else "Unknown"
    rows = []
    for company in companies:
        raw = domains.get(company["domain"], {})
        ads = raw.get("ads", {})
        reason = {"no_chosen_website": "No chosen website", "invalid_stored_domain": "Invalid stored domain"}.get(company["skip_reason"])
        reason = reason or company["error"] or ("Not yet checked" if company["lookup_status"] == "unchecked" else "")
        rows.append([company["company_number"], company["name"], company["screen_answer"] or "Unknown",
                     company["domain"] or "", company["identity_tier"] or "", label(company["has_ads"]),
                     label(company["recent_ads"]), company["ads_seen_lower_bound"], date_value(company["last_shown"]),
                     company["lookup_status"], date_value(company["checked_at"]), reason,
                     "; ".join(ads.get("advertisers", [])), "; ".join(ads.get("formats", [])),
                     f"https://adstransparency.google.com/?region=GB&domain={company['domain']}" if company["domain"] and not company["skip_reason"] else ""])
    status = "Domain checks complete. Missing websites remain unknown." if state.get("phase") == "complete" else (
        "Run paused after a Google challenge or error. Saved evidence remains available." if state.get("stop_reason") else
        "Retry scheduled after cooldown." if state.get("phase") == "cooldown" else "Run continuing with slower requests.")
    payload = {"rows": rows, "company_count": len(rows), "recorded_domains": summary["checked_domains"],
               "http_attempts": state["http_attempts"], "status": status}
    (output / "sheet-values.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = build(args.output)
    print(json.dumps({"company_rows": payload["company_count"], "recorded_domains": payload["recorded_domains"], "status": payload["status"]}))
