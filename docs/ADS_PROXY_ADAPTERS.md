# Ads Transparency proxy adapters

`scripts/website_analysis/proxy_adapters.py` defines the `ProxyAdapter` protocol:
`request_options()` supplies the proxy mapping and TLS verification setting.
`DirectAdapter`, `BrightDataAdapter`, and `OxylabsAdapter` implement it.
Providers manage IP rotation; these adapters do not cycle through a list of IPs or
retry challenges on another route. Block detection stays active.

The population scanner defaults to direct requests. Select a provider explicitly
with `--proxy-provider brightdata` or `--proxy-provider oxylabs`. Use a new output
folder for a different provider: the saved provider cannot change on resume.
Every live run, including a free trial, still needs user authorisation and a cap.
No proxy trial was run when implementing these adapters.

Bright Data configuration in `.env`:

```dotenv
BRIGHTDATA_USERNAME=brd-customer-YOUR_CUSTOMER-zone-YOUR_ZONE
BRIGHTDATA_PASSWORD=YOUR_ZONE_PASSWORD
```

The defaults are `brd.superproxy.io:33335`. Optional `BRIGHTDATA_PROXY_HOST`,
`BRIGHTDATA_PROXY_PORT`, and `BRIGHTDATA_CA_BUNDLE` override the gateway or provide
a trusted CA certificate path. TLS verification is never disabled. Use the full
username supplied by the dashboard. Rotation follows the chosen zone's policy;
a username with a sticky `-session-...` parameter requests session persistence.
See [Bright Data proxy documentation](https://docs.brightdata.com/api-reference/proxy/configuring_dns_resolution).

Oxylabs uses `OXYLABS_USERNAME` (the full proxy username), `OXYLABS_PASSWORD`,
and optional `OXYLABS_PROXY_HOST`, `OXYLABS_PROXY_PORT`, `OXYLABS_CA_BUNDLE`.
Its default gateway is `pr.oxylabs.io:7777`.

Example command, only after authorising the specific run and its usage limit:

```powershell
.venv-claude/Scripts/python.exe -m scripts.website_analysis.ads_transparency_population --output data/raw/ads-brightdata-trial --proxy-provider brightdata --header-mode browser-rotate --http-cap 25 --interval 8 --jitter 2 --batch-size 25 --batch-break 120
```

The HTTP cap counts requests, not currency or bandwidth. It is not a guaranteed
dollar spending limit; set a provider-side allowance before a trial.
Evidence records only the provider name, never proxy credentials.

The standalone `ads_transparency check` command loads `.env` and supports
`TRANSPARENCY_PROXY_PROVIDER=brightdata` or `oxylabs`; an explicit
`TRANSPARENCY_PROXY_URL` retains precedence. Existing Oxylabs credentials work
without a provider selector for compatibility. The original 50-domain experiment
remains direct-only.

The default outgoing User-Agent is
`Mozilla/5.0 (compatible; companies-house-leads-research/0.1)`.
The robots parser checks rules locally against a research crawler name; it does
not transmit that parser name. A missing robots file (404) permits proceeding;
a Google challenge still stops requests regardless of proxy provider.

## Optional browser-style header rotation

`--header-mode browser-rotate` cycles Chrome/Firefox header profiles between
lookup sessions. Each session keeps one User-Agent, Accept and Accept-Language
profile across its requests. Endpoint-specific Content-Type headers remain intact.
The profiles use fixed browser version strings, not a claim to track current
browser releases. The original direct-only experiment remains unchanged.

The browser profiles omit the research crawler identifier from outgoing headers.
They do not emulate JavaScript, browser TLS fingerprints, cookies or user actions,
and cannot guarantee acceptance. Robots rules, pacing, TLS verification and block
stops still apply. The selected mode is saved in the run manifest and cannot
change on resume. Evidence records each profile's name for review. No live
requests were made while adding this option.
