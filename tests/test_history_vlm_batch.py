import json

from scripts.pdf_vision_extraction import history_vlm_batch as hv


def _filing(company, period_end, tx):
    return {"company_number": company, "transaction_id": tx, "period_end": period_end, "filing_date": "2020-01-01",
            "pdf_url": f"https://x/document/{company}{tx}/content"}


def test_document_id_is_the_path_segment_before_content():
    assert hv.document_id("https://x/document/ABC123/content") == "ABC123"


def test_minimal_cover_reads_every_other_filing_in_a_run_of_scans():
    # four consecutive scanned years: reading 2019 covers 2019 and 2018, so 2018 is skipped;
    # reading 2017 covers 2017 and 2016, so 2016 is skipped.
    filings = [_filing("1", f"{y}-12-31", str(y)) for y in (2019, 2018, 2017, 2016)]
    chosen = hv.plan_filings(filings, {"1": set()})
    assert [f["period_end"][:4] for f in chosen] == ["2019", "2017"]


def test_a_year_already_held_from_xhtml_is_not_read_again():
    filings = [_filing("1", "2018-12-31", "a"), _filing("1", "2016-12-31", "b")]
    chosen = hv.plan_filings(filings, {"1": {2018, 2017}})
    assert [f["period_end"][:4] for f in chosen] == ["2016"]


def test_cover_all_reads_every_filing_not_already_read():
    filings = [_filing("1", f"{y}-12-31", str(y)) for y in (2019, 2018)]
    assert len(hv.plan_filings(filings, {"1": set()}, cover="all")) == 2
    done = frozenset({hv.document_id(filings[0]["pdf_url"])})
    assert [f["period_end"][:4] for f in hv.plan_filings(filings, {"1": set()}, cover="all", done_documents=done)] == ["2018"]


def test_a_filing_already_read_still_counts_as_covering_its_years():
    filings = [_filing("1", "2019-12-31", "a"), _filing("1", "2018-12-31", "b")]
    done = frozenset({hv.document_id(filings[0]["pdf_url"])})
    assert hv.plan_filings(filings, {"1": set()}, done_documents=done) == []


def test_log_reader_deduplicates_across_restarts_and_skips_bad_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    good = _filing("1", "2019-12-31", "a")
    path.write_text("\n".join([json.dumps(good), json.dumps(good), "{broken", json.dumps({"company_number": "2"})]),
                    encoding="utf-8")
    assert [e["transaction_id"] for e in hv.read_pdf_log(path)] == ["a"]
