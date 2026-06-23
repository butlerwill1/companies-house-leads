#!/usr/bin/env python3
"""API-first Companies House extractor.

The main path uses the official Companies House Public Data API and Document
API. An optional website scraper exists in a separate module and is only used
when you explicitly enable it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from companies_house_website_fallback import CompaniesHouseWebsiteFallback

PUBLIC_API_BASE = "https://api.company-information.service.gov.uk"
DOCUMENT_API_BASE = "https://document-api.company-information.service.gov.uk"


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def strip_tags(value: str) -> str:
    return normalize_whitespace(re.sub(r"<[^>]+>", " ", value))


def parse_display_number(raw: str) -> int | None:
    text = strip_tags(raw)
    if not text or text == "-":
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9]", "", text)
    if not cleaned:
        return None
    number = int(cleaned)
    return -number if negative else number


def format_currency(value: int | None) -> str | None:
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    return f"{sign}GBP {abs(value):,}"


def percentage_change(current: int | None, previous: int | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1) * 100


def ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def find_first(pattern: str, text: str, flags: int = re.I | re.S) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else None


@dataclass
class SearchResult:
    company_name: str
    company_number: str
    company_status: str | None = None
    address: str | None = None
    source: str | None = None


class HttpClient:
    def __init__(self, api_key: str | None = None, timeout: int = 30) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _make_request(self, url: str, headers: dict[str, str] | None = None) -> urllib.request.Request:
        request = urllib.request.Request(url, headers=headers or {})
        if self.api_key:
            token = base64.b64encode(f"{self.api_key}:".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        request.add_header("User-Agent", "companies-house-extract/1.0")
        return request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            return None

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        request = self._make_request(url, headers=headers)
        opener = urllib.request.build_opener(self._NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers.get("Location")
            if not location:
                raise

            redirect_url = urllib.parse.urljoin(url, location)
            original_host = urllib.parse.urlparse(url).netloc
            redirect_host = urllib.parse.urlparse(redirect_url).netloc
            redirect_headers = dict(headers or {})
            if redirect_host != original_host:
                redirect_headers = {}

            redirect_request = urllib.request.Request(redirect_url, headers=redirect_headers)
            redirect_request.add_header("User-Agent", "companies-house-extract/1.0")
            with urllib.request.urlopen(redirect_request, timeout=self.timeout) as response:
                return response.read()

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        data = self.get_bytes(url, headers=headers)
        return data.decode("utf-8", errors="ignore")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return json.loads(self.get_text(url, headers=headers))

    def download(self, url: str, destination: Path, headers: dict[str, str] | None = None) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.get_bytes(url, headers=headers)
        destination.write_bytes(data)
        return destination


class CompaniesHouseExtractor:
    def __init__(self, api_key: str | None = None, allow_website_fallback: bool = False) -> None:
        self.api_key = api_key
        self.allow_website_fallback = allow_website_fallback
        self.api_client = HttpClient(api_key=api_key)
        self.web_client = HttpClient(api_key=None)
        self.website_fallback = CompaniesHouseWebsiteFallback(self.web_client) if allow_website_fallback else None

    @property
    def has_api(self) -> bool:
        return bool(self.api_key)

    def search_companies(self, query: str) -> list[SearchResult]:
        if self.has_api:
            try:
                url = f"{PUBLIC_API_BASE}/search/companies?q={urllib.parse.quote(query)}"
                payload = self.api_client.get_json(url)
                results = []
                for item in payload.get("items", []):
                    results.append(
                        SearchResult(
                            company_name=item.get("title", "").strip(),
                            company_number=item.get("company_number", "").strip(),
                            company_status=item.get("company_status"),
                            address=normalize_whitespace(item.get("address_snippet", "")) or None,
                            source="public_api",
                        )
                    )
                return results
            except Exception:
                if not self.website_fallback:
                    raise
        if not self.website_fallback:
            raise RuntimeError("No API key available and website fallback is disabled.")
        return [SearchResult(**item) for item in self.website_fallback.search_companies(query)]

    def get_company_profile(self, company_number: str) -> dict[str, Any]:
        if self.has_api:
            try:
                return self.api_client.get_json(f"{PUBLIC_API_BASE}/company/{company_number}")
            except Exception:
                if not self.website_fallback:
                    raise
        if not self.website_fallback:
            raise RuntimeError("No API key available and website fallback is disabled.")
        return self.website_fallback.get_company_profile(company_number)

    def get_accounts_filings(self, company_number: str) -> list[dict[str, Any]]:
        if self.has_api:
            try:
                url = (
                    f"{PUBLIC_API_BASE}/company/{company_number}/filing-history"
                    f"?category=accounts&items_per_page=100"
                )
                payload = self.api_client.get_json(url)
                return payload.get("items", [])
            except Exception:
                if not self.website_fallback:
                    raise
        if not self.website_fallback:
            raise RuntimeError("No API key available and website fallback is disabled.")
        return self.website_fallback.get_accounts_filings(company_number)

    def get_document_urls(self, company_number: str, filing: dict[str, Any] | None) -> dict[str, str]:
        if not filing:
            return {}
        if self.has_api and filing.get("links", {}).get("document_metadata"):
            metadata_path = filing["links"]["document_metadata"]
            metadata_url = metadata_path if metadata_path.startswith("http") else f"{DOCUMENT_API_BASE}{metadata_path}"
            metadata = self.api_client.get_json(metadata_url)
            links = {"metadata": metadata_url}
            resources = metadata.get("resources", {})
            document_id = metadata.get("id") or metadata_url.rstrip("/").split("/")[-1]
            content_base = f"{DOCUMENT_API_BASE}/document/{document_id}/content"
            if "application/xhtml+xml" in resources:
                links["xhtml"] = content_base
            if "application/pdf" in resources:
                links["pdf"] = content_base
            return links

        if not self.website_fallback:
            return {}
        return self.website_fallback.get_document_urls(filing)

    def fetch_document(self, url: str, content_type: str | None = None) -> bytes:
        headers = {"Accept": content_type} if content_type else None
        if "document-api.company-information.service.gov.uk" in url:
            client = self.api_client
        else:
            if not self.website_fallback:
                raise RuntimeError("Website document fetch requested while website fallback is disabled.")
            client = self.web_client
        return client.get_bytes(url, headers=headers)

    def parse_xhtml_accounts(self, xhtml_text: str) -> dict[str, Any]:
        metrics = self._extract_ixbrl_metrics(xhtml_text)
        visible_rows = {
            "turnover": self._extract_visible_two_column_row(xhtml_text, "Turnover"),
            "gross_profit": self._extract_visible_two_column_row(xhtml_text, "Gross profit"),
            "operating_result": self._extract_visible_two_column_row(xhtml_text, "Operating loss")
            or self._extract_visible_two_column_row(xhtml_text, "Operating profit"),
            "uk_revenue": self._extract_visible_two_column_row(xhtml_text, "United Kingdom"),
            "rest_of_europe_revenue": self._extract_visible_two_column_row(xhtml_text, "Rest of Europe"),
        }
        commentary = self._extract_commentary(xhtml_text)
        years = self._build_year_views(metrics, visible_rows)
        derived = self._derive_metrics(years)
        return {
            "years": years,
            "derived": derived,
            "commentary": commentary,
            "raw_metric_count": len(metrics),
        }

    def _extract_ixbrl_metrics(self, xhtml_text: str) -> dict[tuple[str, str], int]:
        ns = {"ix": "http://www.xbrl.org/2013/inlineXBRL"}
        root = ET.fromstring(xhtml_text)
        metrics: dict[tuple[str, str], int] = {}
        for tag in root.findall(".//ix:nonFraction", ns):
            name = tag.attrib.get("name")
            context_ref = tag.attrib.get("contextRef")
            if not name or not context_ref:
                continue
            value_text = "".join(tag.itertext()).strip()
            cleaned = re.sub(r"[^0-9]", "", value_text)
            if not cleaned:
                continue
            value = int(cleaned)
            if tag.attrib.get("sign") == "-":
                value = -value
            metrics[(name, context_ref)] = value
        return metrics

    def _extract_visible_two_column_row(self, xhtml_text: str, label: str) -> dict[str, int | None] | None:
        pattern = re.compile(
            rf">{re.escape(label)}</div>(?P<section>.{{0,1800}}?)"
            r'(?:(?:<div class="crn fn1"[^>]*>(?P<v1>.*?)</div>).*?'
            r'(?:<div class="crn fn1"[^>]*>(?P<v2>.*?)</div>))',
            re.I | re.S,
        )
        match = pattern.search(xhtml_text)
        if not match:
            return None
        return {
            "current": parse_display_number(match.group("v1")),
            "previous": parse_display_number(match.group("v2")),
        }

    def _extract_commentary(self, xhtml_text: str) -> dict[str, Any]:
        snippets: dict[str, Any] = {}
        second_half = find_first(r"resulted in a ([0-9]+%) revenue increase in the second half of the year", xhtml_text)
        backlog = find_first(r"backlog of Ł([0-9.]+) million", xhtml_text)
        revenue = find_first(r"total revenue for the year reached Ł([0-9,]+)", xhtml_text)
        headcount = find_first(
            r"headcount increased from ([0-9]+) in June 2024 to ([0-9]+) in February 2025", xhtml_text
        )
        if second_half:
            snippets["second_half_revenue_growth"] = second_half
        if backlog:
            snippets["backlog_million_gbp"] = float(backlog)
        if revenue:
            snippets["management_stated_revenue_gbp"] = int(revenue.replace(",", ""))
        if headcount:
            counts = re.search(
                r"headcount increased from ([0-9]+) in June 2024 to ([0-9]+) in February 2025", xhtml_text, re.I
            )
            if counts:
                snippets["headcount_growth"] = {
                    "june_2024": int(counts.group(1)),
                    "february_2025": int(counts.group(2)),
                }
        return snippets

    def _build_year_views(
        self,
        metrics: dict[tuple[str, str], int],
        visible_rows: dict[str, dict[str, int | None] | None],
    ) -> dict[str, dict[str, int | None]]:
        current = {
            "turnover": self._visible_value(visible_rows["turnover"], "current"),
            "cost_of_sales": metrics.get(("core:CostSales", "C")),
            "gross_profit": self._visible_value(visible_rows["gross_profit"], "current"),
            "administrative_expenses": metrics.get(("core:AdministrativeExpenses", "C")),
            "operating_result": self._visible_value(visible_rows["operating_result"], "current"),
            "profit_before_tax": metrics.get(("core:ProfitLossOnOrdinaryActivitiesBeforeTax", "C")),
            "tax": metrics.get(("core:TaxTaxCreditOnProfitOrLossOnOrdinaryActivities", "C")),
            "profit_after_tax": metrics.get(("core:ProfitLoss", "C")),
            "cash": metrics.get(("core:CashBankOnHand", "B")),
            "current_assets": metrics.get(("core:CurrentAssets", "B")),
            "current_liabilities": metrics.get(("core:Creditors", "B_AI_BQ")),
            "net_current_assets": metrics.get(("core:NetCurrentAssetsLiabilities", "B")),
            "net_assets": metrics.get(("core:Equity", "B")),
            "debtors": metrics.get(("core:Debtors", "B_AI_BQ_AM_BR")) or metrics.get(("core:Debtors", "B")),
            "trade_debtors": metrics.get(("core:TradeDebtorsTradeReceivables", "B_AI_BQ")),
            "cash_absorbed_by_operations": -metrics.get(("core:NetCashGeneratedFromOperations", "C"), 0),
            "net_cash_from_financing": -metrics.get(
                ("core:FurtherItemCashFlowFromUsedInFinancingActivitiesComponentNetCashFlowsFromUsedInFinancingActivities", "C_BW_BX"),
                0,
            ),
            "net_change_in_cash": -metrics.get(
                ("core:IncreaseDecreaseInCashCashEquivalentsBeforeForeignExchangeDifferencesChangesInConsolidation", "C"),
                0,
            ),
            "employees": metrics.get(("core:AverageNumberEmployeesDuringPeriod", "C")),
            "staff_costs": metrics.get(("core:StaffCostsEmployeeBenefitsExpense", "C")),
            "uk_revenue": self._visible_value(visible_rows["uk_revenue"], "current"),
            "rest_of_europe_revenue": self._visible_value(visible_rows["rest_of_europe_revenue"], "current"),
        }
        previous = {
            "turnover": self._visible_value(visible_rows["turnover"], "previous"),
            "cost_of_sales": metrics.get(("core:CostSales", "F")),
            "gross_profit": self._visible_value(visible_rows["gross_profit"], "previous"),
            "administrative_expenses": metrics.get(("core:AdministrativeExpenses", "F")),
            "operating_result": self._visible_value(visible_rows["operating_result"], "previous"),
            "profit_before_tax": metrics.get(("core:ProfitLossOnOrdinaryActivitiesBeforeTax", "F")),
            "tax": metrics.get(("core:TaxTaxCreditOnProfitOrLossOnOrdinaryActivities", "F")),
            "profit_after_tax": metrics.get(("core:ProfitLoss", "F")),
            "cash": metrics.get(("core:CashBankOnHand", "E")),
            "current_assets": metrics.get(("core:CurrentAssets", "E")),
            "current_liabilities": metrics.get(("core:Creditors", "E_AI_BQ")),
            "net_current_assets": metrics.get(("core:NetCurrentAssetsLiabilities", "E")),
            "net_assets": metrics.get(("core:Equity", "E")),
            "debtors": metrics.get(("core:Debtors", "E_AI_BQ_AM_BR")) or metrics.get(("core:Debtors", "E")),
            "trade_debtors": metrics.get(("core:TradeDebtorsTradeReceivables", "E_AI_BQ")),
            "cash_absorbed_by_operations": -metrics.get(("core:NetCashGeneratedFromOperations", "F"), 0),
            "net_cash_from_financing": -metrics.get(
                ("core:FurtherItemCashFlowFromUsedInFinancingActivitiesComponentNetCashFlowsFromUsedInFinancingActivities", "F_BW_BX"),
                0,
            ),
            "net_change_in_cash": metrics.get(
                ("core:IncreaseDecreaseInCashCashEquivalentsBeforeForeignExchangeDifferencesChangesInConsolidation", "F")
            ),
            "employees": metrics.get(("core:AverageNumberEmployeesDuringPeriod", "F")),
            "staff_costs": metrics.get(("core:StaffCostsEmployeeBenefitsExpense", "F")),
            "uk_revenue": self._visible_value(visible_rows["uk_revenue"], "previous"),
            "rest_of_europe_revenue": self._visible_value(visible_rows["rest_of_europe_revenue"], "previous"),
        }
        return {"current": current, "previous": previous}

    def _visible_value(self, row: dict[str, int | None] | None, key: str) -> int | None:
        if not row:
            return None
        return row.get(key)

    def _derive_metrics(self, years: dict[str, dict[str, int | None]]) -> dict[str, float | None]:
        current = years["current"]
        previous = years["previous"]
        return {
            "turnover_change_pct": percentage_change(current["turnover"], previous["turnover"]),
            "gross_profit_change_pct": percentage_change(current["gross_profit"], previous["gross_profit"]),
            "gross_margin_current_pct": ratio(current["gross_profit"], current["turnover"]),
            "gross_margin_previous_pct": ratio(previous["gross_profit"], previous["turnover"]),
            "operating_margin_current_pct": ratio(current["operating_result"], current["turnover"]),
            "operating_margin_previous_pct": ratio(previous["operating_result"], previous["turnover"]),
            "net_margin_current_pct": ratio(current["profit_after_tax"], current["turnover"]),
            "net_margin_previous_pct": ratio(previous["profit_after_tax"], previous["turnover"]),
            "cash_change_pct": percentage_change(current["cash"], previous["cash"]),
            "current_ratio_current": ratio(current["current_assets"], current["current_liabilities"]),
            "current_ratio_previous": ratio(previous["current_assets"], previous["current_liabilities"]),
            "revenue_per_employee_current": ratio(current["turnover"], current["employees"]),
            "revenue_per_employee_previous": ratio(previous["turnover"], previous["employees"]),
            "uk_revenue_share_current_pct": ratio(current["uk_revenue"], current["turnover"]),
            "uk_revenue_share_previous_pct": ratio(previous["uk_revenue"], previous["turnover"]),
        }


def parse_xhtml_narrative(xhtml_text: str) -> dict[str, Any]:
    """Extract qualitative narrative sections and performance sentences from an iXBRL/XHTML document.

    Strips all HTML/XBRL markup then runs the same section-heading and
    performance-sentence extractors used on OCR'd PDFs.  The result is a dict
    compatible with companies_house_sqlite.insert_narrative_payload().
    """
    from companies_house_pdf_full import (
        extract_sections,
        extract_performance_statements,
        summarize_text_quality,
    )

    # Drop <head>, <style> and <script> blocks before stripping markup so that
    # CSS class names and JS strings don't pollute the extracted text.
    cleaned = re.sub(r"<head\b[^>]*>.*?</head>", " ", xhtml_text, flags=re.I | re.S)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", " ", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
    plain_text = strip_tags(cleaned)

    # Treat the whole document as one page (XHTML has no page boundaries).
    page_texts = [plain_text]
    return {
        "pdf_path": None,
        "text_source": "xhtml",
        "ocr_requested": False,
        "ocr_engine_requested": None,
        "ocr_used": False,
        "ocr_engine_used": None,
        "text_quality": summarize_text_quality(page_texts),
        "sections": extract_sections(page_texts),
        "performance_statements": extract_performance_statements(page_texts),
        "ocr_financials": {},  # Financial data already extracted via iXBRL tags
    }


def choose_company(results: list[SearchResult], company_number: str | None, query: str | None) -> SearchResult:
    if company_number:
        for result in results:
            if result.company_number == company_number:
                return result
        return SearchResult(company_name=query or company_number, company_number=company_number, source="manual")
    if not results:
        raise RuntimeError("No companies matched the search query.")
    if query:
        exact = [r for r in results if r.company_name.lower() == query.lower()]
        if exact:
            return exact[0]
    return results[0]


def pick_latest_accounts_filing(filings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not filings:
        return None
    return filings[0]


def build_report(payload: dict[str, Any]) -> str:
    accounts_extract = payload.get("accounts_extract") or {}
    if "years" not in accounts_extract:
        return "\n".join(
            [
                f"# {payload['label']}",
                "",
                f"Reviewed: {payload['generated_at']}",
                "",
                "Accounts document could not be parsed into structured financial metrics.",
            ]
        ) + "\n"
    current = accounts_extract["years"]["current"]
    previous = accounts_extract["years"]["previous"]
    derived = accounts_extract["derived"]
    lines = [
        f"# {payload['label']}",
        "",
        f"Reviewed: {payload['generated_at']}",
        "",
        "## Company",
        "",
        f"- Legal entity: {payload['company_profile'].get('company_name')}",
        f"- Company number: {payload['company_number']}",
        f"- Status: {payload['company_profile'].get('company_status')}",
        f"- Source mode: {payload['source_mode']}",
        "",
        "## Latest accounts filing",
        "",
    ]
    latest_accounts = payload.get("latest_accounts_filing")
    if latest_accounts:
        lines.extend(
            [
                f"- Filing date: {latest_accounts.get('date')}",
                f"- Description: {latest_accounts.get('description')}",
                "",
                "## Financial highlights",
                "",
                f"- Turnover: {format_currency(current['turnover'])} vs {format_currency(previous['turnover'])}",
                f"- Gross profit: {format_currency(current['gross_profit'])} vs {format_currency(previous['gross_profit'])}",
                f"- Operating result: {format_currency(current['operating_result'])} vs {format_currency(previous['operating_result'])}",
                f"- Profit after tax: {format_currency(current['profit_after_tax'])} vs {format_currency(previous['profit_after_tax'])}",
                f"- Cash: {format_currency(current['cash'])} vs {format_currency(previous['cash'])}",
                f"- Net assets: {format_currency(current['net_assets'])} vs {format_currency(previous['net_assets'])}",
                "",
                "## Derived metrics",
                "",
                f"- Turnover change: {render_pct(derived.get('turnover_change_pct'))}",
                f"- Gross margin: {render_pct(derived.get('gross_margin_current_pct'), scale=100)} vs {render_pct(derived.get('gross_margin_previous_pct'), scale=100)}",
                f"- Operating margin: {render_pct(derived.get('operating_margin_current_pct'), scale=100)} vs {render_pct(derived.get('operating_margin_previous_pct'), scale=100)}",
                f"- Net margin: {render_pct(derived.get('net_margin_current_pct'), scale=100)} vs {render_pct(derived.get('net_margin_previous_pct'), scale=100)}",
                f"- Current ratio: {render_float(derived.get('current_ratio_current'))} vs {render_float(derived.get('current_ratio_previous'))}",
            ]
        )
    else:
        lines.append("- No public accounts filing was found for the selected company.")
    commentary = payload["accounts_extract"].get("commentary", {})
    if commentary:
        lines.extend(["", "## Commentary extracted from filing", ""])
        for key, value in commentary.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def render_pct(value: float | None, scale: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value * scale:.2f}%"


def render_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}x"


def infer_source_mode(
    results: list[SearchResult],
    profile: dict[str, Any] | None,
    latest_filing: dict[str, Any] | None,
) -> str:
    sources = {result.source for result in results if result.source}
    if profile and profile.get("source"):
        sources.add(profile["source"])
    if latest_filing and latest_filing.get("source"):
        sources.add(latest_filing["source"])
    if "website" in sources:
        return "website_fallback"
    return "public_api"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract Companies House financial data for a company.")
    parser.add_argument("--query", help="Company search query.")
    parser.add_argument("--company-number", help="Exact Companies House company number.")
    parser.add_argument("--label", help="Output label.", default="Companies House extract")
    parser.add_argument("--output-json", help="Path to save structured JSON output.", required=True)
    parser.add_argument("--output-report", help="Optional path to save a markdown report.")
    parser.add_argument("--download-dir", help="Optional directory for downloaded source documents.")
    parser.add_argument(
        "--allow-website-fallback",
        action="store_true",
        help="Opt in to HTML scraping if the official API is unavailable.",
    )
    args = parser.parse_args(argv)

    if not args.query and not args.company_number:
        parser.error("Pass at least one of --query or --company-number.")

    load_dotenv(Path(".env"))
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY")
    extractor = CompaniesHouseExtractor(api_key=api_key, allow_website_fallback=args.allow_website_fallback)

    results = extractor.search_companies(args.query or args.company_number)
    selected = choose_company(results, args.company_number, args.query)
    candidate_results = [selected] + [r for r in results if r.company_number != selected.company_number]

    profile: dict[str, Any] | None = None
    filings: list[dict[str, Any]] = []
    latest_filing: dict[str, Any] | None = None
    selected_with_accounts = selected
    for candidate in candidate_results:
        company_number = candidate.company_number
        profile = extractor.get_company_profile(company_number)
        filings = extractor.get_accounts_filings(company_number)
        latest_filing = pick_latest_accounts_filing(filings)
        selected_with_accounts = candidate
        if latest_filing or args.company_number:
            break

    selected = selected_with_accounts
    company_number = selected.company_number
    assert profile is not None
    document_urls = extractor.get_document_urls(company_number, latest_filing)

    downloaded_files: dict[str, str] = {}
    xhtml_text: str | None = None
    download_dir = Path(args.download_dir) if args.download_dir else None
    if download_dir:
        download_dir.mkdir(parents=True, exist_ok=True)

    if latest_filing and document_urls.get("xhtml"):
        xhtml_headers = {"Accept": "application/xhtml+xml"} if "document-api" in document_urls["xhtml"] else None
        xhtml_data = extractor.fetch_document(document_urls["xhtml"], content_type=xhtml_headers["Accept"] if xhtml_headers else None)
        xhtml_text = xhtml_data.decode("utf-8", errors="ignore")
        if download_dir:
            xhtml_path = download_dir / f"{company_number}-latest-accounts.xhtml"
            xhtml_path.write_bytes(xhtml_data)
            downloaded_files["xhtml"] = str(xhtml_path)

    if latest_filing and download_dir and document_urls.get("pdf"):
        pdf_headers = {"Accept": "application/pdf"} if "document-api" in document_urls["pdf"] else None
        pdf_data = extractor.fetch_document(document_urls["pdf"], content_type=pdf_headers["Accept"] if pdf_headers else None)
        pdf_path = download_dir / f"{company_number}-latest-accounts.pdf"
        pdf_path.write_bytes(pdf_data)
        downloaded_files["pdf"] = str(pdf_path)

    accounts_extract = extractor.parse_xhtml_accounts(xhtml_text) if xhtml_text else {}
    source_mode = infer_source_mode(results, profile, latest_filing)

    payload = {
        "generated_at": iso_utc_now(),
        "label": args.label,
        "query": args.query,
        "company_number": company_number,
        "source_mode": source_mode,
        "search_results": [result.__dict__ for result in results],
        "selected_company": selected.__dict__,
        "company_profile": profile,
        "latest_accounts_filing": latest_filing,
        "document_urls": document_urls,
        "downloaded_files": downloaded_files,
        "accounts_extract": accounts_extract,
    }

    output_json = Path(args.output_json)
    output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.output_report:
        Path(args.output_report).write_text(build_report(payload), encoding="utf-8")

    print(json.dumps({"company_number": company_number, "output_json": str(output_json)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
