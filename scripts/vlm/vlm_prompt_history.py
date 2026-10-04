#!/usr/bin/env python3
"""Rebuild the VLM prompt history from git and publish it to Langfuse.

Run once, when the `vlm-financial/` prompts were first registered
(2026-10-03). `vlm_prompt_registry` publishes the current text; this publishes
the versions before it, oldest first, so each Langfuse entry has the history
the code had. Each commit in `VERSIONS` is one where some prompt's text, or
the way the pipeline assembled it, changed. Nothing else touched the prompts
between 2026-07-24 and 2026-08-18, and nothing has since.

Two things differ from the current registry, both read from the old call
sites:
  * the recovery and completeness prompts did not exist at first
    (coverage/row-validation from v4, completeness from v6), so those
    entries start at their first version rather than v1;
  * the recovery prompts gained the high-resolution suffix in v5, and the
    rationalisation prompt gained the company-context block in v6.

Publishing goes through `sync_prompt`, so an entry whose text did not change
in a version is relabelled rather than duplicated. Langfuse versions are
sequential and cannot be inserted before an existing one, so `--reset` first
deletes the seven `vlm-financial/` entries (and only those).

Usage:
    python -m scripts.vlm.vlm_prompt_history            # dry run: what would be published
    python -m scripts.vlm.vlm_prompt_history publish --reset
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.companies_house_extractor import load_dotenv  # noqa: E402
from scripts.eval_support.langfuse_prompts import sync_prompt  # noqa: E402
from scripts.eval_support.langfuse_tracing import langfuse_from_config  # noqa: E402
from scripts.vlm import vlm_prompt_registry as registry  # noqa: E402

FOLDER = registry.FOLDER
SOURCE_PATHS = ("scripts/vlm/companies_house_pdf_vlm_financials.py",
                "scripts/ocr/companies_house_pdf_vlm_financials.py")
# (version number, commit, date, what changed)
VERSIONS = (
    (1, "f9c4381", "2026-07-24", "hosted pipeline: locator, extraction, rationalisation"),
    (2, "b350b85", "2026-07-24", "rationalisation returns canonical summaries"),
    (3, "28b7a1a", "2026-07-28", "insurance metrics in extraction and rationalisation"),
    (4, "b00927c", "2026-08-03", "employee extraction, row-validation and coverage recovery added"),
    (5, "6f9f1a1", "2026-08-09", "high-resolution recovery suffix"),
    (6, "7119ab5", "2026-08-13", "completeness recovery, company context in rationalisation"),
    (7, "18da9a3", "2026-08-13", "employee extraction reworded"),
    (8, "dd7e65d", "2026-08-16", "standalone equity rows in extraction"),
    (9, "2298868", "2026-08-18", "extraction: currency, operating-result, profit-before-tax"),
)
_CONSTANTS = ("LOCATOR_PROMPT", "EXTRACTION_PROMPT", "EMPLOYEE_EXTRACTION_PROMPT",
              "ROW_VALIDATION_RECOVERY_PROMPT", "HIGH_RESOLUTION_RECOVERY_PROMPT",
              "STATEMENT_COMPLETENESS_RECOVERY_PROMPT", "RATIONALISATION_PROMPT")
_COVERAGE = ("Coverage recovery: return the rows for Document page {{page_number}}. "
             "This page was classified as a primary financial statement. "
             "Do not omit it and do not return any other page.")


def version_label(number: int) -> str:
    return f"vlm-financials-v{number}"


def constants_at(commit: str) -> dict[str, str]:
    for path in SOURCE_PATHS:
        result = subprocess.run(["git", "show", f"{commit}:{path}"], capture_output=True,
                                text=True, encoding="utf-8", cwd=REPOSITORY_ROOT)
        if result.returncode == 0:
            found: dict[str, str] = {}
            for node in ast.parse(result.stdout).body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name) and node.targets[0].id in _CONSTANTS):
                    found[node.targets[0].id] = ast.literal_eval(node.value)
            return found
    raise RuntimeError(f"no pipeline source at {commit}")


def templates_for(number: int, c: dict[str, str]) -> dict[str, str]:
    """The Langfuse prompts as the pipeline assembled them in version ``number``."""
    high = f"\n\n{c['HIGH_RESOLUTION_RECOVERY_PROMPT']}" if number >= 5 else ""
    templates = {
        f"{FOLDER}/locator": c["LOCATOR_PROMPT"],
        f"{FOLDER}/extraction": c["EXTRACTION_PROMPT"],
    }
    if number >= 4:
        templates[f"{FOLDER}/employee-extraction"] = c["EMPLOYEE_EXTRACTION_PROMPT"]
        templates[f"{FOLDER}/coverage-recovery"] = f"{c['EXTRACTION_PROMPT']}\n\n{_COVERAGE}{high}"
        templates[f"{FOLDER}/row-validation-recovery"] = (
            f"{c['EXTRACTION_PROMPT']}\n\n{c['ROW_VALIDATION_RECOVERY_PROMPT']}{high}")
    if number >= 6:
        templates[f"{FOLDER}/completeness-recovery"] = (
            f"{c['EXTRACTION_PROMPT']}\n\n{c['STATEMENT_COMPLETENESS_RECOVERY_PROMPT']}\n\n"
            "Completeness signals for this page: {{completeness_signals}}.")
    context = "COMPANY_CONTEXT_ADVISORY_ONLY:\n{{company_context}}\n\n" if number >= 6 else ""
    templates[f"{FOLDER}/rationalisation"] = (
        f"{c['RATIONALISATION_PROMPT']}\n\n{context}CANDIDATES:\n{{{{candidates}}}}")
    return templates


def history() -> list[tuple[int, str, str, str, dict[str, str]]]:
    rows = [(n, commit, date, note, templates_for(n, constants_at(commit)))
            for n, commit, date, note in VERSIONS]
    # The newest historical version must be the text the code sends today.
    if rows[-1][4] != registry.langfuse_templates():
        raise RuntimeError("the last historical version does not match the current pipeline prompts")
    return rows


def publish(client: Any, rows: list[tuple[int, str, str, str, dict[str, str]]], *, reset: bool) -> None:
    if reset:
        for name in registry.PROMPT_NAMES:
            # The SDK does not encode the folder slash on delete, as it does on reads.
            client.api.prompts.delete(quote(name, safe=""))
    for number, commit, date, note, templates in rows:
        for name, template in templates.items():
            sync_prompt(client, name=name, langfuse_template=template, version_tag=version_label(number),
                        commit_message=f"{version_label(number)} ({date}, {commit}): {note}")
    client.flush()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=("publish",))
    parser.add_argument("--reset", action="store_true",
                        help="delete the vlm-financial/ prompts first (needed unless they are empty)")
    args = parser.parse_args(argv)
    rows = history()
    previous: dict[str, str] = {}
    for number, commit, date, note, templates in rows:
        changed = [name.split("/")[1] for name, text in templates.items() if previous.get(name) != text]
        print(f"{version_label(number)}  {date}  {commit}  new Langfuse version of: {', '.join(changed) or '-'}")
        previous = templates
    if args.command == "publish":
        load_dotenv(Path(".env"))
        client = langfuse_from_config(registry.LANGFUSE_CONFIG)
        if client is None:
            raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
        publish(client, rows, reset=args.reset)
        for name, reference in registry.prompt_references(client).items():
            print(f"{name}: {reference}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
