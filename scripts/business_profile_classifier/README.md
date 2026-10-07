# Business profile extraction (Gate A2)

Reads a company's filed narrative (already extracted from XHTML by
`core/companies_house_pdf_text.py`) and records how it acquires customers:
`demand_model`, `customer_type`, `delivery_model`, `geography_served`, plus
`business_description`, `trading_status_confirmed`, and `sic_agreement`.

Text only — one chat-completion call per company, no vision, no rendering.
Design rationale, the full field taxonomy, and why each design choice was
made live in
[docs/BUSINESS_PROFILE_EXTRACTION.md](../../docs/BUSINESS_PROFILE_EXTRACTION.md).
This file is the quick "how do I run it" reference.

Run the commands below with the repository environment:

```powershell
.\.venv-claude\Scripts\python.exe -m pytest
```

## Why a fabricated quote is rejected, not scored down

The one thing worth understanding before touching this code: every
classification the model returns must carry a quote copied verbatim from the
text it was given. `business_profile_policy.validate_fields` checks
`quote in text` for every non-`unclear` field, and a field whose quote fails
is dropped -- stored as a null value whose `reason` records the rejection --
while the fields that passed stand. That turns "did the model hallucinate"
from a judgement call into a substring check, which is why it is load-bearing
rather than a nice-to-have.

Until 2026-09-14 one failing field rejected the whole response. That was
meant as a signal ("one invented quote, trust nothing"), but two 109-case
runs showed the failures were one-letter drifts in otherwise honest quotes
("principal activit**y**" for "activit**ies**"), and the other fields in
those responses had passed the same check -- the only grounding guarantee
there is. Discarding them cost ~10% of cases and bought nothing. A response
is now rejected outright only when it is not JSON at all; the run report
counts both grades (`responses_rejected_outright`,
`responses_with_dropped_fields`, `fields_rejected`).

`unclear` needs no quote and is a correct answer, not a failure — the
taxonomy exists to be refused when the filed text does not support a
confident call.

## Files

| File | Purpose |
|---|---|
| `business_profile_policy.py` | Taxonomy, prompt template, JSON parsing, quote/enum validation. No I/O. |
| `companies_house_business_profile.py` | Pipeline: read narrative from SQLite, call the model, validate, persist to `company_profiles`. |
| `business_profile_eval.py` | `initialise` builds gold-set case stubs from live data; `run` scores verified cases against a model. |
| `business_profile_review.py` | Local browser tool for hand-labelling case stubs (`expected` block) against the filed narrative. |
| `business_profile_report_sheet.py` | Turns run report JSON(s) into a multi-tab workbook (summary, per-class, confidence bands, per-case, disagreements to adjudicate). Published to Drive by the `publish-eval-sheet` skill. |

## Running it

```bash
# Profile specific companies, or the next N unprofiled companies with narrative (highest turnover first)
python -m scripts.business_profile_classifier.companies_house_business_profile --db companies-house.db \
    --config evals/business_profiles/configs/openrouter-gemini.yaml --company 00482197
python -m scripts.business_profile_classifier.companies_house_business_profile --db companies-house.db \
    --config evals/business_profiles/configs/openrouter-gemini.yaml --limit 20

# Build (or extend) the gold set from live data -- free, no API calls
python -m scripts.business_profile_classifier.business_profile_eval initialise --db companies-house.db --count 50
#   --bias consumer  tilts candidate selection toward retail / hospitality /
#   personal-services SIC divisions, to rebalance a B2B-relationship-heavy gold
#   set toward consumer_search / b2c cases. --sic-prefix 47 (repeatable) is the
#   general form. It tilts, it does not restrict -- the trading_status spread stays.

# Pre-fill the new cases' expected blocks with a model's answers, as DRAFTS to
# check (review.status = "drafted"). Costs one model call per case. Each case is
# written to disk as its call returns; re-running skips drafted/verified cases.
python -m scripts.business_profile_classifier.business_profile_eval draft-labels \
    --config evals/business_profiles/configs/openrouter-gemini.yaml
#   A drafted case is never scored by `run` (verified-only) and lands in the
#   annotation queue as a PENDING item -- model guess pre-filled, for a human to
#   confirm or correct rather than type from scratch.

# Push cases into the Langfuse annotation queue for human labelling -- see
# "Reviewing gold labels in Langfuse" below. Requires the Langfuse instance
# (docs/LANGFUSE_SETUP.md); free, no model calls.
python -m scripts.business_profile_classifier.business_profile_eval sync-annotation-queue \
    --config evals/business_profiles/configs/openrouter-gemini.yaml

# ... review at http://localhost:3000, then pull human answers back into the case files
python -m scripts.business_profile_classifier.business_profile_eval export-annotations \
    --config evals/business_profiles/configs/openrouter-gemini.yaml

# Score a model against the verified subset of the gold set
python -m scripts.business_profile_classifier.business_profile_eval run --config evals/business_profiles/configs/openrouter-gemini-3.7.yaml
#   openrouter-gpt-5.4-mini.yaml is the second-opinion config: the gold was
#   drafted by gemini-3.7-flash, so a gemini-3.7-flash run mostly measures
#   self-agreement. Where a different lineage disagrees with the gold is
#   where to adjudicate.

# Read the result(s) as a spreadsheet; pass two reports to compare runs
python -m scripts.business_profile_classifier.business_profile_report_sheet logs/business-profile-eval/report-<ts>.json
#   ... then publish it to Drive with the `publish-eval-sheet` skill.

# Draft a separate, human-reviewable search-opportunity snapshot from saved
# responses. This does not make model calls or change the historical rule.
python -m scripts.business_profile_classifier.business_profile_search_recall \
    --report logs/business-profile-eval/report-<ts>.json
```

A run leaves three things under `logs/business-profile-eval/`:
`report-<ts>.json` (scores and metrics only), `responses-<ts>/<company>.md`
(one file per case: the validation verdict, the model's JSON verbatim, and
the exact prompt it was sent -- the thing to open when a number looks
wrong), and, in Langfuse, one trace per case with the same text. A
"rejected" case is not malformed JSON; it parsed, and then failed a
validation rule -- a quote that is not verbatim in the filing, or a value
outside the field's list -- so the whole response is discarded rather than
partly trusted.

## Rechecking saved model responses

Classifier JSON is parsed with conservative repair (Markdown fences,
surrounding prose containing one complete object, and trailing commas) before
Pydantic checks its structure. The original response is always retained and
the result records repairs, normalisations, errors, and unexpected fields in a
`validation` object. Source-quote checks remain separate from those structural
checks.

To audit an existing JSONL result or checkpoint without model calls or SQLite
writes, use the shared revalidator. It writes a separate JSONL file and can
take the original source context when it is available:

```bash
python -m scripts.eval_support.revalidate_classifiers \
    --pipeline business-profile --input logs/old-results.jsonl \
    --output logs/business-profile-revalidation.jsonl --context saved-context.jsonl
```

The same command supports `search-screen`, `web-profile`, `website-identity`,
and `vlm`. Without a context file it still checks JSON structure and marks
evidence-dependent checks as `not_checked`.

`business_profile_review.py` (a tiny local HTTP server) is still there for
offline reading of the narrative text and raw JSON, but the Langfuse
annotation queue is the reviewing workflow -- see below. (`sync-review-queue`
/ `export-reviews` remain as hidden aliases.)

## Reviewing gold labels in Langfuse

`sync-annotation-queue` creates one Langfuse trace per case (name:
`<number> <company name> (gold review)`; tags: `company:<number>`; metadata:
`company_number`, `sic_code`, ...; input: the narrative sections a reviewer
needs, output: this session's draft labels),
seeds every field's current draft value as an API-sourced score, and adds
all 57 traces to an annotation queue named **"Business profile gold-label
review"**. Each field is a categorical score config (its taxonomy values);
`business_description` is free text.

A **human-verified** case (`review.status == "verified"`) with a full
`expected` block is marked **complete** on sync -- the queue opens ready to
check, not as a backlog. A **`drafted`** case (model-filled by `draft-labels`,
not yet human-confirmed) is also fully populated but stays **pending**: that
is the backlog to work through, with the model's guess pre-filled.
Open `http://localhost:3000` -> the project -> Annotation Queues ->
"Business profile gold-label review".

### Correcting a label after a case is verified

`export-annotations` never rewrites a verified case, so a label changed in
Langfuse after sign-off is picked up only by

```
python -m scripts.profile.business_profile_eval export-annotations --config <cfg> --corrections
```

which re-reads every verified case's scores and rewrites just the cases
where a value differs from `expected`: the changed field gets the new value
with its evidence nulled, `review.changed_fields` grows to include it,
`review.corrected_at` is set and `reviewed_at` is kept. The `draft` block is
untouched, so the file still shows the model's original guess, the first
review and the correction.

### Finding one company

- **Its gold labels** (to re-check or correct a label): Tracing -> Traces,
  type the company number or name into the search box -- the review trace
  is named `<number> <company name> (gold review)`. Open it and use
  **Annotate** on the right; the queue item is the same object. Traces
  created before 2026-09-14 were all named `business_profile_review`; the
  sync restates them in place (`restate_trace`: one extra root-level
  `identity` span carrying the name, tags, metadata and the case's current
  input/output, which is how a trace is updated in Langfuse v4's
  events-only storage), so nothing needs recreating. The same happens
  whenever a case's text or draft changes, so the trace a reviewer opens
  always shows the current filing text, not the window it was made with.
- **Its result in an experiment run**: Datasets -> `business-profile-gold`
  -> Items, where the item id *is* the company number; the item page shows
  the company's output in every run side by side. In the Experiments
  compare view, the left-hand **Item Metadata** filter with
  `company_number = <number>` narrows the table to one company (the
  lower **Metadata** filter is the run's own metadata -- prompt version and
  model -- and cannot select a company; the "Search experiments" box only
  searches run names).

### Draft vs human, and how a case gets verified

Each draft is seeded as a **score with source `ANNOTATION`** and a
`config_id` -- that is the only kind of score Langfuse pre-selects the
annotate-panel dropdowns from (an `API`-source score shows only as a
read-only eval score, leaving the form blank). So the queue opens with the
model's guess already filled in; the reviewer changes what is wrong and
leaves what is right.

The annotate panel edits the seeded score **in place** -- same score id,
comment still saying "draft" -- so a seed and a reviewer's answer are the
same row and can't be told apart per-field. "Has this case been reviewed"
is answered at the **whole-case** level: the reviewer marks the queue item
**Complete**. `export-annotations` imports a case only when its queue item
is COMPLETED. Until then the case stays `drafted` / `unreviewed` and is
never counted as ground truth by `... eval run`.

What export writes (`apply_annotations`): the model's draft moves, untouched,
to a `draft` block (`draft.expected`, `draft.drafted_by`); the reviewer's
values go into `expected`; `review.changed_fields` lists every field the
reviewer overrode. A field the reviewer kept carries the draft's quote and
confidence through as its evidence; a field they changed gets the new value
with `quote`/`section`/`confidence` null, because the draft's quote argued
for the old value. `git diff` on an export is therefore the review's
decisions, and the case file -- not Langfuse -- is the durable record.

`sync-annotation-queue` is idempotent and **never overwrites a score**: it
tracks the trace per case in `logs/business-profile-eval/annotation-traces.json`
and seeds a draft only for a field that has no score yet. This matters
because of what happened on 2026-09-09: an earlier version re-seeded every
field on every run, and a routine re-sync put the on-disk drafts back over
two days of review edits made in the UI. ClickHouse had merged the old
versions away; they were not recoverable.

**This does not start a second Langfuse instance.** The `langfuse:` block in
the config selects a key pair from `.env`; it points at the one instance
every stage in this repo talks to (`docs/LANGFUSE_SETUP.md`). If it is not
running, `sync-annotation-queue` / `export-annotations` fail to connect
rather than launching one.

## Gold-set case shape

```json
{
  "company_number": "00482197",
  "company_name": "CAMBRIDGE UNITED FOOTBALL CLUB LIMITED",
  "sic_code": "93110",
  "sic_label": "Sport / fitness / gyms",
  "sections": {"principal_activity": "...", "strategic_report": "...", "..."},
  "expected": {
    "demand_model": {"value": null, "quote": null, "section": null},
    "...": "..."
  },
  "review": {"status": "unreviewed", "reviewed_at": null}
}
```

An `expected` block deliberately carries **no `reason`**, even though a model
response must. `reason` is the model's rationale for reaching a value;
`expected` is the reviewer's ground truth for what the value should be, and
scoring reads only `value` and `confidence`. `validate_expected_block` fills a
placeholder so the shape check passes — don't "fix" the case files by adding
one. For the same reason gold `sic_agreement` blocks carry no `quote`: they
predate v6 and cannot get one without re-reading every filing, so the review
path passes `require_sic_quote=False` while model responses stay held to it.

`sections` is, for 108 of 109 cases, the whole filed document minus the
auditor's report as one `filed_report` section (`filed_report_text` in
`core/companies_house_extractor.py`, built by
`business_profile_refresh_sections --whole-document` from the raw XHTML
cached by `save_raw_filings.py`). A filing that only exists as a scanned PDF
(08029548 SMART CURRENCY GROUP, the original pilot company, files nothing
else) has no XHTML: `save_raw_filings.py` reports it as `pdf_only` and saves
the PDF under `data/raw/business-profile-pdf/`, the transcription harness
(`scripts/vlm/companies_house_pdf_transcribe.py`, see `scripts/vlm/README.md`)
turns it into `<company>.filed_report.txt`, and the refresh picks that file
up when there is no `.xhtml`. `sections` is a snapshot taken at `initialise` time, not a live pointer —
re-run `initialise` after a narrative re-extraction to refresh it (this
happened once already: the gold set was rebuilt after fixing the iXBRL
header leak and auditor-boilerplate bugs in
`core/companies_house_pdf_text.py`, since 9 of the first 49 cases had
corrupted text from before that fix).

`expected` mirrors the shape of a real model response, checked by the same
`validate_response` the pipeline uses (see
`business_profile_review.validate_expected_block`) — a verified case with a
quote that does not actually appear in its section is rejected on save, the
same as a bad model response would be. Leaving a field's `value` as `null`
after review is a legitimate label: it means the text genuinely does not
support a confident call, and that case will not count toward that field's
accuracy score (see `score_case` — only reviewed fields are scored).

## Selecting gold-set candidates

`select_candidate_companies` round-robins across Gate A `trading_status`
buckets and prefers an unseen SIC group within each pick, so a 50-case set
does not end up mostly ordinary trading companies. A gold set skewed that
way would never exercise `unclear`, `investment_holding`, or `spv` — the
ambiguous cases this stage exists to resolve.
