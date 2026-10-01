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


def resolve_template_variables(langfuse_template: str, values: dict[str, Any]) -> str:
    """Substitute only the named ``{{variable}}`` placeholders, leaving every
    other variable untouched.

    Used to bake the static parts of a prompt into what gets registered.
    Anything not in ``values`` -- the per-case variables -- stays a
    placeholder, so the registered entry is still recognisably the template
    rather than one rendered example."""
    return re.sub(
        r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}",
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        langfuse_template,
    )


def register_prompt(
    client: "Langfuse",
    *,
    name: str,
    python_format_template: str,
    version_tag: str,
    commit_message: str | None = None,
    resolved_variables: dict[str, Any] | None = None,
) -> Any:
    """Publish the current template as a new version, tagged AND labelled with
    the code's ``version_tag``, and labelled ``production``.

    ``version_tag`` is applied as a Langfuse *label* as well as a tag, and
    that is the point of this function. Langfuse assigns its own
    auto-incrementing ``version`` per prompt name, which cannot be set and
    counts registrations rather than meaning anything -- our fourth
    registration was numbered 4 while the code said `business-profile-v5`, so
    a run's traceability string disagreed with the code that produced it.
    Labels are the one identifier we control, so the semantic version becomes
    a label and the auto-number is demoted to a way of finding the entry in
    the Langfuse UI. Re-registering the same semantic version moves the label
    to the newer entry, which is why the reference below still reports the
    number alongside it.

    ``version_tag`` is ALSO written as a tag, but read nothing into that.
    Langfuse tags are per-PROMPT, not per-version: each registration rewrites
    the tag set for every version of this name, so after publishing v6 all
    eight historical versions report ``tags=['business-profile-v6']``,
    including two published before v6 existed. The tag is kept because it is
    what the UI filters on, and it is accurate for the current version, which
    is the only one a filter is looking for. Nothing may use it to identify
    which semantic version a given entry came from -- that is what the label
    is for, and ``registered_prompt_reference`` reads the label.

    ``resolved_variables`` bakes the named placeholders into the registered
    text before publishing. Without it the entry stores only the skeleton, and
    every change that lives in a substituted block is invisible: registrations
    3 to 7 of business-profile-extraction are byte-identical across semantic
    v4 and v5, because all of v5's work was in the option blocks."""
    template = to_langfuse_template(python_format_template)
    if resolved_variables:
        template = resolve_template_variables(template, resolved_variables)
    return client.create_prompt(
        name=name,
        prompt=template,
        type="text",
        labels=["production", version_tag],
        tags=[version_tag],
        commit_message=commit_message or f"Synced from code ({version_tag}).",
    )


def registered_prompt_reference(
    client: "Langfuse", *, name: str, expected_version_tag: str
) -> str | None:
    """A ``name@<semantic version>`` reference to the live prompt, but only
    when its tag matches the code's current version -- otherwise ``None`` (the
    code was bumped without re-running ``register``). Never raises:
    traceability, not a hard dependency.

    The reference is keyed on the semantic version, not Langfuse's
    auto-number, so it says the same thing the code does and resolves through
    ``get_prompt(name, label=<semantic version>)``. Langfuse's own version is
    reported in brackets because it is what the UI lists entries by, and
    because it is the only thing that distinguishes two registrations of the
    same semantic version.

    The match is checked against the version's LABELS, not its tags. Tags are
    per-prompt and are rewritten by every registration, so the tag always
    reports the most recent sync regardless of which version currently holds
    the ``production`` label -- reading it would answer "does the code match
    the newest upload", when the question is "does the code match what is
    actually in production". Roll production back to an older entry and the
    tag check still passes while the label check correctly returns None."""
    try:
        prompt = client.get_prompt(name, label="production", cache_ttl_seconds=0)
    except Exception:
        return None
    if prompt is None or expected_version_tag not in (getattr(prompt, "labels", None) or []):
        return None
    return f"{name}@{expected_version_tag} [langfuse v{prompt.version}]"
