from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from core.companies_house_sqlite import init_db, upsert_document_text
from scripts.vlm import companies_house_pdf_transcribe as T
from scripts.vlm.companies_house_pdf_vlm_financials import (
    ModelCallResult,
    OpenRouterVlmModelClient,
    RenderedPage,
)
from tests.langfuse_fakes import FakeLangfuse

AUDIT_PAGE = "\n".join([
    "INDEPENDENT AUDITORS' REPORT",
    "TO THE MEMBERS OF WIDGETS LIMITED",
    "We conducted our audit in accordance with ISAs (UK).",
])
DIRECTORS_PAGE = "\n".join([
    "Directors' report",
    "The principal activity of the company is the sale of widgets.",
    "The directors present their report and the financial statements for the year.",
    "Results and dividends | The profit for the year after taxation was 200.",
    "Directors | The directors who served during the year were A Smith and B Jones.",
])
PNL_PAGE = "\n".join([
    "Statement of comprehensive income",
    "Turnover | 1,000 | 900",
])


class RecordingClient:
    """generate_text fake: replies per page number, records every call, and
    can be told to fail some calls first."""

    provider_name = "fake"

    def __init__(self, replies: dict[int, str], *, fail_first: int = 0, finish_reason: str | None = None) -> None:
        self.replies = replies
        self.fail_first = fail_first
        self.finish_reason = finish_reason
        self.calls: list[int] = []

    def generate_text(self, model: str, prompt: str, pages: list[RenderedPage], timeout: int) -> ModelCallResult:
        page = pages[0].page
        self.calls.append(page)
        if self.fail_first:
            self.fail_first -= 1
            raise requests.ConnectionError("boom")
        metadata = {"finish_reason": self.finish_reason or "stop", "model": model}
        return ModelCallResult(
            {"text": self.replies.get(page, "")}, {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
            0.1, provider_metadata=metadata,
        )

    def pricing_snapshot(self) -> dict:
        return {}


def _rendered(n: int) -> list[RenderedPage]:
    return [RenderedPage(i, "aGVsbG8=") for i in range(1, n + 1)]


def _pdf(tmp_path: Path, name: str = "08029548-2026-06-10.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake")
    return path


@pytest.fixture
def three_page_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "render_pages", lambda pdf, *, max_pages, long_edge, page_numbers=None: _rendered(3))
    monkeypatch.setattr(T, "page_count", lambda pdf: 3)
    monkeypatch.setattr(T, "text_layer_chars", lambda pdf: 0)
    monkeypatch.setattr(T, "pdf_media", lambda path: {"path": str(path)})
    return tmp_path


# --- transport ---------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.headers: dict = {}
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_generate_text_returns_plain_text_usage_and_finish_reason(monkeypatch) -> None:
    body = {
        "id": "gen-1", "model": "m", "provider": "google",
        "choices": [{"message": {"content": "Hello page"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(body))
    client = OpenRouterVlmModelClient("key")
    result = client.generate_text("m", "prompt", _rendered(1), 10)
    assert result.payload == {"text": "Hello page"}
    assert result.usage["completion_tokens"] == 3
    assert result.provider_metadata["finish_reason"] == "stop"
    assert result.response_handling == {"method": "plain_text"}


def test_generate_json_still_parses_after_the_transport_split(monkeypatch) -> None:
    body = {"choices": [{"message": {"content": '{"pages": []}'}, "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(body))
    result = OpenRouterVlmModelClient("key").generate_json("m", "p", _rendered(1), 10)
    assert result.payload == {"pages": []}


# --- one page ----------------------------------------------------------------

def test_transcribe_page_retries_once_on_transport_error_then_succeeds() -> None:
    client = RecordingClient({1: "Some text"}, fail_first=1)
    slept: list[float] = []
    page = T.transcribe_page(client, model="m", rendered=_rendered(1)[0], timeout=5, retry_attempts=1, sleep=slept.append)
    assert page.status == "ok" and page.text == "Some text" and page.attempts == 2
    assert slept == [T.RETRY_BACKOFF_SECONDS]


def test_transcribe_page_treats_empty_and_truncated_replies_as_failures() -> None:
    empty = RecordingClient({1: "   "})
    page = T.transcribe_page(empty, model="m", rendered=_rendered(1)[0], timeout=5, retry_attempts=1, sleep=lambda s: None)
    assert page.status == "error" and page.error == "empty reply" and page.attempts == 2

    truncated = RecordingClient({1: "long text"}, finish_reason="length")
    page = T.transcribe_page(truncated, model="m", rendered=_rendered(1)[0], timeout=5, retry_attempts=0, sleep=lambda s: None)
    assert page.status == "error" and "max_tokens" in (page.error or "")


def test_transcribe_page_records_the_error_after_every_attempt_fails_without_raising() -> None:
    client = RecordingClient({1: "x"}, fail_first=5)
    page = T.transcribe_page(client, model="m", rendered=_rendered(1)[0], timeout=5, retry_attempts=1, sleep=lambda s: None)
    assert page.status == "error"
    assert page.error and page.error.startswith("ConnectionError")
    assert client.calls == [1, 1]


@pytest.mark.parametrize("reply", ["ILLEGIBLE_PAGE", "ILLEGIBLE_PAGE.", "illegible_page", "```\nILLEGIBLE_PAGE\n```"])
def test_normalise_page_text_recognises_the_illegible_marker(reply: str) -> None:
    assert T.normalise_page_text(reply) == ("illegible", "")


def test_normalise_page_text_strips_structure_but_not_words() -> None:
    raw = "```text\n# Directors' report\n| Turnover | 1,000 |\n| --- | --- |\n**Bold heading**\nSome [illegible] words   here\n\n```"
    status, text = T.normalise_page_text(raw)
    assert status == "ok"
    assert text == "Directors' report\nTurnover | 1,000\nBold heading\nSome [illegible] words here"


# --- assembly ----------------------------------------------------------------

def _pages() -> list[T.PageTranscript]:
    return [
        T.PageTranscript(1, "ok", DIRECTORS_PAGE),
        T.PageTranscript(2, "ok", AUDIT_PAGE),
        T.PageTranscript(3, "illegible", ""),
        T.PageTranscript(4, "ok", PNL_PAGE),
        T.PageTranscript(5, "error", "", error="boom"),
    ]


def test_assemble_raw_text_marks_every_page_in_order() -> None:
    raw = T.assemble_raw_text(list(reversed(_pages())))
    markers = [line for line in raw.split("\n") if T.PAGE_MARKER_RE.match(line)]
    assert markers == [f"--- page {n} ---" for n in (1, 2, 3, 4, 5)]
    assert "ILLEGIBLE_PAGE" in raw and "[page 5: transcription failed]" in raw


def test_assemble_filed_report_drops_the_audit_report_and_has_no_markers() -> None:
    report = T.assemble_filed_report(_pages())
    assert "sale of widgets" in report
    assert "Turnover | 1,000 | 900" in report
    assert "ISAs (UK)" not in report
    assert not any(T.PAGE_MARKER_RE.match(line) for line in report.split("\n"))
    assert "ILLEGIBLE_PAGE" not in report


def test_auditor_strip_stats_reports_removed_pages_and_warns_on_a_swallowed_report() -> None:
    pages = _pages()
    stats = T.auditor_strip_stats(pages, T.assemble_filed_report(pages))
    assert stats["pages_fully_removed"] == [2]
    assert stats["warning"] is None

    # A contents page flattened onto one line starts the strip; the next
    # entry never arrives on its own line, so the directors' report vanishes.
    swallowed = [
        T.PageTranscript(1, "ok", "Contents Directors' report 1 Independent auditor's report 2 Statement of comprehensive income 4"),
        T.PageTranscript(2, "ok", DIRECTORS_PAGE),
        T.PageTranscript(3, "ok", PNL_PAGE),
    ]
    stats = T.auditor_strip_stats(swallowed, T.assemble_filed_report(swallowed))
    assert 2 in stats["pages_fully_removed"]
    assert stats["suspicious_pages"] == [1, 2]  # the contents page and the swallowed directors' report
    assert stats["warning"] and "ran past" in stats["warning"]


def test_transcript_markdown_has_a_section_per_page() -> None:
    identity = T.DocumentIdentity("08029548", "doc-1", Path("x.pdf"), "ab" * 32, "2026-06-10", "SMART CURRENCY")
    md = T.transcript_markdown(identity, model="m", pages=_pages())
    assert md.startswith("# SMART CURRENCY (08029548) -- m transcription")
    assert md.count("## Page ") == 5
    assert "*ILLEGIBLE_PAGE*" in md and "*transcription failed: boom*" in md


# --- checkpoint --------------------------------------------------------------

def test_load_checkpoint_ignores_other_prompt_versions_and_a_half_written_tail(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    identity = T.DocumentIdentity("c", "d", Path("x.pdf"), "sha", None)
    T.append_checkpoint(path, T.checkpoint_record(identity, model="m", render_long_edge=1440, page=T.PageTranscript(1, "ok", "one")))
    stale = T.checkpoint_record(identity, model="m", render_long_edge=1440, page=T.PageTranscript(2, "ok", "two"))
    stale["prompt_version"] = "transcribe-v0"
    T.append_checkpoint(path, stale)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"document_id": "d", "model": "m", "page": 3')  # killed mid-write
    done = T.load_checkpoint(path)
    assert list(done) == [("d", "m", 1440, 1, "sha")]
    assert T.page_from_checkpoint(done[("d", "m", 1440, 1, "sha")]).replayed is True


def test_resume_replays_checkpointed_pages_and_calls_the_model_only_for_the_rest(three_page_setup) -> None:
    tmp_path = three_page_setup
    pdf = _pdf(tmp_path)
    identity = T.document_identity(pdf, out_dir=tmp_path)
    ckpt = T.checkpoint_path("m", tmp_path / "logs")
    for n, text in ((1, DIRECTORS_PAGE), (2, AUDIT_PAGE)):
        T.append_checkpoint(ckpt, T.checkpoint_record(identity, model="m", render_long_edge=1440, page=T.PageTranscript(n, "ok", text)))
    client = RecordingClient({3: PNL_PAGE})
    lf = FakeLangfuse()

    payload = T.transcribe_document(
        identity, client=client, config={"render_long_edge": 1440}, model="m", out_dir=tmp_path / "out",
        conn=None, langfuse=lf, pricing={}, checkpoint_root=tmp_path / "logs", sleep=lambda s: None,
    )

    assert client.calls == [3]
    assert [p["replayed"] for p in payload["pages"]] == [True, True, False]
    assert payload["status"] == "complete" and payload["transcribed_pages"] == 3
    # every page got an observation, replayed or not; the trace was flushed once
    names = [obs["name"] for obs in lf.observations]
    assert sum(1 for n in names if n.startswith("transcribe page")) == 3
    assert lf.flushed == 1


def test_errored_pages_are_not_checkpointed_so_a_rerun_retries_them(three_page_setup) -> None:
    tmp_path = three_page_setup
    identity = T.document_identity(_pdf(tmp_path), out_dir=tmp_path)
    client = RecordingClient({1: DIRECTORS_PAGE, 2: AUDIT_PAGE, 3: PNL_PAGE}, fail_first=2)  # page 1 fails twice
    payload = T.transcribe_document(
        identity, client=client, config={"render_long_edge": 1440, "retry_attempts": 1}, model="m",
        out_dir=tmp_path / "out", conn=None, langfuse=None, pricing={},
        checkpoint_root=tmp_path / "logs", sleep=lambda s: None,
    )
    assert payload["failed_pages"] == [1] and payload["status"] == "partial"
    done = T.load_checkpoint(T.checkpoint_path("m", tmp_path / "logs"))
    assert sorted(key[3] for key in done) == [2, 3]


# --- identity ----------------------------------------------------------------

def test_document_identity_prefers_flags_then_sidecar_then_filename_then_hash(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path, "10234133-NrohI4CePsST_abc.pdf")
    identity = T.document_identity(pdf, out_dir=tmp_path)
    assert identity.company_number == "10234133" and identity.document_id == "NrohI4CePsST_abc"

    dated = _pdf(tmp_path, "08029548-2026-06-10.pdf")
    identity = T.document_identity(dated, out_dir=tmp_path)
    assert identity.filing_date == "2026-06-10"
    assert identity.document_id == f"sha256:{identity.pdf_sha256[:16]}"

    (tmp_path / "08029548.metadata.json").write_text(json.dumps({"document_id": "hnbP", "company_name": "SMART"}), encoding="utf-8")
    identity = T.document_identity(dated, out_dir=tmp_path)
    assert identity.document_id == "hnbP" and identity.company_name == "SMART"

    identity = T.document_identity(dated, company_number="99999999", document_id="explicit", out_dir=tmp_path)
    assert (identity.company_number, identity.document_id) == ("99999999", "explicit")


# --- persistence -------------------------------------------------------------

def test_upsert_document_text_inserts_then_updates_the_same_key() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    first = upsert_document_text(
        conn, company_number="c", document_id="d", source="vlm_transcription", model="m",
        prompt_version="v1", raw_text="raw", filed_report_text="report",
        payload={"status": "complete", "page_count": 2, "cost": {"usd": 0.1, "gbp": 0.08, "method": "provider_reported"}},
    )
    created = conn.execute("select created_at from document_texts where id = ?", (first,)).fetchone()[0]
    second = upsert_document_text(
        conn, company_number="c", document_id="d", source="vlm_transcription", model="m",
        prompt_version="v1", raw_text="raw2", filed_report_text="report2", payload={"status": "complete"},
    )
    rows = conn.execute("select id, raw_text, created_at from document_texts").fetchall()
    assert rows == [(first, "raw2", created)]
    assert second == first
    upsert_document_text(
        conn, company_number="c", document_id="d", source="vlm_transcription", model="other",
        prompt_version="v1", raw_text="r", filed_report_text="f", payload={},
    )
    assert conn.execute("select count(*) from document_texts").fetchone()[0] == 2
    assert conn.execute("select count(*) from sqlite_master where name = 'idx_document_texts_company_number'").fetchone()[0] == 1


def test_transcribe_document_writes_files_db_row_and_summary(three_page_setup) -> None:
    tmp_path = three_page_setup
    identity = T.document_identity(_pdf(tmp_path), out_dir=tmp_path)
    client = RecordingClient({1: DIRECTORS_PAGE, 2: AUDIT_PAGE, 3: PNL_PAGE})
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    pricing = {"m": {"prompt": "0.0000005", "completion": "0.000003"}}

    payload = T.transcribe_document(
        identity, client=client, config={"render_long_edge": 1440, "gbp_per_usd": 0.8}, model="m",
        out_dir=tmp_path / "out", conn=conn, langfuse=None, pricing=pricing,
        checkpoint_root=tmp_path / "logs", sleep=lambda s: None,
    )

    out = tmp_path / "out"
    assert (out / "08029548.filed_report.txt").read_text(encoding="utf-8") == T.assemble_filed_report(payload["_pages"])
    assert "ISAs (UK)" not in (out / "08029548.filed_report.txt").read_text(encoding="utf-8")
    assert "## Page 2" in (out / "08029548.transcript.md").read_text(encoding="utf-8")
    summary = json.loads((out / "08029548.transcription.json").read_text(encoding="utf-8"))
    assert summary["cost"]["method"] == "provider_reported" and summary["cost"]["usd"] == pytest.approx(0.003)
    assert summary["cost"]["gbp"] == pytest.approx(0.0024)
    assert summary["usage"]["prompt_tokens"] == 300
    assert "raw_text" not in summary
    row = conn.execute("select source, model, status, filed_report_text, cost_usd from document_texts").fetchone()
    assert row[:3] == ("vlm_transcription", "m", "complete") and "sale of widgets" in row[3]


def test_compare_model_files_never_overwrite_the_canonical_ones(three_page_setup) -> None:
    tmp_path = three_page_setup
    identity = T.document_identity(_pdf(tmp_path), out_dir=tmp_path)
    paths = T.output_paths(identity, tmp_path, canonical=False, model="google/gemini-3.7-flash")
    assert paths["transcript"].name == "08029548.transcript.google-gemini-3.7-flash.md"
    assert paths["filed_report"].name == "08029548.filed_report.google-gemini-3.7-flash.txt"


def test_dry_run_makes_no_calls_and_writes_nothing(three_page_setup) -> None:
    tmp_path = three_page_setup
    identity = T.document_identity(_pdf(tmp_path), out_dir=tmp_path)
    estimate = T.transcribe_document(
        identity, client=None, config={"max_pages": 2}, model="m", out_dir=tmp_path / "out", conn=None,
        langfuse=None, pricing={"m": {"prompt": "0.0000005", "completion": "0.000003"}}, dry_run=True,
    )
    assert estimate["dry_run"] and estimate["page_count"] == 3 and estimate["pages_to_transcribe"] == 2
    assert estimate["estimated_cost_usd"] == pytest.approx(2 * (1500 * 5e-7 + 1200 * 3e-6), rel=1e-3)
    assert not (tmp_path / "out").exists()


# --- comparison --------------------------------------------------------------

def test_compare_transcripts_flags_low_similarity_and_numeric_disagreement() -> None:
    a = [T.PageTranscript(1, "ok", "Turnover 1,000\nProfit 200"), T.PageTranscript(2, "ok", "Same   text\nhere"), T.PageTranscript(3, "ok", "abc def ghi")]
    b = [T.PageTranscript(1, "ok", "Turnover 1,000\nProfit 208"), T.PageTranscript(2, "ok", "same text here"), T.PageTranscript(3, "illegible", "")]
    result = T.compare_transcripts(a, b)
    by_page = {row["page"]: row for row in result["pages"]}
    assert by_page[1]["flagged"] and by_page[1]["numbers_only_in_primary"] == ["200"] and by_page[1]["numbers_only_in_secondary"] == ["208"]
    assert by_page[2]["ratio"] == 1.0 and not by_page[2]["flagged"]  # whitespace/case only
    assert by_page[3]["flagged"] and result["status_disagreements"] == [3]
    assert result["flagged_pages"] == [1, 3]


def test_diff_report_lists_both_versions_for_flagged_pages_only() -> None:
    identity = T.DocumentIdentity("c", "d", Path("x.pdf"), "sha", None)
    a = [T.PageTranscript(1, "ok", "Turnover 1,000"), T.PageTranscript(2, "ok", "identical")]
    b = [T.PageTranscript(1, "ok", "Turnover 1,600"), T.PageTranscript(2, "ok", "identical")]
    comparison = T.compare_transcripts(a, b)
    report = T.diff_report_markdown(identity, primary_model="A", secondary_model="B", comparison=comparison, primary=a, secondary=b)
    assert "## Page 1" in report and "## Page 2" not in report
    assert "-Turnover 1,000" in report and "+Turnover 1,600" in report


# --- config ------------------------------------------------------------------

def test_configuration_rejects_secret_keys_and_page_batching(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("provider: openrouter\nmodel: m\nopenrouter_request_options:\n  api_key: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="secret key"):
        T.configuration_from_file(bad)
    batched = tmp_path / "batched.yaml"
    batched.write_text("provider: openrouter\nmodel: m\npages_per_call: 4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pages_per_call"):
        T.configuration_from_file(batched)
    good = tmp_path / "good.yaml"
    good.write_text("provider: openrouter\nmodel: m\n", encoding="utf-8")
    config = T.configuration_from_file(good)
    assert config["render_long_edge"] == T.DEFAULT_RENDER_LONG_EDGE and config["retry_attempts"] == 1
