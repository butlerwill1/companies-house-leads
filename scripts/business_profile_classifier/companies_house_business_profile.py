#!/usr/bin/env python3
"""
Gate A2: read a company's filed narrative and record how it acquires
customers -- demand_model, customer_type, delivery_model, geography_served
-- via a single text-only LLM call per company.

Text only, no vision, no browser: this reads narrative_sections (already
extracted from XHTML by companies_house_core/companies_house_pdf_text.py) and calls an
OpenRouter chat model once per company. See
docs/BUSINESS_PROFILE_EXTRACTION.md for the design and
scripts/business_profile_classifier/business_profile_policy.py for the taxonomy, prompt, and
the verbatim-quote validation that makes a hallucinated answer rejectable
before it is ever persisted.

Usage:
    python -m scripts.business_profile_classifier.companies_house_business_profile --db companies-house.db \
        --config evals/business_profile_gold_set/configs/openrouter-gemini.yaml --company 00482197
    python -m scripts.business_profile_classifier.companies_house_business_profile --db companies-house.db \
        --config evals/business_profile_gold_set/configs/openrouter-gemini.yaml --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from companies_house_core.companies_house_extractor import load_dotenv  # noqa: E402
from companies_house_core.companies_house_sqlite import init_db, upsert_company_profile  # noqa: E402
from scripts.business_profile_classifier.business_profile_policy import (  # noqa: E402
    PROMPT_VERSION,
    build_prompt,
    mark_quote_matches,
    normalise_retired_values,
    parse_json_response_with_validation,
    select_narrative_sections,
    reject_failed_fields,
    validate_fields,
    structure_validation,
)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class BusinessProfileModelClient:
    """Thin OpenRouter text-completion client. No images, no page rendering
    -- the vision transport in scripts/pdf_vision_extraction exists for a genuinely different
    problem and would be the wrong thing to reuse here."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def generate(self, model: str, prompt: str, timeout: int) -> str:
        """response_format=json_object rules out an entire failure class
        (malformed/fenced/prose-wrapped JSON) at the provider level instead
        of leaving parse_json_response() to repair it after the fact -- worth
        having since that failure class disproportionately hits smaller
        models, which is exactly the tier this stage is cheap enough to use.
        Not every model on OpenRouter honours the field, so a request that
        fails because of it retries once, unconstrained, rather than making
        the whole model unusable through this client."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        response = self._post(payload, timeout)
        body = response.json()
        if body.get("error") is not None:
            payload.pop("response_format", None)
            response = self._post(payload, timeout)
            body = response.json()
        if body.get("error") is not None:
            raise RuntimeError(f"OpenRouter request failed: {body['error']}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenRouter response did not contain completion choices")
        return choices[0]["message"]["content"]

    def _post(self, payload: dict[str, Any], timeout: int) -> requests.Response:
        response = requests.post(
            OPENROUTER_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response


def load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fetch_narrative_context(conn: sqlite3.Connection, company_number: str) -> dict[str, Any] | None:
    """The most recent narrative_run for this company, with its sections
    reconstructed from section_payload. A company can have several runs
    since the history backfill (one per filing); only the latest is used,
    or a stage built on this would silently mix text from different years."""
    run = conn.execute(
        "select id, document_id from narrative_runs where company_number = ? order by id desc limit 1",
        (company_number,),
    ).fetchone()
    if not run:
        return None
    narrative_run_id, document_id = run

    all_sections: dict[str, dict[str, Any]] = {}
    for section_key, payload in conn.execute(
        "select section_key, section_payload from narrative_sections where narrative_run_id = ?",
        (narrative_run_id,),
    ):
        try:
            all_sections[section_key] = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            all_sections[section_key] = {}

    row = conn.execute(
        """
        select c.company_name, c.sic_code_primary, g.sic_label, f.financial_year
        from narrative_runs nr
        join companies c on c.company_number = nr.company_number
        left join sic_groups g on g.sic_code = c.sic_code_primary
        left join financial_period_summaries f
          on f.company_number = c.company_number and f.period_type = 'current'
         and f.id = (
             select id from financial_period_summaries
             where company_number = c.company_number and period_type = 'current'
             order by financial_year desc, id desc limit 1
         )
        where nr.id = ?
        """,
        (narrative_run_id,),
    ).fetchone()
    company_name, sic_code, sic_label, financial_year = row or (None, None, None, None)

    return {
        "narrative_run_id": narrative_run_id,
        "document_id": document_id,
        "company_name": company_name,
        "sic_code": sic_code,
        "sic_label": sic_label,
        "financial_year": financial_year,
        "sections": select_narrative_sections(all_sections),
    }


def extract_business_profile(
    client: BusinessProfileModelClient,
    model: str,
    context: dict[str, Any],
    *,
    timeout: int = 120,
) -> tuple[dict[str, Any] | None, list[str], str, str | None]:
    """Returns (profile_or_none, errors, prompt, raw_response).

    profile is None only when there was nothing usable at all -- the
    response was not JSON. A field that fails validation (a quote that is
    not verbatim in the filing, a value outside the taxonomy) is dropped
    from the profile on its own: its entry becomes a null value whose
    `reason` records the rejection, the other fields stand, and `errors`
    lists what was dropped. Until 2026-09-14 one failing field rejected the
    whole response; see validate_response for why that changed.

    raw_response is None only when the request itself raised before any
    text came back (the caller must decide how to handle that)."""
    prompt = build_prompt(
        company_name=context["company_name"],
        sections=context["sections"],
        sic_label=context["sic_label"],
        sic_code=context["sic_code"],
    )
    raw = client.generate(model, prompt, timeout)
    try:
        payload, validation = parse_json_response_with_validation(raw)
    except (ValueError, TypeError) as exc:
        return None, [f"response was not valid JSON: {exc}"], prompt, raw
    retired = normalise_retired_values(payload)
    if retired:
        validation["normalisations"] = [
            {"path": f"{field}.value", "code": "retired_label", "message": "mapped retired label"}
            for field in retired
        ]
    structural = structure_validation(payload)
    field_errors = validate_fields(payload, context["sections"])
    for issue in structural["errors"]:
        field = issue["path"].split(".", 1)[0]
        field_errors.setdefault(field, []).append(issue["message"])
    # Marked before rejection so a fuzzy acceptance is on the record: the
    # field passed, but not on the model's exact words (see
    # quote_matches_fuzzily for the tolerance).
    mark_quote_matches(payload, context["sections"])
    if not field_errors:
        return {**payload, "validation": validation}, [], prompt, raw
    errors = [error for errors in field_errors.values() for error in errors]
    rejected = reject_failed_fields(payload, field_errors)
    rejected["validation"] = {**validation, **structural, "status": "partial"}
    return rejected, errors, prompt, raw


def process_company(
    conn: sqlite3.Connection,
    client: BusinessProfileModelClient,
    model: str,
    company_number: str,
    *,
    dry_run: bool,
) -> str:
    context = fetch_narrative_context(conn, company_number)
    if context is None:
        return "no_narrative"
    if not context["sections"]:
        return "no_usable_sections"

    profile, errors, _, _ = extract_business_profile(client, model, context)
    if profile is None:
        print(f"  {company_number}: rejected -- {'; '.join(errors)}", file=sys.stderr)
        return "invalid_response"
    if errors:
        print(f"  {company_number}: {len(errors)} field(s) dropped -- {'; '.join(errors)}", file=sys.stderr)

    if not dry_run:
        upsert_company_profile(
            conn,
            company_number,
            context["financial_year"],
            profile,
            narrative_run_id=context["narrative_run_id"],
            extraction_model=model,
            prompt_version=PROMPT_VERSION,
        )
    return "profiled"


def candidate_companies(conn: sqlite3.Connection, limit: int | None) -> list[str]:
    """Companies with narrative text and no profile yet, highest turnover
    first (matches the priority order the rest of the pipeline uses)."""
    sql = """
        select distinct nr.company_number
        from narrative_runs nr
        left join company_profiles p on p.company_number = nr.company_number
        left join financial_period_summaries f
          on f.company_number = nr.company_number and f.period_type = 'current'
        where p.id is null
        order by f.turnover desc nulls last
    """
    if limit:
        sql += f" limit {int(limit)}"
    return [row[0] for row in conn.execute(sql)]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--config", required=True, help="Provider/model YAML config.")
    parser.add_argument("--company", action="append", default=None, help="A company number to profile. Repeatable.")
    parser.add_argument("--limit", type=int, default=None, help="Profile this many unprofiled companies with narrative, highest turnover first.")
    parser.add_argument("--dry-run", action="store_true", help="Extract and validate but do not write to the database.")
    args = parser.parse_args(argv)

    if not args.company and not args.limit:
        parser.error("Specify --company (repeatable) or --limit N.")

    load_dotenv(Path(".env"))
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set in .env or environment.", file=sys.stderr)
        return 1

    config = load_config(Path(args.config))
    model = config["model"]
    timeout = int(config.get("timeout_seconds", 120))

    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.execute("pragma busy_timeout=30000")
    init_db(conn)

    company_numbers = args.company or candidate_companies(conn, args.limit)
    if not company_numbers:
        print("No companies to profile.", file=sys.stderr)
        conn.close()
        return 0

    client = BusinessProfileModelClient(api_key)
    counts: dict[str, int] = {}
    start = time.monotonic()
    for i, company_number in enumerate(company_numbers, 1):
        try:
            status = process_company(conn, client, model, company_number, dry_run=args.dry_run)
        except requests.RequestException as exc:
            print(f"  {company_number}: request failed: {exc}", file=sys.stderr)
            status = "error"
        counts[status] = counts.get(status, 0) + 1
        if i % 5 == 0 or i == len(company_numbers):
            elapsed = time.monotonic() - start
            print(
                f"  [{i:>4}/{len(company_numbers):,}]  "
                + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
                + f"  {elapsed:.0f}s elapsed",
                file=sys.stderr,
            )

    print(f"\nDone: {counts}", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
