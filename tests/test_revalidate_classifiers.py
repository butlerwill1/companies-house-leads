from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.langfuse_eval_helpers import revalidate_classifiers as revalidate


def test_revalidate_search_screen_writes_a_separate_audit_without_changing_input(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    output = tmp_path / "audit.jsonl"
    original = {"company_number": "1", "raw": '{"answer":"likely","quote":"site sales","reason":"x"}',
                "answer": "likely", "passes": True}
    source.write_text(json.dumps(original) + "\n", encoding="utf-8")

    assert revalidate.main(["--pipeline", "search-screen", "--input", str(source), "--output", str(output)]) == 0
    assert json.loads(source.read_text(encoding="utf-8")) == original
    audit = json.loads(output.read_text(encoding="utf-8"))
    assert audit["validation"]["version"] == "classifier-validation-v1"
    assert audit["validation"]["evidence"]["status"] == "not_checked"


@pytest.mark.parametrize("pipeline", ["business-profile", "web-profile", "website-identity", "vlm"])
def test_revalidate_supports_every_other_pipeline_without_a_model_call(tmp_path: Path, pipeline: str) -> None:
    source = tmp_path / "input.jsonl"
    output = tmp_path / "audit.jsonl"
    source.write_text(json.dumps({"company_number": "1", "raw": "{}"}) + "\n", encoding="utf-8")

    assert revalidate.main(["--pipeline", pipeline, "--input", str(source), "--output", str(output)]) == 0
    audit = json.loads(output.read_text(encoding="utf-8"))
    assert audit["pipeline"] == pipeline
    assert audit["validation"]["version"] == "classifier-validation-v1"
