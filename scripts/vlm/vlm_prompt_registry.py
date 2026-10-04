#!/usr/bin/env python3
"""Register the financial-PDF VLM prompts in Langfuse prompt management.

The pipeline (`companies_house_pdf_vlm_financials.py`) sends seven distinct
prompts, so they are published as seven entries in one Langfuse folder,
`vlm-financial/`, each labelled with `PROMPT_VERSION` and `production`:

| entry                   | pipeline stage(s)                          |
| ----------------------- | ------------------------------------------ |
| locator                 | locator (page thumbnails)                  |
| extraction              | vision                                     |
| employee-extraction     | employee_detail, employee_note_extraction  |
| coverage-recovery       | vision_recovery (page missing from batch)  |
| row-validation-recovery | vision_recovery (row check failed)         |
| completeness-recovery   | vision_recovery (rows may be omitted)      |
| rationalisation         | rationalisation (text only)                |

Each entry is the whole text the model receives, built by the pipeline's own
functions. The per-call parts are `{{page_number}}`,
`{{completeness_signals}}`, `{{company_context}}` and `{{candidates}}`.
`verify_round_trips` renders every entry with sample values and refuses to
publish if it differs from what the pipeline builds. `EMPLOYEE_LOCATOR_PROMPT`
is not published, because no stage sends it.

One `PROMPT_VERSION` covers all seven. An entry whose text has not changed
since its `production` version gets the new label rather than a duplicate
version (`sync_prompt`), so Langfuse's history for each entry only moves when
that prompt does. Re-run `register` after every bump.

Usage:
    python -m scripts.vlm.vlm_prompt_registry register
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_extractor import load_dotenv  # noqa: E402
from scripts.eval_support.langfuse_prompts import (  # noqa: E402
    registered_prompt_reference,
    render_langfuse_template,
    sync_prompt,
)
from scripts.eval_support.langfuse_tracing import langfuse_from_config  # noqa: E402
from scripts.vlm import companies_house_pdf_vlm_financials as pipeline  # noqa: E402
from scripts.vlm.companies_house_pdf_vlm_financials import PROMPT_VERSION  # noqa: E402

FOLDER = "vlm-financial"
LANGFUSE_CONFIG = {"langfuse": {"enabled": True, "key_env": "VLM_FINANCIAL"}}
_SAMPLE = {
    "page_number": 12,
    "completeness_signals": "turnover_missing, cash_missing",
    "company_context": '{"sic_codes":["47910"],"company_number":"01234567"}',
    "candidates": '{"candidates":[{"id":"c1","metric":"turnover"}]}',
}
_PLACEHOLDERS = {variable: "{{" + variable + "}}" for variable in _SAMPLE}


def _prompts(values: dict[str, Any]) -> dict[str, str]:
    """Every published prompt, as the pipeline builds it from ``values``."""
    return {
        f"{FOLDER}/locator": pipeline.LOCATOR_PROMPT,
        f"{FOLDER}/extraction": pipeline.EXTRACTION_PROMPT,
        f"{FOLDER}/employee-extraction": pipeline.EMPLOYEE_EXTRACTION_PROMPT,
        f"{FOLDER}/coverage-recovery": pipeline.coverage_recovery_prompt(values["page_number"]),
        f"{FOLDER}/row-validation-recovery": pipeline.row_validation_recovery_prompt(),
        f"{FOLDER}/completeness-recovery": pipeline.statement_completeness_recovery_prompt(
            values["completeness_signals"]),
        f"{FOLDER}/rationalisation": pipeline.rationalisation_prompt(
            values["company_context"], values["candidates"]),
    }


PROMPT_NAMES = tuple(_prompts(_PLACEHOLDERS))


def langfuse_templates() -> dict[str, str]:
    return _prompts(_PLACEHOLDERS)


def verify_round_trips() -> None:
    """A stray ``{{word}}`` in the prompt text would raise KeyError here or
    render differently, so either way nothing is published."""
    expected = _prompts(_SAMPLE)
    for name, template in langfuse_templates().items():
        if render_langfuse_template(template, _SAMPLE) != expected[name]:
            raise ValueError(f"{name}: the Langfuse template does not render like the pipeline's prompt")


def prompt_references(client: Any) -> dict[str, str | None]:
    """``name@<version> [langfuse v<n>]`` per prompt, None for any entry that
    is behind the code. Never raises."""
    if client is None:
        return {}
    return {
        name: registered_prompt_reference(client, name=name, expected_version_tag=PROMPT_VERSION)
        for name in PROMPT_NAMES
    }


def register(client: Any) -> dict[str, str | None]:
    verify_round_trips()
    for name, template in langfuse_templates().items():
        sync_prompt(client, name=name, langfuse_template=template, version_tag=PROMPT_VERSION,
                    commit_message=f"Synced from scripts/vlm/companies_house_pdf_vlm_financials.py ({PROMPT_VERSION}).")
    client.flush()
    return prompt_references(client)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("register",))
    parser.parse_args(argv)
    load_dotenv(Path(".env"))
    client = langfuse_from_config(LANGFUSE_CONFIG)
    if client is None:
        raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
    for name, reference in register(client).items():
        print(f"{name}: {reference}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
