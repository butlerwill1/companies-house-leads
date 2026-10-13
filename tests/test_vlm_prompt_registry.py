from __future__ import annotations

import json
import re

from scripts.pdf_vision_extraction import companies_house_pdf_vlm_financials as pipeline
from scripts.pdf_vision_extraction import vlm_prompt_registry as R
from tests.langfuse_fakes import FakeLangfuse


def test_the_langfuse_templates_render_like_the_pipeline():
    R.verify_round_trips()


def test_each_template_has_exactly_its_per_call_placeholders():
    found = {name: set(re.findall(r"\{\{(\w+)\}\}", text)) for name, text in R.langfuse_templates().items()}
    assert found == {
        "vlm-financial/locator": set(),
        "vlm-financial/extraction": set(),
        "vlm-financial/employee-extraction": set(),
        "vlm-financial/coverage-recovery": {"page_number"},
        "vlm-financial/row-validation-recovery": set(),
        "vlm-financial/completeness-recovery": {"completeness_signals"},
        "vlm-financial/rationalisation": {"company_context", "candidates"},
    }


def test_the_builders_send_the_same_text_as_the_inline_prompts_they_replaced():
    E = pipeline.EXTRACTION_PROMPT
    H = pipeline.HIGH_RESOLUTION_RECOVERY_PROMPT
    page_number = 12
    assert pipeline.coverage_recovery_prompt(page_number) == (
        f"{E}\n\n"
        f"Coverage recovery: return the rows for Document page {page_number}. "
        "This page was classified as a primary financial statement. "
        "Do not omit it and do not return any other page.\n\n"
        f"{H}"
    )
    assert pipeline.row_validation_recovery_prompt() == (
        f"{E}\n\n{pipeline.ROW_VALIDATION_RECOVERY_PROMPT}\n\n{H}"
    )
    triggers = ["turnover_missing", "cash_missing"]
    assert pipeline.statement_completeness_recovery_prompt(", ".join(triggers)) == (
        f"{E}\n\n{pipeline.STATEMENT_COMPLETENESS_RECOVERY_PROMPT}\n\n"
        f"Completeness signals for this page: {', '.join(triggers)}."
    )
    company_context = {"sic_codes": ["47910"]}
    candidates = [{"id": "c1", "metric": "turnover"}]
    assert pipeline.rationalisation_prompt(
        json.dumps(company_context, separators=(",", ":")),
        json.dumps({"candidates": candidates}, separators=(",", ":")),
    ) == (
        f"{pipeline.RATIONALISATION_PROMPT}\n\n"
        f"COMPANY_CONTEXT_ADVISORY_ONLY:\n"
        f"{json.dumps(company_context, separators=(',', ':'))}\n\n"
        f"CANDIDATES:\n{json.dumps({'candidates': candidates}, separators=(',', ':'))}"
    )


def test_register_publishes_and_labels_every_prompt():
    client = FakeLangfuse()
    references = R.register(client)
    assert set(references) == set(R.PROMPT_NAMES)
    for name, reference in references.items():
        assert reference == f"{name}@{pipeline.PROMPT_VERSION} [langfuse v1]"
        assert client.prompts[name][0].prompt == R.langfuse_templates()[name]


def test_registering_the_same_version_again_adds_no_versions():
    client = FakeLangfuse()
    R.register(client)
    R.register(client)
    assert {name: len(versions) for name, versions in client.prompts.items()} == {
        name: 1 for name in R.PROMPT_NAMES}


def test_a_bump_publishes_only_the_prompt_that_changed(monkeypatch):
    client = FakeLangfuse()
    R.register(client)
    monkeypatch.setattr(R, "PROMPT_VERSION", "vlm-financials-v10")
    monkeypatch.setattr(pipeline, "LOCATOR_PROMPT", pipeline.LOCATOR_PROMPT + " Changed.")
    references = R.register(client)

    locator = client.prompts["vlm-financial/locator"]
    assert len(locator) == 2 and locator[1].prompt.endswith(" Changed.")
    assert "production" not in locator[0].labels
    for name in R.PROMPT_NAMES:
        if name != "vlm-financial/locator":
            (only,) = client.prompts[name]
            assert {"production", "vlm-financials-v9", "vlm-financials-v10"} <= set(only.labels)
    assert all(reference and "@vlm-financials-v10 " in reference for reference in references.values())


def test_references_are_none_when_the_registry_is_behind_the_code(monkeypatch):
    client = FakeLangfuse()
    R.register(client)
    monkeypatch.setattr(R, "PROMPT_VERSION", "vlm-financials-v10")
    assert R.prompt_references(client) == {name: None for name in R.PROMPT_NAMES}
    assert R.prompt_references(None) == {}
