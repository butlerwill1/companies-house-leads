#!/usr/bin/env python3
"""Shared narrative-section and performance-sentence extraction over plain text pages.

Used by companies_house_extractor.py to parse XHTML/iXBRL narrative sections.
"""

from __future__ import annotations

import re
from typing import Any

SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("strategic_report", re.compile(r"\bstrategic report\b", re.I)),
    ("directors_report", re.compile(r"\bdirectors?[’']?\s+report\b", re.I)),
    ("principal_activity", re.compile(r"\bprincipal activit(?:y|ies)\b", re.I)),
    ("business_review", re.compile(r"\bbusiness review\b", re.I)),
    ("results_and_dividends", re.compile(r"\bresults?\s+and\s+dividends?\b", re.I)),
    ("going_concern", re.compile(r"\bgoing concern\b", re.I)),
    ("future_developments", re.compile(r"\bfuture developments?\b", re.I)),
    ("principal_risks", re.compile(r"\bprincipal risks?(?: and uncertainties)?\b", re.I)),
    ("post_balance_sheet", re.compile(r"\bpost balance sheet events?\b", re.I)),
    # Financial notes, not qualitative narrative -- but decisive evidence for
    # geography_served and customer_type that the qualitative sections often
    # don't state at all (see docs/BUSINESS_PROFILE_EXTRACTION.md). Anchored
    # to the note's distinctive opening phrasing, not the word "turnover" or
    # "employees" alone, which recur constantly in unrelated KPI prose.
    ("turnover_note", re.compile(
        r"\bturnover analysed by\b|\bturnover and other revenue\b|\ban analysis of turnover\b",
        re.I,
    )),
    ("employee_note", re.compile(r"\baverage (?:monthly )?number of (?:persons|employees)\b", re.I)),
]

# A section runs to the next heading match, but the LAST match in a document
# has no following heading and would otherwise swallow everything to the end
# -- which is how going_concern ended up with a median of 2,633 words, since
# the phrase recurs late in accounting policies. Cap it.
MAX_SECTION_CHARS = 6000

# The independent auditor's report quotes the same headings the company
# uses ("principal risks", "going concern"), so a naive match can capture
# the auditor's boilerplate instead of what the company said about itself.
# Candidates containing this are only used when nothing cleaner exists.
#
# Deliberately excludes the bare phrase "independent auditor's report":
# every filing on hand lists it as a plain line in the table of contents,
# right alongside "Strategic report" and "Directors' report" -- so it
# appears near the very start of the document regardless of whether the
# candidate being classified is real company narrative or the auditor's
# actual text. Confirmed on 55 of 58 filed reports on hand: that bare
# mention sits well before the first genuinely audit-specific phrase, and
# _is_inside_auditor_report's lookback check took it as evidence the real
# strategic report's own opening heading was still "inside" the auditor's
# report -- fragmenting content candidates near the front of a filing.
# Every phrase kept here is specific enough that it does not double as
# ordinary contents-page or heading text.
AUDITOR_BOILERPLATE_PATTERN = re.compile(
    r"\b(we have audited|in our opinion|our audit|audit procedures|engagement team|"
    r"ISAs?\s*\(UK\)|auditor'?s?\s+responsibilit|reasonable assurance|"
    r"material misstatement|we considered the opportunities)\b",
    re.I,
)

# The giveaway is often before the heading, not inside the section: the
# auditor's report opens with its own title and "we have audited...", then
# refers to principal risks further down. Look back this far to notice we
# are inside it.
AUDITOR_LOOKBACK_CHARS = 1500

# ...but a company-authored report heading appearing after that boilerplate
# means the auditor's report has ended and we are back in the company's own
# words, so the lookback must not fire.
COMPANY_REPORT_HEADING_PATTERN = re.compile(
    r"\b(strategic report|directors?[’']?\s+report|business review|"
    r"chairman'?s?\s+statement|chief executive'?s?\s+(report|statement))\b",
    re.I,
)


def _is_inside_auditor_report(content: str, preceding: str) -> bool:
    if AUDITOR_BOILERPLATE_PATTERN.search(content):
        return True
    auditor_hits = list(AUDITOR_BOILERPLATE_PATTERN.finditer(preceding))
    if not auditor_hits:
        return False
    company_hits = list(COMPANY_REPORT_HEADING_PATTERN.finditer(preceding))
    if company_hits and company_hits[-1].start() > auditor_hits[-1].start():
        return False
    return True

PERFORMANCE_SENTENCE_PATTERN = re.compile(
    r"(?P<sentence>[^.]*\b("
    r"revenue|turnover|growth|profit|loss|margin|demand|pipeline|cash|liquidity|funding|"
    r"performance|headcount|client|customer|market|backlog|outlook"
    r")\b[^.]*\.)",
    re.I,
)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def split_sentences(text: str) -> list[str]:
    return [normalize_whitespace(part) for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def build_page_map(page_texts: list[str]) -> dict[int, str]:
    return {index + 1: text for index, text in enumerate(page_texts)}


def _drop_self_referential_repeats(
    matches: list[tuple[int, str, str]]
) -> list[tuple[int, str, str]]:
    """Drop a match that shares its key with the match immediately before it
    in the globally sorted (all-keys) list.

    A section runs to the next heading match, of any kind -- but several
    heading phrases recur inside their own section's body prose. "principal
    activity" is the clearest example: it appears once as the heading, then
    again in the boilerplate sentence pair filings use to separate group
    activity from parent-company activity ("The principal activity of the
    group... The principal activity of the company was that of a holding
    company") -- exactly the sentence trading_status_confirmed exists to
    read. "strategic report", "directors' report" and "going concern" show
    the same pattern for their own reasons (a cross-reference to "the
    Strategic Report and Directors' Report Regulations", a running page
    header, an accounting-policy note discussing the going concern basis).
    Left alone, the second occurrence is mistaken for a new section
    boundary, fragmenting the real one -- confirmed on real filings for
    principal_activity, where the fragment that survives the longest-wins
    tie-break below can omit the single sentence a downstream field most
    needs (see docs/BUSINESS_PROFILE_CLASSIFIER_IMPROVEMENT_PLAN.md, Risks).
    A corpus check found 752 such adjacent same-key pairs across 57 of 58
    filed reports on hand -- this is routine, not an edge case.

    Adjacency in this merged, all-keys list -- not character distance -- is
    the right test. A bare heading (a contents-page entry, say) that is
    genuinely followed later by its own real section, with some other
    section's heading appearing in between, must still produce two
    separate candidates so the longest-wins tie-break below can discard the
    bare one (test_a_bare_heading_does_not_beat_a_real_section). Dropping
    by proximity alone would wrongly fuse that bare heading, and whatever
    unrelated heading sits between it and its real content, into one
    candidate; requiring no other heading in between leaves that case
    untouched while still catching true self-reference, which by
    definition has nothing else between the two mentions.
    """
    kept: list[tuple[int, str, str]] = []
    for start_pos, key, heading_text in matches:
        if kept and kept[-1][1] == key:
            continue
        kept.append((start_pos, key, heading_text))
    return kept


# Several SECTION_PATTERNS anchor to a phrase that is naturally the object of
# a leading "The" in the source sentence -- "The principal activity of the
# company...", "The average monthly number of persons...", "...present the
# strategic report for the year..." -- but the pattern itself starts
# matching at the noun phrase, not the article, so the extracted text began
# mid-sentence, missing that first word. Harmless as long as nothing checks
# the text against anything else -- but it isn't harmless: a model quoting
# the real sentence from the source document (as it is required to) quotes
# "The average monthly number...", which then fails verbatim-match
# validation against a stored section that starts "average monthly
# number...", rejecting a correct, non-hallucinated extraction outright.
# Confirmed live: 6 of 19 smoke-test rejections on 2026-09-02 were exactly
# this, not a bad quote. Extending the match to include an immediately
# preceding "The "/"the " (nothing but the article and its own whitespace in
# between) restores the sentence a model would actually quote.
_LEADING_ARTICLE_RE = re.compile(r"the\s+$", re.I)


def _extend_match_over_leading_article(text: str, start: int) -> int:
    article = _LEADING_ARTICLE_RE.search(text, 0, start)
    return article.start() if article else start


def extract_sections(page_texts: list[str]) -> dict[str, Any]:
    joined = "\n\n".join(f"[Page {page_no}]\n{text}" for page_no, text in build_page_map(page_texts).items() if text)
    matches: list[tuple[int, str, str]] = []
    for key, pattern in SECTION_PATTERNS:
        for match in pattern.finditer(joined):
            start = _extend_match_over_leading_article(joined, match.start())
            matches.append((start, key, match.group(0)))
    matches.sort(key=lambda item: item[0])
    matches = _drop_self_referential_repeats(matches)

    candidates: dict[str, list[dict[str, Any]]] = {}
    for index, (start_pos, key, heading_text) in enumerate(matches):
        end_pos = matches[index + 1][0] if index + 1 < len(matches) else len(joined)
        end_pos = min(end_pos, start_pos + MAX_SECTION_CHARS)
        content = normalize_whitespace(joined[start_pos:end_pos])
        preceding = joined[max(0, start_pos - AUDITOR_LOOKBACK_CHARS):start_pos]
        page_match = re.search(r"\[Page (\d+)\]", content)
        candidates.setdefault(key, []).append(
            {
                "heading": heading_text,
                "page": int(page_match.group(1)) if page_match else None,
                "text": content,
                "is_auditor_text": _is_inside_auditor_report(content, preceding),
            }
        )

    sections: dict[str, dict[str, Any]] = {}
    for key, items in candidates.items():
        # Prefer the company's own words; fall back to auditor-quoted text
        # only when that is all the document offers. Within the preferred
        # pool the longest candidate wins, since a bare heading in a
        # contents list carries no content.
        pool = [item for item in items if not item["is_auditor_text"]] or items
        best = max(pool, key=lambda item: len(item["text"]))
        # Retained rather than dropped: some filings only ever mention a
        # heading inside the auditor's report, so the fallback fires and the
        # text is not the company describing itself. Downstream consumers
        # need to be able to tell the difference.
        sections[key] = dict(best)
    return sections


def extract_performance_statements(page_texts: list[str]) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for page_number, page_text in build_page_map(page_texts).items():
        for sentence in split_sentences(page_text):
            if PERFORMANCE_SENTENCE_PATTERN.search(sentence):
                statements.append({"page": page_number, "text": sentence})
    return statements


def summarize_text_quality(page_texts: list[str]) -> dict[str, Any]:
    non_empty_pages = sum(1 for text in page_texts if text)
    total_chars = sum(len(text) for text in page_texts)
    return {
        "pages": len(page_texts),
        "non_empty_pages": non_empty_pages,
        "total_characters": total_chars,
        "average_characters_per_non_empty_page": round(total_chars / non_empty_pages, 2) if non_empty_pages else 0,
    }
