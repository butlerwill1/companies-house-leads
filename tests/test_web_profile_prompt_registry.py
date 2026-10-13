from __future__ import annotations

from scripts.website_analysis import web_profile_prompt_registry as R
from tests.langfuse_fakes import FakeLangfuse


def test_the_langfuse_template_renders_like_the_python_one():
    R.verify_round_trip()


def test_register_labels_the_current_version():
    client = FakeLangfuse()
    reference = R.register(client)
    assert reference is not None and reference.startswith(f"web-profile@{R.policy.PROMPT_VERSION}")
