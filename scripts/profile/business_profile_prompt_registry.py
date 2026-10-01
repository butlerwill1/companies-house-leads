"""Register the business-profile prompt in Langfuse prompt management -- the
"Prompts" tab in the Langfuse UI, and a genuinely different thing from
`PROMPT_VERSION`.

`PROMPT_VERSION` (business_profile_policy.py) is a label logged as run
metadata -- it lets you filter/compare runs by which prompt they used, but
Langfuse never sees the prompt's actual text through it. Prompt management is
a separate feature: a versioned, diffable record of the template text itself
(`langfuse.create_prompt` / `get_prompt`). The two are kept in sync by hand
-- there is no automatic link between a Python string constant and a registry
entry -- so this module makes that a repeatable, mechanical step.

Langfuse and MLflow both use `{{variable}}` prompt syntax, so
`to_langfuse_template` (in scripts.eval_support.langfuse_prompts) is the old
`_to_mlflow_template` unchanged: our `PROMPT_TEMPLATE` is a plain Python
`str.format()` template (single `{field}` for substitution, doubled
`{{` / `}}` as the literal-brace escape), and registering it raw would make
Langfuse try to parse `{{"value": ...}}` as a variable. `verify_prompt_round_trips`
renders both forms with identical inputs and diffs the output, so a
conversion mistake is caught before it is published.

Usage:
    python -m scripts.profile.business_profile_prompt_registry register
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.eval_support.langfuse_prompts import (  # noqa: E402
    register_prompt,
    render_langfuse_template,
    resolve_template_variables,
    to_langfuse_template,
)
from scripts.eval_support.langfuse_prompts import (  # noqa: E402
    registered_prompt_reference as _langfuse_prompt_reference,
)
from scripts.profile.business_profile_policy import (  # noqa: E402
    PROMPT_TEMPLATE,
    PROMPT_VERSION,
    build_prompt,
    prompt_option_blocks,
)

PROMPT_NAME = "business-profile-extraction"


def _sample_render_inputs() -> dict[str, Any]:
    """One representative, deterministic set of inputs to render the template
    with for the equivalence check -- content doesn't matter, only that every
    variable the template references gets a value."""
    return {
        "company_name": "EXAMPLE TRADING LIMITED",
        "sections_block": "[principal_activity]\nThe company trades as a retailer.",
        **prompt_option_blocks(),
        "sic_label": "Retail sale via mail order",
        "sic_code": "47910",
    }


def verify_prompt_round_trips(langfuse_template: str) -> None:
    """Render the Python template and the converted Langfuse template with
    identical inputs and require byte-identical output. Raises on any
    mismatch -- the check that makes the conversion trustworthy rather than
    assumed, run before every registration."""
    inputs = _sample_render_inputs()
    expected = PROMPT_TEMPLATE.format(**inputs)
    actual = render_langfuse_template(langfuse_template, inputs)
    if actual != expected:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(actual, expected)) if a != b),
            min(len(actual), len(expected)),
        )
        raise ValueError(
            "Langfuse template does not render identically to the Python template -- "
            f"first difference at character {first_diff}:\n"
            f"  python template gave: {expected[max(0, first_diff - 40):first_diff + 40]!r}\n"
            f"  langfuse template gave: {actual[max(0, first_diff - 40):first_diff + 40]!r}"
        )


def register_current_prompt(client: Any, commit_message: str | None = None) -> Any:
    """Register PROMPT_TEMPLATE's current content as a new version of the
    `business-profile-extraction` prompt, verified to render identically to
    what build_prompt() sends the model before it is published.

    The six `{<field>_options}` blocks are baked in rather than left as
    placeholders. They are static -- built from FIELD_DEFINITIONS, identical
    for every company -- and leaving them unresolved made the registry blind
    to the only thing most versions change. Registrations 3 to 7 are
    byte-identical across semantic v4 and v5 for exactly that reason: v5 added
    three delivery_model values and rewrote three glosses, all of it inside
    those blocks, none of it in the skeleton. The per-case variables
    (company_name, sections_block, sic_label, sic_code) stay as placeholders,
    so the entry still reads as a template rather than one rendered example.

    Nothing builds a prompt FROM Langfuse -- build_prompt formats the Python
    template directly and get_prompt is only used for the traceability string
    -- so resolving these costs nothing and buys a diffable record."""
    resolved_variables = prompt_option_blocks()
    langfuse_template = resolve_template_variables(
        to_langfuse_template(PROMPT_TEMPLATE), resolved_variables
    )
    verify_prompt_round_trips(langfuse_template)
    return register_prompt(
        client,
        name=PROMPT_NAME,
        python_format_template=PROMPT_TEMPLATE,
        version_tag=PROMPT_VERSION,
        commit_message=commit_message or f"Synced from business_profile_policy.PROMPT_TEMPLATE ({PROMPT_VERSION}).",
        resolved_variables=resolved_variables,
    )


def registered_prompt_reference(client: Any, expected_version: str = PROMPT_VERSION) -> str | None:
    """`name@version` for the live prompt, but only when its tag matches the
    code's current PROMPT_VERSION -- otherwise None (the code was bumped
    without re-running `register`). Never raises: traceability, not a hard
    dependency, and must never block an eval run."""
    if client is None:
        return None
    return _langfuse_prompt_reference(client, name=PROMPT_NAME, expected_version_tag=expected_version)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] != "register":
        print("Usage: python -m scripts.profile.business_profile_prompt_registry register", file=sys.stderr)
        return 1

    from core.companies_house_extractor import load_dotenv
    from scripts.eval_support.langfuse_tracing import flush, langfuse_from_config

    load_dotenv(Path(".env"))
    client = langfuse_from_config(
        {"langfuse": {"enabled": True, "key_env": "BUSINESS_PROFILE"}}
    )
    if client is None:
        print("Langfuse not configured (see docs/LANGFUSE_SETUP.md).", file=sys.stderr)
        return 1
    registered = register_current_prompt(client)
    flush(client)
    print(f"Registered {registered.name} version {registered.version}")
    print(f"tags: {registered.tags}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
