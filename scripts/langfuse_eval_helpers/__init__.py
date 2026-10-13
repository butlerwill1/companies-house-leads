"""Shared Langfuse + DeepEval plumbing for the eval harnesses.

Both `scripts/business_profile_classifier/business_profile_eval.py` and
`scripts/pdf_vision_extraction/vlm_financial_eval.py` log traces, dataset runs, annotation
queues and scores to one self-hosted Langfuse instance (see
docs/LANGFUSE_SETUP.md). This package holds the code they share so neither
harness reinvents it and the two never drift.

Every `langfuse` / `deepeval` import in this package is function-local: the
test suite and the non-eval code paths must import these modules without
either third-party package installed, exactly as the old MLflow code did.
"""
