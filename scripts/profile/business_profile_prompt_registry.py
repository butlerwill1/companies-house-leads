"""Register the business-profile prompt in MLflow's Prompt Registry -- the
"Prompts" tab in the MLflow UI, and a genuinely different thing from
`PROMPT_VERSION`.

`PROMPT_VERSION` (business_profile_policy.py) is a label we log as a run
*parameter* -- it lets you filter/compare runs by which prompt they used,
but MLflow never sees the prompt's actual text through it. The Prompt
Registry is a separate MLflow feature: a versioned, diffable record of the
template text itself (`mlflow.genai.register_prompt`/`load_prompt`), which
is what actually populates the "Prompts" page. The two need to be kept in
sync by hand -- there is no automatic link between a Python string constant
and a registry entry -- so this module exists to make that a repeatable,
mechanical step rather than a manual one someone forgets.

Why this doesn't just register `PROMPT_TEMPLATE` verbatim: MLflow's prompt
templates use double curly braces for variables (`{{field}}`), but our
template is a plain Python `str.format()` template -- single braces
(`{field}`) for substitution, doubled braces (`{{`/`}}`) as the
escape for a literal brace (needed throughout the JSON response-shape
example). Registering the raw string would make MLflow try to parse things
like `{{"value": "...", "confidence": 0.0, ...}}` as a variable named
literally `"value": "...", "confidence": 0.0, ...`, which is nonsense.
`_to_mlflow_template` mechanically converts between the two escaping
conventions; `verify_prompt_round_trips` renders both forms with identical
inputs and diffs the output, so a conversion mistake is caught before it's
registered, not discovered later by someone reading a broken prompt in
the UI.

Usage:
    python -m scripts.profile.business_profile_prompt_registry register
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.profile.business_profile_policy import (  # noqa: E402
    PROMPT_TEMPLATE,
    PROMPT_VERSION,
    build_prompt,
    prompt_option_blocks,
)

PROMPT_NAME = "business-profile-extraction"

# Matches, in order of preference: a doubled brace (literal-brace escape in
# Python's str.format()) or a single {identifier} substitution field. Our
# template never uses a format spec (e.g. {value:>10}) or a positional/
# attribute field ({0}, {a.b}), so a bare identifier is the whole grammar
# actually in use -- this is not a general str.format() parser.
_FORMAT_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _to_mlflow_template(python_format_template: str) -> str:
    """Convert a Python str.format() template to MLflow's double-brace
    variable syntax: {field} -> {{field}}, and the {{ / }} literal-brace
    escapes collapse to a single { / } (MLflow needs no escaping for a
    literal single brace -- only {{...}} is special to it)."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return "{{" + match.group(1) + "}}"

    return _FORMAT_TOKEN_RE.sub(replace, python_format_template)


def _sample_render_inputs() -> dict[str, Any]:
    """One representative, deterministic set of inputs to render the
    template with for the equivalence check -- content doesn't matter, only
    that every variable the template references gets a value so both
    rendering paths produce a complete, comparable string."""
    return {
        "company_name": "EXAMPLE TRADING LIMITED",
        "sections_block": "[principal_activity]\nThe company trades as a retailer.",
        **prompt_option_blocks(),
        "sic_label": "Retail sale via mail order",
        "sic_code": "47910",
    }


def verify_prompt_round_trips(mlflow_template: str) -> None:
    """Render the Python template and the converted MLflow template with
    identical inputs and require byte-identical output. Raises on any
    mismatch -- this is the check that makes the conversion trustworthy
    rather than assumed, and it runs before every registration, not just
    once by hand."""
    inputs = _sample_render_inputs()
    expected = PROMPT_TEMPLATE.format(**inputs)

    # Mirrors MLflow's own {{var}} substitution without needing a live
    # server round trip to check it -- MLflow's Prompt.format() does the
    # same plain substitution, but exercising it here keeps this checkable
    # offline, in a test, with no server dependency.
    actual = re.sub(
        r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}",
        lambda m: str(inputs[m.group(1)]),
        mlflow_template,
    )
    if actual != expected:
        first_diff = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b), min(len(actual), len(expected)))
        raise ValueError(
            "MLflow template does not render identically to the Python template -- "
            f"first difference at character {first_diff}:\n"
            f"  python template gave: {expected[max(0, first_diff - 40):first_diff + 40]!r}\n"
            f"  mlflow template gave: {actual[max(0, first_diff - 40):first_diff + 40]!r}"
        )


def register_current_prompt(commit_message: str | None = None) -> Any:
    """Register PROMPT_TEMPLATE's current content as a new version of the
    `business-profile-extraction` prompt, verified to render identically to
    what build_prompt() actually sends the model before it is published."""
    mlflow_template = _to_mlflow_template(PROMPT_TEMPLATE)
    verify_prompt_round_trips(mlflow_template)

    import mlflow

    mlflow.set_tracking_uri("http://127.0.0.1:5000")
    return mlflow.genai.register_prompt(
        name=PROMPT_NAME,
        template=mlflow_template,
        commit_message=commit_message or f"Synced from business_profile_policy.PROMPT_TEMPLATE ({PROMPT_VERSION}).",
        tags={"prompt_version": PROMPT_VERSION},
    )


def registered_prompt_uri() -> str | None:
    """The URI of the latest registered prompt version, for logging against
    an eval run so the run links back to a real, diffable entry in the
    Prompts UI -- but only when its `prompt_version` tag actually matches
    the code's current PROMPT_VERSION. A mismatch means PROMPT_VERSION was
    bumped in code without re-running `register`, which happens to a
    manual step eventually -- returning None rather than a stale URI makes
    that visible (no link logged) instead of quietly mislabelling a run
    with the wrong prompt version. Returns None if nothing is registered
    yet, or the registry server is unreachable; this is traceability, not a
    hard dependency, and must never block an eval run."""
    import mlflow

    mlflow.set_tracking_uri("http://127.0.0.1:5000")
    try:
        prompt = mlflow.genai.load_prompt(PROMPT_NAME, link_to_model=False, allow_missing=True)
    except Exception:
        return None
    if prompt is None or (prompt.tags or {}).get("prompt_version") != PROMPT_VERSION:
        return None
    return prompt.uri


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] != "register":
        print("Usage: python -m scripts.profile.business_profile_prompt_registry register", file=sys.stderr)
        return 1
    registered = register_current_prompt()
    print(f"Registered {registered.name} version {registered.version} (uri={registered.uri})")
    print(f"tags: {registered.tags}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
