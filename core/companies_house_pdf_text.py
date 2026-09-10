#!/usr/bin/env python3
"""Shared narrative-section and performance-sentence extraction over plain text pages.

Used by companies_house_extractor.py to parse XHTML/iXBRL narrative sections.
"""

from __future__ import annotations

import re
from bisect import bisect_right
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

# Patterns that only count when they are the whole of a heading line.
#
# These phrases are far too common to match as free text -- "turnover" appears
# in most KPI sentences in a filing -- which is why turnover_note above is
# anchored to three distinctive opening phrasings instead. That was the right
# call while the extractor could only see a flat string; it is needlessly
# narrow now that block structure survives. A standalone "Turnover" heading
# appears in all 108 cached filings against roughly 60 for those three
# phrasings, and the turnover note carries the UK/overseas split that
# geography_served turns on and the class-of-business split that often settles
# customer_type. "Review of the business" is the same gap in the qualitative
# narrative: 64 of 108 filings head a section that way and "business review"
# does not match it.
#
# Adding these was tried once BEFORE occurrences were stitched, and it lost
# text badly (quotes surviving nowhere rose 51 -> 95): each new anchor created
# several candidates and all but the longest were discarded. They are only
# safe now that _stitch_occurrences keeps every occurrence of a section.
# MEASURED AND REJECTED, three ways -- left here because the coverage numbers
# make it look obviously worth doing, and it is not:
#
#     ("turnover_note",   r"^turnover(\s+and\s+other\s+revenue)?\s*$")
#     ("business_review", r"^review of (the )?(business|operations)\b")
#
# Coverage rises exactly as hoped -- turnover_note 60 -> 106 of 108 cases,
# business_review 44 -> 70 -- but gold-label quotes surviving nowhere in the
# extracted text rise with it, every way it was wired:
#
#   as ordinary patterns (anchor + boundary)          40 -> 89 quotes lost
#   as anchors only, never bounding another section   40 -> 78
#   as above, and not filtered out by heading pref.   40 -> 70 (and re-admits
#                                                     the accounting-policy
#                                                     boilerplate this file
#                                                     works to keep out)
#
# The cause is not the patterns. Only a dozen named windows are kept and each
# is capped, so every section added competes for the same budget and pushes
# other evidence out of the extract entirely. Widening the aperture is the
# right instinct, but it cannot be done section by section -- it needs the
# whole-document context path, which the 2026-08-20 A/B already showed
# favours the stronger model.
HEADING_ONLY_SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = []

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


# A heading is short: it names a section, it does not say anything about one.
# Generous enough for "Principal risks and uncertainties" plus a stray page
# marker, tight enough to exclude any real sentence.
MAX_HEADING_LINE_CHARS = 80


def _heading_line_positions(text: str) -> list[int]:
    """Start offsets of every line that reads as a heading, whatever it is
    called -- "Directors", "Auditor", "Employees", "Communities" included.

    This is what separates anchoring from bounding. Section STARTS are named
    by SECTION_PATTERNS, because we only care about a dozen sections; section
    ENDS come from here, because a section ends wherever the document starts a
    new heading, not merely where it starts one of the dozen we can name.
    While ends came from the naming list too, adding a name silently moved
    boundaries in unrelated sections -- the side effect that made "Turnover"
    unusable as a pattern.

    The awkward part is that flattening puts every table cell on its own line,
    so a financial table is hundreds of short lines and a naive "short line =
    heading" rule shatters the notes into fragments. Two conditions rule that
    out: a heading is followed by longer body text (in a table, short lines
    follow short lines), and it is not mostly digits and currency.
    """
    positions: list[int] = []
    offset = 0
    lines = text.split("\n")
    for index, raw in enumerate(lines):
        line = raw.strip()
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if _is_heading_line(line, following):
            positions.append(offset + (len(raw) - len(raw.lstrip())))
        offset += len(raw) + 1
    return positions


_MOSTLY_FIGURES_RE = re.compile(r"^[\d\s.,()%£$€:/-]*$")


def _is_heading_line(line: str, following: str) -> bool:
    if not line or len(line) > MAX_HEADING_LINE_CHARS:
        return False
    if line.endswith((".", ";", ",")) or len(line.split()) > 9:
        return False
    if not re.search(r"[A-Za-z]", line) or _MOSTLY_FIGURES_RE.match(line):
        return False
    # A heading introduces body text. Inside a table every short line is
    # followed by another short line, which is how table cells are excluded
    # without needing to know the markup they came from.
    return len(following) > len(line)


def _starts_a_heading_line(text: str, start: int) -> bool:
    """Whether the match at ``start`` begins a line that is only a heading.

    Filings render a heading in its own block element, so after
    strip_tags_preserving_blocks() it occupies a line of its own. Deliberately
    structural rather than vendor-specific: every accounts package puts a
    heading in its own block because that is what makes it render on its own
    line, whereas the CSS class names that mark it (CCH uses "clb") differ per
    package. Callers passing plain text with no line structure -- OCR pages,
    most unit tests -- simply get False for every candidate, which restores
    the previous longest-wins behaviour rather than breaking it.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line = text[line_start:line_end if line_end != -1 else len(text)].strip()
    if not line or len(line) > MAX_HEADING_LINE_CHARS:
        return False
    # Nothing but whitespace before the phrase on a short line: the line is
    # the heading, not a sentence that happens to contain the phrase.
    return text[line_start:start].strip() == ""


def _stitch_occurrences(pool: list[dict[str, Any]], best: dict[str, Any]) -> str:
    """Join every occurrence of one section, longest first, skipping any whose
    text is already covered by what has been kept. Capped at MAX_SECTION_CHARS
    so several occurrences of a heading cannot rebuild the runaway capture the
    cap exists to prevent."""
    ordered = sorted(pool, key=lambda item: len(item["text"]), reverse=True)
    kept: list[str] = []
    total = 0
    for item in ordered:
        text = item["text"].strip()
        if not text or any(text in already for already in kept):
            continue
        if total + len(text) > MAX_SECTION_CHARS:
            continue
        kept.append(text)
        total += len(text)
    if not kept:
        return best["text"]
    # Restore document order for readability -- the model reads this top down.
    by_position = {item["text"].strip(): index for index, item in enumerate(pool)}
    kept.sort(key=lambda text: by_position.get(text, 0))
    return " ".join(kept)


def extract_sections(page_texts: list[str]) -> dict[str, Any]:
    joined = "\n\n".join(f"[Page {page_no}]\n{text}" for page_no, text in build_page_map(page_texts).items() if text)
    matches: list[tuple[int, str, str]] = []
    for key, pattern in SECTION_PATTERNS:
        for match in pattern.finditer(joined):
            start = _extend_match_over_leading_article(joined, match.start())
            matches.append((start, key, match.group(0)))
    # Heading-only patterns are matched a line at a time, so a word as common
    # as "turnover" can anchor a section without matching every mention of it
    # in surrounding prose.
    matches.sort(key=lambda item: item[0])
    matches = _drop_self_referential_repeats(matches)

    # Heading-only anchors: matched a line at a time, so a word as common as
    # "turnover" can start a section without matching every mention of it in
    # prose. Held apart from `matches` on purpose -- these START a section but
    # never END anyone else's. That is the whole separation of anchoring from
    # bounding: adding one of these used to truncate whatever section it fell
    # inside, so coverage went up while evidence went down (quotes surviving
    # nowhere: 40 -> 89). Sections may now overlap, which is fine -- this has
    # never been a partition of the document, only a set of places to read.
    anchor_only: list[tuple[int, str, str]] = []
    offset = 0
    for line in joined.split("\n"):
        stripped = line.strip()
        for key, pattern in HEADING_ONLY_SECTION_PATTERNS:
            if pattern.search(stripped):
                anchor_only.append((offset + (len(line) - len(line.lstrip())), key, stripped))
        offset += len(line) + 1

    # Where the document has real headings, a section ends at the next
    # heading -- not at the next phrase match anywhere, which let a passing
    # mid-sentence mention truncate the section containing it (cutting
    # SC712711's turnover note off before "wholly undertaken in the United
    # Kingdom", the evidence geography_served rests on).
    #
    # But heading boundaries alone are not enough: where a filing has no
    # further heading-pattern match before its auditor's report, the section
    # runs to the 6,000-char cap and swallows the auditor's text, which then
    # fails the auditor check and is discarded WHOLESALE -- 06597073's
    # principal activity went from 2,733 usable characters to nothing at all.
    # Over-running is more destructive than truncating, because the penalty
    # is losing the section entirely rather than shortening it. So the start
    # of the auditor's own text is a boundary too.
    #
    # Guarded on the document actually having headings, so plain text with no
    # line structure (OCR pages, unit tests) keeps the previous behaviour.
    # Boundaries come from NAMED headings, not from every heading in the
    # document. Ending at every heading was tried and measured: it is much
    # worse (gold-label quotes surviving nowhere rose 51 -> 189), because the
    # text under headings we do not name -- "Directors", "Employees",
    # "Communities", the s172 sub-headings -- is then dropped instead of being
    # absorbed into the named section it follows. Only a dozen sections are
    # kept, so absorbing unnamed prose into its neighbour is a feature, not
    # the accident it looked like.
    #
    # The auditor's own text is a boundary too: a section that runs into it is
    # flagged as auditor text and discarded WHOLESALE, so over-running there
    # costs the entire section (06597073 lost 2,733 usable characters to this).
    heading_matches = [m[0] for m in matches if _starts_a_heading_line(joined, m[0])]
    if heading_matches:
        boundaries = sorted(
            set(heading_matches)
            | {m.start() for m in AUDITOR_BOILERPLATE_PATTERN.finditer(joined)}
        )
    else:
        boundaries = [m[0] for m in matches]

    candidates: dict[str, list[dict[str, Any]]] = {}
    for start_pos, key, heading_text in sorted(matches + anchor_only):
        following = bisect_right(boundaries, start_pos)
        end_pos = boundaries[following] if following < len(boundaries) else len(joined)
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
                "is_own_line_heading": _starts_a_heading_line(joined, start_pos),
            }
        )

    sections: dict[str, dict[str, Any]] = {}
    for key, items in candidates.items():
        # Prefer the company's own words; fall back to auditor-quoted text
        # only when that is all the document offers. Within the preferred
        # pool the longest candidate wins, since a bare heading in a
        # contents list carries no content.
        pool = [item for item in items if not item["is_auditor_text"]] or items
        # Then prefer a match that is a real heading -- alone on its line,
        # because the filing rendered it as its own block. Longest-wins alone
        # let a mid-sentence mention of the phrase beat the actual section:
        # the boilerplate "...principal activities ... are disclosed in the
        # Director's Report" sentence sits in the accounting-policy notes and
        # runs on into pages of policy text, so it outscored a real
        # Principal-activities heading whose section is one sentence long.
        # A preference, not a filter: plenty of filings state "The principal
        # activity of the company was..." with no heading above it at all,
        # and that sentence must still be found.
        pool = [item for item in pool if item["is_own_line_heading"]] or pool
        best = max(pool, key=lambda item: len(item["text"]))
        # Keep every occurrence, not just the longest. A filing states a
        # section's content in more than one place -- a turnover note appears
        # as a heading a median of 3 times per filing, and a company's
        # activity is often described once in the directors' report and again
        # in the strategic report. Keeping only the longest silently discarded
        # the rest, which is what made adding new section patterns lose text
        # instead of gaining it: a new pattern created several candidates and
        # all but one were thrown away. Stitched in document order, deduped,
        # and capped so a repeated heading cannot reintroduce a runaway.
        best = dict(best)
        best["text"] = _stitch_occurrences(pool, best)
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
