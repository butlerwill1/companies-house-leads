"""Langfuse client + per-case tracing helpers.

The MLflow equivalents this replaces:
`business_profile_eval._log_gold_eval_case_trace`,
`vlm_financial_eval.log_saved_case_trace`, and the
`mlflow.flush_trace_async_logging()` / `link_traces_to_run` dance. Langfuse
links a trace to a dataset run synchronously (see `langfuse_runs.py`), so
there is no create-then-link race -- but `flush()` is still required before a
short-lived process exits or spans are lost.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langfuse import Langfuse

_DEFAULT_HOST = "http://localhost:3000"


def langfuse_from_config(config: dict[str, Any], *, tracing: bool = True) -> "Langfuse | None":
    """Build a Langfuse client from a config's ``langfuse:`` block, or return
    ``None`` when it is disabled / unconfigured / the package is missing --
    the same guard shape the old ``use_mlflow`` check had.

    The block carries ``key_env`` (e.g. ``BUSINESS_PROFILE``) which selects
    the ``LANGFUSE_PUBLIC_KEY_<key_env>`` / ``LANGFUSE_SECRET_KEY_<key_env>``
    pair from the environment. Keys never live in the YAML.
    """
    settings = config.get("langfuse") or {}
    if not settings.get("enabled"):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        print(
            "langfuse not installed (pip install -r requirements-eval.txt); skipping Langfuse logging.",
            file=sys.stderr,
        )
        return None

    key_env = str(settings.get("key_env") or "").strip()
    public_key = os.getenv(f"LANGFUSE_PUBLIC_KEY_{key_env}") if key_env else None
    secret_key = os.getenv(f"LANGFUSE_SECRET_KEY_{key_env}") if key_env else None
    host = settings.get("host") or os.getenv("LANGFUSE_HOST") or _DEFAULT_HOST
    if not public_key or not secret_key:
        print(
            f"LANGFUSE_PUBLIC_KEY_{key_env} / LANGFUSE_SECRET_KEY_{key_env} not set in .env; "
            "skipping Langfuse logging.",
            file=sys.stderr,
        )
        return None

    return Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        tracing_enabled=tracing,
    )


def flush(client: "Langfuse | None") -> None:
    """Block until queued spans/scores/media are sent. No-op when disabled."""
    if client is not None:
        client.flush()


def pdf_media(path: str | Path) -> Any:
    """Wrap a local PDF as a Langfuse media object for a span input/output.
    The SDK uploads it to the object store and stores a reference in the
    trace -- the replacement for MLflow's ``Attachment.from_file(...)``."""
    from langfuse import LangfuseMedia

    return LangfuseMedia(file_path=str(path), content_type="application/pdf")


def restate_trace(
    client: "Langfuse",
    trace_id: str,
    name: str,
    *,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    input: Any = None,
    output: Any = None,
) -> None:
    """Give an existing trace a new name, tags, metadata, input and output
    without recreating it.

    Langfuse v4 stores traces as events and derives trace-level fields from
    the newest event: the name from the newest non-empty ``trace_name`` and
    the input/output from the newest *root-level* event
    (``argMaxIf(input, event_ts, parent_span_id = '')`` in the web build).
    So one more root-level span carrying all of them restates the trace.
    Scores and annotation-queue items stay attached because the trace id
    does not change. The span must carry the input and output too: a bare
    rename span is itself the newest root-level event and would blank the
    trace's input (which the first version of this helper did, 2026-09-14).
    """
    from langfuse import propagate_attributes

    with propagate_attributes(trace_name=name, tags=tags or None, metadata=metadata or None):
        with client.start_as_current_observation(
            name="identity", as_type="span", trace_context={"trace_id": trace_id}, input=input, output=output
        ):
            pass


@contextmanager
def case_trace(
    client: "Langfuse",
    *,
    name: str,
    tags: list[str] | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    input: Any = None,
    output: Any = None,
) -> Iterator[Any]:
    """One trace for one eval case. Yields the root span (which carries
    ``.trace_id`` and ``.start_as_current_observation(...)`` for child
    spans). Trace-level name/tags/session/metadata are set via
    ``propagate_attributes`` -- Langfuse v4's replacement for
    ``mlflow.update_current_trace``.

    ``input`` / ``output`` are set on the root observation, which is the
    trace's own IO in Langfuse v4.
    """
    from langfuse import propagate_attributes

    with propagate_attributes(
        trace_name=name,
        tags=tags or None,
        session_id=session_id,
        metadata=metadata or None,
    ):
        with client.start_as_current_observation(
            name=name, as_type="span", input=input, output=output
        ) as root:
            yield root


@contextmanager
def observation(
    parent: Any,
    *,
    name: str,
    as_type: str = "span",
    input: Any = None,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
) -> Iterator[Any]:
    """A nested observation under ``parent`` -- an LLM call is
    ``as_type="generation"``. ``parent`` is either a span yielded by
    :func:`case_trace` or the ``Langfuse`` client itself (inside an experiment
    ``task`` the runner has already made the item's trace the current span, so
    ``client.start_as_current_observation`` nests correctly)."""
    kwargs: dict[str, Any] = {"name": name, "as_type": as_type}
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    if metadata is not None:
        kwargs["metadata"] = metadata
    if model is not None:
        kwargs["model"] = model
    with parent.start_as_current_observation(**kwargs) as span:
        yield span


def trace_score(
    client: "Langfuse",
    trace_id: str,
    name: str,
    value: float | str,
    *,
    data_type: str | None = None,
    comment: str | None = None,
    config_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Attach a score to a finished trace (deterministic per-field correctness,
    a DeepEval metric result, a backfill correction status)."""
    client.create_score(
        name=name,
        value=value,
        trace_id=trace_id,
        data_type=data_type,
        comment=comment,
        config_id=config_id,
        metadata=metadata,
    )
