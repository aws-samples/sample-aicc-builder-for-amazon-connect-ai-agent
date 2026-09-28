"""WIRING-GUIDE.md of an ACXD-only bundle (the customer wires their own Contact Flow).

Live facts it has to carry: the contact's language must be set before the
Agentic CX block (without it every contact failed with "NLX Chat Streaming
Failed", 2026-09-14), and the block's alias list names the deployment
`Production` even when deploy.sh deployed to the development environment
(sandbox, 2026-09-28).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.asset_packager import _generate_acxd_only_wiring_guide  # noqa: E402


def test_the_language_is_set_before_the_block_and_the_alias_is_named_as_acxd_shows_it():
    guide = _generate_acxd_only_wiring_guide({
        "application": {"name": "hanbit-booking", "settings": {"languageCode": "ko-KR", "languageCodes": ["ko-KR"]}},
        "context_variables": [{"name": "patientName"}]})
    assert "ships no Contact Flow" in guide
    assert "`UpdateContactData` action" in guide and "`LanguageCode: ko-KR`" in guide
    assert guide.index("set the contact's language") < guide.index("Add the **Agentic CX** block")
    assert "`Production`" in guide and "development environment" in guide
    assert "**Confirm**" in guide and "Interim messages" in guide
    assert "five" not in guide
    assert "`patientName`" in guide


def test_the_branches_and_a_redeploy_test_are_described_as_seen_live():
    # Live (2026-09-28, separate prompts per branch): a goodbye left through Default,
    # stored as NoMatchingCondition; an agent request or emergency through Escalation.
    # A chat that had not ended stayed on the build it started with, so the guide
    # sends the reader to a new contact instead of blaming the service.
    guide = _generate_acxd_only_wiring_guide({
        "application": {"name": "hanbit-booking", "settings": {"languageCode": "ko-KR"}},
        "context_variables": []})
    assert "`NoMatchingCondition`" in guide and "`NoMatchingError`" in guide
    assert "Testing a change" in guide and "greets you again" in guide
    assert "replaced build 45 minutes" not in guide
    assert "Error branch" not in guide


def test_an_application_without_a_language_gets_a_visible_placeholder():
    guide = _generate_acxd_only_wiring_guide({"application": {"name": "x"}, "context_variables": []})
    assert "<the application's language code>" in guide
