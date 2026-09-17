"""Reviewer summary counts must equal the number of findings, not emoji occurrences."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agents.reviewer_agent.agent import count_findings  # noqa: E402

REPORT = """## Summary
- **Critical (❌)**: 2 — blocks deployment
- **Warning (⚠️)**: 2 — should fix

| Asset Type | Status | ❌ | ⚠️ | 💡 |
|---|---|---|---|---|
| OpenAPI | ⚠️ Issues | 0 | 2 | 0 |
| CloudFormation | ❌ Issues | 1 | 0 | 0 |
| Cross-Asset Consistency | ❌ Issues | 1 | 0 | 0 |

### ⚠️ OpenAPI Spec Review — 2 WARNINGS
- ⚠️ Missing `x-amazon-connect-tool-description` for /check-availability
- ⚠️ Response schema missing for POST /reservations

### ❌ Cross-Asset Consistency — 1 CRITICAL
- ❌ FIELD MISMATCH: Lambda uses `phone_number`, OpenAPI uses `phoneNumber`

### ❌ CloudFormation Review — 1 CRITICAL
- ❌ GSI `phone-index` defined but Lambda references `phone_index`

## Action Items
1. ❌ **CRITICAL** — Fix `phone_number` → `phoneNumber`
2. ❌ **CRITICAL** — Fix GSI name `phone_index` → `phone-index`
3. ⚠️ **WARNING** — Add x-amazon-connect-tool-description
4. ⚠️ **WARNING** — Add response schema for POST /reservations
"""


def test_counts_findings_once_despite_headers_tables_and_action_list():
    assert REPORT.count("❌") == 10  # what the old code reported
    assert count_findings(REPORT, "❌") == 2
    assert count_findings(REPORT, "⚠️") == 2


def test_falls_back_to_raw_count_for_free_form_text():
    assert count_findings("❌ one ❌ two", "❌") == 2
