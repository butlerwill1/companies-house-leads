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

# Label it by hand, reading the same narrative sections the model would see
python -m scripts.profile.business_profile_review --cases-dir evals/business_profiles/cases

# Score a model against the verified subset of the gold set
python -m scripts.profile.business_profile_eval run --config evals/business_profiles/configs/openrouter-gemini.yaml
```

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
