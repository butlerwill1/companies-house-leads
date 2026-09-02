"""A minimal in-memory fake of the bits of the Langfuse v4 SDK that
``scripts/eval_support`` touches. Not a pytest module (no ``test_`` prefix)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any


class _Page:
    def __init__(self, data: list[Any]) -> None:
        self.data = data


class _ScoreConfigs:
    def __init__(self) -> None:
        self._items: list[Any] = []

    def get(self, *, page: int = 1, limit: int = 100) -> _Page:
        return _Page(list(self._items))

    def create(self, *, name: str, data_type: str, categories: Any = None, description: str | None = None) -> Any:
        cfg = SimpleNamespace(id=f"cfg-{name}", name=name, data_type=data_type, categories=categories)
        self._items.append(cfg)
        return cfg


class _AnnotationQueues:
    def __init__(self) -> None:
        self.queues: list[Any] = []
        self.items: dict[str, list[Any]] = {}

    def list_queues(self, *, page: int = 1, limit: int = 100) -> _Page:
        return _Page(list(self.queues))

    def create_queue(self, *, name: str, score_config_ids: list[str], description: str | None = None) -> Any:
        q = SimpleNamespace(id=f"q-{name}", name=name, score_config_ids=list(score_config_ids))
        self.queues.append(q)
        self.items[q.id] = []
        return q

    def list_queue_items(self, queue_id: str, *, status: str | None = None, page: int = 1, limit: int = 100) -> _Page:
        return _Page(list(self.items.get(queue_id, [])))

    def create_queue_item(self, queue_id: str, *, object_id: str, object_type: str, status: str | None = None) -> Any:
        item = SimpleNamespace(id=f"it-{uuid.uuid4().hex[:8]}", object_id=object_id, object_type=object_type, status=status or "PENDING")
        self.items.setdefault(queue_id, []).append(item)
        return item

    def delete_queue_item(self, queue_id: str, item_id: str) -> None:
        self.items[queue_id] = [i for i in self.items.get(queue_id, []) if i.id != item_id]

    def update_queue_item(self, queue_id: str, item_id: str, *, status: str | None = None) -> Any:
        for i in self.items.get(queue_id, []):
            if i.id == item_id:
                i.status = status
                return i
        raise KeyError(item_id)


class _ScoresV3:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    def get_many_v3(self, *, trace_id: str | None = None, limit: int = 100, **kwargs: Any) -> _Page:
        return _Page([s for s in self._store if trace_id is None or s.trace_id == trace_id])


class _Datasets:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def get(self, name: str) -> Any:
        if name not in self._store:
            raise KeyError(name)
        return self._store[name]


class FakeDatasetClient:
    def __init__(self, name: str, items: list[Any], on_run: Any) -> None:
        self.name = name
        self.items = items
        self._on_run = on_run

    def run_experiment(self, *, name: str, run_name: str | None = None, description: str | None = None,
                       task: Any, evaluators: list[Any] = (), run_evaluators: list[Any] = (),
                       max_concurrency: int = 1, metadata: Any = None) -> Any:
        return self._on_run(self, run_name or name, task, list(evaluators), list(run_evaluators))


class FakeLangfuse:
    """Records calls; runs experiments synchronously in-process."""

    def __init__(self) -> None:
        self.datasets_store: dict[str, Any] = {}
        self.dataset_items: dict[str, list[Any]] = {}
        self.scores: list[Any] = []
        self.prompts: dict[str, list[Any]] = {}
        self.flushed = 0
        self.score_configs = _ScoreConfigs()
        self.annotation_queues = _AnnotationQueues()
        self.api = SimpleNamespace(
            datasets=_Datasets(self.datasets_store),
            score_configs=self.score_configs,
            annotation_queues=self.annotation_queues,
            scores_v3=_ScoresV3(self.scores),
        )

    # -- top-level client methods --
    def create_dataset(self, *, name: str, description: str | None = None, metadata: Any = None) -> Any:
        ds = SimpleNamespace(name=name, description=description, metadata=metadata or {})
        self.datasets_store[name] = ds
        self.dataset_items.setdefault(name, [])
        return ds

    def create_dataset_item(self, *, dataset_name: str, id: str, input: Any = None,
                            expected_output: Any = None, metadata: Any = None) -> Any:
        items = self.dataset_items.setdefault(dataset_name, [])
        items[:] = [i for i in items if i.id != id]
        item = SimpleNamespace(id=id, input=input, expected_output=expected_output, metadata=metadata)
        items.append(item)
        return item

    def get_dataset(self, name: str) -> FakeDatasetClient:
        return FakeDatasetClient(name, list(self.dataset_items.get(name, [])), self._run_experiment)

    def _run_experiment(self, dataset: FakeDatasetClient, run_name: str, task: Any,
                        evaluators: list[Any], run_evaluators: list[Any]) -> Any:
        item_results = []
        for item in dataset.items:
            trace_id = f"tr-{item.id}"
            output = task(item=item)
            evals = []
            for ev in evaluators:
                r = ev(input=item.input, output=output, expected_output=item.expected_output,
                       metadata=item.metadata)
                evals.extend(r if isinstance(r, list) else [r])
            for e in evals:
                self.scores.append(SimpleNamespace(name=e.name, value=e.value, string_value=None,
                                                   data_type=getattr(e, "data_type", None),
                                                   source="API", comment=getattr(e, "comment", None),
                                                   config_id=getattr(e, "config_id", None), trace_id=trace_id))
            item_results.append(SimpleNamespace(item=item, output=output, evaluations=evals, trace_id=trace_id))
        run_evals = []
        for rev in run_evaluators:
            run_evals.append(rev(item_results=item_results))
        return SimpleNamespace(run_name=run_name, dataset_run_id=f"run-{run_name}",
                               dataset_run_url=f"http://fake/runs/{run_name}",
                               experiment_id=f"exp-{run_name}", item_results=item_results,
                               run_evaluations=run_evals, format=lambda **k: "fake result")

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        return _FakeSpanCtx(self)

    def create_score(self, *, name: str, value: Any, trace_id: str | None = None, dataset_run_id: str | None = None,
                     data_type: str | None = None, comment: str | None = None, config_id: str | None = None,
                     metadata: Any = None, **kwargs: Any) -> None:
        self.scores.append(SimpleNamespace(name=name, value=value, string_value=None, data_type=data_type,
                                           source="API", comment=comment, config_id=config_id,
                                           trace_id=trace_id, dataset_run_id=dataset_run_id))

    def create_prompt(self, *, name: str, prompt: str, labels: list[str] = (), tags: list[str] = None,
                      type: str = "text", config: Any = None, commit_message: str | None = None) -> Any:
        versions = self.prompts.setdefault(name, [])
        p = SimpleNamespace(name=name, prompt=prompt, version=len(versions) + 1, tags=list(tags or []),
                            labels=list(labels))
        versions.append(p)
        return p

    def get_prompt(self, name: str, *, version: int | None = None, label: str | None = None, **kwargs: Any) -> Any:
        versions = self.prompts.get(name)
        if not versions:
            raise KeyError(name)
        return versions[-1]

    def flush(self) -> None:
        self.flushed += 1

    def auth_check(self) -> bool:
        return True


class _FakeSpanCtx:
    def __init__(self, client: FakeLangfuse) -> None:
        self._client = client
        self.trace_id = f"tr-{uuid.uuid4().hex[:8]}"
        self.id = f"sp-{uuid.uuid4().hex[:8]}"

    def __enter__(self) -> "_FakeSpanCtx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def set_trace_io(self, *, input: Any = None, output: Any = None) -> "_FakeSpanCtx":
        return self

    def update(self, **kwargs: Any) -> "_FakeSpanCtx":
        return self

    def start_as_current_observation(self, **kwargs: Any) -> "_FakeSpanCtx":
        return _FakeSpanCtx(self._client)
