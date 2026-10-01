---
name: publish-eval-sheet
description: Turn business-profile eval report JSON(s) into a multi-tab workbook and publish it to the "Projects / companies-house-leads" Google Drive folder as a native Google Sheet. Use after any `business_profile_eval run`, or when comparing two runs.
---

# Publish an eval report as a Google Sheet

Every `python -m scripts.profile.business_profile_eval run ...` writes
`logs/business-profile-eval/report-<timestamp>.json`. That file is the
record; this skill is how it gets read.

## 1. Build the workbook

```bash
.venv-claude/Scripts/python.exe -m scripts.profile.business_profile_report_sheet \
    logs/business-profile-eval/report-<A>.json [logs/business-profile-eval/report-<B>.json ...] \
    --out logs/business-profile-eval/eval-sheet-<label>.xlsx
```

Pass two or more reports to compare runs: the Summary tab gets one column
per run. Tabs: Summary, Per-class, Confidence, Cases, Adjudicate (see the
module docstring for what each answers). No model calls; reads the report
and `evals/business_profiles/cases/`.

## 2. Publish it

**Publish every tab as CSV text**, one Drive file per tab, via `textContent`
with `contentMimeType: text/csv` (Drive converts each to a Sheet). Do not
upload the `.xlsx` as base64: a 19 KB two-run metrics workbook uploaded on
2026-09-14 arrived with the Summary and Per-class tabs blank and only
Confidence intact, silently -- the earlier 32 KB "invalid argument"
rejection was the loud version of the same problem. Plain text cannot be
corrupted that way, and a one-tab-per-file layout is what the reader gets
anyway once a workbook is a Sheet.

Export the tabs with openpyxl (`ws.iter_rows(values_only=True)` -> `csv`),
shortening the long run labels to something a column header can hold
("v7 responses (rescored on v8 rules)", "v8 live run"). Files to publish:

1. **Summary**, **Per-class**, **Confidence** -- from the metrics workbook
   (`--tabs Summary,Per-class,Confidence`).
2. **Adjudicate** -- from `--adjudicate-csv <path>`, slimmed to the
   columns a reviewer fills in (company, name, field, gold, model, conf,
   gold provenance, verdict, notes; show `(rejected)` where the model
   answer is empty).

Keep the full workbook (all tabs, `--out ...-full.xlsx`) on disk beside
them; say where it is. After publishing, `read_file_content` on one of the
new files is a cheap check that the content arrived.

`create_file` parameters (the `.xlsx` route below is kept for reference
only):

- `parentId`: `1O5PTTHtfDFeKiIkRubbLOukRF6xGycmy` -- the
  "Projects / companies-house-leads" folder. If that id ever stops resolving,
  find it with `search_files`:
  `title = 'companies-house-leads' and mimeType = 'application/vnd.google-apps.folder'`.
- `contentMimeType`: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- `base64Content`: the file, base64-encoded. Write it to a file with
  `python -c "import base64,sys;sys.stdout.write(base64.b64encode(open(sys.argv[1],'rb').read()).decode())" <path> > x.b64`,
  Read it, and paste the string. Keep it under ~20 KB (see above).
- Leave `disableConversionToGoogleType` unset so it becomes a Sheet.
- `title`: `Business profile eval - <model(s)> <prompt version> (<date>, <n> cases) - metrics` / `- adjudicate`.

Report the `viewUrl` from the response to the user.

## 3. What to look at first

- Summary: `search-addressable` precision / recall / F1, then each field's
  accuracy against its majority baseline and `accuracy when committed,
  answerable cases` against coverage.
- Confidence: accuracy should rise with the band. If it does not, the
  confidence number cannot be used as a downstream filter.
- Adjudicate: fill in `verdict` for each disagreement. The share that comes
  back "model right" is gold-label noise and bounds the accuracy ceiling.

## Notes

- This replaces the user-level `publish-google-sheet` skill that AGENTS.md
  used to point at; that skill is not present on this machine.
- The connector may not be attached in every session. If `create_file` is
  unavailable, say the local `.xlsx` is ready and give its path, rather than
  leaving publishing unmentioned.
