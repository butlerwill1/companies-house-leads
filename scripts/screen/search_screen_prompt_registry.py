"""Register the search-screen prompt versions in Langfuse prompt management.

Every version in ``TEMPLATES`` is published (oldest first, so the ``production``
label ends on the current one) under one prompt name, each tagged and labelled
with its semantic version string. A run then records which entry it used as
``search-screen@<version> (Langfuse v<n>)``. ``verify_round_trips`` renders the
Python template and the converted Langfuse template with the same inputs and
refuses to publish if they differ, so a brace-escaping mistake cannot slip in.

Usage:
    python -m scripts.screen.search_screen_prompt_registry register
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
    register_prompt,
    registered_prompt_reference,
    render_langfuse_template,
    to_langfuse_template,
)
from scripts.eval_support.langfuse_tracing import langfuse_from_config  # noqa: E402
from scripts.screen.search_screen_policy import PROMPT_VERSION, TEMPLATES, build_prompt  # noqa: E402
from scripts.screen.search_screen_publish import LANGFUSE_CONFIG  # noqa: E402

PROMPT_NAME = "search-screen"
_SAMPLE = {"company_name": "EXAMPLE TRADING LIMITED", "sic_label": "Specialist retail",
           "text": "[principal activity] The company trades as a retailer."}


def verify_round_trips() -> None:
    for version, template in TEMPLATES.items():
        python_form = build_prompt(version=version, **_SAMPLE)
        langfuse_form = render_langfuse_template(to_langfuse_template(template), _SAMPLE)
        if python_form != langfuse_form:
            raise ValueError(f"{version}: the Langfuse template does not render like the Python one")


def prompt_reference(client: Any) -> str | None:
    """``search-screen@<version> (Langfuse v<n>)`` for the current version, or
    None when the registry is behind the code."""
    return registered_prompt_reference(client, name=PROMPT_NAME, expected_version_tag=PROMPT_VERSION)


def register(client: Any) -> str | None:
    verify_round_trips()
    for version, template in TEMPLATES.items():
        register_prompt(client, name=PROMPT_NAME, python_format_template=template, version_tag=version,
                        commit_message=f"Synced from scripts/screen/search_screen_policy.py ({version}).")
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
