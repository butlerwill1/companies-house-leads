"""Whole-document transcription of scanned Companies House filings.

Some filings exist only as image PDFs: no XHTML resource, no text layer
(08029548 SMART CURRENCY GROUP files nothing else; ~600 more sit in
data/raw/pdf-only-accounts-vlm-gold-set/). The business-profile stage reads a company's whole filed
document minus the auditor's report, and for those filings there is no text
to read. This harness renders each page and has a vision model transcribe it
verbatim, then applies the same auditor-stripping rule the XHTML path uses
(`companies_house_core.companies_house_extractor.strip_auditor_report`), so the result is
interchangeable with `filed_report_text()` over a filed XHTML.

No local OCR runs here (AGENTS.md): every page is a vision-model call. One
call per page, on purpose -- output tokens are most of the cost, so batching
pages saves almost nothing and would hand page boundaries to the model.

Outputs, per document:

- data/raw/business-profile-filed-reports/<company>.transcript.md -- every page,
  human-readable (AGENTS.md's readability rule), `## Page N` per page.
- data/raw/business-profile-filed-reports/<company>.filed_report.txt -- the
  auditor-stripped text, what `business_profile_refresh_sections
  --whole-document` reads when there is no .xhtml.
- data/raw/business-profile-filed-reports/<company>.transcription.json -- identity,
  model, per-page status and usage, cost. No text duplicated.
- a `document_texts` row in the SQLite database (source
  'vlm_transcription'), one per (document, model).
- logs/vlm-transcription/<model>/checkpoint.jsonl -- one line per finished
  page, fsync'd, so a killed run resumes where it stopped (langfuse-eval-
  discipline rule 3). Delete it for a clean re-run.
- one Langfuse trace per document with a generation observation per page.

`--compare-model` transcribes the document a second time with another model
and writes <company>.transcript-diff.md: pages where the two disagree are
where a transcription error (or a hallucinated line) would show up. Two
readers agreeing on a page is cheap evidence the text is what is printed.

    python -m scripts.pdf_vision_extraction.companies_house_pdf_transcribe \\
        --pdf data/raw/business-profile-scanned-pdfs/08029548-2026-06-10.pdf \\
        --config evals/vlm_transcription_configs/configs/gemini-3-flash-preview.yaml \\
        --compare-model google/gemini-3.7-flash
    python -m scripts.pdf_vision_extraction.companies_house_pdf_transcribe --pdf-dir data/raw/pdf-only-accounts-vlm-gold-set \\
        --config ... --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companies_house_core.companies_house_extractor import load_dotenv, strip_auditor_report  # noqa: E402
from companies_house_core.companies_house_sqlite import init_db, upsert_document_text  # noqa: E402
from scripts.langfuse_eval_helpers.langfuse_tracing import (  # noqa: E402
    case_trace,
    flush,
    langfuse_from_config,
    observation,
    pdf_media,
)
from scripts.business_profile_classifier.save_raw_filings import readable_markdown_from_lines  # noqa: E402
from scripts.pdf_vision_extraction.companies_house_pdf_vlm_financials import (  # noqa: E402
    ModelCallResult,
    OpenRouterVlmModelClient,
    RenderedPage,
    fetch_pricing,
    render_pages,
    usage_cost_usd,
)

PROMPT_VERSION = "transcribe-v1"
DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_RENDER_LONG_EDGE = 1440
ILLEGIBLE_TOKEN = "ILLEGIBLE_PAGE"
PAGE_MARKER = "--- page {page} ---"
PAGE_MARKER_RE = re.compile(r"^--- page \d+ ---$")
DEFAULT_OUT_DIR = Path("data/raw/business-profile-filed-reports")
CHECKPOINT_ROOT = Path("logs/vlm-transcription")
SIMILARITY_THRESHOLD = 0.97
SOURCE = "vlm_transcription"
RETRY_BACKOFF_SECONDS = 2.0

TRANSCRIPTION_PROMPT = """Transcribe this scanned page of a UK Companies House filing verbatim, as plain text.
Rules:
- Keep every line of text on the page, in reading order, including page furniture: running headers and footers, page numbers, "continued" markers, company name and registered number lines.
- Put each heading on its own line.
- Transcribe a table as one row per line, with the cells separated by " | " (space, pipe, space). No leading or trailing pipes, no header separator row.
- Keep numbers, brackets, dashes, currency symbols and column headings exactly as printed. Do not convert, total, reorder or summarise.
- Plain text only: no Markdown (#, *, _, backticks, fenced blocks), no HTML.
- No commentary, no description of logos or images, no notes about legibility.
- If the whole page is unreadable, reply with exactly: ILLEGIBLE_PAGE
- If only part of a page is unreadable, transcribe the rest and write [illegible] in place of each unreadable run.
Reply with the transcription only."""

# Heading words that, if they sit on a page the auditor strip removed
# entirely, mean the strip ran past the report -- most likely a contents
# page transcribed onto one line (see auditor_strip_stats).
_COMPANY_REPORT_HEADINGS_RE = re.compile(r"strategic report|directors'? report", re.I)
_NUMERIC_TOKEN_RE = re.compile(r"\(?[£$€]?\d[\d,]*(?:\.\d+)?\)?")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")


# ---------------------------------------------------------------------------
# configuration and client
# ---------------------------------------------------------------------------

def configuration_from_file(path: Path) -> dict[str, Any]:
    """Load a transcription config. Same secret-key rule as the financial
    eval config: keys are for .env, never for a file that gets committed."""
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("transcription configuration must be a mapping")
    forbidden = {"api_key", "token", "secret", "password"}

    def check(value: Any, location: str = "") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in forbidden:
                    raise ValueError(f"secret key '{location}{key}' is not allowed in transcription config")
                check(nested, f"{location}{key}.")
        elif isinstance(value, list):
            for nested in value:
                check(nested, location)

    check(config)
    if config.get("provider") != "openrouter":
        raise ValueError("provider must be openrouter")
    if not isinstance(config.get("model"), str) or not config["model"]:
        raise ValueError("model is required")
    config.setdefault("timeout_seconds", 180)
    config.setdefault("render_long_edge", DEFAULT_RENDER_LONG_EDGE)
    config.setdefault("pages_per_call", 1)
    config.setdefault("max_pages", None)
    config.setdefault("retry_attempts", 1)
    config.setdefault("gbp_per_usd", None)
    if config["pages_per_call"] != 1:
        # Output tokens are ~85% of the cost, so batching saves only the
        # per-call prompt; it would also hand page boundaries to the model
        # and make one truncated reply lose every page in the batch.
        raise ValueError("pages_per_call must be 1 (one vision call per page)")
    return config


class TranscriptionClient(Protocol):
    provider_name: str

    def generate_text(
        self, model: str, prompt: str, pages: list[RenderedPage], timeout: int
    ) -> ModelCallResult: ...

    def pricing_snapshot(self) -> dict[str, dict[str, str]]: ...


def build_client(config: dict[str, Any]) -> TranscriptionClient:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set in .env or environment")
    return OpenRouterVlmModelClient(api_key, request_options=config.get("openrouter_request_options"))


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", model).strip("-")


# ---------------------------------------------------------------------------
# document identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DocumentIdentity:
    company_number: str
    document_id: str
    pdf_path: Path
    pdf_sha256: str
    filing_date: str | None = None
    company_name: str | None = None


def pdf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CH_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def document_identity(
    pdf_path: Path,
    *,
    company_number: str | None = None,
    document_id: str | None = None,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> DocumentIdentity:
    """Who this PDF belongs to. Filenames follow save_raw_filings
    (<company>-<filing date>.pdf) or the VLM sample folder
    (<company>-<document id>.pdf); <company>.metadata.json beside the
    output, when present, is authoritative for the document id. The id is
    always deterministic -- the content hash as a last resort -- because it
    is the document_texts key."""
    stem = pdf_path.stem
    prefix, _, suffix = stem.partition("-")
    company = company_number or prefix
    if not company:
        raise ValueError(f"cannot tell the company number from {pdf_path.name}; pass --company-number")

    sidecar: dict[str, Any] = {}
    sidecar_path = out_dir / f"{company}.metadata.json"
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            sidecar = {}

    digest = pdf_sha256(pdf_path)
    filing_date = suffix if _DATE_RE.match(suffix or "") else sidecar.get("filing_date")
    resolved = (
        document_id
        or sidecar.get("document_id")
        or (suffix if suffix and _CH_DOCUMENT_ID_RE.match(suffix) and not _DATE_RE.match(suffix) else None)
        or f"sha256:{digest[:16]}"
    )
    return DocumentIdentity(
        company_number=company,
        document_id=resolved,
        pdf_path=pdf_path,
        pdf_sha256=digest,
        filing_date=filing_date,
        company_name=sidecar.get("company_name"),
    )


def discover_pdfs(pdf_dir: Path, *, limit: int | None = None) -> list[Path]:
    found = sorted(pdf_dir.glob("*.pdf"))
    return found[:limit] if limit else found


def text_layer_chars(pdf_path: Path) -> int:
    """How much text the PDF already carries. A scanned filing has none; a
    born-digital one could skip the vision model entirely (not done here --
    recorded so the batch decision can route on it)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - PyMuPDF is an eval dependency
        return 0
    document = fitz.open(str(pdf_path))
    try:
        return sum(len(page.get_text().strip()) for page in document)
    finally:
        document.close()


def page_count(pdf_path: Path) -> int:
    import fitz  # PyMuPDF

    document = fitz.open(str(pdf_path))
    try:
        return document.page_count
    finally:
        document.close()


# ---------------------------------------------------------------------------
# one page
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageTranscript:
    page: int
    status: str  # "ok" | "illegible" | "error"
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    error: str | None = None
    replayed: bool = False


_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+")
_BOLD_WRAP_RE = re.compile(r"^\*{1,2}(.+?)\*{1,2}$")
_PIPE_EDGES_RE = re.compile(r"^\|\s*|\s*\|$")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_SEPARATOR_ROW_RE = re.compile(r"^[\s|:-]+$")


def normalise_page_text(raw: str) -> tuple[str, str]:
    """(status, text). Removes only structural habits the prompt forbids
    but models still produce -- a wrapping code fence, `#` headings, bold
    wrappers, table edge pipes and separator rows -- and never touches a
    content word. Whole-page illegibility is the ILLEGIBLE_PAGE marker on
    its own."""
    text = _FENCE_RE.sub("", raw.strip())
    if re.fullmatch(rf"{ILLEGIBLE_TOKEN}[.!]?", text.strip(), flags=re.I):
        return "illegible", ""
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = _MARKDOWN_HEADING_RE.sub("", line)
        match = _BOLD_WRAP_RE.match(line)
        if match:
            line = match.group(1)
        line = _PIPE_EDGES_RE.sub("", line).strip()
        if not line or (_SEPARATOR_ROW_RE.match(line) and "|" in line):
            continue
        line = _SPACE_RUN_RE.sub(" ", line)
        lines.append(line)
    return "ok", "\n".join(lines)


def transcribe_page(
    client: TranscriptionClient,
    *,
    model: str,
    rendered: RenderedPage,
    timeout: int,
    retry_attempts: int,
    prompt: str = TRANSCRIPTION_PROMPT,
    sleep: Callable[[float], None] = time.sleep,
) -> PageTranscript:
    """One page, at most 1 + retry_attempts calls. A transport error, an
    empty reply, or a reply cut off at max_tokens is a failed attempt; the
    page becomes status "error" only when every attempt failed, and the
    document carries on -- one bad page must not sink 38 good ones."""
    last_error: str | None = None
    usage: dict[str, Any] = {}
    elapsed = 0.0
    metadata: dict[str, Any] = {}
    attempts = 0
    for attempt in range(1 + retry_attempts):
        attempts = attempt + 1
        if attempt:
            sleep(RETRY_BACKOFF_SECONDS)
        try:
            result = client.generate_text(model, prompt, [rendered], timeout)
        except (requests.RequestException, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:500]
            continue
        usage, elapsed, metadata = result.usage, result.elapsed_seconds, result.provider_metadata
        raw = (result.payload or {}).get("text") or ""
        if metadata.get("finish_reason") == "length":
            last_error = "reply truncated at max_tokens (finish_reason=length)"
            continue
        if not raw.strip():
            last_error = "empty reply"
            continue
        status, text = normalise_page_text(raw)
        return PageTranscript(
            page=rendered.page, status=status, text=text, usage=usage,
            elapsed_seconds=elapsed, provider_metadata=metadata, attempts=attempts,
        )
    return PageTranscript(
        page=rendered.page, status="error", text="", usage=usage, elapsed_seconds=elapsed,
        provider_metadata=metadata, attempts=attempts, error=last_error,
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def assemble_raw_text(pages: list[PageTranscript]) -> str:
    """Every page in order, each under its marker; illegible and failed
    pages keep a placeholder so the page count is visible in the text."""
    parts: list[str] = []
    for item in sorted(pages, key=lambda p: p.page):
        if item.status == "ok":
            body = item.text
        elif item.status == "illegible":
            body = ILLEGIBLE_TOKEN
        else:
            body = f"[page {item.page}: transcription failed]"
        parts.append(f"{PAGE_MARKER.format(page=item.page)}\n{body}\n")
    return "\n".join(parts)


def assemble_filed_report(pages: list[PageTranscript]) -> str:
    """The document minus the auditor's report, no page markers -- the same
    shape filed_report_text() gives for an XHTML filing."""
    text = "\n".join(item.text for item in sorted(pages, key=lambda p: p.page) if item.status == "ok")
    return strip_auditor_report(text)


def auditor_strip_stats(pages: list[PageTranscript], filed_report: str) -> dict[str, Any]:
    """How much the strip removed and which pages vanished entirely. The
    warning is for the failure the plain-text path is most exposed to: a
    contents page transcribed onto one line starts the strip and the next
    contents entry never arrives to end it, so the real strategic and
    directors' reports go with the audit report."""
    ok_pages = [item for item in pages if item.status == "ok"]
    input_chars = sum(len(item.text) for item in ok_pages)
    kept_lines = set(line for line in filed_report.split("\n") if line)

    def removed_share(text: str) -> float:
        # Short lines (running headers, page numbers, the company name) recur
        # on every page and survive via some other page, so they say nothing
        # about whether THIS page's content was kept. Judge on the long lines.
        lines = [line for line in text.split("\n") if len(line) > 40]
        if not lines:
            return 0.0
        return sum(1 for line in lines if line not in kept_lines) / len(lines)

    fully_removed = [item.page for item in ok_pages if item.text and removed_share(item.text) >= 0.9]
    removed_fraction = round(1 - len(filed_report) / input_chars, 4) if input_chars else 0.0
    warning: str | None = None
    if removed_fraction > 0.35:
        warning = f"strip removed {removed_fraction:.0%} of the text; check the audit-report boundaries"
    suspicious = [
        item.page
        for item in ok_pages
        if item.page in fully_removed and _COMPANY_REPORT_HEADINGS_RE.search(item.text)
    ]
    if suspicious:
        warning = (
            f"page(s) {', '.join(str(p) for p in suspicious)} removed entirely but mention a "
            "strategic/directors' report; the strip probably ran past the audit report"
        )
    return {
        "input_chars": input_chars,
        "kept_chars": len(filed_report),
        "removed_fraction": removed_fraction,
        "pages_fully_removed": fully_removed,
        "suspicious_pages": suspicious,
        "warning": warning,
    }


def transcript_markdown(
    identity: DocumentIdentity, *, model: str, pages: list[PageTranscript]
) -> str:
    title = f"# {identity.company_name or identity.company_number} ({identity.company_number}) -- {model} transcription"
    header = (
        f"Source: {identity.pdf_path} (sha256 {identity.pdf_sha256[:16]}), document {identity.document_id}, "
        f"{len(pages)} pages, prompt {PROMPT_VERSION}."
    )
    sections = [title, "", header, ""]
    for item in sorted(pages, key=lambda p: p.page):
        sections.append(f"## Page {item.page}")
        sections.append("")
        if item.status == "ok":
            sections.append(readable_markdown_from_lines(item.text.split("\n")))
        elif item.status == "illegible":
            sections.append(f"*{ILLEGIBLE_TOKEN}*")
        else:
            sections.append(f"*transcription failed: {item.error}*")
        sections.append("")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------

CheckpointKey = tuple[str, str, int, int, str]  # (document_id, model, render_long_edge, page, pdf_sha256)


def checkpoint_path(model: str, root: Path = CHECKPOINT_ROOT) -> Path:
    return root / model_slug(model) / "checkpoint.jsonl"


def load_checkpoint(path: Path, *, prompt_version: str = PROMPT_VERSION) -> dict[CheckpointKey, dict[str, Any]]:
    done: dict[CheckpointKey, dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a hard kill
        if rec.get("prompt_version") != prompt_version:
            continue
        try:
            key = (rec["document_id"], rec["model"], int(rec["render_long_edge"]), int(rec["page"]), rec["pdf_sha256"])
        except (KeyError, TypeError, ValueError):
            continue
        done[key] = rec
    return done


def append_checkpoint(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def checkpoint_record(
    identity: DocumentIdentity, *, model: str, render_long_edge: int, page: PageTranscript
) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "document_id": identity.document_id,
        "company_number": identity.company_number,
        "model": model,
        "render_long_edge": render_long_edge,
        "pdf_sha256": identity.pdf_sha256,
        **asdict(page),
    }


def page_from_checkpoint(rec: dict[str, Any]) -> PageTranscript:
    return PageTranscript(
        page=int(rec["page"]),
        status=rec.get("status") or "ok",
        text=rec.get("text") or "",
        usage=rec.get("usage") or {},
        elapsed_seconds=float(rec.get("elapsed_seconds") or 0.0),
        provider_metadata=rec.get("provider_metadata") or {},
        attempts=int(rec.get("attempts") or 0),
        error=rec.get("error"),
        replayed=True,
    )


# ---------------------------------------------------------------------------
# one document
# ---------------------------------------------------------------------------

def _sum_usage(pages: list[PageTranscript]) -> dict[str, Any]:
    total: dict[str, Any] = {}
    for item in pages:
        for key, value in item.usage.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value
            elif isinstance(value, dict):
                nested = total.setdefault(key, {})
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, (int, float)) and not isinstance(nested_value, bool):
                        nested[nested_key] = nested.get(nested_key, 0) + nested_value
    return total


def _cost(pages: list[PageTranscript], *, model: str, pricing: dict[str, Any], gbp_per_usd: float | None) -> dict[str, Any]:
    model_pricing = pricing.get(model) or {}
    usd_total = 0.0
    methods: set[str] = set()
    priced = 0
    for item in pages:
        if not item.usage:
            continue
        usd, method = usage_cost_usd(item.usage, model_pricing)
        methods.add(method)
        if usd is not None:
            usd_total += usd
            priced += 1
    if not priced:
        method = "unavailable"
        usd_value: float | None = None
    else:
        method = "provider_reported" if methods == {"provider_reported"} else "estimated_from_token_usage"
        usd_value = round(usd_total, 6)
    return {
        "usd": usd_value,
        "gbp": round(usd_value * gbp_per_usd, 6) if usd_value is not None and gbp_per_usd else None,
        "method": method,
        "pricing": {"gbp_per_usd": gbp_per_usd, "models": {model: model_pricing}},
    }


def output_paths(identity: DocumentIdentity, out_dir: Path, *, canonical: bool, model: str) -> dict[str, Path]:
    """Canonical files feed the profile refresh; a compare-model run writes
    model-suffixed siblings so it can never overwrite the primary."""
    base = out_dir / identity.company_number
    if canonical:
        return {
            "transcript": base.with_name(f"{identity.company_number}.transcript.md"),
            "filed_report": base.with_name(f"{identity.company_number}.filed_report.txt"),
            "summary": base.with_name(f"{identity.company_number}.transcription.json"),
        }
    slug = model_slug(model)
    return {
        "transcript": base.with_name(f"{identity.company_number}.transcript.{slug}.md"),
        "filed_report": base.with_name(f"{identity.company_number}.filed_report.{slug}.txt"),
        "summary": base.with_name(f"{identity.company_number}.transcription.{slug}.json"),
    }


def transcribe_document(
    identity: DocumentIdentity,
    *,
    client: TranscriptionClient | None,
    config: dict[str, Any],
    model: str,
    out_dir: Path,
    conn: sqlite3.Connection | None,
    langfuse: Any,
    pricing: dict[str, Any],
    dry_run: bool = False,
    write_canonical_files: bool = True,
    checkpoint_root: Path = CHECKPOINT_ROOT,
    role: str = "primary",
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Transcribe one PDF end to end. Returns the summary payload (also
    written as .transcription.json). With dry_run nothing is rendered,
    called or written -- only the page count and a cost estimate."""
    render_long_edge = int(config.get("render_long_edge") or DEFAULT_RENDER_LONG_EDGE)
    timeout = int(config.get("timeout_seconds") or 180)
    retry_attempts = int(config.get("retry_attempts") or 0)
    max_pages = config.get("max_pages")
    total_pages = page_count(identity.pdf_path)
    layer_chars = text_layer_chars(identity.pdf_path)

    if dry_run:
        pages_to_do = min(total_pages, max_pages) if max_pages else total_pages
        per_page = pricing.get(model) or {}
        try:
            est = pages_to_do * (1500 * float(per_page["prompt"]) + 1200 * float(per_page["completion"]))
        except (KeyError, ValueError):
            est = None
        return {
            "company_number": identity.company_number,
            "document_id": identity.document_id,
            "pdf_path": str(identity.pdf_path),
            "model": model,
            "page_count": total_pages,
            "pages_to_transcribe": pages_to_do,
            "text_layer_chars": layer_chars,
            "estimated_cost_usd": round(est, 4) if est is not None else None,
            "dry_run": True,
        }

    if client is None:
        raise RuntimeError("a model client is required unless --dry-run")

    rendered = render_pages(identity.pdf_path, max_pages=max_pages, long_edge=render_long_edge)
    ckpt_path = checkpoint_path(model, checkpoint_root)
    done = load_checkpoint(ckpt_path)

    pages: list[PageTranscript] = []
    trace_id: str | None = None
    tags = [
        "harness:vlm-transcription",
        f"company:{identity.company_number}",
        f"document:{identity.document_id}",
        f"model:{model}",
        f"prompt:{PROMPT_VERSION}",
        f"role:{role}",
    ]
    started = time.monotonic()

    def run_pages(parent: Any) -> None:
        for item in rendered:
            key: CheckpointKey = (identity.document_id, model, render_long_edge, item.page, identity.pdf_sha256)
            rec = done.get(key)
            if rec is not None:
                page = page_from_checkpoint(rec)
            else:
                page = transcribe_page(
                    client, model=model, rendered=item, timeout=timeout,
                    retry_attempts=retry_attempts, sleep=sleep,
                )
                if page.status != "error":
                    append_checkpoint(ckpt_path, checkpoint_record(
                        identity, model=model, render_long_edge=render_long_edge, page=page,
                    ))
            pages.append(page)
            print(
                f"  [{item.page}/{len(rendered)}] {page.status}"
                f"{' (replayed)' if page.replayed else ''}"
                f"{'' if page.status != 'error' else ' -- ' + str(page.error)}",
                file=sys.stderr,
            )
            if parent is not None:
                with observation(
                    parent,
                    name=f"transcribe page {item.page}",
                    as_type="generation",
                    model=model,
                    input={"page": item.page, "prompt_version": PROMPT_VERSION, "prompt": TRANSCRIPTION_PROMPT},
                    output={"status": page.status, "chars": len(page.text), "head": page.text[:200], "error": page.error},
                    metadata={
                        "usage": page.usage,
                        "elapsed_seconds": page.elapsed_seconds,
                        "attempts": page.attempts,
                        "replayed_from_checkpoint": page.replayed,
                        "provider": page.provider_metadata,
                    },
                ):
                    pass

    if langfuse is not None:
        with case_trace(
            langfuse,
            name=f"transcribe {identity.company_number}/{identity.document_id}",
            tags=tags,
            metadata={
                "company_number": identity.company_number,
                "document_id": identity.document_id,
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "render_long_edge": render_long_edge,
                "pdf_sha256": identity.pdf_sha256,
            },
            input={"pdf": str(identity.pdf_path), "pages": len(rendered)},
        ) as root:
            trace_id = root.trace_id
            try:
                with observation(root, name="source_pdf", input={"source_pdf": pdf_media(identity.pdf_path)}):
                    pass
            except Exception as exc:  # noqa: BLE001 -- media upload is a nicety, never the run
                print(f"  (source_pdf media not attached: {exc})", file=sys.stderr)
            run_pages(root)
            root.update(output={
                "transcribed_pages": sum(1 for p in pages if p.status == "ok"),
                "illegible_pages": [p.page for p in pages if p.status == "illegible"],
                "failed_pages": [p.page for p in pages if p.status == "error"],
            })
        flush(langfuse)
    else:
        run_pages(None)

    raw_text = assemble_raw_text(pages)
    filed_report = assemble_filed_report(pages)
    strip = auditor_strip_stats(pages, filed_report)
    failed = [p.page for p in pages if p.status == "error"]
    illegible = [p.page for p in pages if p.status == "illegible"]
    status = "error" if len(failed) == len(pages) else "partial" if failed else "complete"
    payload: dict[str, Any] = {
        "company_number": identity.company_number,
        "company_name": identity.company_name,
        "document_id": identity.document_id,
        "pdf_path": str(identity.pdf_path),
        "pdf_sha256": identity.pdf_sha256,
        "filing_date": identity.filing_date,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "provider": getattr(client, "provider_name", None),
        "role": role,
        "render_long_edge": render_long_edge,
        "page_count": total_pages,
        "text_layer_chars": layer_chars,
        "transcribed_pages": sum(1 for p in pages if p.status == "ok"),
        "illegible_pages": illegible,
        "failed_pages": failed,
        "status": status,
        "pages": [
            {
                "page": p.page, "status": p.status, "chars": len(p.text), "attempts": p.attempts,
                "elapsed_seconds": round(p.elapsed_seconds, 3), "usage": p.usage,
                "provider_metadata": p.provider_metadata, "replayed": p.replayed, "error": p.error,
            }
            for p in pages
        ],
        "usage": _sum_usage(pages),
        "auditor_strip": strip,
        "cost": _cost(pages, model=model, pricing=pricing, gbp_per_usd=config.get("gbp_per_usd")),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "langfuse_trace_id": trace_id,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }

    if conn is not None:
        payload["document_texts_id"] = upsert_document_text(
            conn,
            company_number=identity.company_number,
            document_id=identity.document_id,
            source=SOURCE,
            model=model,
            prompt_version=PROMPT_VERSION,
            raw_text=raw_text,
            filed_report_text=filed_report,
            payload=payload,
        )

    paths = output_paths(identity, out_dir, canonical=write_canonical_files, model=model)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths["transcript"].write_text(transcript_markdown(identity, model=model, pages=pages), encoding="utf-8")
    paths["filed_report"].write_text(filed_report, encoding="utf-8")
    paths["summary"].write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    payload["files"] = {name: str(path) for name, path in paths.items()}
    payload["_pages"] = pages  # in-memory only, for --compare-model; stripped before printing
    return payload


# ---------------------------------------------------------------------------
# two-model comparison
# ---------------------------------------------------------------------------

def normalise_for_similarity(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def page_similarity(a: str, b: str) -> float:
    na, nb = normalise_for_similarity(a), normalise_for_similarity(b)
    if not na and not nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()


def numeric_tokens(text: str) -> set[str]:
    return set(_NUMERIC_TOKEN_RE.findall(text))


def compare_transcripts(
    primary: list[PageTranscript], secondary: list[PageTranscript], *, threshold: float = SIMILARITY_THRESHOLD
) -> dict[str, Any]:
    """Per-page agreement between two independent readings. A page is
    flagged when the texts differ beyond `threshold` or when one reading
    contains a number the other does not -- numbers are where a
    transcription error costs most and where difflib's ratio is least
    sensitive (one digit in a 3,000-character page barely moves it)."""
    by_page_b = {p.page: p for p in secondary}
    rows: list[dict[str, Any]] = []
    for a in sorted(primary, key=lambda p: p.page):
        b = by_page_b.get(a.page)
        b_text = b.text if b else ""
        ratio = page_similarity(a.text, b_text)
        only_a = sorted(numeric_tokens(a.text) - numeric_tokens(b_text))
        only_b = sorted(numeric_tokens(b_text) - numeric_tokens(a.text))
        status_b = b.status if b else "missing"
        flagged = ratio < threshold or bool(only_a) or bool(only_b) or a.status != status_b
        rows.append({
            "page": a.page, "ratio": round(ratio, 4),
            "numbers_only_in_primary": only_a, "numbers_only_in_secondary": only_b,
            "status_primary": a.status, "status_secondary": status_b, "flagged": flagged,
        })
    ratios = [r["ratio"] for r in rows]
    return {
        "threshold": threshold,
        "pages": rows,
        "mean_ratio": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "min_ratio": min(ratios) if ratios else None,
        "flagged_pages": [r["page"] for r in rows if r["flagged"]],
        "status_disagreements": [r["page"] for r in rows if r["status_primary"] != r["status_secondary"]],
    }


def diff_report_markdown(
    identity: DocumentIdentity,
    *,
    primary_model: str,
    secondary_model: str,
    comparison: dict[str, Any],
    primary: list[PageTranscript],
    secondary: list[PageTranscript],
) -> str:
    by_a = {p.page: p for p in primary}
    by_b = {p.page: p for p in secondary}
    lines = [
        f"# {identity.company_number} transcript agreement: {primary_model} vs {secondary_model}",
        "",
        f"Mean similarity {comparison['mean_ratio']}, minimum {comparison['min_ratio']}, "
        f"threshold {comparison['threshold']}. Flagged pages: "
        f"{', '.join(str(p) for p in comparison['flagged_pages']) or 'none'}.",
        "",
        "| page | ratio | primary | secondary | numbers only in primary | numbers only in secondary |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in comparison["pages"]:
        lines.append(
            f"| {row['page']} | {row['ratio']} | {row['status_primary']} | {row['status_secondary']} | "
            f"{' '.join(row['numbers_only_in_primary']) or '-'} | {' '.join(row['numbers_only_in_secondary']) or '-'} |"
        )
    for row in comparison["pages"]:
        if not row["flagged"]:
            continue
        a_text = by_a[row["page"]].text if row["page"] in by_a else ""
        b_text = by_b[row["page"]].text if row["page"] in by_b else ""
        lines += ["", f"## Page {row['page']} (ratio {row['ratio']})", "", f"### {primary_model}", "", "```text", a_text, "```", "",
                  f"### {secondary_model}", "", "```text", b_text, "```", "", "### diff", "", "```diff"]
        lines += list(difflib.unified_diff(
            a_text.split("\n"), b_text.split("\n"), fromfile=primary_model, tofile=secondary_model, lineterm="",
        ))
        lines.append("```")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", type=Path, help="One scanned filing.")
    source.add_argument("--pdf-dir", type=Path, help="Every *.pdf in a folder, named <company>-<document id|date>.pdf.")
    parser.add_argument("--company-number", default=None, help="Override the company number (single --pdf only).")
    parser.add_argument("--document-id", default=None, help="Override the Companies House document id (single --pdf only).")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("companies-house.db"), help="SQLite database to persist into ('' to skip).")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Page counts and a cost estimate; no calls, no writes.")
    compare = parser.add_mutually_exclusive_group()
    compare.add_argument("--compare-model", default=None,
                         help="Transcribe again with this model (same request options as --config) and write a per-page diff.")
    compare.add_argument("--compare-config", type=Path, default=None,
                         help="Transcribe again with this config's model and request options (for a model that needs "
                              "different options, e.g. one whose endpoint refuses reasoning: {enabled: false}).")
    parser.add_argument("--no-langfuse", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    config = configuration_from_file(args.config)
    model = config["model"]
    compare_config: dict[str, Any] | None = None
    if args.compare_config is not None:
        compare_config = configuration_from_file(args.compare_config)
    elif args.compare_model:
        compare_config = {**config, "model": args.compare_model}
    compare_model = compare_config["model"] if compare_config else None

    if args.pdf is not None:
        pdfs = [args.pdf]
    else:
        pdfs = discover_pdfs(args.pdf_dir, limit=args.limit)
        if not pdfs:
            print(f"No PDFs found in {args.pdf_dir}", file=sys.stderr)
            return 1
    if args.pdf_dir is not None and (args.company_number or args.document_id):
        parser.error("--company-number / --document-id only apply to a single --pdf")

    identities = [
        document_identity(pdf, company_number=args.company_number, document_id=args.document_id, out_dir=args.out_dir)
        for pdf in pdfs
    ]

    pricing = fetch_pricing()
    if args.dry_run:
        total = 0.0
        for identity in identities:
            estimate = transcribe_document(
                identity, client=None, config=config, model=model, out_dir=args.out_dir,
                conn=None, langfuse=None, pricing=pricing, dry_run=True,
            )
            total += estimate.get("estimated_cost_usd") or 0.0
            print(json.dumps(estimate, indent=1))
        print(f"\n{len(identities)} document(s); estimated ~${total:.2f} at {model}"
              f"{' (x2 with the compare model)' if compare_model else ''}. Nothing written.")
        return 0

    client = build_client(config)
    compare_client = build_client(compare_config) if compare_config else None
    conn = None
    if str(args.db):
        conn = sqlite3.connect(str(args.db))
        init_db(conn)
    langfuse = None if args.no_langfuse else langfuse_from_config(config)

    grand_usd = 0.0
    for identity in identities:
        print(f"\n{identity.company_number} {identity.pdf_path.name} -> {model}", file=sys.stderr)
        primary = transcribe_document(
            identity, client=client, config=config, model=model, out_dir=args.out_dir,
            conn=conn, langfuse=langfuse, pricing=pricing,
        )
        grand_usd += primary["cost"].get("usd") or 0.0
        summary = {k: v for k, v in primary.items() if k not in ("pages", "_pages")}
        print(json.dumps({k: summary[k] for k in (
            "company_number", "document_id", "model", "page_count", "transcribed_pages",
            "illegible_pages", "failed_pages", "status", "auditor_strip", "cost", "elapsed_seconds", "files",
        )}, indent=1))

        if compare_config and compare_model and compare_client is not None:
            print(f"\n{identity.company_number} {identity.pdf_path.name} -> {compare_model} (compare)", file=sys.stderr)
            secondary = transcribe_document(
                identity, client=compare_client, config=compare_config, model=compare_model, out_dir=args.out_dir,
                conn=conn, langfuse=langfuse, pricing=pricing, write_canonical_files=False, role="compare",
            )
            grand_usd += secondary["cost"].get("usd") or 0.0
            comparison = compare_transcripts(primary["_pages"], secondary["_pages"])
            report = diff_report_markdown(
                identity, primary_model=model, secondary_model=compare_model,
                comparison=comparison, primary=primary["_pages"], secondary=secondary["_pages"],
            )
            diff_path = args.out_dir / f"{identity.company_number}.transcript-diff.md"
            diff_path.write_text(report, encoding="utf-8")
            print(json.dumps({
                "compare_model": compare_model,
                "mean_ratio": comparison["mean_ratio"],
                "min_ratio": comparison["min_ratio"],
                "flagged_pages": comparison["flagged_pages"],
                "status_disagreements": comparison["status_disagreements"],
                "diff_report": str(diff_path),
                "cost": secondary["cost"],
            }, indent=1))

    print(f"\nTotal cost across models: ${grand_usd:.4f}")
    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
