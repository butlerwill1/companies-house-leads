"""DeepEval LLM-as-judge metrics for the free-text quality the deterministic
scorers can't measure.

Business profile: ``business_description`` faithfulness -- is every claim in
the one free-text field grounded in the filed narrative? (Today that field is
only checked non-empty.)

VLM: an advisory ``extraction plausibility`` GEval rubric -- the deterministic
cell report stays authoritative.

The judge runs on OpenRouter (this repo has no OpenAI/Anthropic SDK), model
from ``config["langfuse"]["deepeval"]["judge_model"]`` (default
``google/gemini-2.5-flash``). Everything here is lazy-imported and inert when
``deepeval.enabled`` is false or the package is missing.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:  # pragma: no cover
    from deepeval.models import DeepEvalBaseLLM

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_JUDGE = "google/gemini-2.5-flash"


def judge_enabled(config: dict[str, Any]) -> bool:
    block = (config.get("langfuse") or {}).get("deepeval") or {}
    return bool(block.get("enabled"))


def judge_model_name(config: dict[str, Any]) -> str:
    block = (config.get("langfuse") or {}).get("deepeval") or {}
    return str(block.get("judge_model") or _DEFAULT_JUDGE)


def openrouter_complete(
    model: str, prompt: str, *, api_key: str | None, json_mode: bool = False, timeout: int = 120
) -> str:
    """One OpenRouter chat completion -- same endpoint/auth/`temperature=0` as
    ``BusinessProfileModelClient``. Standalone so it is unit-testable without
    deepeval installed."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    response = requests.post(
        _OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"OpenRouter judge call failed: {body['error']}")
    return body["choices"][0]["message"]["content"]


def make_judge(config: dict[str, Any], *, api_key: str | None = None) -> Any:
    """Build the DeepEval model wrapper, or raise ImportError if deepeval is
    absent (callers guard with ``judge_enabled`` first)."""
    from deepeval.models import DeepEvalBaseLLM

    model = judge_model_name(config)
    key = api_key or os.getenv("OPENROUTER_API_KEY")

    class OpenRouterJudge(DeepEvalBaseLLM):
        def __init__(self) -> None:
            self._model = model
            self._key = key

        def load_model(self) -> "OpenRouterJudge":
            return self

        def get_model_name(self) -> str:
            return f"openrouter:{self._model}"

        def generate(self, prompt: str, schema: Any = None) -> Any:
            text = openrouter_complete(self._model, prompt, api_key=self._key, json_mode=schema is not None)
            if schema is None:
                return text
            data = json.loads(text)
            return schema.model_validate(data) if hasattr(schema, "model_validate") else data

        async def a_generate(self, prompt: str, schema: Any = None) -> Any:
            return self.generate(prompt, schema)

    return OpenRouterJudge()


def business_description_faithfulness_metric(judge: Any, *, threshold: float = 0.7) -> Any:
    from deepeval.metrics import FaithfulnessMetric

    return FaithfulnessMetric(model=judge, threshold=threshold, include_reason=True)


def measure_with_timeout(metric: Any, test_case: Any, *, timeout: int = 90) -> tuple[float | None, str | None]:
    """Run ``metric.measure(test_case)`` with a hard wall-clock cap.

    DeepEval metrics make several LLM calls and, with a non-OpenAI judge whose
    JSON is occasionally malformed, can retry long enough to stall a whole eval
    run. This bounds one case: a timeout (or any error) yields ``(None, None)``
    and the caller logs no score for that case.
    """
    import concurrent.futures

    def _run() -> tuple[float | None, str | None]:
        metric.measure(test_case)
        score = metric.score
        return (float(score) if score is not None else None, metric.reason or None)

    # Do not use a `with` block: on timeout the worker thread cannot be killed,
    # and the executor's context-manager exit would block waiting for it.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run)
    try:
        return future.result(timeout=timeout)
    except Exception:
        return None, None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def business_description_test_case(case: dict[str, Any], description: str) -> Any:
    """An LLMTestCase whose retrieval context is the filed narrative -- the
    faithfulness metric then checks the description's claims against it."""
    from deepeval.test_case import LLMTestCase

    sections = case.get("sections") or {}
    context = [f"[{key}]\n{text}" for key, text in sections.items() if text]
    return LLMTestCase(
        input=f"Describe what {case.get('company_name') or 'this company'} does, "
        "in one sentence, using only its filed accounts narrative.",
        actual_output=description,
        retrieval_context=context or ["(no narrative sections)"],
    )


def vlm_plausibility_metric(judge: Any, *, threshold: float = 0.6) -> Any:
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    return GEval(
        name="extraction plausibility",
        model=judge,
        threshold=threshold,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        evaluation_steps=[
            "Check whether the extracted financial figures are internally consistent "
            "(totals equal the sum of their parts; current and prior year are plausible).",
            "Check whether the figures and units are consistent with the statement text provided.",
            "Penalise fabricated precision, impossible values, or figures with no support in the input.",
        ],
    )
