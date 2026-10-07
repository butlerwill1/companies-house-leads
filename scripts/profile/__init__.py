"""Compatibility namespace for the renamed business-profile classifier.

New code imports :mod:`scripts.business_profile_classifier`.  Keeping this
small forwarding package lets saved commands and third-party callers complete
the rename without duplicating the implementation.
"""

from __future__ import annotations

import importlib
import sys

_MODULES = (
    "business_profile_confidence_check",
    "business_profile_eval",
    "business_profile_metrics",
    "business_profile_policy",
    "business_profile_prompt_registry",
    "business_profile_refresh_sections",
    "business_profile_report_sheet",
    "business_profile_review",
    "business_profile_search_recall",
    "companies_house_business_profile",
    "save_raw_filings",
)

for _name in _MODULES:
    _module = importlib.import_module(f"scripts.business_profile_classifier.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module
