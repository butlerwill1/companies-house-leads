#!/usr/bin/env python3
"""Extract narrative sections from Companies House PDF filings.

This is separate from the API-first XHTML extractor because many Companies House
PDFs are scanned/image-first documents. For text PDFs, direct extraction is
usually enough. For scanned PDFs, this script can optionally OCR page images.
The default OCR path prefers RapidOCR if installed and falls back to Tesseract.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader


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


def extract_text_pages(pdf_path: Path, max_pages: int | None = None) -> list[str]:
    reader = PdfReader(str(pdf_path))
    pages: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        if max_pages is not None and page_number > max_pages:
            break
        pages.append(normalize_whitespace(page.extract_text() or ""))
    return pages


def has_useful_text(page_texts: list[str], threshold_chars: int = 400) -> bool:
    return sum(len(text) for text in page_texts) >= threshold_chars


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def rapidocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def extract_page_images(pdf_path: Path, image_dir: Path, max_pages: int | None = None) -> list[Path]:
    reader = PdfReader(str(pdf_path))
    image_paths: list[Path] = []
    image_dir.mkdir(parents=True, exist_ok=True)
    for page_number, page in enumerate(reader.pages, start=1):
        if max_pages is not None and page_number > max_pages:
            break
        images = list(page.images)
        if not images:
            continue
        image = images[0].image.convert("L")
        output_path = image_dir / f"page-{page_number:03d}.png"
        image.save(output_path)
        image_paths.append(output_path)
    return image_paths


def ocr_images_tesseract(image_paths: list[Path]) -> list[str]:
    pages: list[str] = []
    for image_path in image_paths:
        result = subprocess.run(
            ["tesseract", str(image_path), "stdout"],
            check=True,
            capture_output=True,
            text=True,
        )
        pages.append(normalize_whitespace(result.stdout))
    return pages


def ocr_images_rapidocr(image_paths: list[Path]) -> list[str]:
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    pages: list[str] = []
    for image_path in image_paths:
        result, _ = ocr(str(image_path))
        text = "\n".join(line[1] for line in result) if result else ""
        pages.append(normalize_whitespace(text))
    return pages


def choose_ocr_engine(preferred: str) -> str | None:
    if preferred == "rapidocr":
        return "rapidocr" if rapidocr_available() else None
    if preferred == "tesseract":
        return "tesseract" if tesseract_available() else None
    if preferred == "auto":
        if rapidocr_available():
            return "rapidocr"
        if tesseract_available():
            return "tesseract"
        return None
    return None


def ocr_images(image_paths: list[Path], engine: str) -> list[str]:
    if engine == "rapidocr":
        return ocr_images_rapidocr(image_paths)
    if engine == "tesseract":
        return ocr_images_tesseract(image_paths)
    raise ValueError(f"Unsupported OCR engine: {engine}")


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract narrative sections from a Companies House PDF.")
    parser.add_argument("--pdf", required=True, help="Path to the PDF filing.")
    parser.add_argument("--output-json", required=True, help="Path to save extracted narrative JSON.")
    parser.add_argument(
        "--ocr-if-needed",
        action="store_true",
        help="If direct PDF text extraction is poor, OCR page images using a free local OCR engine.",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=["auto", "rapidocr", "tesseract"],
        default="auto",
        help="OCR engine to use if OCR is needed.",
    )
    parser.add_argument("--max-pages", type=int, help="Optional limit on the number of pages to process.")
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf)
    direct_pages = extract_text_pages(pdf_path, max_pages=args.max_pages)
    source_mode = "pdf_text"
    final_pages = direct_pages
    ocr_used = False
    ocr_engine_used: str | None = None

    if args.ocr_if_needed and not has_useful_text(direct_pages):
        selected_engine = choose_ocr_engine(args.ocr_engine)
        if selected_engine:
            with tempfile.TemporaryDirectory(prefix="companies-house-pdf-") as tmp:
                image_paths = extract_page_images(pdf_path, Path(tmp), max_pages=args.max_pages)
                final_pages = ocr_images(image_paths, selected_engine)
                source_mode = "ocr"
                ocr_used = True
                ocr_engine_used = selected_engine
        else:
            source_mode = "pdf_text_insufficient_no_ocr"

    payload = {
        "pdf_path": str(pdf_path),
        "text_source": source_mode,
        "ocr_requested": args.ocr_if_needed,
        "ocr_engine_requested": args.ocr_engine,
        "ocr_used": ocr_used,
        "ocr_engine_used": ocr_engine_used,
        "max_pages": args.max_pages,
        "tesseract_available": tesseract_available(),
        "rapidocr_available": rapidocr_available(),
        "text_quality": summarize_text_quality(final_pages),
        "sections": extract_sections(final_pages),
        "performance_statements": extract_performance_statements(final_pages),
    }

    Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"output_json": args.output_json, "text_source": source_mode}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
