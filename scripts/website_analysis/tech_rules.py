#!/usr/bin/env python3
"""Curated marketing-technology rules for the web stage (W2).

About 80 tools a PPC specialist cares about, not a general fingerprint
library: every rule is written, tested against fixture HTML, and explainable.
Detection reads the cached pages of a site (the `page` surface) and the public
Google Tag Manager containers those pages load (the `gtm` surface). Most tags
on the sites we looked at were inside Tag Manager, so both matter.

Each `Rule` says where it may match:

- `page`: the raw HTML (scripts, links and markup) of any fetched page;
- `gtm`: the text of a Tag Manager container (`gtm.js?id=GTM-...`).

Platform, shop, booking, review and landing-page rules are page-only: Tag
Manager's own code mentions Wix and WooCommerce, which in the first preview
made every site look like it ran both.

`id_patterns` capture account identifiers (Google Ads and GA4 ids, a HubSpot
portal id, a pixel id). Two companies sharing an id usually share an owner or
an agency. An entry is `(regex, prefix)`: the first capture group is the id,
and `prefix` is put in front of it (Tag Manager stores a Google Ads conversion
id without its `AW-`).

Changing any rule means bumping `RULE_VERSION`: detection is re-run from the
cached pages (free) and stored under the new version beside the old one.

Consent Mode caveat: Tag Manager's runtime code contains the strings
`ad_storage` and `wait_for_update` whether or not a site uses Consent Mode, so
the `gtm` surface only counts an explicit default (`"ad_storage":"denied"`).
Check this against real containers when the first crawl is run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RULE_VERSION = "rules-v1"
CATEGORIES = ("tag_manager", "analytics", "search_ads", "social_ads", "consent", "crm_automation", "email",
              "call_tracking", "optimisation", "landing_pages", "chat", "booking", "ecommerce", "cms", "reviews")
BOTH = ("page", "gtm")
PAGE = ("page",)


@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    patterns: tuple[str, ...]
    where: tuple[str, ...] = BOTH
    id_patterns: tuple[tuple[str, str], ...] = ()
    gtm_patterns: tuple[str, ...] | None = None   # used instead of `patterns` on the Tag Manager surface
    _compiled: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)
    _compiled_gtm: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"{self.name}: unknown category {self.category!r}")
        object.__setattr__(self, "_compiled", tuple(re.compile(p, re.I) for p in self.patterns))
        gtm = self.patterns if self.gtm_patterns is None else self.gtm_patterns
        object.__setattr__(self, "_compiled_gtm", tuple(re.compile(p, re.I) for p in gtm))

    def search(self, text: str, surface: str = "page") -> re.Match[str] | None:
        for pattern in (self._compiled_gtm if surface == "gtm" else self._compiled):
            match = pattern.search(text)
            if match:
                return match
        return None

    def ids(self, text: str) -> list[str]:
        found: list[str] = []
        for pattern, prefix in self.id_patterns:
            for match in re.finditer(pattern, text, re.I):
                value = f"{prefix}{match.group(1)}"
                if value not in found:
                    found.append(value)
        return found


def _fn(name: str) -> str:
    """A Tag Manager tag or trigger type as it appears in a container: `"function":"__awct"`.
    Tag Manager's runtime also lists these names, and `__lcl` is its link-click trigger, so
    only this form counts as the site using the tag."""
    return r"[\"']function[\"']\s*:\s*[\"']" + re.escape(name) + r"[\"']"


def _r(name: str, category: str, *patterns: str, where: tuple[str, ...] = BOTH,
       ids: tuple[tuple[str, str], ...] = (), gtm: tuple[str, ...] | None = None) -> Rule:
    return Rule(name=name, category=category, patterns=tuple(patterns), where=where, id_patterns=ids,
                gtm_patterns=gtm)


RULES: tuple[Rule, ...] = (
    # ---- tag management
    _r("Google Tag Manager", "tag_manager",
       r"googletagmanager\.com/(?:gtm\.js|ns\.html)\?id=GTM-", r"\bGTM-[A-Z0-9]{5,8}\b",
       where=PAGE, ids=((r"\b(GTM-[A-Z0-9]{5,8})\b", ""),)),
    _r("Server-side tagging", "tag_manager",
       r"<script[^>]+src=[\"'](?:https?:)?//(?!www\.googletagmanager\.com)[^\"']+/gtm\.js\?id=GTM-",
       r"server_container_url|transport_url", where=PAGE),
    # ---- analytics
    _r("Google Analytics 4", "analytics",
       r"gtag/js\?id=G-[A-Z0-9]{6,}", r"\bG-[A-Z0-9]{8,12}\b", _fn("__gaawc"), r"google-analytics\.com/g/collect",
       ids=((r"\b(G-[A-Z0-9]{8,12})\b", ""),)),
    _r("Universal Analytics (legacy)", "analytics",
       r"google-analytics\.com/analytics\.js", r"\bUA-\d{4,10}-\d+\b", _fn("__ua"),
       ids=((r"\b(UA-\d{4,10}-\d+)\b", ""),)),
    _r("Matomo", "analytics", r"matomo\.js|piwik\.js", r"_paq\.push"),
    _r("Adobe Analytics", "analytics", r"assets\.adobedtm\.com", r"AppMeasurement\.js", r"omniture"),
    # ---- search advertising
    _r("Google Ads", "search_ads",
       r"\bAW-\d{8,12}\b", r"googleadservices\.com/pagead/conversion", _fn("__awct"), _fn("__gclidw"),
       ids=((r"\b(AW-\d{8,12})\b", ""), (r"vtp_conversionId[\"']\s*:\s*[\"'](\d{8,12})", "AW-"))),
    _r("Google Ads conversion event", "search_ads",
       r"gtag\(\s*[\"']event[\"']\s*,\s*[\"']conversion[\"']", r"send_to[\"']?\s*[:=]\s*[\"']AW-\d+/[\w-]+",
       r"googleadservices\.com/pagead/conversion/\d+/\?[^\"']*label=", _fn("__awct"), r"vtp_conversionLabel"),
    # `viewthroughconversion` is also in Tag Manager's own runtime, so the container surface needs the tag type.
    _r("Google Ads remarketing", "search_ads", r"google_remarketing_only|viewthroughconversion|google_conversion_id",
       _fn("__sp"), gtm=(_fn("__sp"),)),
    _r("Microsoft Ads (UET)", "search_ads", r"bat\.bing\.com/bat\.js", _fn("__baut"), r"\buetq\b"),
    # ---- social advertising
    _r("Meta Pixel", "social_ads",
       r"connect\.facebook\.net/[^\"'\s]*/fbevents\.js", r"\bfbq\(\s*[\"']init[\"']", r"facebook\.com/tr\?id=",
       ids=((r"fbq\(\s*[\"']init[\"']\s*,\s*[\"']?(\d{10,20})", ""), (r"facebook\.com/tr\?id=(\d{10,20})", ""))),
    _r("TikTok Pixel", "social_ads", r"analytics\.tiktok\.com/i18n/pixel", r"\bttq\.load\(",
       ids=((r"ttq\.load\(\s*[\"']([A-Z0-9]{8,30})", ""),)),
    _r("LinkedIn Insight Tag", "social_ads",
       r"snap\.licdn\.com/li\.lms-analytics", r"_linkedin_partner_id", r"px\.ads\.linkedin\.com",
       ids=((r"_linkedin_partner_id\s*=\s*[\"'](\d+)", ""),)),
    _r("Pinterest Tag", "social_ads", r"s\.pinimg\.com/ct/core\.js", r"\bpintrk\("),
    _r("Snapchat Pixel", "social_ads", r"sc-static\.net/scevent\.min\.js", r"\bsnaptr\("),
    _r("X (Twitter) Pixel", "social_ads", r"static\.ads-twitter\.com/uwt\.js", r"\btwq\(\s*[\"']init[\"']"),
    # ---- consent
    _r("Consent Mode", "consent",
       r"gtag\(\s*[\"']consent[\"']\s*,\s*[\"']default[\"']", r"[\"']ad_storage[\"']\s*:\s*[\"'](?:denied|granted)[\"']"),
    _r("Cookiebot", "consent", r"consent\.cookiebot\.com", r"\bcookiebot\b"),
    _r("OneTrust", "consent", r"cdn\.cookielaw\.org", r"onetrust"),
    _r("CookieYes", "consent", r"cdn-cookieyes\.com", r"cookieyes"),
    _r("Civic Cookie Control", "consent", r"cc\.cdn\.civiccomputing\.com", r"civiccomputing"),
    _r("Termly", "consent", r"app\.termly\.io"),
    _r("Iubenda", "consent", r"cdn\.iubenda\.com"),
    _r("Complianz", "consent", r"complianz|\bcmplz_"),
    _r("Usercentrics", "consent", r"usercentrics\.eu|app\.usercentrics"),
    # ---- CRM and marketing automation
    _r("HubSpot", "crm_automation",
       r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"js\.hs-analytics\.net", r"js\.usemessages\.com",
       r"js\.hsadspixel\.net", r"js\.hs-banner\.com", r"forms\.hsforms\.com", r"\bhbspt\.forms\.create", r"\b_hsq\b",
       r"track\.hubspot\.com", ids=((r"js\.hs-scripts\.com/(\d{5,9})\.js", ""), (r"portalId[\"']?\s*[:=]\s*[\"']?(\d{5,9})", ""))),
    _r("Salesforce / Pardot", "crm_automation",
       r"pi\.pardot\.com", r"\bpiAId\b", r"\bpiCId\b", r"webto\.salesforce\.com", r"servlet\.WebToLead",
       r"salesforceliveagent"),
    _r("Zoho", "crm_automation", r"forms\.zohopublic\.(?:com|eu)", r"crm\.zoho\.(?:com|eu)", r"zohocrm",
       r"zoho\.(?:com|eu)/crm"),
    _r("ActiveCampaign", "crm_automation", r"trackcmp\.net", r"activehosted\.com", r"prism\.app-us1\.com"),
    _r("Marketo", "crm_automation", r"munchkin\.marketo\.net", r"Munchkin\.init", r"mktoForms2", r"mktoForm_"),
    _r("Pipedrive", "crm_automation", r"pipedrive\.com/LeadBooster", r"leadbooster-chat\.pipedrive",
       r"webforms\.pipedrive\.com"),
    _r("Microsoft Dynamics 365", "crm_automation", r"d365mktformcapture|mktdplp102cdn\.azureedge\.net",
       r"crm\d*\.dynamics\.com"),
    # ---- email
    _r("Klaviyo", "email", r"static\.klaviyo\.com", r"a\.klaviyo\.com", r"klaviyo\.com/onsite", r"\b_learnq\b"),
    _r("Mailchimp", "email", r"chimpstatic\.com", r"list-manage\.com", r"mc-embedded-subscribe-form"),
    _r("Dotdigital", "email", r"r1-t\.trackedlink\.net", r"dotdigital", r"trackedweb\.net"),
    _r("Brevo (Sendinblue)", "email", r"sibautomation|sendinblue\.com|sibforms", r"brevo\.com/forms"),
    # ---- call tracking
    _r("CallRail", "call_tracking", r"cdn\.callrail\.com", r"calltrk\.com"),
    _r("Mediahawk", "call_tracking", r"mediahawk\.(?:co\.uk|com)", r"\bmhcdn\."),
    _r("ResponseTap", "call_tracking", r"responsetap\.com", r"adinsight\.eu"),
    _r("Infinity Call Tracking", "call_tracking", r"infinity-tracking\.net", r"infinitycdn\.net",
       r"callconversioncloud"),
    _r("Ruler Analytics", "call_tracking", r"ruler\.analytics", r"rulerdata", r"ruleranalytics"),
    _r("WhatConverts", "call_tracking", r"scripts\.iconnode\.com", r"whatconverts"),
    _r("CallTrackingMetrics", "call_tracking", r"tctm\.co", r"calltrackingmetrics"),
    # ---- conversion optimisation
    _r("Hotjar", "optimisation", r"static\.hotjar\.com", r"script\.hotjar\.com", r"\b_hjSettings\b",
       ids=((r"hjid\s*[:=]\s*(\d{5,9})", ""),)),
    _r("Microsoft Clarity", "optimisation", r"clarity\.ms/tag/", r"clarity\.ms/s/",
       ids=((r"clarity\.ms/tag/([a-z0-9]{8,12})", ""),)),
    _r("VWO", "optimisation", r"visualwebsiteoptimizer\.com", r"\b_vwo_code\b"),
    _r("Optimizely", "optimisation", r"cdn\.optimizely\.com", r"optimizelyEndUserId"),
    _r("AB Tasty", "optimisation", r"try\.abtasty\.com"),
    _r("Crazy Egg", "optimisation", r"script\.crazyegg\.com"),
    _r("Mouseflow", "optimisation", r"cdn\.mouseflow\.com|mouseflow\.com/projects"),
    _r("Lucky Orange", "optimisation", r"luckyorange\.(?:com|net)"),
    # ---- landing-page builders (page-only)
    _r("Unbounce", "landing_pages", r"unbounce\.com|ubembed\.com", where=PAGE),
    _r("Instapage", "landing_pages", r"instapage\.com|instapagemetrics\.com", where=PAGE),
    _r("Leadpages", "landing_pages", r"leadpages\.(?:com|net)|lpages\.co", where=PAGE),
    _r("Landingi", "landing_pages", r"landingi\.com", where=PAGE),
    _r("ClickFunnels", "landing_pages", r"clickfunnels\.com", where=PAGE),
    # ---- chat
    _r("Intercom", "chat", r"widget\.intercom\.io", r"js\.intercomcdn\.com", r"window\.intercomSettings"),
    _r("Drift", "chat", r"js\.driftt\.com"),
    _r("Tidio", "chat", r"code\.tidio\.co", r"tidiochat"),
    _r("LiveChat", "chat", r"cdn\.livechatinc\.com", r"__lc\.license"),
    _r("Zendesk", "chat", r"static\.zdassets\.com", r"\bze-snippet\b", r"zopim"),
    _r("Tawk.to", "chat", r"embed\.tawk\.to"),
    _r("Crisp", "chat", r"client\.crisp\.chat"),
    _r("WhatsApp click-to-chat", "chat", r"api\.whatsapp\.com/send", r"\bwa\.me/\d", where=PAGE),
    # ---- booking (page-only)
    _r("Fresha", "booking", r"fresha\.com", where=PAGE),
    _r("Treatwell", "booking", r"treatwell\.co\.uk|connect\.treatwell", where=PAGE),
    _r("Dentally", "booking", r"dentally\.co(?:m)?\b", where=PAGE),
    _r("ResDiary", "booking", r"resdiary\.com", where=PAGE),
    _r("OpenTable", "booking", r"opentable\.(?:co\.uk|com)", where=PAGE),
    _r("Calendly", "booking", r"calendly\.com", where=PAGE),
    _r("SevenRooms", "booking", r"sevenrooms\.com", where=PAGE),
    _r("Mindbody", "booking", r"mindbodyonline\.com|\bhealcode\b", where=PAGE),
    _r("Acuity Scheduling", "booking", r"acuityscheduling\.com|squarespacescheduling\.com", where=PAGE),
    _r("SimplyBook.me", "booking", r"simplybook\.me", where=PAGE),
    _r("Cliniko", "booking", r"cliniko\.com", where=PAGE),
    _r("Phorest", "booking", r"phorest\.com", where=PAGE),
    # ---- e-commerce (page-only)
    _r("Shopify", "ecommerce", r"cdn\.shopify\.com", r"myshopify\.com", r"shopify-section", where=PAGE),
    _r("WooCommerce", "ecommerce", r"wp-content/plugins/woocommerce", r"\bwc-ajax\b", r"woocommerce-", where=PAGE),
    _r("Magento", "ecommerce", r"Magento_[A-Z]\w+", r"mage/cookies", r"/static/version\d+/frontend/", where=PAGE),
    _r("BigCommerce", "ecommerce", r"cdn\d*\.bigcommerce\.com", r"bigcommerce\.com/s-", where=PAGE),
    _r("PrestaShop", "ecommerce", r"prestashop", where=PAGE),
    _r("Stripe", "ecommerce", r"js\.stripe\.com", r"checkout\.stripe\.com", where=PAGE),
    # ---- content management (page-only)
    _r("WordPress", "cms", r"/wp-content/", r"/wp-includes/", r"<meta[^>]+generator[^>]+WordPress", where=PAGE),
    _r("Wix", "cms", r"static\.wixstatic\.com", r"wixsite\.com", r"parastorage\.com", where=PAGE),
    _r("Squarespace", "cms", r"static1\.squarespace\.com", r"squarespace-cdn\.com", r"Static\.SQUARESPACE_CONTEXT",
       where=PAGE),
    _r("Webflow", "cms", r"website-files\.com", r"data-wf-(?:page|site)", where=PAGE),
    _r("Drupal", "cms", r"Drupal\.settings", r"/sites/default/files/", r"drupal-settings-json", where=PAGE),
    _r("Joomla", "cms", r"/media/jui/", r"<meta[^>]+generator[^>]+Joomla", where=PAGE),
    _r("GoDaddy Website Builder", "cms", r"img1\.wsimg\.com", r"websitebuilder\.godaddy", where=PAGE),
    _r("Framer", "cms", r"framerusercontent\.com", where=PAGE),
    # ---- reviews (page-only)
    _r("Trustpilot", "reviews", r"widget\.trustpilot\.com", r"tp\.widget\.bootstrap", r"trustpilot\.com/review",
       where=PAGE),
    _r("Feefo", "reviews", r"api\.feefo\.com", r"cdn\.feefo\.com", r"feefo\.com/(?:feefo|widget)", where=PAGE),
    _r("Reviews.io", "reviews", r"widget\.reviews\.co\.uk", r"reviews\.io\b", where=PAGE),
    _r("Yotpo", "reviews", r"yotpo\.com", where=PAGE),
    _r("Trustindex / Elfsight reviews", "reviews", r"cdn\.trustindex\.io", r"elfsight\.com", where=PAGE),
    _r("ReviewSolicitors", "reviews", r"reviewsolicitors\.co\.uk", where=PAGE),
    _r("Checkatrade badge", "reviews", r"checkatrade\.com/(?:widget|trades)|cdn\.checkatrade", where=PAGE),
)

BY_NAME = {rule.name: rule for rule in RULES}
assert len(BY_NAME) == len(RULES), "rule names must be unique"


def rules_for(surface: str) -> tuple[Rule, ...]:
    return tuple(rule for rule in RULES if surface in rule.where)
