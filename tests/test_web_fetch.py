from __future__ import annotations

from scripts.website_analysis.web_fetch import Fetcher, parse_html


class Response:
    def __init__(self, status=200, text="", content_type="text/html; charset=utf-8", url=None):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {"Content-Type": content_type}
        self.content = text.encode("utf-8")


class Session:
    def __init__(self, routes):
        self.routes = routes
        self.asked = []

    def get(self, url, **kwargs):
        self.asked.append(url)
        response = self.routes.get(url, Response(404))
        if response.url is None:
            response.url = url
        return response


def _fetcher(tmp_path, routes, **kwargs):
    return Fetcher(cache_dir=tmp_path / "pages", session=Session(routes), sleep=lambda _: None, **kwargs)


def test_parse_html_text_title_links_skip_scripts():
    page = parse_html('<html><head><title> Shop </title><script>var x="hidden"</script></head>'
                      '<body><p>Hello&nbsp;there</p><a href="/privacy">Privacy policy</a>'
                      '<a href="mailto:a@b.c">mail</a></body></html>', "https://shop.example.com/x")
    assert page.title == "Shop"
    assert "hidden" not in page.text and "Hello" in page.text
    assert page.links == [("https://shop.example.com/privacy", "Privacy policy")]


def test_get_caches_pages_and_honours_robots(tmp_path):
    routes = {"https://a.co.uk/robots.txt": Response(text="User-agent: *\nDisallow: /private", content_type="text/plain"),
              "https://a.co.uk/": Response(text="<p>home</p>")}
    fetcher = _fetcher(tmp_path, routes)
    assert fetcher.get("https://a.co.uk/").ok
    assert fetcher.get("https://a.co.uk/private/x").error == "disallowed by robots.txt"
    asked = list(fetcher.session.asked)
    again = _fetcher(tmp_path, routes)
    assert again.get("https://a.co.uk/").html == "<p>home</p>" and again.session.asked == []
    assert "https://a.co.uk/private/x" not in asked


def test_transient_errors_are_not_cached(tmp_path):
    routes = {"https://b.co.uk/": Response(status=503, text="busy")}
    fetcher = _fetcher(tmp_path, routes)
    assert not fetcher.get("https://b.co.uk/").ok
    routes["https://b.co.uk/"] = Response(text="<p>back</p>")
    assert fetcher.get("https://b.co.uk/").ok


def test_non_html_is_refused_and_cache_only_never_fetches(tmp_path):
    routes = {"https://c.co.uk/file.pdf": Response(content_type="application/pdf", text="%PDF")}
    assert _fetcher(tmp_path, routes).get("https://c.co.uk/file.pdf").error.startswith("not html")
    offline = _fetcher(tmp_path, {}, cache_only=True)
    assert offline.get("https://d.co.uk/").error.startswith("not cached") and offline.session.asked == []


def test_utf8_without_charset_is_not_garbled(tmp_path):
    routes = {"https://e.co.uk/": Response(text="<p>Café £</p>", content_type="text/html")}
    assert "Café £" in _fetcher(tmp_path, routes, respect_robots=False).get("https://e.co.uk/").html


# ---------------------------------------------------------------- W2 additions

import gzip
import hashlib
import json

from scripts.website_analysis.web_fetch import Page, is_challenge, is_thin


def _cache_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "pages").rglob("*") if p.is_file())


def test_cache_is_gzipped_and_old_entries_are_still_read(tmp_path):
    routes = {"https://a.co.uk/": Response(text="<p>home</p>")}
    fetcher = _fetcher(tmp_path, routes, respect_robots=False)
    fetcher.get("https://a.co.uk/")
    assert all(name.endswith(".json.gz") for name in _cache_files(tmp_path))
    # a legacy uncompressed entry, as written by the first version of the fetcher
    legacy_url = "https://old.co.uk/"
    legacy = tmp_path / "pages" / "old.co.uk" / f"{hashlib.sha1(legacy_url.encode()).hexdigest()}.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"url": legacy_url, "final_url": legacy_url, "status": 200, "html": "<p>legacy</p>",
                                  "error": None}), encoding="utf-8")
    offline = _fetcher(tmp_path, {}, cache_only=True)
    assert offline.get(legacy_url).html == "<p>legacy</p>" and offline.get("https://a.co.uk/").ok


def test_put_replaces_the_entry_and_removes_the_legacy_file(tmp_path):
    fetcher = _fetcher(tmp_path, {}, respect_robots=False)
    url = "https://b.co.uk/"
    legacy = fetcher._cache_base(url).with_suffix(".json")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"url": url, "status": 403, "html": "", "error": "blocked (403)"}), encoding="utf-8")
    fetcher.put(Page(url=url, final_url=url, status=200, html="<p>rendered</p>", fetched_with="browser",
                     script_hosts=["www.googletagmanager.com"]))
    page = _fetcher(tmp_path, {}, cache_only=True).get(url)
    assert page.html == "<p>rendered</p>" and page.fetched_with == "browser"
    assert page.script_hosts == ["www.googletagmanager.com"] and not legacy.exists()


def test_a_403_is_recorded_as_blocked_and_cached(tmp_path):
    routes = {"https://c.co.uk/": Response(status=403, text="Forbidden")}
    fetcher = _fetcher(tmp_path, routes, respect_robots=False)
    page = fetcher.get("https://c.co.uk/")
    assert page.blocked and page.error == "blocked (403)" and not page.ok
    assert _fetcher(tmp_path, {}, cache_only=True).get("https://c.co.uk/").blocked


def test_a_429_is_blocked_but_not_cached(tmp_path):
    routes = {"https://d.co.uk/": Response(status=429, text="slow down")}
    fetcher = _fetcher(tmp_path, routes, respect_robots=False)
    assert fetcher.get("https://d.co.uk/").blocked
    assert _cache_files(tmp_path) == [] if (tmp_path / "pages").exists() else True


def test_a_challenge_page_is_blocked_even_with_status_200(tmp_path):
    body = "<html><title>Just a moment...</title><body>Checking your browser before accessing</body></html>"
    fetcher = _fetcher(tmp_path, {"https://e.co.uk/": Response(text=body)}, respect_robots=False)
    page = fetcher.get("https://e.co.uk/")
    assert page.blocked and page.error == "challenge page" and page.html == ""
    assert is_challenge(body) and not is_challenge("<p>An ordinary page about our services</p>")


def test_javascript_is_accepted_only_when_asked_for(tmp_path):
    js = Response(text="(function(){var x='AW-123456789';})()", content_type="application/javascript")
    fetcher = _fetcher(tmp_path, {"https://www.googletagmanager.com/gtm.js?id=GTM-ABC123": js}, respect_robots=False)
    url = "https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"
    assert fetcher.get(url).error.startswith("not html")
    other = _fetcher(tmp_path / "x", {url: js}, respect_robots=False)
    page = other.get(url, accept=("script",))
    assert page.ok and "AW-123456789" in page.html


def test_pdf_text_is_extracted_and_cached(tmp_path):
    import fitz
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Registered in England, company number 01234567")
    data = document.tobytes()
    response = Response(content_type="application/pdf")
    response.content = data
    url = "https://f.co.uk/privacy.pdf"
    fetcher = _fetcher(tmp_path, {url: response}, respect_robots=False)
    assert fetcher.get(url).error.startswith("not html")                          # not asked for: refused
    page = _fetcher(tmp_path / "y", {url: response}, respect_robots=False).get(url, accept=("html", "pdf"))
    assert page.ok and "01234567" in page.html and page.content_type == "application/pdf"


def test_is_thin_counts_visible_words():
    assert is_thin("<html><body><div id='root'></div><script>app()</script></body></html>")
    assert not is_thin("<p>" + "word " * 200 + "</p>")


def test_parse_collects_forms_tel_links_meta_jsonld_scripts_and_nav():
    html = """<html lang="en-GB"><head><title>T</title>
      <meta name="description" content="Desc"><meta name="robots" content="noindex, follow">
      <meta name="viewport" content="width=device-width"><meta property="og:title" content="OG">
      <link rel="canonical" href="/home/">
      <script src="/static/app.js"></script>
      <script type="application/ld+json">{"@type": "LocalBusiness"}</script>
      <script>var hidden = 'not text';</script></head>
      <body><header><nav><a href="/services">Services</a></nav></header>
      <h1>Welcome to Bott</h1><a href="tel:+441625415800">Call</a><a href="/contact">Contact</a>
      <form action="/enquiry" class="hs-form"><input name="a"><input type="hidden" name="b"><textarea></textarea>
      <input type="submit"></form></body></html>"""
    page = parse_html(html, "https://x.co.uk/")
    assert page.lang == "en-GB" and page.h1 == "Welcome to Bott" and page.canonical == "https://x.co.uk/home/"
    assert page.meta["description"] == "Desc" and "noindex" in page.meta["robots"] and "viewport" in page.meta
    assert page.meta["og:title"] == "OG"
    assert page.jsonld == ['{"@type": "LocalBusiness"}'] and "hidden" not in page.text
    assert page.scripts == ["https://x.co.uk/static/app.js"] and page.tel_links == ["+441625415800"]
    assert page.nav_links == ["https://x.co.uk/services"]
    assert page.forms == [{"action": "/enquiry", "id": "", "class": "hs-form", "inputs": 2}]
    assert ("https://x.co.uk/contact", "Contact") in page.links


def test_a_legacy_403_cache_entry_is_read_as_blocked(tmp_path):
    url = "https://old403.co.uk/"
    legacy = tmp_path / "pages" / "old403.co.uk" / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"url": url, "final_url": url, "status": 403, "html": "", "error": "HTTP 403"}),
                      encoding="utf-8")
    page = _fetcher(tmp_path, {}, cache_only=True).get(url)
    assert page.blocked and page.error == "blocked (403)"
