from __future__ import annotations

import json

from scripts.web import web_settle as S
from scripts.web import web_settle_model as M

INPUTS = {"company_name": "HEATON GROUP DEVELOPMENTS LIMITED", "company_number": "08615014",
          "registered_office": "Wigan WN2 3BE", "principal_activity": "Construction of domestic buildings for sale.", "sic": "Property development",
          "listing": "none found", "domain": "heatongroup.co.uk", "facts": "the registered postcode appears on the site",
          "title": "Property Development | Heaton Group",
          "home_excerpt": "Heaton Group is an award-winning UK property development group creating vibrant communities.",
          "snippets": "- The Heaton Group is a trading style of Heaton Group Manchester Limited"}
CASE = {"company_number": "08615014", "company_name": "HEATON GROUP DEVELOPMENTS LIMITED", "domain": "heatongroup.co.uk",
        "evidence": {"pages_read": 19, "postcode_found": True}, "inputs": INPUTS}


def _answer(**over):
    return json.dumps({"verdict": "same_business", "reason": "The brand and the activity match.",
                       "quote": "award-winning UK property development group", **over})


def test_the_prompt_carries_the_filing_the_site_text_and_the_footer():
    prompt = M.build_prompt(INPUTS)
    assert "Construction of domestic buildings for sale." in prompt and "trading style of Heaton Group Manchester Limited" in prompt
    assert "{" not in prompt.split("Answer with one JSON object")[0]


def test_a_same_business_verdict_must_quote_the_text_it_was_shown():
    assert M.parse_verdict(_answer(), INPUTS)["verdict"] == "same_business"
    assert M.parse_verdict(_answer(), INPUTS)["quote_valid"] == 1
    bad = M.parse_verdict(_answer(quote="a family run letting agency in Peterborough"), INPUTS)
    assert bad["verdict"] == "cannot_tell" and bad["quote_valid"] == 0 and "quote not found" in bad["problem"]
    assert M.parse_verdict(_answer(verdict="different_business", quote=""), INPUTS)["verdict"] == "different_business"
    # the filing's and the listing's words are not the site's
    assert M.parse_verdict(_answer(quote="Construction of domestic buildings for sale."), INPUTS)["verdict"] == "cannot_tell"


def test_unusable_or_off_list_answers_are_cannot_tell():
    assert M.parse_verdict(None, INPUTS)["problem"] == "empty response"
    assert M.parse_verdict("no idea", INPUTS)["problem"] == "unparseable response"
    assert M.parse_verdict(_answer(verdict="probably", quote=""), INPUTS)["verdict"] == "cannot_tell"


def test_run_checkpoints_each_company_and_replays_on_a_rerun(tmp_path):
    path = tmp_path / "cp.jsonl"
    calls = []

    def fake(api_key, model, prompt, timeout):
        calls.append(prompt)
        return _answer(), {"prompt_tokens": 900, "completion_tokens": 40}

    results = M.run([CASE], "m", call=fake, api_key="k", checkpoint=path, workers=1, log=lambda _: None)
    assert results[0]["verdict"] == "same_business" and len(calls) == 1
    again = M.run([CASE], "m", call=fake, api_key="k", checkpoint=path, workers=1, log=lambda _: None)
    assert len(calls) == 1 and again[0]["verdict"] == "same_business"        # replayed, no second call
    path.write_text(path.read_text() + '{"company_number": "000')              # a torn final line is ignored
    assert len(M.load_checkpoint(path)) == 1


def test_a_failed_request_is_recorded_and_retried_next_run(tmp_path):
    path = tmp_path / "cp.jsonl"

    def broken(*a):
        raise RuntimeError("boom")

    first = M.run([CASE], "m", call=broken, api_key="k", checkpoint=path, workers=1, log=lambda _: None)
    assert first[0]["problem"].startswith("request failed") and first[0]["verdict"] == "cannot_tell"
    second = M.run([CASE], "m", call=lambda *a: (_answer(), {}), api_key="k", checkpoint=path, workers=1, log=lambda _: None)
    assert second[0]["verdict"] == "same_business"


def test_only_a_valid_same_business_verdict_settles_the_company():
    record = {"company_number": "08615014", "company_name": CASE["company_name"], "tier": "ambiguous",
              "resolver_version": "identity-v3-places-first", "domain": "heatongroup.co.uk",
              "candidates": [{"domain": "heatongroup.co.uk", "tier": "ambiguous", "sources": ["maps"], "evidence": {}}]}
    good = M.run_case("k", "m", CASE, 10, call=lambda *a: (_answer(), {}))
    bad = M.run_case("k", "m", CASE, 10, call=lambda *a: (_answer(verdict="different_business", quote=""), {}))
    settled = M.settled_records([record], [good, bad])
    assert len(settled) == 1
    assert settled[0]["tier"] == "probable" and settled[0]["settled_rule"] == "model:same_business"
    assert settled[0]["resolver_version"] == S.SETTLED_VERSION and settled[0]["candidates"][0]["tier"] == "probable"
    assert M.settled_records([record], [bad]) == []
    assert record["tier"] == "ambiguous"                                      # the original is untouched


def test_a_global_brand_site_with_no_sign_of_the_uk_is_held_back():
    record = {"company_number": "08615014", "company_name": CASE["company_name"], "tier": "ambiguous",
              "resolver_version": "identity-v3-places-first", "domain": "heatongroup.co.uk",
              "candidates": [{"domain": "heatongroup.co.uk", "tier": "ambiguous", "sources": ["maps"], "evidence": {}}]}
    uk = {**CASE, "evidence": {"uk_site": True}}
    abroad = {**CASE, "evidence": {"uk_site": False, "postcode_found": False}}
    postcode = {**CASE, "evidence": {"uk_site": False, "postcode_found": True}}
    judge = lambda case: M.run_case("k", "m", case, 10, call=lambda *a: (_answer(), {}))   # noqa: E731
    assert len(M.settled_records([record], [judge(uk)])) == 1
    assert len(M.settled_records([record], [judge(postcode)])) == 1
    assert M.settled_records([record], [judge(abroad)]) == []
