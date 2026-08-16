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
]

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


def extract_sections(page_texts: list[str]) -> dict[str, Any]:
    joined = "\n\n".join(f"[Page {page_no}]\n{text}" for page_no, text in build_page_map(page_texts).items() if text)
    matches: list[tuple[int, str, str]] = []
    for key, pattern in SECTION_PATTERNS:
        for match in pattern.finditer(joined):
            matches.append((match.start(), key, match.group(0)))
    matches.sort(key=lambda item: item[0])

    sections: dict[str, dict[str, Any]] = {}
    for index, (start_pos, key, heading_text) in enumerate(matches):
        end_pos = matches[index + 1][0] if index + 1 < len(matches) else len(joined)
        content = normalize_whitespace(joined[start_pos:end_pos])
        if key not in sections or len(content) > len(sections[key]["text"]):
            page_match = re.search(r"\[Page (\d+)\]", content)
            sections[key] = {
                "heading": heading_text,
                "page": int(page_match.group(1)) if page_match else None,
                "text": content,
            }
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
