"""Explicitly authorised, capped direct-only Ads Transparency experiment.

Selection and every response/result live under --output (gitignored data/).
Re-running resumes results.jsonl without resending completed cases. A recorded
block stops the whole run, including on resume. No paid providers are called.
Reports are Markdown, not spreadsheet deliverables.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import time
from typing import Any

import requests
from dotenv import load_dotenv

from scripts.website_analysis import ads_transparency as at
from scripts.website_analysis.proxy_adapters import DirectAdapter, ProxyAdapter
from scripts.website_analysis.header_profiles import HeaderProfile
from scripts.langfuse_eval_helpers.langfuse_tracing import case_trace, flush, langfuse_from_config


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def valid_company_domain(domain: str | None) -> bool:
    """Reject bare common country suffixes mistakenly stored as websites.
    This is a conservative input guard, not a replacement public-suffix list.
    """
    return bool(domain and not re.fullmatch(r"(?:com|co|net|org|gov|edu|ac)\.[a-z]{2}", domain))


class EvidenceSession(requests.Session):
    """Explicit TLS-verified transport with credential-free request evidence."""

    def __init__(self, folder: Path, *, adapter: ProxyAdapter | None = None,
                 header_profile: HeaderProfile | None = None) -> None:
        super().__init__()
        self.trust_env = False
        self.adapter = adapter or DirectAdapter()
        self.header_profile = header_profile
        self.folder = folder
        self.records: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.update(self.adapter.request_options(), allow_redirects=False)
        if self.header_profile is not None:
            # Preserve endpoint-specific headers such as Content-Type. A lookup
            # uses one profile, including its robots and pagination requests.
            headers = requests.structures.CaseInsensitiveDict(kwargs.get("headers") or {})
            headers.update(self.header_profile.headers())
            kwargs["headers"] = dict(headers)
        started = time.monotonic()
        record = {"method": method, "url": url, "request": kwargs.get("data"),
                  "started_at": at.utc_now(), "proxy": self.adapter.name != "direct",
                  "proxy_provider": self.adapter.name,
                  "header_profile": self.header_profile.name if self.header_profile else "research"}
        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException as exc:
            # Classify locally without putting exception text/credentials in logs.
            kind = "dns" if "NameResolutionError" in str(exc) else (
                "timeout" if isinstance(exc, requests.Timeout) else type(exc).__name__)
            record.update(http_status=None, transport_error=kind)
            self.records.append(record)
            save(self.folder / "http.json", self.records)
            raise
        filename = f"http-{len(self.records) + 1:02d}.body"
        (self.folder / filename).write_bytes(response.content)
        record.update(http_status=response.status_code, body_file=filename,
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      content_type=response.headers.get("Content-Type"),
                      location=response.headers.get("Location"))
        self.records.append(record)
        save(self.folder / "http.json", self.records)
        return response


def selection(db: Path, count: int, seed: int) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute("select c.company_number, c.company_name, w.domain "
                            "from company_web_identity_current w join companies c "
                            "on c.company_number=w.company_number where w.role='main' "
                            "and w.domain is not null order by c.company_number").fetchall()
    pool = {}
    for number, name, domain in rows:
        domain = at.registrable_domain(domain)
        if valid_company_domain(domain):
            pool.setdefault(domain, {"company_number": number, "name": name, "domain": domain})
    # Include the historical 20-site pilot for context; remaining cases are a
    # seeded random draw. Old counts are context, never current ground truth.
    controls = []
    for path in sorted(Path("data/raw/search-api-responses/dataforseo/serp_google_ads_search_live_advanced").glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        domain = at.registrable_domain(body["request"]["target"])
        if domain in pool:
            case = dict(pool.pop(domain))
            case.update(selection_group="historical_pilot", baseline_file=str(path))
            controls.append(case)
    controls.sort(key=lambda c: (c["domain"] != "rettie.co.uk", c["domain"]))
    remaining = list(pool.values())
    random.Random(seed).shuffle(remaining)
    for case in remaining:
        case["selection_group"] = "seeded_random"
    return (controls + remaining)[:count]


def report(output: Path, cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    attempts = len(results)
    rpc_attempts = sum(r.get("rpc_attempts", 0) for r in results)
    excluded = [c["domain"] for c in cases if not valid_company_domain(c["domain"])]
    cases = [c for c in cases if valid_company_domain(c["domain"])]
    latest = {r["domain"]: r for r in results}
    results = [latest[c["domain"]] for c in cases if c["domain"] in latest]
    statuses = Counter(r["status"] for r in results)
    found = sum(bool(r.get("creatives")) for r in results)
    current = sum(r["ads"]["advertising_now"] is True for r in results)
    creatives = [c for r in results for c in r.get("creatives", [])]
    readable = sum(c.get("dates_read") is True for c in creatives)
    lines = ["# Direct Ads Transparency experiment", "",
             f"Selected {len(cases)} distinct domains; attempted {len(results)} ({attempts} total case attempts).", "",
             f"Statuses: {dict(statuses)}. Ads found for {found}; recent ads for {current}.", "",
             f"Retained {len(creatives)} creatives; {readable} have readable last-shown dates.", "",
             f"RPC attempts including development retries and excluded inputs: {rpc_attempts}.", "",
             "No paid providers, proxies or model calls. Each case is checkpointed and has HTTP evidence.",
             "Counts are capped at 120; partial counts are lower bounds. Regions are advisory.",
             "Historical pilot counts are not current ground truth. Empty protobuf responses were cross-checked against browser zero-ad results; unexpected shapes remain unknown.", "",
             "| Domain | Group | Status | Ads | Recent | Last shown | Error |",
             "|---|---|---|---:|---|---|---|"]
    for result in results:
        ads = result["ads"]
        lines.append(f"| {result['domain']} | {result['selection_group']} | {result['status']} | "
                     f"{ads['ads_seen']} | {ads['advertising_now']} | {ads['last_shown']} | {result['error'] or ''} |")
    if excluded:
        lines += ["", f"Excluded invalid stored website domains: {', '.join(excluded)}. Their attempts remain in the checkpoint but are not company results."]
    lines += ["", "```mermaid", "flowchart LR", " A[Direct request] --> B{Valid response?}",
              " B -->|Yes| C[Parse ads and explicit timestamps]", " B -->|No| D[Unknown or failed]",
              " C --> E[Complete or partial evidence]", " A --> F[Stop entire run on a block]", "```", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def trace_checkpoint(lf: Any, dataset: str, run_id: str, case: dict[str, Any], result: dict[str, Any]) -> None:
    """Backfill a checkpoint interrupted between saving and tracing, for free."""
    folder = Path(result["evidence_folder"])
    if lf is None or (folder / "trace.json").exists():
        return
    records = json.loads((folder / "http.json").read_text(encoding="utf-8")) if (folder / "http.json").exists() else []
    with case_trace(lf, name=f"ads-transparency: {case['domain']}", session_id=run_id,
                    tags=["ads-transparency", "direct-experiment"], input=case,
                    output={"result": result, "http": records}) as span:
        trace_id = span.trace_id
    flush(lf)
    attempt = int(folder.name.rsplit("-attempt-", 1)[1]) if "-attempt-" in folder.name else 1
    ident = hashlib.sha256(f"{run_id}:{case['domain']}:{attempt}".encode()).hexdigest()
    lf.create_dataset_item(dataset_name=dataset, id=ident, input=case,
                           metadata={"run": run_id, "status": result["status"]}, source_trace_id=trace_id)
    save(folder / "trace.json", {"trace_id": trace_id, "dataset": dataset})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=at.DATABASE_PATH)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20261014)
    parser.add_argument("--rpc-cap", type=int, default=600)
    parser.add_argument("--langfuse-key-env", default="BUSINESS_PROFILE")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed cases using saved responses where possible")
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 60 or args.rpc_cap < 1:
        parser.error("count must be 1..60 and rpc-cap positive")
    args.output.mkdir(parents=True, exist_ok=True)
    selection_path = args.output / "selection.json"
    if selection_path.exists():
        saved_selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if (saved_selection["count"], saved_selection["seed"], saved_selection["rpc_cap"]) != (args.count, args.seed, args.rpc_cap):
            parser.error("resume must preserve the saved count, seed and RPC cap; use a new output directory for a new experiment")
        cases = saved_selection["cases"]
    else:
        cases = selection(args.db, args.count, args.seed)
        save(selection_path, {"seed": args.seed, "count": args.count, "rpc_cap": args.rpc_cap, "cases": cases})
    checkpoint = args.output / "results.jsonl"
    results = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()] if checkpoint.exists() else []
    latest = {r["domain"]: r for r in results}
    done = {domain for domain, result in latest.items() if not args.retry_failed or result["status"] != "failed"}
    if any(r["blocked"] for r in results):
        report(args.output, cases, results)
        print("Stopped: this experiment previously encountered a block. No requests sent.")
        return 2
    load_dotenv()
    config = {"langfuse": {"enabled": True, "host": "http://localhost:3000", "key_env": args.langfuse_key_env}}
    lf = langfuse_from_config(config)
    dataset = "ads-transparency-direct-experiment"
    if lf is not None:
        if not lf.auth_check():
            raise RuntimeError("Langfuse authentication/availability failed before live requests")
        lf.create_dataset(name=dataset, description="Direct transport and parser experiment; not an accuracy gold set")
    run_id = args.output.name
    by_domain = {c["domain"]: c for c in cases}
    for result in results:
        trace_checkpoint(lf, dataset, run_id, by_domain[result["domain"]], result)
    total_calls = sum(r["rpc_attempts"] for r in results)
    for index, case in enumerate(cases, 1):
        if not valid_company_domain(case["domain"]):
            continue
        if case["domain"] in done:
            continue
        if total_calls >= args.rpc_cap:
            break
        attempt = sum(r["domain"] == case["domain"] for r in results) + 1
        folder = args.output / (f"case-{index:02d}" if attempt == 1 else f"case-{index:02d}-attempt-{attempt}")
        folder.mkdir(exist_ok=True)
        with EvidenceSession(folder) as session:
            # Reuse prior raw bodies to repair parsing without re-spending calls.
            client = at.TransparencyClient(session=session, proxy={}, cache_dir=args.output / f"case-{index:02d}" / "cache",
                                           pause_seconds=1.5,
                                           budget=at.RequestBudget(min(
                                               max(0, 12 - sum(r["rpc_attempts"] for r in results if r["domain"] == case["domain"])),
                                               args.rpc_cap - total_calls)))
            result = at.check(client, domain=case["domain"], name=case["name"], alpha2=["GB"])
        result.update(company_number=case["company_number"], name=case["name"],
                      selection_group=case["selection_group"], rpc_attempts=client.requests_made,
                      http_attempts=len(session.records), evidence_folder=str(folder))
        # Save before tracing or any next case, so interruption loses no results.
        save(folder / "result.json", result)
        with checkpoint.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        results.append(result)
        total_calls += client.requests_made
        report(args.output, cases, results)
        trace_checkpoint(lf, dataset, run_id, case, result)
        print(json.dumps({"case": index, "domain": case["domain"], "status": result["status"],
                          "ads": result["ads"]["ads_seen"], "recent": result["ads"]["advertising_now"],
                          "error": result["error"]}), flush=True)
        if result["blocked"]:
            break
        time.sleep(2)
    report(args.output, cases, results)
    return 2 if any(r["blocked"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
