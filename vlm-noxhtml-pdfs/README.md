# vlm-noxhtml-pdfs

Local cache of source PDF accounts for filings that have no XHTML/iXBRL
version — i.e. filings Companies House only published as a scanned or
PDF-native document, so they need visual (VLM) extraction rather than
structured-tag parsing.

This folder is not committed to git (PDFs are gitignored via `*.pdf`, and any
JSON sidecar written here is ignored via `vlm-noxhtml-pdfs/*.json`). It exists
locally because the gold-label eval cases reference these PDFs by path:

- `evals/vlm_financials/cases/*.json` each store a `pdf_path` pointing at a
  file in this folder, plus a SHA-256 hash of its contents. Reviewing or
  re-running a case requires the matching PDF to be present here.
- `scripts/vlm/ch_vlm_financial_sample.py` defaults `--pdf-dir` to this
  folder when sampling and downloading new no-XHTML filings.

Do not delete PDFs referenced by existing eval cases — doing so breaks the
ability to review or re-verify that case. Anything else here is disposable
and can be re-downloaded from the Companies House document API if needed.
