from __future__ import annotations

from scripts.vlm import vlm_prompt_history as H
from scripts.vlm import vlm_prompt_registry as R
from tests.langfuse_fakes import FakeLangfuse


def test_the_newest_historical_version_is_the_current_code():
    rows = H.history()
    assert rows[-1][4] == R.langfuse_templates()
    assert H.version_label(rows[-1][0]) == R.PROMPT_VERSION


def test_prompts_appear_at_the_version_they_were_introduced():
    first = {}
    for number, _commit, _date, _note, templates in H.history():
        for name in templates:
            first.setdefault(name.split("/")[1], number)
    assert first == {"locator": 1, "extraction": 1, "rationalisation": 1, "employee-extraction": 4,
                     "coverage-recovery": 4, "row-validation-recovery": 4, "completeness-recovery": 6}


def test_publishing_the_history_gives_each_prompt_one_version_per_text_change():
    client = FakeLangfuse()
    H.publish(client, H.history(), reset=False)
    counts = {name.split("/")[1]: len(versions) for name, versions in client.prompts.items()}
    assert counts == {"locator": 4, "extraction": 7, "rationalisation": 5, "employee-extraction": 3,
                      "coverage-recovery": 5, "row-validation-recovery": 5, "completeness-recovery": 3}
    assert all(R.prompt_references(client).values())
