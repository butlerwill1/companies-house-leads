"""Prompt management -- the replacement for MLflow's Prompt Registry.

Langfuse prompt templates use the same ``{{variable}}`` syntax MLflow did, so
`to_langfuse_template` is the old `business_profile_prompt_registry._to_mlflow_template`
verbatim: a Python ``str.format()`` template (single ``{field}`` for
substitution, doubled ``{{`` / ``}}`` for a literal brace) becomes
``{{field}}`` for substitution and a bare ``{`` / ``}`` for a literal.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from langfuse import Langfuse

# A doubled brace (literal-brace escape) or a single {identifier} field. The
# template never uses a format spec or positional/attribute field, so a bare
# identifier is the whole grammar in use.
_FORMAT_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def to_langfuse_template(python_format_template: str) -> str:
    """``{field}`` -> ``{{field}}``; ``{{`` / ``}}`` -> ``{`` / ``}``."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return "{{" + match.group(1) + "}}"

    return _FORMAT_TOKEN_RE.sub(replace, python_format_template)


def render_langfuse_template(langfuse_template: str, values: dict[str, Any]) -> str:
    """Offline equivalent of Langfuse's own ``{{var}}`` substitution -- used
    by the round-trip check so it needs no live server."""
    return re.sub(
        r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}",
        lambda m: str(values[m.group(1)]),
        langfuse_template,
    )


def register_prompt(
    client: "Langfuse",
    *,
    name: str,
    python_format_template: str,
    version_tag: str,
    commit_message: str | None = None,
) -> Any:
    """Publish the current template as a new version, tagged with the code's
    ``version_tag`` and labelled ``production``."""
    return client.create_prompt(
        name=name,
        prompt=to_langfuse_template(python_format_template),
        type="text",
        labels=["production"],
        tags=[version_tag],
        commit_message=commit_message or f"Synced from code ({version_tag}).",
    )


def registered_prompt_reference(
    client: "Langfuse", *, name: str, expected_version_tag: str
) -> str | None:
    """A ``name@version`` reference to the live prompt, but only when its tag
    matches the code's current version -- otherwise ``None`` (the code was
    bumped without re-running ``register``), exactly as the MLflow
    ``registered_prompt_uri`` behaved. Never raises: traceability, not a
    hard dependency."""
    try:
        prompt = client.get_prompt(name, label="production", cache_ttl_seconds=0)
    except Exception:
        return None
    if prompt is None or expected_version_tag not in (getattr(prompt, "tags", None) or []):
        return None
    return f"{name}@{prompt.version}"
