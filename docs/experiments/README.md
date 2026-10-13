# Experiments: results and lessons

Evidence reviewed through **2026-10-05**. This collection summarises existing
experiments and saved results; creating it involved no new model, search-API
or GPU run.

The project developed from extracting company accounts into a lead-research
funnel. These reviews explain the experiments behind that change: which
questions could be answered from filings, which needed website evidence,
which data sources proved useful, and where apparently good scores were
misleading.

## The reviews

| Review | Main question | Strongest finding | What remains uncertain |
|---|---|---|---|
| [PDF financial extraction](financial-extraction.md) | Can vision models reliably recover figures from PDF-only accounts? | Later 50-document runs reached 568/572 expected core financial values correct (99.30%). | Employee evidence remained weaker; high cell accuracy did not mean every document was fully correct. |
| [Filing-based business classification](filing-business-classification.md) | Can accounts explain the business and its customer-acquisition model? | Taxonomy, source preparation and validation materially affected results; acquisition-channel inference remained unreliable. | Minority-class performance and repeatability were insufficient for a confident exclusion rule. |
| [Search screening](search-screen.md) | Can a cheap first pass retain plausible search prospects while removing unsuitable companies? | Short extracts offered a useful cost tradeoff; stricter rejection improved removal but lost real prospects. | The population removal rate did not establish recall, and the latest population run had no repeatability check. |
| [Website discovery and technology](website-discovery-and-technology.md) | Can the right website be identified and its marketing setup observed? | Cheaper places-first lookup preserved most pilot choices; wider crawls and conservative checks resolved additional companies. | Chosen sites and model verdicts are not equivalent to independently verified identity accuracy. |
| [Website classification and advertising](website-classification-and-advertising.md) | What can the site and search APIs tell us about PPC fit and opportunity? | Ads Transparency detected substantially more advertisers than paid-traffic estimates in the pilot. | Website-label agreement was still measured against drafts; phrase style strongly affected demand estimates. |
| [Lessons and decisions](lessons-and-decisions.md) | What should carry forward across stages? | Separate observations, classifications and commercial decisions, and inspect failures before changing the model. | Prospect quality still needs validation against contact and sales outcomes. |

## How to read the evidence

The reviews distinguish four kinds of result:

- **Reviewed reference results:** comparisons against labels checked by a
  reviewer, with the sample and metric stated.
- **Draft-label agreement:** agreement with model-proposed labels awaiting
  review. This is a diagnostic, not established accuracy.
- **Pilot or population observations:** what happened on the companies or
  sites processed. Coverage and rejection rates are not accuracy measures.
- **Rescoring:** a different interpretation of already saved responses,
  without a new model call. It can reveal a validator or scoring improvement
  but cannot demonstrate a better model generation.

```mermaid
flowchart LR
    Q["Research question"] --> E["Existing cases and experiment"]
    E --> R["Results and individual failures"]
    R --> L["Lesson with stated limits"]
    L --> D["Adopt, reject or investigate"]
    D --> O["Open questions and next evidence"]
```

Every detailed review follows that progression. Counts and denominators are
included where available, and results are attached to dates and versions
rather than presented as a timeless score for the whole project.

## Current conclusions

The financial pipeline can perform well on the reviewed benchmark, but
employee extraction and unusual statements need separate attention. Accounts
are useful for describing a business; they often say much less about how
customers find it. That led to the separate search screen and then the web
stage, rather than continually broadening the filing classifier.

Website research adds identity, customer journeys, detectable tools and
advertising evidence. It also introduces new uncertainty: brand names differ
from legal names, blocked sites hide evidence, category definitions overlap,
and generated search phrases can change the apparent size of a market.

The lead sheet brings this evidence together. Its sorting rules and gap
segments are not yet a demonstrated prediction of sales success. Outcome
feedback is the missing test of the funnel's commercial value.

## Sources and maintenance

The linked reviews identify the design notes and saved reports supporting
each result. Files under `logs/` are local and gitignored: their links work in
the research workspace but may be absent from a fresh clone. The prose and
tables here preserve the principal findings without committing raw responses,
downloaded filings, databases or bulk output.

Older notes sometimes refer to MLflow. That is historical terminology; the
project migrated to Langfuse on 2026-10-05. Existing run identifiers are
retained as evidence references, not instructions to use the retired server.
See [the tracking migration](../LANGFUSE_SETUP.md#relationship-to-mlflow).

When adding a result, retain the experiment date, model and prompt or rule
version, sample and label status, metric definition, cost basis, and source
report. A changed label set or scoring rule needs an explicit comparability
note. Keep proposals separate from completed experiments.
