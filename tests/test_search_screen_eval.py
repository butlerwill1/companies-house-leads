from pathlib import Path

from scripts.screen import search_screen_eval as ev
from scripts.screen import search_screen_policy as policy

TEXT = "The principal activity of the company is retail of homeware through its own website."


def test_clean_answer_with_a_verbatim_quote():
    out = policy.parse_answer('{"answer": "Likely", "quote": "retail of homeware through its own website", "reason": "x"}', TEXT)
    assert out["answer"] == "likely" and out["quote_ok"] is True and out["passes"] is True


def test_invented_quote_is_flagged_but_the_answer_stands():
    out = policy.parse_answer('{"answer": "unlikely", "quote": "a sentence that is not there at all", "reason": "x"}', TEXT)
    assert out["answer"] == "unlikely" and out["quote_ok"] is False and out["passes"] is False


def test_unparseable_or_invalid_responses_fail_open():
    assert policy.parse_answer("not json", TEXT)["passes"] is True
    bad = policy.parse_answer('{"answer": "maybe", "quote": "x", "reason": "y"}', TEXT)
    assert bad["answer"] is None and bad["passes"] is True and bad["problem"]


def test_only_unlikely_is_rejected():
    for label, passes in (("likely", True), ("possible", True), ("unlikely", False)):
        assert policy.parse_answer('{"answer": "%s", "quote": "", "reason": ""}' % label, TEXT)["passes"] is passes


def test_short_extract_skips_the_contents_page_and_boilerplate(tmp_path: Path):
    filed = "\n".join([
        "CONTENTS", "Directors' report", "DIRECTORS' REPORT",
        "The principal activity of the company continued to be that of property development and management.",
        "Statement of directors' responsibilities",
        "DIRECTORS' REPORT (CONTINUED)", "Turnover figures that must not appear in the extract",
    ])
    case = {"company_number": "0", "sections": {"filed_report": filed}}
    text = policy.short_extract(case, raw_dir=tmp_path)
    assert "property development and management" in text
    assert "must not appear" not in text


def _case(number, gold):
    return {"company_number": number, "expected": {"search_screen": {"value": gold}}}


def test_score_counts_recall_on_the_pass_decision():
    cases = [_case("1", "likely"), _case("2", "possible"), _case("3", "unlikely"), _case("4", "likely")]
    records = [
        {"company_number": "1", "answer": "likely", "passes": True},
        {"company_number": "2", "answer": "unlikely", "passes": False},
        {"company_number": "3", "answer": "unlikely", "passes": False},
        {"company_number": "4", "answer": None, "passes": True},  # unparseable: fails open
    ]
    result = ev.score(cases, records)
    assert result["recall_likely"] == (2, 2)
    assert result["recall_likely_or_possible"] == (2, 3)
    assert result["removal_rate"] == 0.5
    assert result["gold_unlikely_rejected"] == (1, 1)
    assert result["confusion"]["likely"]["none"] == 1


def test_checkpoint_keeps_each_version_and_run_apart_and_skips_half_written_lines(tmp_path: Path):
    import json
    path = tmp_path / "c.jsonl"
    base = {"model": "m", "input": "short", "prompt_version": "v1", "company_number": "1"}
    legacy = base                                   # written before run ids existed: run 1
    second_run = {**base, "run_id": 2}
    other_version = {**base, "prompt_version": "v2"}
    path.write_text("\n".join(json.dumps(r) for r in (legacy, second_run, other_version)) + "\n" + '{"model": "m", "inp',
                    encoding="utf-8")
    done = ev.load_checkpoint(path)
    assert set(done) == {("m", "short", "v1", 1, "1"), ("m", "short", "v1", 2, "1"), ("m", "short", "v2", 1, "1")}


def test_a_second_run_is_not_a_replay_of_the_first():
    assert ev._key("m", "short", "1", "v1", 1) != ev._key("m", "short", "1", "v1", 2)


def test_v1_prompt_is_unchanged_and_v2_states_the_contractor_rule():
    v1 = policy.build_prompt(company_name="X", sic_label="s", text="t", version=policy.PROMPT_VERSION_V1)
    v2 = policy.build_prompt(company_name="X", sic_label="s", text="t", version=policy.PROMPT_VERSION_V2)
    assert "tender- or framework-only public contracting" in v1 and "Evidence decides" not in v1
    assert "Evidence decides" in v2 and "framework-only" not in v2
    assert policy.PROMPT_VERSION != policy.PROMPT_VERSION_V1


def test_v3_is_current_and_v2_is_kept_verbatim():
    v2 = policy.build_prompt(company_name="X", sic_label="s", text="t", version=policy.PROMPT_VERSION_V2)
    v3 = policy.build_prompt(company_name="X", sic_label="s", text="t")
    assert "v3" in policy.PROMPT_VERSION and "A direct channel settles it" in v3
    assert "Evidence decides" in v2 and "A direct channel settles it" not in v2
