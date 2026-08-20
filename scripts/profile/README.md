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

## Why a fabricated quote is rejected, not scored down

The one thing worth understanding before touching this code: every
classification the model returns must carry a quote copied verbatim from the
section it claims to support. `business_profile_policy.validate_response`
checks `quote in section_text` for every non-`unclear` field and rejects the
*entire* response if any quote fails — there is no partial acceptance. That
turns "did the model hallucinate" from a judgement call into a substring
check, which is why it is load-bearing rather than a nice-to-have.

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

## Running it

```bash
# Profile specific companies, or the next N unprofiled companies with narrative (highest turnover first)
python -m scripts.profile.companies_house_business_profile --db companies-house.db \
    --config evals/business_profiles/configs/openrouter-gemini.yaml --company 00482197
python -m scripts.profile.companies_house_business_profile --db companies-house.db \
    --config evals/business_profiles/configs/openrouter-gemini.yaml --limit 20

# Build (or extend) the gold set from live data -- free, no API calls
python -m scripts.profile.business_profile_eval initialise --db companies-house.db --count 50

# Push cases into MLflow's review queue for human labelling -- see "Reviewing
# gold labels in MLflow" below. Requires an MLflow tracking server; free, no
# model calls.
python -m scripts.profile.business_profile_eval sync-review-queue \
    --config evals/business_profiles/configs/openrouter-gemini.yaml

# ... review at http://127.0.0.1:5000, then pull human answers back into the case files
python -m scripts.profile.business_profile_eval export-reviews \
    --config evals/business_profiles/configs/openrouter-gemini.yaml

# Score a model against the verified subset of the gold set
python -m scripts.profile.business_profile_eval run --config evals/business_profiles/configs/openrouter-gemini.yaml
```

`business_profile_review.py` (a tiny local HTTP server, separate from
MLflow) is still there for offline reading of the narrative text and raw
JSON without a tracking server running, but MLflow is the reviewing
workflow -- see below.

## Reviewing gold labels in MLflow

`sync-review-queue` creates one MLflow trace per case (tags:
`eval.company_number`, `eval.sic_code`, ...; inputs: the narrative sections
a reviewer needs), pre-fills every field with this session's draft value,
and adds all 47 traces to a review queue named **"Business profile
gold-label review"** in the `companies-house-business-profile-eval`
experiment. Each field is a dropdown of its allowed taxonomy values
(`mlflow.genai.label_schemas.InputCategorical`), not free text, so
confirming a correct draft is one click.

Every item is marked **complete** as soon as it is synced, since it already
carries a full set of draft answers -- the queue opens showing 47/47 done,
ready to check rather than to work through as a backlog. Marking an item
complete does not lock it: open one, and every field is still an editable
dropdown. Open `http://127.0.0.1:5000` -> Experiments ->
`companies-house-business-profile-eval` -> Review -> "Business profile
gold-label review" to go through them.

### Why drafts are written as HUMAN-sourced

Non-obvious, and worth knowing before changing `_seed_draft_expectations`:
**MLflow's Review UI only renders a pre-filled answer for an expectation
whose source is `source_type=HUMAN` *and* whose `source_id` equals the
viewer's own identity.** An `LLM_JUDGE`-sourced expectation is not shown at
all -- the dropdown renders an empty "Select an option" no matter what the
trace actually contains. (The relevant filter lives in the frontend bundle's
review-item hook; there is no server-side setting for it.) On a no-auth
local server that identity is `default`, which `_reviewer_identity` recovers
by reading the name of the auto-created USER-type review queue.

So a draft has to be written under the reviewer's identity to be visible.
That means **source type can no longer distinguish a draft from a real
human judgement**, and each seeded draft instead carries the metadata marker
`draft_source: claude-opus-5`. `export-reviews` keys off that marker, not
the source type: a field counts as genuinely reviewed only when its
expectation lacks the marker. A case is marked `review.status = "verified"`
only once every field is marker-free, so scoring (`... eval run`) still
never counts an unconfirmed draft as ground truth.

Re-running the sync updates each existing draft in place and leaves any
reviewer-entered answer untouched. Duplicate same-named expectations must be
avoided -- they make the UI's per-field lookup ambiguous and it falls back
to rendering the field empty.

**Re-running `sync-review-queue` is safe and idempotent.** It looks up the
existing trace for a company by its `eval.company_number` tag before
creating a new one, so running it again after `initialise` adds more cases
does not duplicate the 47 already there.

**This does not start a second MLflow instance.** `tracking_uri` in the
config (`http://127.0.0.1:5000`, same as `evals/vlm_financials/configs/`)
is the one tracking server every stage in this repo talks to; a new
*experiment* name is just a namespace inside it, not a new server. If that
server is not running, `sync-review-queue` and `export-reviews` fail to
connect rather than launching one -- start it the same way you already do
for the VLM financial review queue.

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

`sections` is a snapshot taken at `initialise` time, not a live pointer —
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
