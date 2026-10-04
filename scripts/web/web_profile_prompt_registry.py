#!/usr/bin/env python3
"""Register the W3 site-profile prompt in Langfuse prompt management.

The current template (`web_profile_policy.TEMPLATE`) is published under one
prompt name, labelled and tagged with `PROMPT_VERSION` and labelled
`production`, the same way as the search screen
(`scripts/screen/search_screen_prompt_registry.py`). The keyword-count range is
baked in; the per-company parts (name, principal activity, Maps category, the
category instruction, the site text) stay `{{variables}}`.

Only the current version is kept in code, so only it can be registered: v1
(`web-profile-v1-quoted`) and v2 (`web-profile-v2-sources`) were edited in
place before the registry existed. Re-run `register` after every prompt bump.
`verify_round_trip` renders the Python and the Langfuse forms with the same
inputs and refuses to publish if they differ.

Usage:
    python -m scripts.web.web_profile_prompt_registry register
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv  # noqa: E402

from scripts.eval_support.langfuse_prompts import (  # noqa: E402
    register_prompt, registered_prompt_reference, render_langfuse_template, resolve_template_variables,
    to_langfuse_template)
from scripts.eval_support.langfuse_tracing import langfuse_from_config  # noqa: E402
from scripts.web import web_profile_policy as policy  # noqa: E402

PROMPT_NAME = "web-profile"
LANGFUSE_CONFIG = {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}
BAKED = {"min_keywords": policy.MIN_SEED_KEYWORDS + 5, "max_keywords": policy.MAX_SEED_KEYWORDS}
_SAMPLE = {"company_name": "EXAMPLE TRADING LIMITED", "principal_activity": "The provision of legal services.",
           "listing_category": "Law firm", "text": "[home: https://example.co.uk/] We help people claim compensation."}


def verify_round_trip() -> None:
    python_form = policy.build_prompt(**_SAMPLE)
    values = {**BAKED, **_SAMPLE,
              "category_instruction": policy.CATEGORY_WITH_LISTING.format(listing=_SAMPLE["listing_category"])}
    langfuse_form = render_langfuse_template(
        resolve_template_variables(to_langfuse_template(policy.TEMPLATE), BAKED), values)
    if python_form != langfuse_form:
        raise ValueError(f"{policy.PROMPT_VERSION}: the Langfuse template does not render like the Python one")


def prompt_reference(client: Any) -> str | None:
    return registered_prompt_reference(client, name=PROMPT_NAME, expected_version_tag=policy.PROMPT_VERSION)


def register(client: Any) -> str | None:
    verify_round_trip()
    register_prompt(client, name=PROMPT_NAME, python_format_template=policy.TEMPLATE,
                    version_tag=policy.PROMPT_VERSION, resolved_variables=BAKED,
                    commit_message=f"Synced from scripts/web/web_profile_policy.py ({policy.PROMPT_VERSION}).")
    client.flush()
    return prompt_reference(client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("register",))
    parser.parse_args(argv)
    load_dotenv(Path(".env"))
    client = langfuse_from_config(LANGFUSE_CONFIG)
    if client is None:
        raise SystemExit("Langfuse not configured (see docs/LANGFUSE_SETUP.md).")
    print(register(client))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
