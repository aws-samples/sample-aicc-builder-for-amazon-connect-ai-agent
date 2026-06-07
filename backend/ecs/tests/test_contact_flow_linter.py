"""Tests for the Contact Flow structural / import-safety linter.

Covers the hallucinated-block regressions that broke Amazon Connect import:
  - `Trigger` / `EntryPoint` entry wrappers (no such block exists)
  - `InvokeAgentAction` (→ ConnectParticipantWithLexBot, with param rewrite)
  - `CheckCondition` (→ Compare)
  - `RealTime` in Voice AnalyticsModes
and asserts valid flows are left untouched (no false positives).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.asset_linters import lint_contact_flow  # noqa: E402


def _valid_flow():
    return {
        "Version": "2019-10-30",
        "StartAction": "log",
        "Actions": [
            {"Identifier": "log", "Type": "UpdateFlowLoggingBehavior",
             "Parameters": {"FlowLoggingBehavior": "Enabled"},
             "Transitions": {"NextAction": "end"}},
            {"Identifier": "end", "Type": "DisconnectParticipant",
             "Parameters": {}, "Transitions": {}},
        ],
    }


def test_valid_flow_passes_untouched():
    r = lint_contact_flow(json.dumps(_valid_flow()))
    assert r["ok"] is True
    assert r["fixes_applied"] == []
    assert r["errors"] == []


def test_trigger_block_is_removed_and_startaction_repointed():
    flow = {
        "Version": "2019-10-30",
        "StartAction": "entry-point",
        "Actions": [
            {"Identifier": "entry-point", "Type": "Trigger", "Parameters": {},
             "Transitions": {"NextAction": "log"}},
            {"Identifier": "log", "Type": "UpdateFlowLoggingBehavior",
             "Parameters": {"FlowLoggingBehavior": "Enabled"},
             "Transitions": {"NextAction": "end"}},
            {"Identifier": "end", "Type": "DisconnectParticipant",
             "Parameters": {}, "Transitions": {}},
        ],
    }
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    assert fixed["StartAction"] == "log"
    assert all(a["Type"] != "Trigger" for a in fixed["Actions"])


def test_checkcondition_renamed_to_compare():
    flow = _valid_flow()
    flow["Actions"].insert(1, {
        "Identifier": "branch", "Type": "CheckCondition",
        "Parameters": {"ComparisonValue": "$.Channel"},
        "Transitions": {"NextAction": "end",
                        "Conditions": [{"NextAction": "end",
                                        "Condition": {"Operator": "Equals", "Operands": ["VOICE"]}}],
                        "Errors": [{"ErrorType": "NoMatchingCondition", "NextAction": "end"}]},
    })
    flow["Actions"][0]["Transitions"]["NextAction"] = "branch"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    types = {a["Type"] for a in fixed["Actions"]}
    assert "CheckCondition" not in types
    assert "Compare" in types


def test_invoke_agent_action_rewritten_to_lex_bot():
    flow = _valid_flow()
    flow["Actions"].insert(1, {
        "Identifier": "ai", "Type": "InvokeAgentAction",
        "Parameters": {"AgentAliasArn": "{{AgentAliasArn}}",
                       "IdleSessionTimeout": "300",
                       "EndConversationPhrase": "Goodbye"},
        "Transitions": {"NextAction": "end",
                        "Errors": [{"ErrorType": "NoMatchingError", "NextAction": "end"}]},
    })
    flow["Actions"][0]["Transitions"]["NextAction"] = "ai"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    ai = next(a for a in fixed["Actions"] if a["Identifier"] == "ai")
    assert ai["Type"] == "ConnectParticipantWithLexBot"
    # invalid params dropped, valid Lex params present
    assert "AgentAliasArn" not in ai["Parameters"]
    assert "IdleSessionTimeout" not in ai["Parameters"]
    assert ai["Parameters"]["LexV2Bot"]["AliasArn"] == "{{AgentAliasArn}}"
    assert ai["Parameters"]["Text"] == "Goodbye"


def test_realtime_stripped_from_voice_analytics():
    flow = _valid_flow()
    flow["Actions"].insert(1, {
        "Identifier": "rec", "Type": "UpdateContactRecordingBehavior",
        "Parameters": {"AnalyticsBehavior": {"ChannelConfiguration": {
            "Voice": {"AnalyticsModes": ["RealTime", "PostContact"]}}}},
        "Transitions": {"NextAction": "end"},
    })
    flow["Actions"][0]["Transitions"]["NextAction"] = "rec"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is True, r["errors"]
    fixed = json.loads(r["fixed_json"])
    rec = next(a for a in fixed["Actions"] if a["Identifier"] == "rec")
    modes = rec["Parameters"]["AnalyticsBehavior"]["ChannelConfiguration"]["Voice"]["AnalyticsModes"]
    assert "RealTime" not in modes
    assert modes == ["PostContact"]


def test_truly_unknown_type_is_hard_error():
    flow = _valid_flow()
    flow["Actions"][0]["Type"] = "TotallyMadeUpBlock"
    r = lint_contact_flow(json.dumps(flow))
    assert r["ok"] is False
    assert any("TotallyMadeUpBlock" in e for e in r["errors"])
