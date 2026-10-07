from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.website_analysis import web_profile_eval as E
from scripts.website_analysis import web_profile_policy as P
from scripts.website_analysis.web_fetch import Fetcher, Page

TEXT = ("[home: https://x.co.uk/] Bott and Co is a specialist consumer rights law firm. We help people claim flight "
        "delay compensation. Request a free quote online or call us on 01625 415800. We act for clients across the "
        "North West of England, including Manchester and Wilmslow.")
GOOD = {
    "summary": "A consumer rights law firm handling flight delay and car finance claims.",
    "products_services": ["flight delay claims", "car finance claims"],
    "customer_type": {"value": "consumer", "quote": "We help people claim flight delay compensation"},
    "conversion_action": {"value": "enquiry_form", "quote": "Request a free quote online"},
    "geography": {"value": "regional", "main_town": "Wilmslow", "quote": "clients across the North West of England"},
    "urgency": "considered", "ticket_band": "100_to_1000", "channel_fit": "search",
    "google_category": "Law firm",
    "seed_keywords": ["Flight Delay Compensation", "flight delay claim", "car finance claim", "pcp claim",
                      "claim for cancelled flight", "bott and co reviews", "flight delay claim"],
}


def _raw(**over):
    return json.dumps({**GOOD, **over})


# ---------------------------------------------------------------- policy

def test_parse_a_good_response_with_valid_quotes():
    out = P.parse_profile(_raw(), TEXT, listing_category="Law firm", brand_terms=["Bott and Co"])
    assert out["customer_type"] == "consumer" and out["customer_type_quote_valid"] == 1
    assert out["conversion_action"] == "enquiry_form" and out["geography"] == "regional" and out["main_town"] == "Wilmslow"
    assert out["urgency"] == "considered" and out["ticket_band"] == "100_to_1000" and out["channel_fit"] == "search"
    assert out["google_category"] == "Law firm" and out["category_source"] == "google_listing"
    assert out["seed_keywords"] == ["flight delay compensation", "flight delay claim", "car finance claim",
                                    "pcp claim", "claim for cancelled flight"]      # deduped, brand phrase removed
    assert out["problem"] is None


def test_a_quote_not_in_the_text_is_flagged_not_dropped():
    out = P.parse_profile(_raw(customer_type={"value": "business", "quote": "we serve large corporates"}), TEXT)
    assert out["customer_type"] == "business" and out["customer_type_quote_valid"] == 0
    assert "customer_type quote not found" in out["problem"]


def test_quote_matching_ignores_case_whitespace_and_trailing_punctuation():
    assert P.quote_in_text("  REQUEST a free\nquote online. ", TEXT)
    assert not P.quote_in_text("", TEXT) and not P.quote_in_text(None, TEXT)


ACTIVITY = "The principal activity of the company is that of the retail of civil, safety and construction products."


def test_a_quote_from_the_principal_activity_line_is_valid():
    # The prompt shows the filing's principal activity, so quoting it is evidence;
    # the first live run rejected five such quotes by checking the site text only.
    raw = _raw(customer_type={"value": "business", "quote": "the retail of civil, safety and construction products"})
    out = P.parse_profile(raw, TEXT, principal_activity=ACTIVITY)
    assert out["customer_type_quote_valid"] == 1 and out["customer_type_quote_match"] == "exact"
    assert P.parse_profile(raw, TEXT)["customer_type_quote_valid"] == 0


def test_quote_match_kinds_and_their_limits():
    sources = [TEXT, ACTIVITY]
    assert P.quote_match("Request a free quote online", sources) == "exact"
    # one ordinary word tidied in a long quote: fuzzy, recorded as such
    assert P.quote_match("Bott and Co is a specialist consumer rights legal firm", sources) == "fuzzy"
    # a word a label turns on may not differ, however long the quote
    assert P.quote_match("Bott and Co is a specialist business rights law firm", sources) is None
    # two real passages joined with an ellipsis: each must be found
    assert P.quote_match("specialist consumer rights law firm ... clients across the North West", sources) == "joined"
    assert P.quote_match("specialist consumer rights law firm … clients across Scotland", sources) is None
    assert P.quote_match("throughout the UK", sources) is None


def test_values_outside_the_enumeration_become_unclear_and_need_no_quote():
    out = P.parse_profile(_raw(urgency="very urgent", channel_fit="telepathy",
                               conversion_action={"value": "carrier pigeon", "quote": "x"}), TEXT)
    assert out["urgency"] == "unclear" and out["channel_fit"] == "unclear"
    assert out["conversion_action"] == "unclear" and out["conversion_action_quote"] is None
    assert out["conversion_action_quote_valid"] is None


def test_category_is_the_listings_when_there_is_one_else_a_checked_pick():
    shortlist = ["Law firm", "Solicitor"]
    chosen = P.parse_profile(_raw(google_category="law firm"), TEXT, allowed_categories=shortlist)
    assert chosen["google_category"] == "Law firm" and chosen["category_source"] == "model_assigned"
    off_list = P.parse_profile(_raw(google_category="Astronaut training"), TEXT, allowed_categories=shortlist)
    assert off_list["google_category"] is None and "not on the shortlist" in off_list["problem"]
    from_listing = P.parse_profile(_raw(google_category="Astronaut training"), TEXT, listing_category="Lawyer")
    assert from_listing["google_category"] == "Lawyer" and from_listing["category_source"] == "google_listing"


@pytest.mark.parametrize("raw,problem", [(None, "empty response"), ("", "empty response"),
                                         ("I cannot help with that", "unparseable response"), ("[1, 2]", "unparseable response")])
def test_unusable_responses_give_empty_fields_and_a_problem(raw, problem):
    out = P.parse_profile(raw, TEXT, listing_category="Law firm")
    assert out["problem"] == problem and out["customer_type"] is None and out["seed_keywords"] == []
    assert out["google_category"] == "Law firm"


def test_json_in_a_code_fence_or_with_chatter_is_accepted():
    assert P.parse_profile("```json\n" + _raw() + "\n```", TEXT)["customer_type"] == "consumer"
    assert P.parse_profile("Here you go: " + _raw() + " hope that helps", TEXT)["customer_type"] == "consumer"


def test_seed_keywords_are_cleaned_capped_and_brand_free():
    phrases = ["  Boiler   Repair ", "boiler repair", "x " * 9, "Acme boiler cover", "emergency plumber £99", ""] + \
              [f"phrase number {i}" for i in range(30)]
    out = P.seed_keywords(phrases, ["Acme"])
    assert out[0] == "boiler repair" and "acme boiler cover" not in out and len(out) == P.MAX_SEED_KEYWORDS
    assert "emergency plumber £99" in out
    assert P.seed_keywords("not a list") == []


def test_shortlist_categories_by_word_overlap():
    categories = [{"category_name": "dental_clinic", "business_count": 120000},
                  {"category_name": "kitchen_remodeler", "business_count": 30000},
                  {"category_name": "law_firm", "business_count": 90000},
                  {"category_name": "solicitor", "business_count": 20000},
                  {"category_name": "dental_laboratory", "business_count": 5000}]
    shortlist = P.shortlist_categories(categories, "A law firm and solicitor practice offering legal advice", limit=3)
    assert shortlist == ["law_firm", "solicitor"]
    assert P.shortlist_categories(categories, "dental clinic and dental laboratory")[:2] == ["dental_clinic",
                                                                                           "dental_laboratory"]
    assert P.shortlist_categories(categories, "zzz") == []


def test_prompt_contains_the_text_the_listing_and_the_instructions():
    with_listing = P.build_prompt(company_name="Bott", principal_activity="Legal services", listing_category="Law firm",
                                  text=TEXT)
    assert "Google Maps category: Law firm" in with_listing and "copy it" in with_listing and TEXT in with_listing
    without = P.build_prompt(company_name="Bott", principal_activity=None, listing_category=None, text=TEXT,
                             categories=["law_firm", "solicitor"])
    assert '"law_firm", "solicitor"' in without and "not available" in without and "none found" in without
    assert len(P.build_prompt(company_name="B", principal_activity=None, listing_category=None,
                              text="x" * 50_000)) < 20_000


# ---------------------------------------------------------------- inputs

def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    for number, name in (("00000001", "BOTT AND CO SOLICITORS LTD"), ("00000002", "NO SITE LTD"),
                         ("00000003", "BLOCKED LTD")):
        conn.execute("insert into companies (company_number, company_name, company_status, source_mode, "
                     "profile_payload, updated_at) values (?, ?, 'active', 'api', '{}', '2026')", (number, name))
    return conn


def _seed_site(conn, tmp_path, domain, pages):
    fetcher = Fetcher(cache_dir=tmp_path / "pages", respect_robots=False)
    for url, kind, body in pages:
        fetcher.put(Page(url=url, final_url=url, status=200, html=f"<html><body>{body}</body></html>"))
        conn.execute("insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, status_code, "
                     "fetch_error) values (?, ?, ?, 'crawl-v1', 'http', 200, null)", (domain, url, kind))
    return Fetcher(cache_dir=tmp_path / "pages", cache_only=True)


def test_site_text_orders_pages_caps_each_and_reads_only_the_cache(tmp_path):
    conn = _db(tmp_path)
    cache = _seed_site(conn, tmp_path, "x.co.uk", [
        ("https://x.co.uk/contact", "contact", "Contact " + "c" * 5000), ("https://x.co.uk/", "home", "Welcome home"),
        ("https://x.co.uk/privacy", "privacy", "Privacy words"), ("https://x.co.uk/s", "sitemap", "<urlset/>")])
    text = E.site_text(conn, "x.co.uk", cache)
    assert text.index("[home") < text.index("[contact") < text.index("[privacy")
    assert "[sitemap" not in text and len(text.split("[contact")[1].split("\n\n")[0]) < 3200
    assert cache.requests_made == 0
    assert len(E.site_text(conn, "x.co.uk", cache, max_chars=100)) <= 100
    assert E.site_text(conn, "nowhere.co.uk", cache) == ""


def test_principal_activity_line():
    text = "Directors' report\nThe principal activity of the company is legal services.\nOther"
    assert E.principal_activity(text) == "The principal activity of the company is legal services."
    assert E.principal_activity("nothing relevant") is None


def test_build_cases_uses_identity_listing_trading_names_and_shortlist(tmp_path):
    conn = _db(tmp_path)
    cache = _seed_site(conn, tmp_path, "x.co.uk", [("https://x.co.uk/", "home", "We are a law firm. " * 20)])
    conn.execute("insert into company_web_identity (company_number, resolver_version, domain, role, tier, resolved_at) "
                 "values ('00000001', 'v', 'x.co.uk', 'main', 'verified', '2026')")
    conn.execute("insert into company_web_identity (company_number, resolver_version, role, tier, resolved_at) "
                 "values ('00000002', 'v', 'none', 'none', '2026')")
    conn.execute("insert into company_trading_names (company_number, name, source, found_at) "
                 "values ('00000001', 'Bott and Co', 'website', '2026')")
    categories = [{"category_name": "law_firm", "business_count": 10}]
    cases = {c["company_number"]: c for c in E.build_cases(
        conn, ["00000001", "00000002", "99999999"], cache, categories, filing_text=lambda n: "The principal "
        "activity of the company is legal services and advice.")}
    assert set(cases) == {"00000001", "00000002"}
    bott = cases["00000001"]
    assert bott["domain"] == "x.co.uk" and "law firm" in bott["text"] and bott["categories"] == ["law_firm"]
    assert bott["listing_category"] is None and "principal activity" in bott["principal_activity"]
    assert "Bott and Co" in bott["brand_terms"]
    assert cases["00000002"]["domain"] is None and cases["00000002"]["text"] == ""


# ---------------------------------------------------------------- running, checkpointing, storing

def _case(number="00000001", text=TEXT, **kw):
    return {"company_number": number, "company_name": "BOTT AND CO SOLICITORS LTD", "domain": "x.co.uk", "text": text,
            "principal_activity": "Legal services", "listing_category": "Law firm", "categories": [],
            "brand_terms": ["Bott and Co"], **kw}


def test_run_checkpoints_replays_and_retries_failed_requests(tmp_path):
    path = tmp_path / "cp.jsonl"
    calls = []

    def fake(api_key, model, prompt, timeout):
        calls.append(prompt)
        if "00000002" in prompt or "FAIL" in prompt:
            raise RuntimeError("boom")
        return _raw(), {"prompt_tokens": 1000, "completion_tokens": 200}

    cases = [_case("00000001"), _case("00000002", text="FAIL " + TEXT), _case("00000003", text="")]
    records = E.run(cases, "m", call=fake, api_key="k", checkpoint=path, workers=1, log=lambda _: None)
    by = {r["company_number"]: r for r in records}
    assert by["00000001"]["customer_type"] == "consumer" and by["00000001"]["problem"] is None
    assert by["00000002"]["problem"].startswith("request failed") and by["00000003"]["problem"] == "no website text to profile"
    assert len(calls) == 2                                           # the textless company was never sent
    again = E.run(cases, "m", call=fake, api_key="k", checkpoint=path, workers=1, log=lambda _: None)
    assert len(calls) == 3 and len(again) == 3                       # only the failed one is retried
    path.write_text(path.read_text() + '{"company_number": "000')    # a torn final line is ignored
    assert len(E.load_checkpoint(path)) == 3


def test_a_different_run_id_is_a_fresh_run_for_noise_checks(tmp_path):
    path = tmp_path / "cp.jsonl"
    calls = []
    fake = lambda *a: (calls.append(1) or (_raw(), {}))   # noqa: E731
    E.run([_case()], "m", call=fake, api_key="k", checkpoint=path, run_id=1, log=lambda _: None)
    E.run([_case()], "m", call=fake, api_key="k", checkpoint=path, run_id=2, log=lambda _: None)
    assert len(calls) == 2


def test_a_failed_quote_gets_one_retry_and_both_answers_are_kept():
    prompts = []
    answers = [_raw(geography={"value": "national", "quote": "throughout the UK"}), _raw()]

    def fake(api_key, model, prompt, timeout):
        prompts.append(prompt)
        return answers[len(prompts) - 1], {"prompt_tokens": 1000, "completion_tokens": 100}

    record = E.run_case("k", "m", _case(), 10, call=fake)
    assert len(prompts) == 2 and 'geography: "throughout the UK"' in prompts[1]
    assert record["attempts"] == 2 and record["geography"] == "regional" and record["geography_quote_valid"] == 1
    assert record["first_attempt"]["geography"] == "national" and record["first_attempt"]["geography_quote_valid"] == 0
    assert record["usage"] == {"prompt_tokens": 2000, "completion_tokens": 200}


def test_a_retry_that_fails_again_keeps_the_flag_and_does_not_loop():
    prompts = []
    bad = _raw(geography={"value": "national", "quote": "throughout the UK"})
    record = E.run_case("k", "m", _case(), 10, call=lambda *a: (prompts.append(1) or bad, {}))
    assert len(prompts) == 2 and record["attempts"] == 2
    assert record["geography"] == "national" and record["geography_quote_valid"] == 0


def test_an_unparseable_retry_keeps_the_first_answer():
    answers = iter([_raw(geography={"value": "national", "quote": "throughout the UK"}), "sorry"])
    record = E.run_case("k", "m", _case(), 10, call=lambda *a: (next(answers), {}))
    assert record["geography"] == "national" and record["retry_problem"] == "unparseable response"


def test_a_clean_answer_is_not_retried():
    prompts = []
    record = E.run_case("k", "m", _case(), 10, call=lambda *a: (prompts.append(1) or _raw(), {}))
    assert len(prompts) == 1 and record["attempts"] == 1


def test_no_site_is_recorded_distinctly_from_no_text():
    record = E.run_case(None, "m", _case(domain=None, text=""), 10)
    assert record["problem"] == "no website found" and record["raw"] is None


def test_store_profiles_replaces_and_keeps_quotes(tmp_path):
    conn = _db(tmp_path)
    record = E.run_case("k", "m", _case(), 10, call=lambda *a: (_raw(), {"prompt_tokens": 900, "completion_tokens": 150}))
    assert E.store_profiles(conn, [record, record]) == 2
    row = conn.execute("select customer_type, customer_type_quote_valid, conversion_action, geography, main_town, "
                       "google_category, category_source, seed_keywords, prompt_tokens, profile_version "
                       "from company_web_profile").fetchall()
    assert len(row) == 1
    assert row[0][:7] == ("consumer", 1, "enquiry_form", "regional", "Wilmslow", "Law firm", "google_listing")
    assert json.loads(row[0][7])[0] == "flight delay compensation" and row[0][8] == 900 and row[0][9] == P.PROMPT_VERSION


def test_cost_from_usage():
    records = [{"raw": "x", "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}, {"raw": None, "usage": {}}]
    assert E.cost(records, (0.000001, 0.000004)) == {"calls": 1, "prompt_tokens": 1000, "completion_tokens": 100,
                                                     "usd": 0.0014}


def test_log_to_langfuse_is_a_noop_when_langfuse_is_not_configured(monkeypatch):
    import scripts.langfuse_eval_helpers.langfuse_tracing as tracing
    monkeypatch.setattr(tracing, "langfuse_from_config", lambda config: None)
    assert E.log_to_langfuse([_case()], [{"company_number": "00000001"}], "m") is None


# ---------------------------------------------------------------- gold set

def test_gold_round_trip_blind_review_and_score(tmp_path):
    cases_dir, selection = tmp_path / "cases", tmp_path / "selection.json"
    cases = [_case(f"{i:08d}", text=TEXT) for i in range(1, 7)] + [_case("00000099", text="")]
    drawn = E.gold_draw(cases, count=6, blind=2, seed=1, cases_dir=cases_dir, selection=selection)
    assert len(drawn) == 6 and "00000099" not in drawn
    with pytest.raises(SystemExit):
        E.gold_draw(cases, count=6, blind=2, seed=1, cases_dir=cases_dir, selection=selection)
    records = {n: E.run_case("k", "m", _case(n), 10, call=lambda *a: (_raw(), {})) for n in drawn}

    blind_rows = E.gold_rows("blind", records, cases_dir)
    assert len(blind_rows) == 3 and "model: customer_type" not in blind_rows[0]
    review_before = E.gold_rows("review", records, cases_dir)
    assert len(review_before) == 1 + 4                                # blind cases hidden until labelled
    blind_numbers = [row[0] for row in blind_rows[1:]]

    blind_csv = tmp_path / "blind.csv"
    with blind_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(blind_rows[0])
        for row in blind_rows[1:]:
            filled = list(row)
            filled[6:6 + len(E.GOLD_FIELDS)] = ["consumer", "enquiry form", "regional", "no", "considered", "search"]
            writer.writerow(filled)
    assert E.gold_import(blind_csv, kind="blind", reviewer="will", cases_dir=cases_dir) == 2

    review_rows = E.gold_rows("review", records, cases_dir)
    assert len(review_rows) == 1 + 6
    review_csv = tmp_path / "review.csv"
    with review_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(review_rows[0])
        for index, row in enumerate(review_rows[1:]):
            filled = list(row)
            width = len(E.GOLD_FIELDS)
            start = 6 + width
            filled[start:start + width] = ["agree", "agree", "agree", "agree", "agree"] if index else \
                ["business", "agree", "agree", "agree", "agree"]
            writer.writerow(filled)
    assert E.gold_import(review_csv, kind="review", reviewer="will", cases_dir=cases_dir, records=records) == 6

    score = E.gold_score({1: records}, cases_dir)
    assert score["fields"]["customer_type"]["n"] == 6 and score["fields"]["customer_type"]["correct"] == 5
    assert score["fields"]["conversion_action"]["accuracy"] == 1.0
    assert score["quotes"] == {"checked": 18, "valid": 18}
    assert score["blind_agreement"]["n"] == 10 and set(blind_numbers) <= set(drawn)
    assert "rerun" not in score
    noisy = {n: {**r, "urgency": "emergency"} for n, r in records.items()}
    rerun = E.gold_score({1: records, 2: noisy}, cases_dir)["rerun"]
    assert rerun["companies"] == 6 and rerun["agreement"]["urgency"] == 0.0 and rerun["agreement"]["geography"] == 1.0


def test_gold_import_rejects_bad_values_before_writing_anything(tmp_path):
    cases_dir = tmp_path / "cases"
    E.gold_draw([_case("00000001"), _case("00000002")], count=2, blind=0, seed=1, cases_dir=cases_dir,
                selection=tmp_path / "s.json")
    before = {p.name: p.read_text() for p in cases_dir.glob("*.json")}
    header = ["company number"] + [f"verdict: {f} (agree or true value)" for f in E.GOLD_FIELDS] + ["notes"]
    bad = tmp_path / "bad.csv"
    with bad.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerow(["1", "consumer", "", "", "", "", ""])
        writer.writerow(["2", "martian", "", "", "", "", ""])
    with pytest.raises(ValueError):
        E.gold_import(bad, kind="review", reviewer="will", cases_dir=cases_dir)
    assert {p.name: p.read_text() for p in cases_dir.glob("*.json")} == before
    with pytest.raises(ValueError):
        E.gold_rows("nonsense", {})


def test_principal_activity_after_a_bare_heading():
    text = "Strategic report\nPrincipal activities\nThe principal activity of the group is dental care.\nReview"
    assert E.principal_activity(text) == "The principal activity of the group is dental care."


def test_retired_conversion_values_and_the_tender_flag():
    out = P.parse_profile(_raw(conversion_action={"value": "quote_form", "quote": "Request a free quote online"},
                               wins_by_tender={"value": "no", "quote": ""}), TEXT)
    assert out["conversion_action"] == "enquiry_form" and out["conversion_action_quote_valid"] == 1
    assert out["wins_by_tender"] == "no" and out["wins_by_tender_quote_valid"] is None   # no needs no quote
    yes = P.parse_profile(_raw(wins_by_tender={"value": True, "quote": "awarded a place on the ESPO Framework"}), TEXT)
    assert yes["wins_by_tender"] == "yes" and yes["wins_by_tender_quote_valid"] == 0     # a yes must quote the text
    assert P.parse_profile(_raw(conversion_action={"value": "tender", "quote": "x"}), TEXT)["conversion_action"] == "unclear"
