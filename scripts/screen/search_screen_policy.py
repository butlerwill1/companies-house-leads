"""Prompt, input builders and response checks for the search screen.

v1 states only the definitions and policy rules recorded in docs/SEARCH_SCREEN.md
before the gold review, so the gold labels did not tune it. v2 was written after
reading v1's errors on the gold set (it let construction contractors through),
so its score on those 151 cases is optimistic; trust it on cases it has not seen
(the blind 25, the unlabelled reserve).

The screen answers one question about a filing: would a potential customer,
consumer or business, look for a business like this online and then buy, book
or enquire directly? (docs/SEARCH_SCREEN.md). This is the baseline output shape
(`answer`, `quote`, `reason`); the typed-evidence variant is planned separately.

Two inputs are compared (Variant A): a short extract and the whole filed report
minus the auditor's report. The quote must appear in whichever text the model
was shown; a quote that does not validate is flagged, never used to drop the
answer, and the screen fails open: an unparseable, empty or invalid response is
a pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.llm_validation import StrictResponseModel, JsonResponseError, parse_json_object, validate_object, validation_message
from scripts.business_profile_classifier.business_profile_policy import quote_match_kind
from scripts.screen.search_screen_cases import RAW_DIR, SCREEN_LABELS
from scripts.screen.search_screen_pack import _BOILERPLATE, principal_activity_text

PROMPT_VERSION_V1 = "search-screen-v1-single-quote"
PROMPT_VERSION = "search-screen-v3-balanced-evidence"
SHORT_STRATEGIC_CHARS = 1800
PASSING = frozenset({"likely", "possible"})


class _ScreenResponse(StrictResponseModel):
    answer: str
    quote: str | None = None
    reason: str | None = None

# v1 is kept verbatim: the 2026-09-30 baseline runs were made with it, and a
# stored response is only comparable to the text that produced it.
TEMPLATE_V1 = """You are screening a UK company for a lead list. Read the company's annual \
report text below and answer one question:

Would a potential customer, consumer or business, look for a business like this online and then \
buy, book or enquire directly?

Labels:
- likely: a trading business selling to customers who choose suppliers themselves: shops, e-commerce, \
hospitality, clinics, dentists, pharmacies, dealers, trades, local services, and business-to-business \
products or services that buyers search for (accountants, IT, parts suppliers).
- possible: trading, but the text does not show whether customers pick suppliers themselves. Use this \
when in doubt.
- unlikely: a holding or investment company with no named trade, an SPV, a financing or concession \
vehicle (PFI), captive or intragroup supply, tender- or framework-only public contracting, a handful \
of large contracted buyers, or investment property.

Rules:
- A group parent follows the group's trade: judge a holding company on what its subsidiaries do.
- An NHS-funded service the patient chooses (GP, dentist, pharmacy) is likely. Council-placed care, \
where the council chooses the provider, is possible.
- Do not judge how good a lead the company would be; only whether search-driven demand plausibly exists.

Company: {company_name}
Industry code: {sic_label}

Annual report text:
<<<
{text}
>>>

Return only JSON with these keys:
{{"answer": "likely" | "possible" | "unlikely",
  "quote": "one sentence copied word for word from the text above that best supports your answer",
  "reason": "one sentence"}}"""


# v2 (2026-09-30) changes the definition, not the output shape. v1 let almost
# every trading business through: `unlikely` said "tender- or framework-only",
# which a contractor is never, `likely` listed "trades" and "business-to-business
# services" without qualification, and "when in doubt" read as "when the report
# shows evidence against". v2 states the rule the gold labels were written with:
# evidence of how work is won decides, and "possible" is for no evidence either way.
TEMPLATE_V2 = """You are screening a UK company for a lead list. Read the company's annual \
report text below and answer one question:

Would a potential customer, consumer or business, look for a business like this online and then \
buy, book or enquire directly?

Labels:
- likely: a trading business selling to customers who choose suppliers themselves: shops, e-commerce, \
hospitality, clinics, dentists, pharmacies, dealers, local services, and business-to-business \
products or services that buyers search for (accountants, IT, parts suppliers).
- possible: trading, and the text gives no evidence either way about how customers choose or find \
the business.
- unlikely: a holding or investment company with no named trade, an SPV, a financing or concession \
vehicle (PFI), captive or intragroup supply, investment property, or a trading business whose report \
shows that its work comes through tenders, frameworks, contractor or client lists, long-term or \
repeat contracts with a few large clients, or a handful of contracted buyers rather than through \
customers choosing a supplier.

Rules:
- Evidence decides. If the report shows how the company wins work and that evidence fits the unlikely \
description, answer unlikely even though the business could in principle be found online. Use possible \
only when the report shows nothing either way, not when it points towards unlikely.
- Being a contractor, a consultancy or a business-to-business supplier does not by itself make a \
company unlikely or likely; what the report says about how it wins work does.
- A group parent follows the group's trade: judge a holding company on what its subsidiaries do.
- An NHS-funded service the patient chooses (GP, dentist, pharmacy) is likely. Council-placed care, \
where the council chooses the provider, is possible.
- Do not judge how good a lead the company would be; only whether search-driven demand plausibly exists.

Company: {company_name}
Industry code: {sic_label}

Annual report text:
<<<
{text}
>>>

Return only JSON with these keys:
{{"answer": "likely" | "possible" | "unlikely",
  "quote": "one sentence copied word for word from the text above that best supports your answer",
  "reason": "one sentence"}}"""

# v3 (2026-09-30) keeps v2's evidence rule but stops it over-rejecting. v2 doubled
# removal and then rejected real leads whenever a report mentioned public
# contracts, institutions or relationships, and ignored the NHS rule. v3 makes
# rejection require that the unlikely routes are how the business earns
# substantially all its revenue, lets any direct channel override that, states
# that the rules beat the label descriptions, and says a wrong reject costs more
# than a wrong pass. Written from categories of error, not company names.
TEMPLATE_V3 = """You are screening a UK company for a lead list. Read the company's annual \
report text below and answer one question:

Would a potential customer, consumer or business, look for a business like this online and then \
buy, book or enquire directly?

A wrong "unlikely" throws a real lead away for good; a wrong "possible" only costs a later check. \
So answer unlikely only when the report clearly supports it.

Labels:
- likely: a trading business selling to customers who choose suppliers themselves: shops, e-commerce, \
hospitality, clinics, dentists, pharmacies, dealers, local services, consumer services, online \
platforms and marketplaces that businesses or consumers sign up to, and business-to-business \
products or services that buyers search for (accountants, IT, agencies, parts suppliers).
- possible: trading, and the text gives no clear evidence either way about how customers choose or \
find the business, or the evidence is mixed.
- unlikely: a holding or investment company with no trading subsidiary, an SPV, a financing or \
concession vehicle (PFI), captive or intragroup supply, investment property, or a trading business \
that earns substantially all of its revenue through tenders, frameworks, contractor or client lists, \
long-term contracts with a few large clients, or a handful of contracted buyers, with no sign of a \
channel where customers find and choose it.

Rules (these override the label descriptions above):
- A direct channel settles it. If the report shows the company sells to the public or to businesses \
through a website, online booking or platform, marketing or customer acquisition, private or \
self-funded customers, walk-in or branch customers, or open competition for customers, the answer is \
likely or possible, even if part of its revenue also comes from public contracts or institutional buyers.
- Public-sector or institutional customers, long client relationships, a named sector (defence, \
energy, construction) or being a contractor or consultancy do not by themselves make a company \
unlikely. Reject only when the report shows the work comes almost entirely through tenders, \
frameworks, contractor lists or a few contracted buyers.
- A group parent follows the group's trade: judge a holding or property-owning company on what its \
trading subsidiaries do, even when its own turnover is rent or investment income.
- An NHS-funded service that the patient chooses (GP practice, dentist, pharmacy) is likely. \
Council-placed care, where the council chooses the provider, is possible.
- Do not judge how good a lead the company would be; only whether search-driven demand plausibly exists.

Company: {company_name}
Industry code: {sic_label}

Annual report text:
<<<
{text}
>>>

Return only JSON with these keys:
{{"answer": "likely" | "possible" | "unlikely",
  "quote": "one sentence copied word for word from the text above that best supports your answer",
  "reason": "one sentence"}}"""

PROMPT_VERSION_V2 = "search-screen-v2-contractor-evidence"
TEMPLATES = {PROMPT_VERSION_V1: TEMPLATE_V1, PROMPT_VERSION_V2: TEMPLATE_V2, PROMPT_VERSION: TEMPLATE_V3}
PROMPT_TEMPLATE = TEMPLATES[PROMPT_VERSION]


def build_prompt(*, company_name: str, sic_label: str | None, text: str, version: str | None = None) -> str:
    template = TEMPLATES[version or PROMPT_VERSION]
    return template.format(company_name=company_name, sic_label=sic_label or "(none)", text=text)


def short_extract(case: dict[str, Any], raw_dir: Path = RAW_DIR) -> str:
    """Principal activity plus the opening of the strategic report (or, when
    there is none, of the directors' report): the planned production input."""
    filed = case["sections"]["filed_report"]
    xhtml_path = Path(case.get("raw_dir") or raw_dir) / f"{case['company_number']}.xhtml"
    activity = principal_activity_text(xhtml_path.read_text(encoding="utf-8", errors="replace")) if xhtml_path.is_file() else ""
    lines = [line.strip() for line in filed.splitlines()]
    headings = [i for i, line in enumerate(lines) if re.fullmatch(r"strategic report", line, re.I)]
    headings += [i for i, line in enumerate(lines) if re.fullmatch(r"directors?'?s?'? report", line, re.I)]
    # The first heading followed by real prose: the contents page lists the
    # same headings with nothing under them.
    start = next((i for i in sorted(headings) if any(len(x) >= 60 for x in lines[i + 1:i + 6])), 0)
    opening = ""
    for line in lines[start + 1:]:
        if re.search(r"\(continued\)$", line, re.I):
            break  # the report has moved on to the next page of statements
        if len(line) < 25 or _BOILERPLATE.search(line):
            continue
        opening += line + chr(10)
        if len(opening) >= SHORT_STRATEGIC_CHARS:
            break
    opening = opening[:SHORT_STRATEGIC_CHARS]
    parts = []
    if activity:
        parts.append(f"[principal activity] {activity}")
    if opening.strip():
        parts.append(f"[report opening] {opening.strip()}")
    return "\n".join(parts)


def full_text(case: dict[str, Any]) -> str:
    return case["sections"]["filed_report"]


INPUT_BUILDERS = {"short": short_extract, "full": full_text}


def parse_answer(raw: str | None, text: str) -> dict[str, Any]:
    """Turn a raw model reply into ``{answer, quote, reason, quote_ok, passes,
    problem}``. Never raises. ``passes`` is the screen decision: anything that
    is not a clean ``unlikely`` passes (fail open)."""
    out: dict[str, Any] = {"answer": None, "quote": None, "reason": None, "quote_ok": None,
                           "problem": None}
    try:
        parsed = parse_json_object(raw)
    except JsonResponseError as exc:
        out["problem"] = f"unparseable: {exc}"
        out["passes"] = True
        out["validation"] = exc.validation
        return out
    response, validation = validate_object(parsed, _ScreenResponse)
    payload = parsed.payload
    answer_value = response.answer if response else payload.get("answer")
    answer = answer_value.strip().lower() if isinstance(answer_value, str) else ""
    if isinstance(answer_value, str) and answer != answer_value:
        validation["normalisations"].append({"path": "answer", "code": "normalised_label",
                                               "message": "trimmed and lower-cased label"})
    out["quote"] = response.quote if response else (payload.get("quote") if isinstance(payload.get("quote"), str) else None)
    out["reason"] = response.reason if response else (payload.get("reason") if isinstance(payload.get("reason"), str) else None)
    out["validation"] = validation
    if answer not in SCREEN_LABELS:
        detail = validation_message(validation) if validation["errors"] else f"answer {payload.get('answer')!r} is not one of {SCREEN_LABELS}"
        out["problem"] = detail
        out["passes"] = True
        return out
    out["answer"] = answer
    quote = out["quote"] if isinstance(out["quote"], str) else ""
    out["quote_ok"] = bool(quote.strip()) and quote_match_kind(quote, text) is not None
    out["passes"] = answer in PASSING
    if validation["errors"]:
        out["problem"] = validation_message(validation)
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
