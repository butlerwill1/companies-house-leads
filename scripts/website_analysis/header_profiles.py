"""Browser-style HTTP headers, stable per lookup; not browser emulation."""
from dataclasses import dataclass
from scripts.website_analysis.web_fetch import USER_AGENT


@dataclass(frozen=True)
class HeaderProfile:
    name: str
    user_agent: str

    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "*/*",
                "Accept-Language": "en-GB,en;q=0.9"}


BROWSER_PROFILES = (
    HeaderProfile("chrome-windows", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    HeaderProfile("firefox-windows", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"),
    HeaderProfile("firefox-linux", "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"),
)
RESEARCH_PROFILE = HeaderProfile("research", USER_AGENT)


class HeaderRotation:
    def __init__(self, mode: str = "research"):
        if mode not in ("research", "browser-rotate"):
            raise ValueError("Unknown header mode")
        self.mode = mode
        self.index = 0

    def next_profile(self) -> HeaderProfile:
        if self.mode == "research":
            return RESEARCH_PROFILE
        profile = BROWSER_PROFILES[self.index % len(BROWSER_PROFILES)]
        self.index += 1
        return profile
