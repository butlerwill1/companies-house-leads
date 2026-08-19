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
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any

from core.companies_house_website_fallback import CompaniesHouseWebsiteFallback

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


def parse_financial_year(value: Any) -> int | None:
    """Return an explicit reporting year without inferring one from filing metadata."""
    if value is None or isinstance(value, bool):
        return None
    years = {
        int(year)
        for year in re.findall(r"(?<!\d)((?:19|20|21)\d{2})(?!\d)", str(value))
    }
    return max(years) if years else None


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


# Concepts counting people rather than money. Two filer habits break the
# monetary parsing path for these: a negative `scale` (e.g. rendering "483"
# with scale="-2", implying 4.83 employees at a company with hundreds of
# staff) and a genuinely fractional average headcount (e.g. "2.5" part-time
# equivalents), whose decimal point the monetary path strips. Headcounts are
# never legitimately reported in hundredths of a person, so for these
# concepts the rendered figure is authoritative and a negative scale is
# treated as a tagging error.
COUNT_CONCEPTS = frozenset({"AverageNumberEmployeesDuringPeriod"})

# iXBRL tags are matched by local element name only (the namespace prefix
# varies by filing agent, e.g. "core:ProfitLoss" vs "ns5:ProfitLoss" for the
# same UK-GAAP concept). Each metric lists candidate local names in priority
# order; the first name with exactly one unambiguous, non-dimensional match
# for the target year wins.
METRIC_TAG_SYNONYMS: dict[str, tuple[str, ...]] = {
    "turnover": ("TurnoverRevenue", "Turnover", "Revenue"),
    "cost_of_sales": ("CostSales",),
    "gross_profit": ("GrossProfitLoss", "GrossProfit"),
    "administrative_expenses": ("AdministrativeExpenses",),
    "operating_result": ("OperatingProfitLoss", "OperatingResult"),
    "profit_before_tax": ("ProfitLossOnOrdinaryActivitiesBeforeTax",),
    "tax": ("TaxTaxCreditOnProfitOrLossOnOrdinaryActivities",),
    "profit_after_tax": ("ProfitLoss",),
    "cash": ("CashBankOnHand",),
    "current_assets": ("CurrentAssets",),
    "current_liabilities": ("Creditors",),
    "net_current_assets": ("NetCurrentAssetsLiabilities",),
    "net_assets": ("NetAssetsLiabilities", "Equity"),
    "debtors": ("Debtors",),
    "trade_debtors": ("TradeDebtorsTradeReceivables",),
    "employees": ("AverageNumberEmployeesDuringPeriod",),
    "staff_costs": ("StaffCostsEmployeeBenefitsExpense",),
}


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

    def get_accounts_history(
        self,
        company_number: str,
        *,
        years: int = 5,
        max_filings: int = 4,
    ) -> list[dict[str, Any]]:
        """Up to `max_filings` distinct-period accounts filings within
        `years` of today, newest first. Deduplicates by accounting period
        end date (the API's `made_up_date`), not by filing date or calendar
        year: a company can file twice in one calendar year for two
        different periods (e.g. after a late filing catches up), and an
        AA01 accounting-reference-date change is not an accounts filing at
        all. When two filings share the same period end (an AAMD amendment
        superseding an earlier AA), the more recently *filed* one wins."""
        accounts = [f for f in self.get_accounts_filings(company_number) if f.get("type") in ("AA", "AAMD")]
        by_period: dict[str, dict[str, Any]] = {}
        for filing in accounts:
            period_end = (filing.get("description_values") or {}).get("made_up_date") or filing.get("action_date")
            if not period_end:
                continue
            existing = by_period.get(period_end)
            if existing is None or (filing.get("date") or "") > (existing.get("date") or ""):
                by_period[period_end] = filing
        cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * years)).date().isoformat()
        ordered = sorted(by_period.items(), key=lambda item: item[0], reverse=True)
        recent = [filing for period_end, filing in ordered if period_end >= cutoff]
        return recent[:max_filings]

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
        context_years = self._extract_ixbrl_context_years(xhtml_text)
        context_dates = self._extract_ixbrl_context_dates(xhtml_text)
        context_currencies = self._extract_ixbrl_context_currencies(xhtml_text)
        context_dimensioned = self._extract_ixbrl_context_dimensions(xhtml_text)
        visible_rows = {
            "turnover": self._extract_visible_two_column_row(xhtml_text, "Turnover"),
            "gross_profit": self._extract_visible_two_column_row(xhtml_text, "Gross profit"),
            "operating_result": self._extract_visible_two_column_row(xhtml_text, "Operating loss")
            or self._extract_visible_two_column_row(xhtml_text, "Operating profit"),
            "uk_revenue": self._extract_visible_two_column_row(xhtml_text, "United Kingdom"),
            "rest_of_europe_revenue": self._extract_visible_two_column_row(xhtml_text, "Rest of Europe"),
        }
        commentary = self._extract_commentary(xhtml_text)
        financial_years = self._build_financial_years(context_years)
        financial_periods = self._build_financial_periods(context_dates)
        years = self._build_year_views(
            metrics, visible_rows, context_dates, context_dimensioned, financial_periods
        )
        for period_type, financial_year in financial_years.items():
            years[period_type]["financial_year"] = financial_year
            matching = [key for key, year in context_years.items() if year == financial_year]
            currencies = {context_currencies[key] for key in matching if context_currencies.get(key)}
            years[period_type]["period_end_on"] = max((context_dates[key] for key in matching if context_dates.get(key)), default=None)
            years[period_type]["currency_code"] = next(iter(currencies)) if len(currencies) == 1 else None
            years[period_type]["currency_source"] = "ixbrl_unit_ref" if currencies else None
            years[period_type]["currency_validation_status"] = "valid" if len(currencies) == 1 else ("mixed" if len(currencies) > 1 else "unknown")
        derived = self._derive_metrics(years)
        return {
            "years": years,
            "derived": derived,
            "commentary": commentary,
            "raw_metric_count": len(metrics),
        }

    def _extract_ixbrl_context_years(self, xhtml_text: str) -> dict[str, int]:
        ns = {"xbrli": "http://www.xbrl.org/2003/instance"}
        root = ET.fromstring(xhtml_text)
        context_years: dict[str, int] = {}
        for context in root.findall(".//xbrli:context", ns):
            context_id = context.attrib.get("id")
            period = context.find("xbrli:period", ns)
            if not context_id or period is None:
                continue
            end = period.find("xbrli:endDate", ns)
            instant = period.find("xbrli:instant", ns)
            financial_year = parse_financial_year(
                end.text if end is not None else instant.text if instant is not None else None
            )
            if financial_year is not None:
                context_years[context_id] = financial_year
        return context_years

    def _extract_ixbrl_context_dates(self, xhtml_text: str) -> dict[str, str]:
        ns = {"xbrli": "http://www.xbrl.org/2003/instance"}
        root = ET.fromstring(xhtml_text)
        result: dict[str, str] = {}
        for context in root.findall(".//xbrli:context", ns):
            period = context.find("xbrli:period", ns)
            value = period.find("xbrli:endDate", ns) if period is not None else None
            if value is None and period is not None:
                value = period.find("xbrli:instant", ns)
            if context.attrib.get("id") and value is not None and value.text:
                result[context.attrib["id"]] = value.text.strip()
        return result

    def _extract_ixbrl_context_currencies(self, xhtml_text: str) -> dict[str, str]:
        ns = {"ix": "http://www.xbrl.org/2013/inlineXBRL", "xbrli": "http://www.xbrl.org/2003/instance"}
        root = ET.fromstring(xhtml_text)
        units = {unit.attrib.get("id"): "".join(unit.itertext()).upper() for unit in root.findall(".//xbrli:unit", ns)}
        result: dict[str, str] = {}
        for tag in root.findall(".//ix:nonFraction", ns):
            unit = units.get(tag.attrib.get("unitRef"), "")
            currency = re.search(r"([A-Z]{3})$", unit)
            if currency and tag.attrib.get("contextRef"):
                result[tag.attrib["contextRef"]] = currency.group(1)
        return result

    def _build_financial_years(
        self,
        context_years: dict[str, int],
    ) -> dict[str, int | None]:
        # Period identity comes from explicit context dates, not vendor-specific IDs.
        ordered_years = sorted(set(context_years.values()), reverse=True)
        return {
            "current": ordered_years[0] if ordered_years else None,
            "previous": ordered_years[1] if len(ordered_years) > 1 else None,
        }

    def _build_financial_periods(
        self,
        context_dates: dict[str, str],
    ) -> dict[str, str | None]:
        """The current/previous period identified by their exact end date
        (ISO string), not by calendar year. A company with a shifted
        accounting reference date can have its current and comparative
        periods both end in the same calendar year (e.g. 2024-12-31 and
        2024-03-31), which would collide under year-only matching."""
        ordered_dates = sorted(set(context_dates.values()), reverse=True)
        return {
            "current": ordered_dates[0] if ordered_dates else None,
            "previous": ordered_dates[1] if len(ordered_dates) > 1 else None,
        }

    def _extract_ixbrl_context_dimensions(self, xhtml_text: str) -> dict[str, bool]:
        """True where a context carries an xbrli:segment (a dimensional
        qualifier, e.g. a segment/component breakdown) rather than being a
        plain whole-entity total for its period."""
        ns = {"xbrli": "http://www.xbrl.org/2003/instance"}
        root = ET.fromstring(xhtml_text)
        result: dict[str, bool] = {}
        for context in root.findall(".//xbrli:context", ns):
            context_id = context.attrib.get("id")
            if not context_id:
                continue
            entity = context.find("xbrli:entity", ns)
            has_segment = entity is not None and entity.find("xbrli:segment", ns) is not None
            result[context_id] = has_segment
        return result

    def _extract_ixbrl_metrics(self, xhtml_text: str) -> dict[tuple[str, str], int]:
        """Keyed by (local element name, contextRef). The namespace prefix is
        dropped because it varies by filing agent while the local name (from
        the shared UK-GAAP taxonomy) is stable."""
        ns = {"ix": "http://www.xbrl.org/2013/inlineXBRL"}
        root = ET.fromstring(xhtml_text)
        metrics: dict[tuple[str, str], int] = {}
        for tag in root.findall(".//ix:nonFraction", ns):
            name = tag.attrib.get("name")
            context_ref = tag.attrib.get("contextRef")
            if not name or not context_ref:
                continue
            local_name = name.split(":", 1)[-1]
            value_text = "".join(tag.itertext()).strip()
            scale = int(tag.attrib.get("scale", "0"))
            if local_name in COUNT_CONCEPTS:
                # Keep the decimal point (a fractional average headcount is
                # real) and drop a negative scale (a tagging error).
                magnitude = re.sub(r"[^0-9.]", "", value_text).strip(".")
                if not magnitude:
                    continue
                value = float(magnitude) * (10 ** max(scale, 0))
                if value == int(value):
                    value = int(value)
            else:
                cleaned = re.sub(r"[^0-9]", "", value_text)
                if not cleaned:
                    continue
                value = int(cleaned) * (10 ** scale)
            if tag.attrib.get("sign") == "-":
                value = -value
            metrics[(local_name, context_ref)] = value
        return metrics

    def _resolve_year_metric(
        self,
        metrics: dict[tuple[str, str], int],
        context_dates: dict[str, str],
        context_dimensioned: dict[str, bool],
        names: tuple[str, ...],
        period_end: str | None,
    ) -> int | None:
        """Resolve a metric from one or more synonym concept names, matched
        to the context's real period end date rather than calendar year
        (a shifted accounting reference date can put both the current and
        comparative period end in the same calendar year). A synonym list
        exists because different filing agents tag the same UK-GAAP concept
        under different (but equivalent) element names — e.g. "net assets"
        as both NetAssetsLiabilities and Equity. Every synonym that resolves
        unambiguously (exactly one non-dimensional whole-entity value for
        that date) is collected; if they all agree, that value is returned.
        If any two disagree — e.g. a parent-only figure and a group figure
        that happen to share a context id, or a filer that forgot a sign
        attribute on one of two equivalent tags — that is a genuine,
        irresolvable conflict in the source filing, and None is returned
        rather than guessing which one is right."""
        if period_end is None:
            return None
        resolved: set[int] = set()
        for name in names:
            candidates = {
                value
                for (metric_name, context_ref), value in metrics.items()
                if metric_name == name
                and context_dates.get(context_ref) == period_end
                and not context_dimensioned.get(context_ref, False)
            }
            if len(candidates) == 1:
                resolved.add(next(iter(candidates)))
        if len(resolved) == 1:
            return next(iter(resolved))
        return None

    def _extract_visible_two_column_row(self, xhtml_text: str, label: str) -> dict[str, int | None] | None:
        anchor = re.search(rf">{re.escape(label)}</div>", xhtml_text, re.I)
        if not anchor:
            return None
        window = xhtml_text[anchor.end():anchor.end() + 1800]
        # Stop at the next left/justified-aligned cell (a text label, e.g.
        # class "clb"/"cln"/"cjn") so unrelated rows below don't bleed into
        # this row's value cells.
        next_label = re.search(r'<div class="c[lj][a-z]', window)
        if next_label:
            window = window[:next_label.start()]
        # The current-year column is sometimes rendered bold ("crb fn1")
        # while the comparative stays normal weight ("crn fn1"); both are
        # value cells in the same column order. The same "crb fn1" style is
        # also used for bare-year column headers (e.g. a notes sub-heading
        # like "Operating profit" followed by "2024"/"2023" headers before
        # the real line items) — those aren't amounts, so drop them.
        cells = re.findall(r'<div class="cr[nb] fn1"[^>]*>(.*?)</div>', window, re.S)
        cells = [c for c in cells if not re.fullmatch(r"(?:19|20|21)\d{2}", re.sub(r"<[^>]+>", "", c).strip())]
        numbers = [parse_display_number(cell) for cell in cells]
        if len(numbers) == 2:
            return {"current": numbers[0], "previous": numbers[1]}
        if len(numbers) >= 6 and len(numbers) % 3 == 0:
            # Continuing/discontinued/total columns repeated per year — the
            # total is the third column of each year's block, not the first.
            return {"current": numbers[2], "previous": numbers[5]}
        # Any other column count is a layout we can't confidently interpret
        # (e.g. a segmental note reusing the same row label) — decline
        # rather than reading the wrong cell as current/previous.
        return None

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
        context_dates: dict[str, str],
        context_dimensioned: dict[str, bool],
        financial_periods: dict[str, str | None],
    ) -> dict[str, dict[str, int | None]]:
        def tagged(metric_key: str, period_end: str | None) -> int | None:
            return self._resolve_year_metric(
                metrics, context_dates, context_dimensioned, METRIC_TAG_SYNONYMS[metric_key], period_end
            )

        views: dict[str, dict[str, int | None]] = {}
        for period_type in ("current", "previous"):
            period_end = financial_periods.get(period_type)
            visible = {
                "turnover": self._visible_value(visible_rows["turnover"], period_type),
                "gross_profit": self._visible_value(visible_rows["gross_profit"], period_type),
                "operating_result": self._visible_value(visible_rows["operating_result"], period_type),
            }
            # Legacy literal context refs, kept only for these vendor-specific
            # cash-flow lines that have no reliable cross-vendor tag name yet.
            legacy_ref = {"current": "C", "previous": "F"}[period_type]
            financing_ref = {"current": "C_BW_BX", "previous": "F_BW_BX"}[period_type]
            views[period_type] = {
                "turnover": visible["turnover"] if visible["turnover"] is not None else tagged("turnover", period_end),
                "cost_of_sales": tagged("cost_of_sales", period_end),
                "gross_profit": visible["gross_profit"] if visible["gross_profit"] is not None else tagged("gross_profit", period_end),
                "administrative_expenses": tagged("administrative_expenses", period_end),
                "operating_result": visible["operating_result"] if visible["operating_result"] is not None else tagged("operating_result", period_end),
                "profit_before_tax": tagged("profit_before_tax", period_end),
                "tax": tagged("tax", period_end),
                "profit_after_tax": tagged("profit_after_tax", period_end),
                "cash": tagged("cash", period_end),
                "current_assets": tagged("current_assets", period_end),
                "current_liabilities": tagged("current_liabilities", period_end),
                "net_current_assets": tagged("net_current_assets", period_end),
                "net_assets": tagged("net_assets", period_end),
                "debtors": tagged("debtors", period_end),
                "trade_debtors": tagged("trade_debtors", period_end),
                "cash_absorbed_by_operations": -metrics.get(("NetCashGeneratedFromOperations", legacy_ref), 0),
                "net_cash_from_financing": -metrics.get(
                    (
                        "FurtherItemCashFlowFromUsedInFinancingActivitiesComponentNetCashFlowsFromUsedInFinancingActivities",
                        financing_ref,
                    ),
                    0,
                ),
                "net_change_in_cash": (
                    -metrics.get(("IncreaseDecreaseInCashCashEquivalentsBeforeForeignExchangeDifferencesChangesInConsolidation", legacy_ref), 0)
                    if period_type == "current"
                    else metrics.get(("IncreaseDecreaseInCashCashEquivalentsBeforeForeignExchangeDifferencesChangesInConsolidation", legacy_ref))
                ),
                "employees": tagged("employees", period_end),
                "staff_costs": tagged("staff_costs", period_end),
                "uk_revenue": self._visible_value(visible_rows["uk_revenue"], period_type),
                "rest_of_europe_revenue": self._visible_value(visible_rows["rest_of_europe_revenue"], period_type),
            }
        return {"current": views["current"], "previous": views["previous"]}

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


# iXBRL elements holding machine-readable metadata rather than anything a
# reader sees: context definitions, unit declarations, taxonomy references,
# and the hidden-fact block. Stripping tags without removing these first
# leaves their text content behind, so a narrative section comes out as
# "principal activity ... 07554163 bus:Director2 2024-01-01 2024-12-31"
# instead of prose. This affected roughly 17% of principal_activity rows.
IXBRL_NON_VISIBLE_ELEMENTS = ("header", "hidden", "references", "resources")


def strip_ixbrl_non_visible_blocks(markup: str) -> str:
    for element in IXBRL_NON_VISIBLE_ELEMENTS:
        # Backreference the namespace prefix so <ix:header>...</ix:header>
        # matches whatever prefix the filing agent declared.
        markup = re.sub(
            rf"<([A-Za-z0-9_.-]+):{element}\b[^>]*>.*?</\1:{element}\s*>",
            " ",
            markup,
            flags=re.I | re.S,
        )
    return markup


def parse_xhtml_narrative(xhtml_text: str) -> dict[str, Any]:
    """Extract qualitative narrative sections and performance sentences from an iXBRL/XHTML document.

    Strips all HTML/XBRL markup then runs the same section-heading and
    performance-sentence extractors used on OCR'd PDFs.  The result is a dict
    compatible with companies_house_sqlite.insert_narrative_payload().
    """
    from core.companies_house_pdf_text import (
        extract_sections,
        extract_performance_statements,
        summarize_text_quality,
    )

    # Drop <head>, <style> and <script> blocks before stripping markup so that
    # CSS class names and JS strings don't pollute the extracted text.
    cleaned = re.sub(r"<head\b[^>]*>.*?</head>", " ", xhtml_text, flags=re.I | re.S)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", " ", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
    cleaned = strip_ixbrl_non_visible_blocks(cleaned)
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
