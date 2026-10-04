from __future__ import annotations

import json
import sqlite3

from core.companies_house_sqlite import init_db
from scripts.web import web_settle as S
from scripts.web.web_fetch import Page

COMPANY = {"company_number": "01234567", "company_name": "LOTUS HOMES (UK) LIMITED", "postcode": "E1W 1YW",
           "trading_names": []}


def _candidate(domain, *sources, name_found=True, spells=True, tier="ambiguous"):
    return {"domain": domain, "tier": tier, "sources": list(sources) or ["organic"],
            "evidence": {"domain": domain, "final_url": f"https://{domain}/", "name_found": name_found,
                         "domain_spells_name": spells, "number_found": False}}


def _record(*candidates, tier="ambiguous", listing=None):
    return {"company_number": "01234567", "company_name": COMPANY["company_name"], "tier": tier,
            "resolver_version": "identity-v3-places-first", "domain": candidates[0]["domain"] if candidates else None,
            "candidates": list(candidates), "listing": listing}


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, accept=("html",)):
        html = self.pages.get(url)
        if html is None:
            return Page(url=url, final_url=None, status=None, html="", error="not cached")
        return Page(url=url, final_url=url, status=200, html=html)


def _db(tmp_path, domain, pages):
    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    for kind, url, phones, number in pages:
        conn.execute("insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, status_code, "
                     "phone_numbers, company_number_found) values (?, ?, ?, 'crawl-v1', 'http', 200, ?, ?)",
                     (domain, url, kind, json.dumps(phones), number))
    return conn


def _html(text):
    return f"<html><title>Site</title><body>{text}</body></html>"


def test_sites_to_crawl_takes_each_ambiguous_companys_best_two_and_merges_nominators():
    a = _record(_candidate("a.co.uk", "maps", "organic"), _candidate("b.co.uk", "guess", name_found=False),
                _candidate("c.com", "organic", name_found=False, spells=False))
    b = {**_record(_candidate("a.co.uk", "organic")), "company_number": "09999999"}
    verified = {**_record(_candidate("z.co.uk"), tier="verified"), "company_number": "08888888"}
    sites = {s["domain"]: s for s in S.sites_to_crawl([a, b, verified])}
    assert set(sites) == {"a.co.uk", "b.co.uk"}                      # c.com is the third best, z.co.uk is verified
    assert sites["a.co.uk"]["company_numbers"] == ["01234567", "09999999"]
    assert sites["a.co.uk"]["start_url"] == "https://a.co.uk/"


def _evidence(**over):
    base = {"number_found": False, "legal_name_found": False, "legal_name_disclosed": False, "name_found": False,
            "postcode_found": False, "phone_match": False, "uk_site": False}
    return {**base, **over}


def test_the_company_number_verifies_and_outranks_everything():
    assert S.settle_tier(_evidence(number_found=True)) == ("verified", "number")


def test_the_full_legal_name_needs_corroboration_or_a_disclosure_on_a_uk_site():
    assert S.settle_tier(_evidence(legal_name_found=True, postcode_found=True)) == ("probable", "legal_name+corroboration")
    assert S.settle_tier(_evidence(legal_name_found=True), listing_links_here=True)[1] == "legal_name+corroboration"
    disclosed = _evidence(legal_name_found=True, legal_name_disclosed=True, uk_site=True)
    assert S.settle_tier(disclosed) == ("probable", "legal_name+disclosure")
    assert S.settle_tier({**disclosed, "uk_site": False}) == ("ambiguous", None)
    # the name in a news story or a case study, on a UK site, settles nothing
    assert S.settle_tier(_evidence(legal_name_found=True, uk_site=True)) == ("ambiguous", None)


def test_the_short_name_needs_the_postcode_or_the_listing_phone_of_a_listing_at_the_registered_postcode():
    assert S.settle_tier(_evidence(name_found=True, postcode_found=True)) == ("probable", "name+postcode")
    both = _evidence(name_found=True, phone_match=True)
    assert S.settle_tier(both, listing_at_registered_postcode=True) == ("probable", "name+phone")
    assert S.settle_tier(both) == ("ambiguous", None)       # a namesake's listing and its own site agree with each other
    assert S.settle_tier(_evidence(name_found=True, uk_site=True)) == ("ambiguous", None)
    assert S.settle_tier(_evidence()) == ("ambiguous", None)


def test_a_candidate_is_settled_from_its_crawled_pages(tmp_path):
    conn = _db(tmp_path, "lotushomes.co.uk", [("home", "https://lotushomes.co.uk/", [], 0),
                                              ("privacy", "https://lotushomes.co.uk/privacy", ["02071234567"], 0)])
    fetcher = FakeFetcher({"https://lotushomes.co.uk/": _html("Lotus Homes build homes"),
                           "https://lotushomes.co.uk/privacy": _html("Lotus Homes (UK) Ltd, 1 Wharf Road, E1W 1YW")})
    record = _record(_candidate("lotushomes.co.uk", "guess"), _candidate("lotushomesllc.com", "organic"))
    settled = S.settle_record(record, COMPANY, conn, fetcher)
    assert settled["tier"] == "probable" and settled["domain"] == "lotushomes.co.uk"
    assert settled["settled_rule"] == "legal_name+corroboration" and settled["resolver_version"] == S.SETTLED_VERSION
    assert settled["settled_from"] == "identity-v3-places-first"
    assert record["tier"] == "ambiguous"                           # the original is untouched
    chosen = next(c for c in settled["candidates"] if c["domain"] == "lotushomes.co.uk")
    assert chosen["tier"] == "probable" and chosen["evidence"]["settled"]["postcode_found"] is True


def test_the_better_rule_wins_between_two_candidates(tmp_path):
    conn = _db(tmp_path, "lotushomes.co.uk", [("home", "https://lotushomes.co.uk/", [], 0)])
    for page in (("home", "https://other.com/", [], 1),):
        conn.execute("insert into web_pages (domain, url, page_kind, crawl_version, fetched_with, status_code, "
                     "phone_numbers, company_number_found) values ('other.com', ?, ?, 'crawl-v1', 'http', 200, ?, ?)",
                     (page[1], page[0], json.dumps(page[2]), page[3]))
    fetcher = FakeFetcher({"https://lotushomes.co.uk/": _html("Lotus Homes of E1W 1YW"),
                           "https://other.com/": _html("welcome")})
    settled = S.settle_record(_record(_candidate("lotushomes.co.uk", "maps", "organic"), _candidate("other.com")),
                              COMPANY, conn, fetcher)
    assert settled["domain"] == "other.com" and settled["tier"] == "verified"   # the crawl saw the company number


def test_a_company_stays_ambiguous_when_nothing_ties_the_site_to_it(tmp_path):
    conn = _db(tmp_path, "lotushomes.co.uk", [("home", "https://lotushomes.co.uk/", [], 0)])
    fetcher = FakeFetcher({"https://lotushomes.co.uk/": _html("Lotus Homes Property Management, Peterborough")})
    assert S.settle_record(_record(_candidate("lotushomes.co.uk")), COMPANY, conn, fetcher) is None
    assert S.settle_record(_record(_candidate("not-crawled.com")), COMPANY, conn, fetcher) is None


def test_the_listing_phone_matches_a_phone_shown_on_the_site(tmp_path):
    conn = _db(tmp_path, "lotushomes.co.uk", [("home", "https://lotushomes.co.uk/", ["01625415850"], 0)])
    fetcher = FakeFetcher({"https://lotushomes.co.uk/": _html("Lotus Homes")})
    namesake = {"phone": "+44 1625 415850", "match": "name_only", "domain": "other.co.uk"}
    assert S.settle_record(_record(_candidate("lotushomes.co.uk"), listing=namesake), COMPANY, conn, fetcher) is None
    listing = {"phone": "+44 1625 415850", "match": "name+postcode", "domain": "other.co.uk"}
    settled = S.settle_record(_record(_candidate("lotushomes.co.uk"), listing=listing), COMPANY, conn, fetcher)
    assert settled["settled_rule"] == "name+phone"


def test_a_legal_name_in_a_footer_or_policy_is_a_disclosure_but_news_and_case_studies_are_not():
    name = "GILKS (NANTWICH) LIMITED"
    assert S.disclosure_context("© 2026 Gilks (Nantwich) Ltd. All rights reserved", name)
    assert S.disclosure_context("Gilks (Nantwich) Limited are the data controller of your information", name)
    assert S.disclosure_context("Our address: Gilks Nantwich Limited, Unit 4, Crewe CW1 6AB", name)
    assert S.disclosure_context("Gilks (Nantwich) Limited registered in England", name)
    assert not S.disclosure_context("Infrastructure Limited acquires Simkiss, Gilks Nantwich Limited to file for administration",
                                    name)
    assert not S.disclosure_context("Case study: Gilks (Nantwich) Limited app development CRO and analytics", name)


def test_a_legal_name_found_only_in_a_news_story_does_not_settle_the_company(tmp_path):
    company = {**COMPANY, "company_name": "GILKS (NANTWICH) LIMITED"}
    conn = _db(tmp_path, "themepsource.com", [("home", "https://themepsource.com/", ["02071234567"], 0)])
    fetcher = FakeFetcher({"https://themepsource.com/": _html(
        "MEP News: Infrastructure Limited acquires Simkiss, Gilks Nantwich Limited to file for administration")})
    assert S.settle_record(_record(_candidate("themepsource.com")), company, conn, fetcher) is None


def test_a_legal_name_in_a_privacy_policy_settles_a_uk_site_but_not_a_foreign_one(tmp_path):
    company = {**COMPANY, "company_name": "GILKS (NANTWICH) LIMITED"}
    fetcher = FakeFetcher({"https://gilks.co.uk/privacy": _html("Gilks (Nantwich) Limited is the data controller"),
                           "https://gilks.com/privacy": _html("Gilks (Nantwich) Limited is the data controller")})
    uk = _db(tmp_path, "gilks.co.uk", [("privacy", "https://gilks.co.uk/privacy", [], 0)])
    assert S.settle_record(_record(_candidate("gilks.co.uk")), company, uk, fetcher)["settled_rule"]         == "legal_name+disclosure"
    (tmp_path / "other").mkdir()
    foreign = _db(tmp_path / "other", "gilks.com", [("privacy", "https://gilks.com/privacy", [], 0)])
    assert S.settle_record(_record(_candidate("gilks.com")), company, foreign, fetcher) is None   # no UK sign


def test_a_blocked_companys_best_candidate_is_planned_separately():
    blocked = _record(_candidate("shut.co.uk", "maps", tier="blocked"), _candidate("other.co.uk", tier="blocked"),
                      tier="blocked")
    ambiguous = {**_record(_candidate("a.co.uk")), "company_number": "09999999"}
    assert [s["domain"] for s in S.sites_to_crawl([blocked, ambiguous], 1, "blocked")] == ["shut.co.uk"]
    assert [s["domain"] for s in S.sites_to_crawl([blocked, ambiguous])] == ["a.co.uk"]


def test_other_company_numbers_are_the_sites_not_ours():
    footer = [("home", "(c) 2026 The Heaton Group is a trading style of Heaton Group Manchester Limited, a private "
                       "limited company registered in England and Wales. Company Registration No. 08480568. VAT "
                       "registration number 123456789")]
    assert S.other_numbers(footer, "08615014") == ["08480568"]
    assert S.other_numbers(footer, "08480568") == []                  # our own number is not "other"


def test_the_brand_is_the_distinctive_name_words_on_at_least_two_pages():
    pages = [("home", "Heaton Group | Property Development"), ("about", "About Heaton Group, award winning")]
    assert S.brand_found(pages, "HEATON GROUP DEVELOPMENTS LIMITED")
    assert not S.brand_found(pages[:1], "HEATON GROUP DEVELOPMENTS LIMITED")


def test_footer_snippets_keep_the_trading_style_sentence():
    text = "Home About Contact (c) 2026 The Heaton Group. All Rights Reserved. The Heaton Group is a trading style of Heaton Group Manchester Limited"
    snippets = S.disclosure_snippets([("home", text)])
    assert snippets and "trading style of Heaton Group Manchester Limited" in " ".join(snippets)
