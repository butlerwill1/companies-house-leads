"""Datasets and experiment runs -- the replacement for an MLflow ``Run``.

Langfuse v4 self-hosts in "events_only" mode: the old
``POST /api/public/dataset-run-items`` and ``GET .../datasets/:name/runs``
endpoints are gone. The supported path is the **experiment runner**
(``dataset.run_experiment(task=, evaluators=, run_evaluators=)``), which
creates the dataset run, one trace per item, links them, and attaches scores
-- everything the MLflow ``start_run`` + per-case trace + ``link_traces_to_run``
sequence did, in one call.

A harness supplies:
  * ``task(*, item, **kwargs)``        -> run the model on one case, return output
  * ``evaluators``  list of ``(*, input, output, expected_output, metadata, **kwargs)``
                    -> ``Evaluation`` / ``list[Evaluation]`` (deterministic
                       per-field correctness, DeepEval faithfulness)
  * ``run_evaluators`` list of ``(*, item_results, **kwargs)``
                    -> ``Evaluation`` (aggregate metrics)

`create_mlflow_dataset`'s immutability guard is kept as the ``immutable`` +
``digest`` args to :func:`sync_dataset`.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from langfuse import Langfuse
    from langfuse.experiment import ExperimentResult

_DIGEST_KEY = "gold_label_sha256"


_RUN_LABEL_PREFIXES = ("openrouter-", "ollama-", "context-ab-")


def experiment_run_name(*, model: str, when: datetime, label: str | None = None) -> str:
    """A readable dataset-run name: ``<model> · <label> · <YYYY-MM-DD HH:MM>``.

    The provider prefix is dropped from the model id (``google/gemini-2.5-flash``
    -> ``gemini-2.5-flash``). ``label`` is a config's run_name; its boilerplate
    prefixes and any part that just repeats the model are stripped, and it is
    omitted entirely when nothing useful is left. Used by both the live
    harnesses and the MLflow migration so old and new runs read the same.
    """
    model_short = (model or "model").rsplit("/", 1)[-1]
    stamp = when.strftime("%Y-%m-%d %H:%M")

    clean = (label or "").strip()
    for prefix in _RUN_LABEL_PREFIXES:
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    # A bare timestamp suffix the live harness may already have appended.
    clean = re.sub(r"[-_ ]*\d{8}T\d{6}$", "", clean).strip("-_ ")
    # Drop a leading model echo ("gemini-3.7-flash-whole_document" -> "whole_document").
    clean = re.sub(rf"^{re.escape(model_short)}[-_ ]*", "", clean).strip("-_ ")
    if clean and (
        clean in model_short
        or model_short in clean
        or clean.replace("-", "") == model_short.replace("-", "")
        or model_short.startswith(clean)
    ):
        clean = ""

    if clean:
        return f"{model_short} ({clean}) · {stamp}"
    return f"{model_short} · {stamp}"


def dataset_digest(records: Iterable[dict[str, Any]]) -> str:
    """Stable content hash of dataset records (order-independent)."""
    canonical = sorted(
        json.dumps(record, sort_keys=True, ensure_ascii=False) for record in records
    )
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


def sync_dataset(
    client: "Langfuse",
    name: str,
    items: list[dict[str, Any]],
    *,
    description: str | None = None,
    digest: str | None = None,
    immutable: bool = False,
) -> str:
    """Upsert a dataset and its items. Each item is
    ``{"id", "input", "expected"?, "metadata"?}``; items upsert on ``id``
    (project-unique). Returns the dataset name.

    With ``immutable=True`` and a ``digest``, a pre-existing dataset whose
    stored digest differs raises -- the published-snapshot rule from
    ``create_mlflow_dataset``.
    """
    existing = _get_dataset(client, name)
    if existing is not None and immutable and digest is not None:
        stored = (getattr(existing, "metadata", None) or {}).get(_DIGEST_KEY)
        if stored is not None and stored != digest:
            raise ValueError(
                f"Langfuse dataset {name!r} already published with digest {stored} "
                f"but the current gold labels hash to {digest}. Publish under a new "
                "name (e.g. -v2) rather than mutating a snapshot."
            )

    metadata = {_DIGEST_KEY: digest} if digest is not None else None
    if existing is None or metadata is not None:
        kwargs: dict[str, Any] = {"name": name}
        if description:
            kwargs["description"] = description
        if metadata is not None:
            kwargs["metadata"] = metadata
        client.create_dataset(**kwargs)

    for item in items:
        item_kwargs: dict[str, Any] = {"dataset_name": name, "id": item["id"]}
        if item.get("input") is not None:
            item_kwargs["input"] = item["input"]
        if item.get("expected") is not None:
            item_kwargs["expected_output"] = item["expected"]
        if item.get("metadata") is not None:
            item_kwargs["metadata"] = item["metadata"]
        client.create_dataset_item(**item_kwargs)
    return name


def run_experiment(
    client: "Langfuse",
    *,
    dataset_name: str,
    run_name: str,
    task: Callable[..., Any],
    evaluators: Sequence[Callable[..., Any]] = (),
    run_evaluators: Sequence[Callable[..., Any]] = (),
    description: str | None = None,
    metadata: dict[str, str] | None = None,
    max_concurrency: int = 1,
    item_ids: Sequence[str] | None = None,
) -> "ExperimentResult":
    """Run ``task`` over the items of ``dataset_name`` as one dataset run.

    ``item_ids`` restricts the run to a subset (a ``--limit`` / company-number
    smoke) while the dataset itself keeps the full gold set -- the run still
    links to the dataset. ``None`` runs every item.

    ``max_concurrency`` defaults to 1: the harnesses page paid model calls
    deliberately and their model clients are not proven thread-safe. Bump it
    only where the harness opts in.
    """
    items = list(client.get_dataset(dataset_name).items)
    if item_ids is not None:
        wanted = set(item_ids)
        items = [item for item in items if item.id in wanted]
    return client.run_experiment(
        name=run_name,
        run_name=run_name,
        description=description,
        data=items,
        task=task,
        evaluators=list(evaluators),
        run_evaluators=list(run_evaluators),
        max_concurrency=max_concurrency,
        metadata=metadata,
    )


def evaluation(
    name: str,
    value: float | str | bool,
    *,
    comment: str | None = None,
    data_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Build a Langfuse ``Evaluation`` (import kept local)."""
    from langfuse import Evaluation

    kwargs: dict[str, Any] = {"name": name, "value": value}
    if comment is not None:
        kwargs["comment"] = comment
    if data_type is not None:
        kwargs["data_type"] = data_type
    if metadata is not None:
        kwargs["metadata"] = metadata
    return Evaluation(**kwargs)


def run_score(
    client: "Langfuse",
    dataset_name: str,
    run_name: str,
    name: str,
    value: float | str,
    *,
    data_type: str | None = None,
    comment: str | None = None,
) -> bool:
    """Attach a score to an existing dataset run, looked up by name -- the
    replacement for MLflow's ``start_run(run_id=...)`` + ``log_metric`` for
    post-hoc re-scoring of a completed run. Returns ``False`` (and warns)
    when the run cannot be found, rather than orphaning the score on a
    session that does not exist.
    """
    try:
        run = client.get_dataset_run(dataset_name=dataset_name, run_name=run_name)
    except Exception:
        run = None
    run_id = getattr(run, "id", None)
    if not run_id:
        print(
            f"Langfuse dataset run {run_name!r} not found in dataset {dataset_name!r}; "
            f"skipping score {name!r}.",
            file=sys.stderr,
        )
        return False
    client.create_score(
        name=name,
        value=value,
        dataset_run_id=run_id,
        data_type=data_type,
        comment=comment,
    )
    return True


def _get_dataset(client: "Langfuse", name: str) -> Any | None:
    try:
        return client.api.datasets.get(name)
    except Exception:
        return None
