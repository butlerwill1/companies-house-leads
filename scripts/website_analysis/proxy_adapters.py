"""Explicit outbound routes. Providers manage residential IP rotation.

Adapters never retry challenges, change headers, or log credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping, Protocol
from urllib.parse import quote


class ProxyAdapter(Protocol):
    name: str

    def request_options(self) -> dict:
        """Return requests transport options, including TLS verification."""
        ...


@dataclass(frozen=True)
class DirectAdapter:
    name: str = "direct"

    def request_options(self) -> dict:
        return {"proxies": {}, "verify": True}


@dataclass(frozen=True)
class GatewayAdapter:
    username: str = field(repr=False)
    password: str = field(repr=False)
    host: str
    port: str
    name: str
    ca_bundle: str | None = None

    def request_options(self) -> dict:
        url = f"http://{quote(self.username, safe='')}:{quote(self.password, safe='')}@{self.host}:{self.port}"
        return {"proxies": {"http": url, "https": url}, "verify": self.ca_bundle or True}


class BrightDataAdapter(GatewayAdapter):
    """Use the full brd-customer-...-zone-... username from the dashboard.

    Without a sticky session parameter, rotation follows the zone's policy.
    """
    def __init__(self, username: str, password: str, *, host: str = "brd.superproxy.io",
                 port: str = "33335", ca_bundle: str | None = None):
        super().__init__(username, password, host, port, "brightdata", ca_bundle)


class OxylabsAdapter(GatewayAdapter):
    def __init__(self, username: str, password: str, *, host: str = "pr.oxylabs.io",
                 port: str = "7777", ca_bundle: str | None = None):
        super().__init__(username, password, host, port, "oxylabs", ca_bundle)


def adapter_from_env(provider: str, env: Mapping[str, str] | None = None) -> ProxyAdapter:
    env = os.environ if env is None else env
    if provider == "direct":
        return DirectAdapter()
    adapters = {"brightdata": ("BRIGHTDATA", BrightDataAdapter), "oxylabs": ("OXYLABS", OxylabsAdapter)}
    if provider not in adapters:
        raise ValueError("Unknown proxy provider")
    prefix, adapter_type = adapters[provider]
    username, password = env.get(f"{prefix}_USERNAME"), env.get(f"{prefix}_PASSWORD")
    if not username or not password:
        raise ValueError(f"{prefix}_USERNAME and {prefix}_PASSWORD are required")
    options = {key: env[f"{prefix}_{suffix}"] for key, suffix in
               (("host", "PROXY_HOST"), ("port", "PROXY_PORT"), ("ca_bundle", "CA_BUNDLE"))
               if env.get(f"{prefix}_{suffix}")}
    return adapter_type(username, password, **options)
