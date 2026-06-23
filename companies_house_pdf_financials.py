#!/usr/bin/env python3
"""Extract numeric financial statement data from Companies House PDF filings.

This is the fast, quantitative OCR path. It prioritizes identifying the pages
that contain the primary statements and only OCRs those pages, so it is much
cheaper than running narrative extraction across the whole document.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from companies_house_pdf_full import (
    choose_ocr_engine,
    detect_statement_type,
    extract_ocr_financials,
    normalize_whitespace,
    ocr_images,
    pymupdf_available,
    rapidocr_available,
    render_pdf_page_images,
    summarize_text_quality,
    tesseract_available,
)


STATEMENT_SEARCH_START_PAGE = 3
STATEMENT_SEARCH_END_PAGE = 18


def extract_direct_text_map(pdf_path: Path, max_pages: int | None = None) -> dict[int, str]:
    reader = PdfReader(str(pdf_path))
    page_map: dict[int, str] = {}
    for page_number, page in enumerate(reader.pages, start=1):
        if max_pages is not None and page_number > max_pages:
            break
        page_map[page_number] = normalize_whitespace(page.extract_text() or "")
    return page_map


def find_statement_pages(page_map: dict[int, str]) -> list[int]:
    pages = [page_number for page_number, text in page_map.items() if detect_statement_type(text)]
    return sorted(set(pages))


def expand_statement_pages(page_numbers: list[int], total_pages: int) -> list[int]:
    expanded: set[int] = set()
    for page_number in page_numbers:
        for candidate in (page_number - 1, page_number, page_number + 1):
            if 1 <= candidate <= total_pages:
                expanded.add(candidate)
    return sorted(expanded)


def staggered_search_pages(page_numbers: list[int]) -> list[int]:
    if len(page_numbers) <= 2:
        return page_numbers
    staggered = page_numbers[::2]
    if page_numbers[-1] not in staggered:
        staggered.append(page_numbers[-1])
    return sorted(set(staggered))


def parse_page_list(value: str) -> list[int]:
    page_numbers: set[int] = set()
    for part in value.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", maxsplit=1)
            start = int(start_text)
            end = int(end_text)
            low, high = sorted((start, end))
            page_numbers.update(range(low, high + 1))
        else:
            page_numbers.add(int(piece))
    return sorted(page_numbers)


def build_payload(
    pdf_path: Path,
    final_page_map: dict[int, str],
    source_mode: str,
    ocr_requested: bool,
    ocr_used: bool,
    ocr_engine_requested: str,
    ocr_engine_used: str | None,
    search_page_numbers: list[int],
    selected_page_numbers: list[int],
    render_dpi: int,
) -> dict[str, Any]:
    page_texts = [final_page_map.get(page_number, "") for page_number in selected_page_numbers]
    ocr_financials = extract_ocr_financials(page_texts)

    remapped_pages = []
    for metric in ocr_financials.get("metrics", []):
        local_page = metric.get("page")
        if isinstance(local_page, int) and 1 <= local_page <= len(selected_page_numbers):
            metric["page"] = selected_page_numbers[local_page - 1]
            remapped_pages.append(metric["page"])

    ocr_financials["pages_with_financials"] = sorted(set(remapped_pages))

    return {
        "pdf_path": str(pdf_path),
        "text_source": source_mode,
        "ocr_requested": ocr_requested,
        "ocr_engine_requested": ocr_engine_requested,
        "ocr_used": ocr_used,
        "ocr_engine_used": ocr_engine_used,
        "tesseract_available": tesseract_available(),
        "rapidocr_available": rapidocr_available(),
        "pymupdf_available": pymupdf_available(),
        "search_page_numbers": search_page_numbers,
        "selected_page_numbers": selected_page_numbers,
        "render_dpi": render_dpi,
        "text_quality": summarize_text_quality(page_texts),
        "sections": {},
        "performance_statements": [],
        "ocr_financials": ocr_financials,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract numeric financials from a Companies House PDF.")
    parser.add_argument("--pdf", required=True, help="Path to the PDF filing.")
    parser.add_argument("--output-json", required=True, help="Path to save extracted financial JSON.")
    parser.add_argument(
        "--ocr-if-needed",
        action="store_true",
        help="OCR statement pages if direct text is insufficient.",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=["auto", "rapidocr", "tesseract"],
        default="auto",
        help="OCR engine to use if OCR is needed.",
    )
    parser.add_argument("--max-pages", type=int, help="Optional absolute page cap.")
    parser.add_argument(
        "--page-list",
        help="Optional exact pages to process, for example '13-16' or '12,13,14,15'.",
    )
    parser.add_argument(
        "--search-start-page",
        type=int,
        default=STATEMENT_SEARCH_START_PAGE,
        help="First page to include in the statement-page OCR search window.",
    )
    parser.add_argument(
        "--search-end-page",
        type=int,
        default=STATEMENT_SEARCH_END_PAGE,
        help="Last page to include in the statement-page OCR search window.",
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        default=144,
        help="Rasterization DPI for OCR rendering.",
    )
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf)
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    max_page = min(args.max_pages, total_pages) if args.max_pages else total_pages
    exact_page_numbers = parse_page_list(args.page_list) if args.page_list else []
    exact_page_numbers = [page for page in exact_page_numbers if 1 <= page <= max_page]

    direct_page_map = extract_direct_text_map(pdf_path, max_pages=max_page)
    if exact_page_numbers:
        selected_page_numbers = exact_page_numbers
        selected_direct_page_map = {page: direct_page_map.get(page, "") for page in selected_page_numbers}
        if any(selected_direct_page_map.values()):
            payload = build_payload(
                pdf_path=pdf_path,
                final_page_map=selected_direct_page_map,
                source_mode="pdf_text_selected_pages",
                ocr_requested=args.ocr_if_needed,
                ocr_used=False,
                ocr_engine_requested=args.ocr_engine,
                ocr_engine_used=None,
                search_page_numbers=selected_page_numbers,
                selected_page_numbers=selected_page_numbers,
                render_dpi=args.render_dpi,
            )
            Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(json.dumps({"output_json": args.output_json, "text_source": payload["text_source"]}, indent=2))
            return 0

        if not args.ocr_if_needed:
            payload = build_payload(
                pdf_path=pdf_path,
                final_page_map={page: text for page, text in selected_direct_page_map.items() if text},
                source_mode="pdf_text_selected_pages_empty",
                ocr_requested=False,
                ocr_used=False,
                ocr_engine_requested=args.ocr_engine,
                ocr_engine_used=None,
                search_page_numbers=selected_page_numbers,
                selected_page_numbers=selected_page_numbers,
                render_dpi=args.render_dpi,
            )
            Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(json.dumps({"output_json": args.output_json, "text_source": payload["text_source"]}, indent=2))
            return 0

        selected_engine = choose_ocr_engine(args.ocr_engine)
        if not selected_engine:
            print("No OCR engine available.", file=sys.stderr)
            return 1
        rendered_pages = render_pdf_page_images(
            pdf_path,
            page_numbers=selected_page_numbers,
            dpi=args.render_dpi,
        )
        detail_texts = ocr_images([image for _, image in rendered_pages], selected_engine)
        final_page_map = {
            page_number: text for (page_number, _), text in zip(rendered_pages, detail_texts, strict=False)
        }
        payload = build_payload(
            pdf_path=pdf_path,
            final_page_map=final_page_map,
            source_mode="ocr_selected_pages",
            ocr_requested=True,
            ocr_used=True,
            ocr_engine_requested=args.ocr_engine,
            ocr_engine_used=selected_engine,
            search_page_numbers=selected_page_numbers,
            selected_page_numbers=selected_page_numbers,
            render_dpi=args.render_dpi,
        )
        Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({"output_json": args.output_json, "text_source": payload["text_source"]}, indent=2))
        return 0

    direct_statement_pages = find_statement_pages(direct_page_map)
    if direct_statement_pages:
        selected_page_numbers = expand_statement_pages(direct_statement_pages, max_page)
        final_page_map = {page: direct_page_map.get(page, "") for page in selected_page_numbers}
        payload = build_payload(
            pdf_path=pdf_path,
            final_page_map=final_page_map,
            source_mode="pdf_text_statement_pages",
            ocr_requested=args.ocr_if_needed,
            ocr_used=False,
            ocr_engine_requested=args.ocr_engine,
            ocr_engine_used=None,
            search_page_numbers=selected_page_numbers,
            selected_page_numbers=selected_page_numbers,
            render_dpi=args.render_dpi,
        )
        Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({"output_json": args.output_json, "text_source": payload["text_source"]}, indent=2))
        return 0

    if not args.ocr_if_needed:
        payload = build_payload(
            pdf_path=pdf_path,
            final_page_map={page: text for page, text in direct_page_map.items() if text},
            source_mode="pdf_text_no_statement_pages_found",
            ocr_requested=False,
            ocr_used=False,
            ocr_engine_requested=args.ocr_engine,
            ocr_engine_used=None,
            search_page_numbers=[],
            selected_page_numbers=sorted(page for page, text in direct_page_map.items() if text),
            render_dpi=args.render_dpi,
        )
        Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({"output_json": args.output_json, "text_source": payload["text_source"]}, indent=2))
        return 0

    selected_engine = choose_ocr_engine(args.ocr_engine)
    if not selected_engine:
        print("No OCR engine available.", file=sys.stderr)
        return 1

    search_start_page = max(1, args.search_start_page)
    search_end_page = min(args.search_end_page, max_page)
    search_page_numbers = list(range(search_start_page, search_end_page + 1))

    first_pass_pages = staggered_search_pages(search_page_numbers)
    rendered_first_pass = render_pdf_page_images(
        pdf_path,
        page_numbers=first_pass_pages,
        dpi=args.render_dpi,
    )
    search_texts = ocr_images([image for _, image in rendered_first_pass], selected_engine)
    search_page_map = {
        page_number: text for (page_number, _), text in zip(rendered_first_pass, search_texts, strict=False)
    }

    statement_pages = find_statement_pages(search_page_map)
    if not statement_pages:
        second_pass_pages = [page for page in search_page_numbers if page not in first_pass_pages]
        rendered_second_pass = render_pdf_page_images(
            pdf_path,
            page_numbers=second_pass_pages,
            dpi=args.render_dpi,
        )
        second_pass_texts = ocr_images([image for _, image in rendered_second_pass], selected_engine)
        search_page_map.update(
            {(page_number): text for (page_number, _), text in zip(rendered_second_pass, second_pass_texts, strict=False)}
        )
        statement_pages = find_statement_pages(search_page_map)
    if not statement_pages:
        statement_pages = search_page_numbers

    selected_page_numbers = expand_statement_pages(statement_pages, max_page)
    rendered_detail_pages = render_pdf_page_images(
        pdf_path,
        page_numbers=selected_page_numbers,
        dpi=args.render_dpi,
    )
    detail_texts = ocr_images([image for _, image in rendered_detail_pages], selected_engine)
    final_page_map = {
        page_number: text for (page_number, _), text in zip(rendered_detail_pages, detail_texts, strict=False)
    }

    payload = build_payload(
        pdf_path=pdf_path,
        final_page_map=final_page_map,
        source_mode="ocr_statement_pages",
        ocr_requested=True,
        ocr_used=True,
        ocr_engine_requested=args.ocr_engine,
        ocr_engine_used=selected_engine,
        search_page_numbers=search_page_numbers,
        selected_page_numbers=selected_page_numbers,
        render_dpi=args.render_dpi,
    )
    Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"output_json": args.output_json, "text_source": payload["text_source"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
