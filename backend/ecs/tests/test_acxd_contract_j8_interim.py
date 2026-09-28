"""J8: journey tools carry no interim messages.

Live (2026-09-28, Connect chat through the Agentic CX block, same Hanbit bundle):
the identity turn calls `findPatient`. Both builds whose tool sent an interim
message ("환자 정보를 확인하고 있어요…") ended that turn in the block's Error branch
although the application answered in about 8 s; the bundle with only the
interim messages removed answered the same turn twice (7 s and 9 s).
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from prompts.acxd_contract_fragments import generative_journey_guidance  # noqa: E402
from test_acxd_runtime_contract import _apply_journey, _journey_flow  # noqa: E402

_INTERIM = [{"text": "확인하고 있어요. 잠시만 기다려 주세요.", "delay": 2}]


def test_interim_messages_are_removed_from_every_journey_tool():
    tools = [
        {"type": "dataRequest", "interimMessages": _INTERIM,
         "dataRequest": {"dataRequestId": "requestReturn",
                         "payload": {"orderNumber": "{orderNumber:NLX.Slot}",
                                     "contactPhone": "{contactPhone:NLX.Slot}"}}},
        {"type": "knowledgeBase", "knowledgeBaseId": "{KB:FAQ}", "scopeTags": [],
         "interimMessages": _INTERIM},
    ]
    out, notes = _apply_journey(_journey_flow(journey_cfg={"tools": tools}))
    cfg = out["nodes"]["gj"]["metadata"]["generativeJourney"]
    assert cfg["tools"], "the tools themselves stay"
    assert all("interimMessages" not in t for t in cfg["tools"])
    request = next(t for t in cfg["tools"] if t["type"] == "dataRequest")
    assert request["dataRequest"]["dataRequestId"] == "requestReturn"
    assert sum("interimMessages removed" in n and "(J8)" in n for n in notes) == 2
    again, notes_again = _apply_journey(out)
    assert again == out and not any("interimMessages" in n for n in notes_again)


def test_the_generator_is_told_not_to_add_interim_messages():
    guidance = generative_journey_guidance()
    assert "Do NOT add `interimMessages`" in guidance
    assert "for a call that takes a moment" not in guidance
