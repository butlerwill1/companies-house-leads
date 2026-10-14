"""Scan the frozen lead queue's chosen domains with global pacing and stops.

Direct by default; proxy routes require explicit selection. No model calls. Missing/ambiguous websites stay
unknown. Results are checkpointed per domain and expanded to every company.
Default resume retains a block. A newly authorised --continue-after-block
waits for the cooldown and retries the same route without changing identity.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import time
from typing import Any

from dotenv import load_dotenv
import requests

from scripts.website_analysis import ads_transparency as at
from scripts.website_analysis.proxy_adapters import ProxyAdapter, adapter_from_env
from scripts.website_analysis.header_profiles import HeaderProfile, HeaderRotation
from scripts.website_analysis.ads_transparency_experiment import EvidenceSession, save, trace_checkpoint, valid_company_domain
from scripts.langfuse_eval_helpers.langfuse_tracing import langfuse_from_config


class RunStopped(RuntimeError):
    """A shared limit or sustained failures stopped the run, not a Google block."""


def newly_blocked(records: list[dict[str, Any]], authorised_at: str | None) -> bool:
    return any((r.get("http_status") in (403, 429) or "/sorry/" in str(r.get("location", "")))
               and (not authorised_at or r.get("started_at", "") >= authorised_at) for r in records)


class Pacer:
    """One serial request schedule across robots, RPCs and every company."""

    def __init__(self, *, interval: float = 3, jitter: float = 1, cap: int = 1100,
                 used: int = 0, clock=time.monotonic, sleep=time.sleep, uniform=random.uniform):
        self.interval, self.jitter, self.cap, self.used = interval, jitter, cap, used
        self.clock, self.sleep, self.uniform = clock, sleep, uniform
        self.next_at = 0.0
        self.errors = 0
        self.stopped: str | None = None
        self.events: list[dict[str, Any]] = []

    def rest(self, seconds: float, reason: str) -> None:
        self.next_at = max(self.next_at, self.clock() + seconds)
        self.events.append({"at": at.utc_now(), "reason": reason, "seconds": seconds})

    def before_request(self) -> None:
        if self.stopped:
            raise RunStopped(self.stopped)
        if self.used >= self.cap:
            self.stopped = f"shared HTTP cap of {self.cap} reached"
            raise RunStopped(self.stopped)
        while self.clock() < self.next_at:
            self.sleep(min(30, self.next_at - self.clock()))
        self.used += 1
        self.next_at = self.clock() + self.interval + self.uniform(0, self.jitter)

    def failed(self, retry_after: str | None = None) -> None:
        self.errors += 1
        seconds = min(300, 30 * 2 ** (self.errors - 1))
        if retry_after:
            try:
                seconds = max(seconds, float(retry_after))
            except ValueError:
                try:
                    seconds = max(seconds, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
        self.rest(seconds, "transient_error_backoff")
        if self.errors >= 3:
            self.stopped = "three consecutive transport/server errors"


class PopulationSession(EvidenceSession):
    def __init__(self, folder: Path, pacer: Pacer, *, adapter: ProxyAdapter | None = None,
                 header_profile: HeaderProfile | None = None):
        super().__init__(folder, adapter=adapter, header_profile=header_profile)
        self.pacer = pacer
        if (folder / "http.json").exists():
            self.records = json.loads((folder / "http.json").read_text(encoding="utf-8"))

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.pacer.before_request()
        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException:
            self.pacer.failed()
            raise
        if response.status_code >= 500:
            self.pacer.failed(response.headers.get("Retry-After"))
        elif response.status_code in (200, 404):
            self.pacer.errors = 0
        return response


def build_manifest(db: Path, queue: Path) -> dict[str, Any]:
    numbers = [r["company_number"] for r in json.loads(queue.read_text(encoding="utf-8"))["queue"]]
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        names = dict(conn.execute("select company_number,company_name from companies"))
        sites = {n: (d, t) for n, d, t in conn.execute(
            "select company_number,domain,tier from company_web_identity_current where role='main'")}
        screen = {n: a for n, a in conn.execute("select company_number,answer from company_search_screen order by id")}
    companies, domains = [], {}
    for number in numbers:
        domain, tier = sites.get(number, (None, None))
        reason = None if valid_company_domain(domain) else ("invalid_stored_domain" if domain else "no_chosen_website")
        row = {"company_number": number, "name": names.get(number, ""), "screen_answer": screen.get(number),
               "domain": domain, "identity_tier": tier, "skip_reason": reason}
        companies.append(row)
        if not reason:
            domains.setdefault(domain, []).append(number)
    return {"created_at": at.utc_now(), "source_queue": str(queue), "companies": companies,
            "domains": [{"domain": d, "company_numbers": ns} for d, ns in domains.items()]}


def append(path: Path, result: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def export(output: Path, manifest: dict[str, Any], results: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    by_domain = {r["domain"]: r for r in results}
    rows = []
    for company in manifest["companies"]:
        result = by_domain.get(company["domain"]) if not company["skip_reason"] else None
        ads = result["ads"] if result else {}
        rows.append({**company, "lookup_status": result["status"] if result else "unchecked",
                     "has_ads": bool(result["creatives"]) if result and (result["creatives"] or result["complete"]) else None,
                     "recent_ads": ads.get("advertising_now"), "ads_seen_lower_bound": ads.get("ads_seen"),
                     "last_shown": ads.get("last_shown"), "checked_at": result.get("checked_at") if result else None,
                     "error": result.get("error") if result else None,
                     "unknown_reason": company["skip_reason"] or (result.get("error") if result else "not_yet_checked")})
    with (output / "companies.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    save(output / "companies.json", rows)
    summary = {"population_companies": len(rows), "eligible_companies": sum(not r["skip_reason"] for r in rows),
               "eligible_domains": len(manifest["domains"]), "checked_domains": len(by_domain),
               "companies_with_ads": sum(r["has_ads"] is True for r in rows),
               "companies_with_recent_ads": sum(r["recent_ads"] is True for r in rows),
               "companies_with_no_ads": sum(r["has_ads"] is False for r in rows),
               "companies_unknown": sum(r["has_ads"] is None for r in rows),
               "domain_statuses": dict(Counter(r["status"] for r in by_domain.values())), **state}
    save(output / "summary.json", summary)
    lines = ["# Original lead queue: Google Ads Transparency", "", json.dumps(summary, indent=2), "",
             "Company counts reuse shared-domain evidence; they do not establish which legal entity paid for an ad.",
             "No chosen website, failed lookup and pending lookup remain unknown. Recent means last shown within 30 days.",
             "Ads are capped at one page (40); counts are lower bounds. Country filtering is advisory.", "",
             "```mermaid", "flowchart LR", " A[Original lead queue] --> B{Chosen website?}",
             " B -->|No| C[Unknown]", " B -->|Yes| D[One lookup per distinct domain]",
             " D --> E[Shared pacing and batch breaks]", " E --> F[Checkpoint and company dataset]",
             " E -->|Block or repeated failures| G[Stop; retain remaining unknowns]", "```", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def reuse_pilot(cache: Path, pilot: Path) -> int:
    copied = 0
    for source in pilot.glob("case-*/cache/SearchService_SearchCreatives/*.json"):
        body = json.loads(source.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(body.get("fetched_at", "2000-01-01T00:00:00+00:00"))
        if 0 <= (datetime.now(timezone.utc) - fetched).total_seconds() <= 86400:
            target = cache / "SearchService_SearchCreatives" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied += 1
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=at.DATABASE_PATH)
    parser.add_argument("--queue", type=Path, default=Path("logs/web/queue-order.json"))
    parser.add_argument("--pilot", type=Path, default=Path("data/raw/ads-transparency-experiment-2026-10-14"))
    parser.add_argument("--http-cap", type=int, default=1100)
    parser.add_argument("--proxy-provider", choices=("direct", "brightdata", "oxylabs"), default="direct")
    parser.add_argument("--header-mode", choices=("research", "browser-rotate"), default="research")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Add saved domain responses only; never send to Google")
    parser.add_argument("--continue-after-block", action="store_true", help="Explicit new authorisation to retry the same route after a cooldown")
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--jitter", type=float, default=1)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-break", type=float, default=60)
    parser.add_argument("--block-cooldown", type=float, default=900)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else build_manifest(args.db, args.queue)
    if path.exists() and manifest.get("proxy_provider", "direct") != args.proxy_provider:
        parser.error("resume must preserve the saved proxy provider; use a separate output folder for a new route")
    # A fresh manifest has no saved route yet.
    manifest["proxy_provider"] = args.proxy_provider
    if path.exists() and manifest.get("header_mode", "research") != args.header_mode:
        parser.error("resume must preserve the saved header mode; use a separate output folder")
    manifest["header_mode"] = args.header_mode
    if args.interval < 3 or args.jitter < 0 or args.batch_size < 1 or args.batch_break < 60 or args.block_cooldown < 900:
        parser.error("minimum interval 3s, batch break 60s, block cooldown 900s; nonnegative jitter and positive batch size")
    policy = {"interval_seconds": args.interval, "jitter_seconds": args.jitter,
              "batch_size": args.batch_size, "batch_break_seconds": args.batch_break,
              "http_cap": args.http_cap, "max_ads": 40, "control_every": 100}
    if "policy" in manifest and manifest["policy"] != policy and not args.continue_after_block:
        parser.error("resume must preserve the saved run policy")
    if args.continue_after_block:
        manifest.setdefault("policy_history", []).append({"at": at.utc_now(), "previous_policy": manifest.get("policy"),
                                                          "reason": "user explicitly requested retry after backoff on the same route"})
    manifest["policy"] = policy
    save(path, manifest)
    checkpoint = args.output / "domains.jsonl"
    results = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()] if checkpoint.exists() else []
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"http_attempts": 0, "stop_reason": None}
    recorded = [r for p in args.output.glob("*/http.json") for r in json.loads(p.read_text(encoding="utf-8"))]
    state["http_attempts"] = max(state["http_attempts"], len(recorded))
    authorised_at = (state.get("retry_authorisations") or [{}])[-1].get("at")
    if newly_blocked(recorded, authorised_at):
        state["stop_reason"] = "recorded Google block; no automatic resume"
    cooldown = 0
    if args.continue_after_block and state["stop_reason"] and not args.dry_run and not args.cache_only:
        blocked = [r for r in recorded if r.get("http_status") in (403, 429) or "/sorry/" in str(r.get("location", ""))]
        last = max((datetime.fromisoformat(r["started_at"]) for r in blocked), default=datetime.now(timezone.utc))
        cooldown = max(0, args.block_cooldown - (datetime.now(timezone.utc) - last).total_seconds())
        state.setdefault("retry_authorisations", []).append({"at": at.utc_now(), "cooldown_seconds_remaining": cooldown,
                                                             "prior_stop": state["stop_reason"], "same_direct_route": args.proxy_provider == "direct",
                                                             "proxy_provider": args.proxy_provider})
        state["stop_reason"] = None
        state["phase"] = "cooldown"
        save(state_path, state)
    if args.cache_only:
        by_domain = {r["domain"] for r in results}
        for index, case in enumerate(manifest["domains"], 1):
            if case["domain"] in by_domain:
                continue
            client = at.TransparencyClient(cache_dir=args.output / "cache", cache_only=True, proxy={})
            result = at.check(client, domain=case["domain"], alpha2=["GB"], max_ads=40, domain_only=True)
            if result["error"]:
                continue
            folder = args.output / f"case-{index:04d}"
            folder.mkdir(exist_ok=True)
            result.update(rpc_attempts=0, evidence_folder=str(folder), company_numbers=case["company_numbers"], reused_cache=True)
            save(folder / "result.json", result)
            append(checkpoint, result)
            results.append(result)
        # Preserve the observed stop. Cached evidence never grants permission
        # to resume a blocked live run.
        export(args.output, manifest, results, state)
        load_dotenv()
        lf = langfuse_from_config({"langfuse": {"enabled": True, "host": "http://localhost:3000", "key_env": "BUSINESS_PROFILE"}})
        if lf is not None:
            for result in results:
                case = next(c for c in manifest["domains"] if c["domain"] == result["domain"])
                trace_checkpoint(lf, "ads-transparency-lead-population", args.output.name, case, result)
        print(json.dumps(export(args.output, manifest, results, state)), flush=True)
        return 0
    if args.dry_run or state["stop_reason"]:
        print(json.dumps(export(args.output, manifest, results, state)), flush=True)
        return 2 if state["stop_reason"] else 0
    load_dotenv()
    adapter = adapter_from_env(args.proxy_provider)
    headers = HeaderRotation(args.header_mode)
    lf = langfuse_from_config({"langfuse": {"enabled": True, "host": "http://localhost:3000", "key_env": "BUSINESS_PROFILE"}})
    dataset = "ads-transparency-lead-population"
    if lf is not None:
        if not lf.auth_check():
            raise RuntimeError("Langfuse unavailable before live requests")
        lf.create_dataset(name=dataset, description="Original screen-passing lead queue; domain advertising evidence")
    cache = args.output / "cache"
    if not results:
        reuse_pilot(cache, args.pilot)
    pacer = Pacer(cap=args.http_cap, used=state["http_attempts"], interval=args.interval, jitter=args.jitter)
    pacer.rest(max(cooldown, 60 if results else 0), "block_cooldown" if cooldown else "resume_cooldown" if results else "start")
    state.update(process_id=os.getpid(), phase="cooldown" if cooldown else "running",
                 next_request_not_before=(datetime.now(timezone.utc) + timedelta(seconds=max(cooldown, 60 if results else 0))).isoformat())
    save(state_path, state)
    export(args.output, manifest, results, state)
    done = {domain for domain, result in {r["domain"]: r for r in results}.items() if not result["blocked"]}
    needs_control = bool(args.continue_after_block)
    all_cases = {r["domain"]: r for r in manifest["domains"]}
    for result in results:
        trace_checkpoint(lf, dataset, args.output.name, all_cases[result["domain"]], result)
    robots = None
    for index, case in enumerate(manifest["domains"], 1):
        if case["domain"] in done:
            continue
        attempt = sum(r["domain"] == case["domain"] for r in results) + 1
        folder = args.output / (f"case-{index:04d}" if attempt == 1 else f"case-{index:04d}-attempt-{attempt}")
        folder.mkdir(exist_ok=True)
        try:
            # A fresh known-positive control guards against silent empty results.
            if needs_control or not done or len(done) % 100 == 0:
                control_folder = args.output / (f"control-resume-{len(state.get('retry_authorisations', []))}" if needs_control else f"control-{len(done):04d}")
                control_folder.mkdir(exist_ok=True)
                with PopulationSession(control_folder, pacer, adapter=adapter,
                                       header_profile=headers.next_profile()) as session:
                    client = at.TransparencyClient(session=session, proxy={}, cache_dir=cache, refresh=True,
                                                   pause_seconds=0, budget=at.RequestBudget(1))
                    if robots is not None:
                        client._robots = robots
                    control = at.check(client, domain="rettie.co.uk", alpha2=["GB"], max_ads=40, domain_only=True)
                    robots = client._robots
                save(control_folder / "result.json", control)
                control["evidence_folder"] = str(control_folder)
                trace_checkpoint(lf, dataset, f"{args.output.name}-{control_folder.name}",
                                 {"domain": "rettie.co.uk", "role": "known_positive_control"}, control)
                if control["blocked"] or not control["creatives"]:
                    state["stop_reason"] = "Google block" if control["blocked"] else "known-positive control failed; review required"
                    break
                needs_control = False
                state["phase"] = "running"
            with PopulationSession(folder, pacer, adapter=adapter,
                                   header_profile=headers.next_profile()) as session:
                client = at.TransparencyClient(session=session, proxy={}, cache_dir=cache,
                                               pause_seconds=0, budget=at.RequestBudget(1))
                if robots is not None:
                    client._robots = robots
                result = at.check(client, domain=case["domain"], alpha2=["GB"], max_ads=40, domain_only=True)
                robots = client._robots
            result.update(rpc_attempts=client.requests_made, evidence_folder=str(folder), company_numbers=case["company_numbers"])
            save(folder / "result.json", result)
            append(checkpoint, result)
            results.append(result)
            done.add(case["domain"])
            trace_checkpoint(lf, dataset, args.output.name, case, result)
            if result["blocked"]:
                state["stop_reason"] = "Google block/challenge; no automatic resume"
        except RunStopped as exc:
            state["stop_reason"] = str(exc)
        finally:
            state.update(http_attempts=pacer.used, updated_at=at.utc_now(), timing_events=pacer.events)
            save(state_path, state)
            summary = export(args.output, manifest, results, state)
            print(json.dumps({k: summary[k] for k in ("checked_domains", "eligible_domains", "companies_with_ads", "companies_with_no_ads", "companies_unknown", "http_attempts", "stop_reason")}), flush=True)
        if state["stop_reason"]:
            break
        if pacer.stopped:
            state["stop_reason"] = pacer.stopped
            break
        if len(done) % args.batch_size == 0:
            pacer.rest(args.batch_break, "batch_break")
            robots = None
    state.update(http_attempts=pacer.used, updated_at=at.utc_now(), timing_events=pacer.events)
    state["phase"] = "stopped" if state["stop_reason"] else "complete"
    state["process_id"] = os.getpid()
    save(state_path, state)
    export(args.output, manifest, results, state)
    return 2 if state["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
