from __future__ import annotations

from scripts.web.web_browser import BrowserFetcher
from scripts.web.web_fetch import Fetcher, Page


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeTab:
    def __init__(self, spec, requests):
        self.spec, self._requests, self._callback, self.url = spec, requests, None, spec.get("final", "")
        self.waited = 0

    def on(self, event, callback):
        assert event == "request"
        self._callback = callback

    def goto(self, url, wait_until, timeout):
        if self.spec.get("raise"):
            raise TimeoutError("page took too long")
        self.url = self.spec.get("final", url)
        for request_url in self.spec.get("requests", []):
            self._callback(FakeRequest(request_url))
        return FakeResponse(self.spec.get("status", 200))

    def wait_for_timeout(self, ms):
        self.waited += ms

    def content(self):
        return self.spec.get("html", "")


class FakeContext:
    def __init__(self, browser, user_agent):
        self.browser, self.user_agent, self.closed = browser, user_agent, False

    def new_page(self):
        tab = FakeTab(self.browser.spec, self.browser.requests)
        self.browser.tabs.append(tab)
        return tab

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, spec):
        self.spec, self.requests, self.tabs, self.contexts = spec, [], [], []

    def new_context(self, user_agent):
        context = FakeContext(self, user_agent)
        self.contexts.append(context)
        return context


def _browser(tmp_path, spec, *, robots_ok=True):
    fetcher = Fetcher(cache_dir=tmp_path / "pages", respect_robots=False)
    if not robots_ok:
        fetcher.allowed = lambda url: False
    fake = FakeBrowser(spec)
    stopped = []
    return BrowserFetcher(fetcher, launcher=lambda: (fake, lambda: stopped.append(True)), settle_ms=10), fake, stopped


RENDERED = "<html><body>" + "services " * 200 + "</body></html>"


def test_renders_caches_and_records_third_party_hosts(tmp_path):
    spec = {"html": RENDERED, "final": "https://www.x.co.uk/", "requests": [
        "https://www.x.co.uk/app.js", "https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234",
        "https://connect.facebook.net/en_US/fbevents.js", "https://cdn.x.co.uk/a.png"]}
    browser, fake, stopped = _browser(tmp_path, spec)
    page = browser.fetch("https://x.co.uk/")
    assert page.ok and page.fetched_with == "browser" and page.final_url == "https://www.x.co.uk/"
    assert page.script_hosts == ["connect.facebook.net", "www.googletagmanager.com"]
    assert fake.contexts[0].user_agent.startswith("Mozilla/5.0 (compatible; companies-house-leads-research")
    assert fake.contexts[0].closed and fake.tabs[0].waited == 10
    cached = Fetcher(cache_dir=tmp_path / "pages", cache_only=True).get("https://x.co.uk/")
    assert cached.fetched_with == "browser" and cached.script_hosts == page.script_hosts
    browser.close()
    assert stopped == [True]


def test_a_challenge_page_is_blocked_not_retried(tmp_path):
    spec = {"html": "<title>Just a moment...</title>Checking your browser", "status": 200}
    browser, fake, _ = _browser(tmp_path, spec)
    page = browser.fetch("https://x.co.uk/")
    assert page.blocked and page.error == "challenge page" and page.html == ""
    assert len(fake.tabs) == 1                                   # one attempt, no retry with another identity
    assert Fetcher(cache_dir=tmp_path / "pages", cache_only=True).get("https://x.co.uk/").blocked


def test_a_403_in_the_browser_is_blocked(tmp_path):
    browser, _, _ = _browser(tmp_path, {"html": "denied", "status": 403})
    page = browser.fetch("https://x.co.uk/")
    assert page.blocked and page.error == "blocked (403)" and page.fetched_with == "browser"


def test_robots_txt_is_honoured(tmp_path):
    browser, fake, _ = _browser(tmp_path, {"html": RENDERED}, robots_ok=False)
    page = browser.fetch("https://x.co.uk/")
    assert page.error == "disallowed by robots.txt" and fake.tabs == []


def test_a_timeout_is_recorded_but_not_cached(tmp_path):
    browser, _, _ = _browser(tmp_path, {"raise": True})
    page = browser.fetch("https://x.co.uk/")
    assert page.error.startswith("browser: TimeoutError") and not page.ok
    assert Fetcher(cache_dir=tmp_path / "pages", cache_only=True).get("https://x.co.uk/").error.startswith("not cached")


def test_cache_only_never_launches_a_browser(tmp_path):
    fetcher = Fetcher(cache_dir=tmp_path / "pages", cache_only=True)
    launched = []
    browser = BrowserFetcher(fetcher, launcher=lambda: launched.append(True) or (None, lambda: None))
    assert browser.fetch("https://x.co.uk/").error.startswith("not cached") and launched == []


def test_unavailable_without_playwright_or_a_launcher(tmp_path, monkeypatch):
    import scripts.web.web_browser as module
    monkeypatch.setattr(module, "playwright_installed", lambda: False)
    browser = BrowserFetcher(Fetcher(cache_dir=tmp_path / "pages", respect_robots=False))
    assert not browser.available() and browser.fetch("https://x.co.uk/").error == "browser unavailable"


def test_a_blocked_plain_download_is_replaced_by_the_rendered_page(tmp_path):
    fetcher = Fetcher(cache_dir=tmp_path / "pages", respect_robots=False)
    fetcher.put(Page(url="https://x.co.uk/", final_url=None, status=403, html="", error="blocked (403)",
                     blocked=True))
    fake = FakeBrowser({"html": RENDERED})
    BrowserFetcher(fetcher, launcher=lambda: (fake, lambda: None), settle_ms=1).fetch("https://x.co.uk/")
    again = Fetcher(cache_dir=tmp_path / "pages", cache_only=True).get("https://x.co.uk/")
    assert again.ok and again.fetched_with == "browser" and not again.blocked


def test_all_browser_work_runs_on_one_thread_whichever_thread_calls(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    seen = []

    class Recording(FakeBrowser):
        def new_context(self, user_agent):
            seen.append(threading.get_ident())
            return super().new_context(user_agent)

    fake = Recording({"html": RENDERED})
    stop_thread = []
    fetcher = Fetcher(cache_dir=tmp_path / "pages", respect_robots=False)
    browser = BrowserFetcher(fetcher, launcher=lambda: (fake, lambda: stop_thread.append(threading.get_ident())),
                             settle_ms=1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: browser.fetch(f"https://x{i}.co.uk/"), range(6)))
    browser.close()
    assert len(set(seen)) == 1 and seen[0] not in {threading.get_ident()} and stop_thread == [seen[0]]
