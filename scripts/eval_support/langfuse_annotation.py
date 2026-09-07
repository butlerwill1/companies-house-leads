"""Annotation queues + score configs -- the human gold-label review workflow.

Replaces the MLflow GenAI review queue used by both harnesses
(`business_profile_eval._label_schemas` / `_get_or_create_queue` /
`_seed_draft_expectations` / `sync_review_queue` / `export_reviews`, and the
VLM equivalents `_mlflow_review_schemas` / `_mlflow_review_queue` /
`completed_review_cases` / `export_mlflow_reviews`).

Mapping:
  MLflow label schema (InputCategorical / InputText)  -> Langfuse score config
  MLflow review queue                                 -> Langfuse annotation queue
  MLflow Expectation assessment (HUMAN source)        -> Langfuse score

A seeded draft and a reviewer's answer both have score source ``ANNOTATION``
(a draft must, or Langfuse leaves the annotate form blank instead of
pre-filling it) and the read API doesn't expose a score's comment, so the two
can't be told apart per-field. "Reviewed" is therefore a whole-case signal:
the reviewer marks the queue item COMPLETED (see :func:`completed_trace_ids`),
and export keys off that.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from langfuse import Langfuse

DRAFT_COMMENT = "draft: seeded from case JSON, not a human judgement"


def question_score_configs(specs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise a harness's question spec list into score-config shapes.

    Each input spec is ``{"name", "categories"?: [...], "description"?}``;
    ``categories`` present -> CATEGORICAL, absent -> TEXT (free-form, as the
    VLM metric answers are).
    """
    configs: list[dict[str, Any]] = []
    for spec in specs:
        categories = spec.get("categories")
        configs.append(
            {
                "name": spec["name"],
                "data_type": "CATEGORICAL" if categories else "TEXT",
                "categories": list(categories) if categories else None,
                "description": spec.get("description"),
            }
        )
    return configs


def ensure_score_configs(client: "Langfuse", configs: list[dict[str, Any]]) -> dict[str, str]:
    """Get-or-create a score config per entry. Returns ``{name: config_id}``.

    Score configs are immutable in the categories they allow once created, so
    this only creates missing ones -- it does not try to reconcile a drifted
    category list the way the MLflow code had to (a changed taxonomy gets a
    renamed config instead).
    """
    existing = {c.name: c.id for c in _all_score_configs(client)}
    out: dict[str, str] = {}
    for cfg in configs:
        name = cfg["name"]
        if name in existing:
            out[name] = existing[name]
            continue
        kwargs: dict[str, Any] = {"name": name, "data_type": cfg["data_type"]}
        if cfg["data_type"] == "CATEGORICAL":
            kwargs["categories"] = [
                {"label": str(value), "value": index}
                for index, value in enumerate(cfg["categories"] or [])
            ]
        if cfg.get("description"):
            kwargs["description"] = cfg["description"]
        created = client.api.score_configs.create(**kwargs)
        out[name] = created.id
    return out


def ensure_queue(client: "Langfuse", name: str, score_config_ids: list[str]) -> str:
    """Get-or-create an annotation queue by name. Returns its id."""
    for queue in _all_queues(client):
        if queue.name == name:
            return queue.id
    return client.api.annotation_queues.create_queue(
        name=name, score_config_ids=score_config_ids
    ).id


def find_queue_id(client: "Langfuse", name: str) -> str | None:
    """The id of an existing queue by name, or None. Read-only -- unlike
    :func:`ensure_queue` it never creates one, so callers that only read
    (export) don't need the score-config list."""
    for queue in _all_queues(client):
        if queue.name == name:
            return queue.id
    return None


def completed_trace_ids(client: "Langfuse", queue_id: str) -> set[str]:
    """Trace ids of queue items a reviewer has marked COMPLETED -- the
    "I have checked this case" signal. A case can be fully pre-filled by a
    model draft yet still PENDING; only the reviewer hitting Complete moves
    it, which is what export keys off to promote a case to `verified`."""
    return {
        item.object_id
        for item in _all_queue_items(client, queue_id)
        if str(getattr(item, "status", "")).upper().endswith("COMPLETED")
    }


def sync_queue_items(
    client: "Langfuse",
    queue_id: str,
    trace_ids: Iterable[str],
    *,
    complete: Iterable[str] = (),
) -> dict[str, int]:
    """Ensure the queue contains exactly ``trace_ids`` (add missing, drop
    stale) and mark the ``complete`` subset COMPLETED. Idempotent."""
    wanted = set(trace_ids)
    complete = set(complete)
    current = {item.object_id: item.id for item in _all_queue_items(client, queue_id)}

    added = 0
    for trace_id in wanted - current.keys():
        client.api.annotation_queues.create_queue_item(
            queue_id, object_id=trace_id, object_type="TRACE"
        )
        added += 1

    removed = 0
    for trace_id, item_id in current.items():
        if trace_id not in wanted:
            client.api.annotation_queues.delete_queue_item(queue_id, item_id)
            removed += 1

    # Re-read so newly created items have ids.
    current = {item.object_id: item.id for item in _all_queue_items(client, queue_id)}
    marked = 0
    for trace_id in complete:
        item_id = current.get(trace_id)
        if item_id is not None:
            client.api.annotation_queues.update_queue_item(
                queue_id, item_id, status="COMPLETED"
            )
            marked += 1

    return {"added": added, "removed": removed, "completed": marked}


def _draft_score_id(trace_id: str, name: str) -> str:
    """Deterministic score id for a seeded draft. Re-seeding a trace then
    *updates* the existing draft in place instead of stacking a second score
    for the same question -- duplicates make the review UI's per-field
    lookup ambiguous and the dropdown falls back to rendering empty."""
    return f"draft-{trace_id}-{name}"


def seed_draft_scores(
    client: "Langfuse",
    trace_id: str,
    answers: dict[str, Any],
    config_ids: dict[str, str],
) -> None:
    """Pre-fill each question with the case's current draft value so a
    reviewer opens an already-answered form.

    The score is written with **source ANNOTATION** -- Langfuse only
    pre-selects the annotate-panel dropdowns from ANNOTATION-source scores,
    not API-source ones (which show only as read-only eval scores). It also
    carries ``DRAFT_COMMENT``: that marker is how :func:`read_annotations`
    tells an untouched seed apart from a value the reviewer actually chose
    (choosing a value in the UI writes a fresh score with no draft comment).

    Idempotent: each draft has a deterministic id, so a second
    ``sync-annotation-queue`` run overwrites the draft rather than adding
    another score for the same question."""
    for name, value in answers.items():
        if value is None:
            continue
        config_id = config_ids.get(name)
        if config_id is None:
            continue  # ANNOTATION-source scores require a config_id
        client.api.scores.create(
            id=_draft_score_id(trace_id, name),
            name=name,
            value=value,
            trace_id=trace_id,
            comment=DRAFT_COMMENT,
            config_id=config_id,
            source="ANNOTATION",
        )


def _score_time(score: Any) -> float:
    """Sort key for picking the most recent score. Missing timestamp -> 0."""
    ts = getattr(score, "timestamp", None) or getattr(score, "created_at", None)
    if ts is None:
        return 0.0
    try:
        return ts.timestamp()
    except AttributeError:
        return float(ts) if isinstance(ts, (int, float)) else 0.0


def read_annotations(
    client: "Langfuse", trace_id: str, question_names: Iterable[str]
) -> dict[str, Any]:
    """Latest score value per question for one trace. Returns
    ``{question: value}`` for every question that has a score.

    Since drafts and real answers both have source ``ANNOTATION`` (a draft
    must, or Langfuse won't pre-fill the annotate form) and the read API does
    not return a score's comment, the two cannot be told apart here. "Has a
    human reviewed this case" is a whole-case question, answered by the
    reviewer marking the queue item COMPLETED -- see
    :func:`completed_trace_ids`. Among scores for one question the newest by
    timestamp wins, so a reviewer's correction supersedes the seeded draft.
    """
    names = set(question_names)
    best: dict[str, dict[str, Any]] = {}
    for score in _scores_for_trace(client, trace_id):
        if score.name not in names:
            continue
        value = getattr(score, "value", None)
        if value is None:
            value = getattr(score, "string_value", None)
        ts = _score_time(score)
        current = best.get(score.name)
        if current is None or ts >= current["_ts"]:
            best[score.name] = {"value": value, "_ts": ts}
    return {name: e["value"] for name, e in best.items()}


# --- pagination helpers -------------------------------------------------------

def _all_score_configs(client: "Langfuse") -> list[Any]:
    out: list[Any] = []
    page = 1
    while True:
        result = client.api.score_configs.get(page=page, limit=100)
        out.extend(result.data)
        if len(result.data) < 100:
            return out
        page += 1


def _all_queues(client: "Langfuse") -> list[Any]:
    out: list[Any] = []
    page = 1
    while True:
        result = client.api.annotation_queues.list_queues(page=page, limit=100)
        out.extend(result.data)
        if len(result.data) < 100:
            return out
        page += 1


def _all_queue_items(client: "Langfuse", queue_id: str) -> list[Any]:
    out: list[Any] = []
    page = 1
    while True:
        result = client.api.annotation_queues.list_queue_items(queue_id, page=page, limit=100)
        out.extend(result.data)
        if len(result.data) < 100:
            return out
        page += 1


def _scores_for_trace(client: "Langfuse", trace_id: str) -> list[Any]:
    # v4 read path -- the deprecated scores.get_many 404s in events_only mode.
    # Cursor-paged: a trace re-synced many times can hold >100 scores.
    out: list[Any] = []
    cursor: str | None = None
    while True:
        result = client.api.scores_v3.get_many_v3(
            trace_id=trace_id, limit=100, cursor=cursor
        )
        out.extend(result.data)
        cursor = getattr(getattr(result, "meta", None), "cursor", None)
        if not cursor:
            return out
