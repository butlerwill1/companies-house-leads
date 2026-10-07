from __future__ import annotations

import pytest

from scripts.website_analysis import web_identity as W
from scripts.website_analysis.web_fetch import Page


# ---------------------------------------------------------------- names, numbers, postcodes

def test_clean_name_drops_legal_words_and_parentheticals():
    assert W.clean_name("J F ASHTON (NORTHERN) LIMITED") == "J F Ashton"
    assert W.clean_name("PIPERFINN HOLDINGS LIMITED") == "Piperfinn"
    assert W.clean_name("THE ABC COMPANY UK LTD") == "Abc"
    assert W.clean_name("Smith & Sons Ltd") == "Smith & Sons"


def test_name_qualifier_keeps_the_place_from_parentheses():
    assert W.name_qualifier("CITY HOTELS (DUNFERMLINE) LIMITED") == "Dunfermline"
    assert W.name_qualifier("PERCO (NORTH EAST) LIMITED") == "North East"
    assert W.name_qualifier("HARTWOOD CARE (3) LIMITED") == ""
    assert W.name_qualifier("NOVUSPHARMA (UK) LTD") == ""
    assert W.name_qualifier("PIPERFINN HOLDINGS LIMITED") == ""


def test_clean_name_falls_back_when_only_legal_words_remain():
    assert W.clean_name("GROUP HOLDINGS LIMITED") == "Group Holdings"


def test_number_found_exact_spaced_and_bounded():
    assert W.number_found("Registered in England No. 01234567.", "01234567")
    assert W.number_found("Company number 0123 4567", "01234567")
    assert not W.number_found("Call 012345678 today", "01234567")       # part of a longer number
    assert not W.number_found("ref 0123 4567 89", "01234567")           # spaced, but the run continues


def test_number_found_without_leading_zero_needs_context():
    assert W.number_found("Company Registration No: 1234567", "01234567")
    assert not W.number_found("Order 1234567 has shipped", "01234567")


def test_number_found_with_letter_prefix():
    assert W.number_found("Registered in Scotland SC123456", "SC123456")
    assert W.number_found("Registered in Scotland SC 123456", "SC123456")
    assert not W.number_found("Registered in Scotland 123456", "SC123456")


def test_postcode_found_ignores_spacing_and_case():
    assert W.postcode_found("Unit 4, Leeds ls14ap", "LS1 4AP")
    assert not W.postcode_found("Unit 4, Leeds LS1 4AQ", "LS1 4AP")


def test_name_found_needs_a_distinctive_phrase():
    assert W.name_found("Welcome to Piperfinn, leather baby shoes", "PIPERFINN HOLDINGS LIMITED")
    assert not W.name_found("Our management services cover Leeds", "MANAGEMENT SERVICES LIMITED")
    assert not W.name_found("Hello from ABC", "ABC LIMITED")                  # too short to be evidence


def test_names_similar_and_domain_spelling():
    assert W.names_similar("Hankinson Whittle Ltd", "HANKINSON WHITTLE LIMITED")
    assert not W.names_similar("Whittle Plumbing", "HANKINSON WHITTLE LIMITED")
    assert W.domain_spells_name("hankinsonwhittle.co.uk", "HANKINSON WHITTLE LIMITED")
    assert not W.domain_spells_name("whittle.co.uk", "HANKINSON WHITTLE LIMITED")


def test_blocklist_covers_subdomains():
    assert W.blocked("uk.linkedin.com")
    assert W.blocked("endole.co.uk")
    assert W.blocked(None)
    assert not W.blocked("piperfinn.com")


def test_tier_rules():
    base = {"number_found": False, "name_found": False, "postcode_found": False, "domain_spells_name": False}
    assert W.tier_for({**base, "number_found": True}) == "verified"
    assert W.tier_for({**base, "name_found": True, "postcode_found": True}) == "probable"
    assert W.tier_for(base, listing_links_here=True) == "probable"
    assert W.tier_for({**base, "name_found": True}) == "ambiguous"
    assert W.tier_for({**base, "domain_spells_name": True}) == "ambiguous"
    assert W.tier_for(base) == "none"
    assert W.tier_for({**base, "number_found": True, "parked": True}) == "none"


# ---------------------------------------------------------------- resolver flow

class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get(self, url, accept=("html",)):
        self.asked.append(url)
        html = self.pages.get(url)
        if html is None:
            return Page(url=url, final_url=None, status=None, html="", error="connection failed")
        return Page(url=url, final_url=url, status=200, html=html)


class FakeClient:
    def __init__(self, *, maps=(), places=(), organic=None, number_search=None):
        self.maps, self.places = list(maps), list(places)
        self.organic = organic or {"organic": [], "knowledge_graph": None, "places": []}
        self.number_search = number_search or {"organic": [], "knowledge_graph": None, "places": []}
        self.calls = []

    def postcode_location(self, postcode):
        self.calls.append(("postcode", postcode))
        return {"postcode": postcode, "latitude": 53.8, "longitude": -1.55, "district": "Leeds"}

    def maps_search(self, query, *, lat, lng, zoom=14):
        self.calls.append(("maps", query))
        return self.maps

    def places_search(self, query, *, location="United Kingdom"):
        self.calls.append(("places", query))
        return self.places

    def web_search(self, query, *, num=10):
        self.calls.append(("search", query))
        return self.number_search if query.startswith('"') else self.organic


COMPANY = {"company_number": "01234567", "company_name": "PIPERFINN HOLDINGS LIMITED", "postcode": "LS1 4AP"}
FOOTER = '<html><title>Piperfinn</title><body>Piperfinn shoes <a href="/privacy-policy">Privacy</a></body></html>'
PRIVACY = "<html><body>Piperfinn Holdings Limited, company number 01234567, LS1 4AP</body></html>"


def _listing(domain, postcode="LS1 4AP", title="Piperfinn"):
    return {"title": title, "postcode": postcode, "domain": domain, "website": f"https://{domain}/",
            "category": "Shoe store", "categories": ["Shoe store"], "source": "serper_maps"}


def test_listing_site_verified_by_number_skips_organic_search():
    fetcher = FakeFetcher({"https://piperfinn.com/": FOOTER, "https://piperfinn.com/privacy-policy": PRIVACY})
    client = FakeClient(maps=[_listing("piperfinn.com")])
    record = W.resolve(COMPANY, client, fetcher, dns_ok=lambda d: False)
    assert record["tier"] == "verified" and record["domain"] == "piperfinn.com"
    assert record["listing"]["match"] == "name+postcode" and record["listing"]["category"] == "Shoe store"
    assert [kind for kind, _ in client.calls] == ["postcode", "maps"]   # no organic, no number search
    assert record["candidates"][0]["evidence"]["number_page"] == "https://piperfinn.com/privacy-policy"


def test_listing_on_name_and_postcode_makes_its_site_probable():
    fetcher = FakeFetcher({"https://piperfinn.com/": "<html><body>Baby shoes</body></html>"})
    client = FakeClient(maps=[_listing("piperfinn.com")])
    record = W.resolve(COMPANY, client, fetcher, dns_ok=lambda d: False)
    assert record["tier"] == "probable"
    assert ("search", "Piperfinn") not in client.calls


def test_falls_back_to_uk_wide_places_organic_and_number_search():
    fetcher = FakeFetcher({"https://other.co.uk/": "<html><body>Nothing relevant</body></html>",
                           "https://piperfinn.co.uk/": PRIVACY})
    client = FakeClient(
        maps=[_listing("unrelated.co.uk", title="Totally Different Cafe")],
        places=[],
        organic={"organic": [{"domain": "endole.co.uk"}, {"domain": "other.co.uk"}], "knowledge_graph": None,
                 "places": []},
        number_search={"organic": [{"domain": "piperfinn.co.uk"}], "knowledge_graph": None, "places": []})
    record = W.resolve(COMPANY, client, fetcher, dns_ok=lambda d: False)
    kinds = [kind for kind, _ in client.calls]
    assert kinds == ["postcode", "maps", "places", "search", "search"]
    assert record["tier"] == "verified" and record["domain"] == "piperfinn.co.uk"
    assert record["listing"] is None
    assert all(row["domain"] != "endole.co.uk" for row in record["candidates"])   # directories never checked


def test_nothing_found_is_a_recorded_none():
    client = FakeClient()
    record = W.resolve(COMPANY, client, FakeFetcher({}), dns_ok=lambda d: False)
    assert record["tier"] == "none" and record["domain"] is None and record["candidates"] == []


def test_guessed_domain_is_checked_only_when_it_resolves():
    fetcher = FakeFetcher({"https://piperfinn.co.uk/": PRIVACY})
    record = W.resolve(COMPANY, FakeClient(), fetcher, dns_ok=lambda d: d == "piperfinn.co.uk")
    assert record["tier"] == "verified" and record["candidates"][0]["sources"] == ["guess"]
    assert "https://piperfinn.com/" not in fetcher.asked


def test_parked_domain_is_none():
    fetcher = FakeFetcher({"https://piperfinn.co.uk/": "<html><body>This domain is for sale! 01234567</body></html>"})
    record = W.resolve(COMPANY, FakeClient(), fetcher, dns_ok=lambda d: d == "piperfinn.co.uk")
    assert record["tier"] == "none"


def test_redirect_is_recorded_under_the_landing_domain():
    class RedirectFetcher(FakeFetcher):
        def get(self, url, accept=("html",)):
            if url == "https://piperfinn.co.uk/":
                return Page(url=url, final_url="https://www.piperfinn.com/", status=200, html=PRIVACY)
            return super().get(url)

    record = W.resolve(COMPANY, FakeClient(), RedirectFetcher({}), dns_ok=lambda d: d == "piperfinn.co.uk")
    assert record["domain"] == "piperfinn.com" and record["tier"] == "verified"
    assert [row["domain"] for row in record["candidates"]] == ["piperfinn.com"]


# ---------------------------------------------------------------- W1 fixes (2026-10-02): trading names, PDFs, blocked sites

class BlockedFetcher(FakeFetcher):
    """A site that refuses automated requests."""

    def get(self, url, accept=("html",)):
        self.asked.append(url)
        return Page(url=url, final_url=None, status=403, html="", error="blocked (403)", blocked=True)


def test_blocked_site_is_recorded_as_blocked_not_none():
    record = W.resolve(COMPANY, FakeClient(organic={"organic": [{"domain": "piperfinn.com"}],
                                                   "knowledge_graph": None, "places": []}),
                       BlockedFetcher({}), dns_ok=lambda d: False)
    assert record["tier"] == "blocked" and record["domain"] == "piperfinn.com"
    assert record["candidates"][0]["evidence"]["blocked"] is True


def test_blocked_site_with_a_matching_listing_is_still_probable():
    client = FakeClient(maps=[_listing("piperfinn.com")])
    record = W.resolve(COMPANY, client, BlockedFetcher({}), dns_ok=lambda d: False)
    assert record["tier"] == "probable" and record["domain"] == "piperfinn.com"


def test_tier_for_blocked_without_a_listing():
    assert W.tier_for({"blocked": True, "error": "no page fetched"}) == "blocked"
    assert W.tier_for({"blocked": True, "error": "no page fetched"}, listing_links_here=True) == "probable"
    assert W.TIER_RANK["blocked"] > W.TIER_RANK["ambiguous"] and W.TIER_RANK["blocked"] < W.TIER_RANK["none"]


def test_company_number_in_a_pdf_privacy_policy_verifies_the_site():
    home = ('<html><body>Green &amp; Fortune venues <a href="/wp-content/uploads/Privacy-Policy.pdf">Privacy Policy'
            '</a></body></html>')

    class PdfFetcher(FakeFetcher):
        def get(self, url, accept=("html",)):
            self.asked.append(url)
            if url.endswith(".pdf"):
                if "pdf" not in accept:
                    return Page(url=url, final_url=url, status=200, html="", error="not html: application/pdf")
                return Page(url=url, final_url=url, status=200, html="Green and Fortune Limited, company number 01234567")
            return super().get(url, accept)

    fetcher = PdfFetcher({"https://piperfinn.com/": home})
    record = W.resolve(COMPANY, FakeClient(maps=[_listing("piperfinn.com")]), fetcher, dns_ok=lambda d: False)
    assert record["tier"] == "verified"
    assert record["candidates"][0]["evidence"]["number_page"].endswith("Privacy-Policy.pdf")


TRADING_COMPANY = {"company_number": "01234567", "company_name": "ARA FITNESS LIMITED", "postcode": "HP9 2HN",
                   "trading_names": ["Free Soul", "Sistas"]}


def test_trading_name_finds_a_listing_the_registered_name_misses():
    class Client(FakeClient):
        def maps_search(self, query, *, lat, lng, zoom=14):
            self.calls.append(("maps", query))
            if query == "Free Soul":
                return [{"title": "Free Soul", "postcode": "HP9 2HN", "domain": "freesoul.com",
                         "website": "https://freesoul.com/", "category": "Vitamin & Supplements Shop",
                         "categories": ["Vitamin & Supplements Shop"], "source": "serper_maps"}]
            return []

    fetcher = FakeFetcher({"https://freesoul.com/": "<html><body>Free Soul <a href='/pages/privacy-policy'>Privacy"
                                                    "</a></body></html>",
                           "https://freesoul.com/pages/privacy-policy": "<html><body>Free Soul is a trading name of "
                                                                         "ARA Fitness Limited. Company number "
                                                                         "01234567</body></html>"})
    client = Client()
    record = W.resolve(TRADING_COMPANY, client, fetcher, dns_ok=lambda d: False)
    assert [c for c in client.calls if c[0] == "maps"] == [("maps", "Ara Fitness"), ("maps", "Free Soul")]
    assert record["tier"] == "verified" and record["domain"] == "freesoul.com"
    assert record["listing"]["category"] == "Vitamin & Supplements Shop" and record["listing"]["match"] != "name_only" \
        or record["listing"]["match"] in ("name+postcode", "name+domain")
    assert record["trading_names_used"] == ["Free Soul", "Sistas"]
    assert {"name": "Free Soul", "source": "website"}.items() <= next(
        item for item in record["new_trading_names"] if item["source"] == "website").items()


def test_trading_name_is_searched_organically_when_the_registered_name_finds_nothing():
    client = FakeClient(organic={"organic": [], "knowledge_graph": None, "places": []})
    W.resolve(TRADING_COMPANY, client, FakeFetcher({}), dns_ok=lambda d: False)
    searches = [q for kind, q in client.calls if kind == "search"]
    assert searches[:3] == ["Ara Fitness", "Free Soul", "Sistas"] and searches[-1] == '"01234567"'


def test_site_showing_a_trading_name_and_postcode_is_probable():
    fetcher = FakeFetcher({"https://freesoul.com/": "<html><body>Welcome to Free Soul, HP9 2HN</body></html>"})
    client = FakeClient(organic={"organic": [{"domain": "freesoul.com"}], "knowledge_graph": None, "places": []})
    record = W.resolve(TRADING_COMPANY, client, fetcher, dns_ok=lambda d: False)
    assert record["tier"] == "probable"


def test_places_first_uses_the_one_credit_search_near_the_district():
    fetcher = FakeFetcher({"https://piperfinn.com/": FOOTER, "https://piperfinn.com/privacy-policy": PRIVACY})
    client = FakeClient(places=[_listing("piperfinn.com")])
    record = W.resolve(COMPANY, client, fetcher, dns_ok=lambda d: False, first_lookup="places")
    assert record["tier"] == "verified" and record["resolver_version"] == W.PLACES_FIRST_VERSION
    assert client.calls == [("postcode", "LS1 4AP"), ("places", "Piperfinn Leeds")]   # no Maps call
    with pytest.raises(ValueError):
        W.resolve(COMPANY, client, fetcher, first_lookup="satellite")
