"""Optional website fallback for Companies House extraction.

This module is intentionally separate from the API-first extractor. Import and
use it only when you explicitly want HTML scraping as a fallback path.
"""

from __future__ import annotations

import re
import urllib.parse
from html import unescape
from typing import Any


WEBSITE_BASE = "https://find-and-update.company-information.service.gov.uk"


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def strip_tags(value: str) -> str:
    return normalize_whitespace(re.sub(r"<[^>]+>", " ", value))


def find_first(pattern: str, text: str, flags: int = re.I | re.S) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else None


class CompaniesHouseWebsiteFallback:
    def __init__(self, web_client: Any) -> None:
        self.web_client = web_client

    def search_companies(self, query: str) -> list[dict[str, Any]]:
        url = f"{WEBSITE_BASE}/search/companies?q={urllib.parse.quote(query)}"
        html = self.web_client.get_text(url)
        pattern = re.compile(
            r'href="/company/(?P<number>[A-Z0-9]+)"[^>]*title="View company">\s*(?P<name>[^<]+?)\s*</a>'
            r'[\s\S]*?<p class="meta crumbtrail">\s*(?P<meta>[^<]+?)\s*</p>'
            r'[\s\S]*?<p>(?P<address>[^<]+)</p>',
            re.I,
        )
        results = []
        for match in pattern.finditer(html):
            meta = strip_tags(match.group("meta"))
            status = None
            status_match = re.search(r"-\s*(.+)$", meta)
            if status_match:
                status = status_match.group(1).strip()
            results.append(
                {
                    "company_name": normalize_whitespace(match.group("name")),
                    "company_number": match.group("number").strip(),
                    "company_status": status,
                    "address": normalize_whitespace(match.group("address")),
                    "source": "website",
                }
            )
        return results

    def get_company_profile(self, company_number: str) -> dict[str, Any]:
        html = self.web_client.get_text(f"{WEBSITE_BASE}/company/{company_number}")
        return {
            "company_name": strip_tags(find_first(r'<h1[^>]*>(.*?)</h1>', html) or ""),
            "company_number": company_number,
            "company_status": strip_tags(find_first(r'id="company-status">\s*(.*?)\s*</dd>', html) or ""),
            "type": strip_tags(find_first(r'id="company-type">\s*(.*?)\s*</dd>', html) or ""),
            "date_of_creation": strip_tags(find_first(r'id="company-creation-date">\s*(.*?)\s*</dd>', html) or ""),
            "registered_office_address": {
                "full": strip_tags(find_first(r'id="roa-address">\s*(.*?)\s*</span>', html) or "")
            },
            "accounts": {
                "last_accounts": {
                    "period_end_on": strip_tags(
                        find_first(r"Last accounts made up to\s*<strong>(.*?)</strong>", html) or ""
                    ),
                },
                "next_accounts": {
                    "period_end_on": strip_tags(
                        find_first(r"Next accounts made up to\s*<strong>(.*?)</strong>", html) or ""
                    ),
                    "due_on": strip_tags(find_first(r"due by\s*<strong>(.*?)</strong>", html) or ""),
                },
            },
            "source": "website",
        }

    def get_accounts_filings(self, company_number: str) -> list[dict[str, Any]]:
        html = self.web_client.get_text(f"{WEBSITE_BASE}/company/{company_number}/filing-history")
        rows = []
        pattern = re.compile(
            r"<tr>\s*"
            r"<td class=\"nowrap\">\s*(?P<date>[^<]+?)\s*</td>"
            r"[\s\S]*?<td class=\"filing-type[^\"]*\">\s*(?P<type>[^<]+?)\s*</td>"
            r"[\s\S]*?<td>\s*(?P<desc>[\s\S]*?)\s*</td>"
            r"[\s\S]*?<a href=\"(?P<pdf>/company/[^\"]+/document\?format=pdf&amp;download=0)\""
            r"[\s\S]*?(?:<a[^>]+href=\"(?P<xhtml>/company/[^\"]+/document\?format=xhtml&amp;download=1)\")?"
            r"[\s\S]*?</tr>",
            re.I,
        )
        for match in pattern.finditer(html):
            filing_type = normalize_whitespace(match.group("type"))
            if filing_type != "AA":
                continue
            pdf_href = match.group("pdf")
            xhtml_href = match.group("xhtml")
            rows.append(
                {
                    "date": normalize_whitespace(match.group("date")),
                    "type": filing_type,
                    "description": strip_tags(match.group("desc")),
                    "links": {
                        "pdf": pdf_href.replace("&amp;", "&") if pdf_href else None,
                        "xhtml": xhtml_href.replace("&amp;", "&") if xhtml_href else None,
                    },
                    "source": "website",
                }
            )
        return rows

    def get_document_urls(self, filing: dict[str, Any]) -> dict[str, str]:
        website_links = filing.get("links", {})
        pdf_link = website_links.get("pdf")
        xhtml_link = website_links.get("xhtml")
        if not pdf_link and not xhtml_link:
            return {}
        links: dict[str, str] = {}
        if pdf_link:
            base = pdf_link if pdf_link.startswith("http") else f"{WEBSITE_BASE}{pdf_link}"
            links["metadata"] = base
            links["pdf"] = re.sub(r"format=pdf&download=0", "format=pdf&download=1", base)
        if xhtml_link:
            links["xhtml"] = xhtml_link if xhtml_link.startswith("http") else f"{WEBSITE_BASE}{xhtml_link}"
        elif "pdf" in links:
            links["xhtml"] = re.sub(r"format=pdf&download=1", "format=xhtml&download=1", links["pdf"])
        return links
