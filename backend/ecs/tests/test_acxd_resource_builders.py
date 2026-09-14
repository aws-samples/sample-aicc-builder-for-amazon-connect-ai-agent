"""Task 9 tests: KB / Guardrail / Application builders."""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_resource_builders import (  # noqa: E402
    build_application,
    build_guardrails,
    build_kb_article_prompt,
    build_knowledge_base,
    build_slot_types,
    run_kb_article_generation,
)
from tools.validate_acxd_flow import validate_acxd_asset  # noqa: E402
from tools.validate_acxd_consistency import validate_acxd_consistency  # noqa: E402


SPEC = {
    "business_profile": {"company_name": "Acme", "industry": "e-commerce",
                         "description": "Online electronics store"},
    "flows": [
        {"flow_id": "RefundFlow", "purpose": "refunds", "role": "operation"},
        {"flow_id": "WelcomeFlow", "purpose": "greeting", "role": "welcome"},
        {"flow_id": "EscalationFlow", "purpose": "handoff", "role": "escalation"},
        {"flow_id": "FaqFlow", "purpose": "faq", "role": "unknown"},
    ],
    "guardrails": [
        {"name": "PII Filter", "trigger": "output",
         "policy": "mask credit card numbers",
         "detection_method": "regex", "examples": [r"[0-9]{16}"],
         "action": "mask"},
        {"name": "Abuse Filter", "trigger": "input",
         "policy": "route abusive customers to a human",
         "detection_method": "auto", "examples": ["idiot", "stupid"],
         "action": "route", "route_flow_id": "EscalationFlow"},
        {"name": "Legal Advice", "trigger": "output",
         "policy": "never give legal advice", "detection_method": "auto",
         "action": "modify", "message": "I can't advise on legal matters."},
    ],
    "knowledge_base": {
        "name": "Acme FAQ!",  # '!' must be stripped (KB name charset)
        "articles": [
            {"question": "How do I return an item?",
             "answer": "Within 30 days via the returns portal.",
             "tags": ["returns", "policy"]},
            {"question": "incomplete", "answer": ""},  # dropped
        ],
    },
    "application": {
        "name": "Acme Assistant",
        "languages": ["en-US", "ko-KR"],
        "primary_language": "ko-KR",
        "voice": "Seoyeon",
        "conversation_ttl_minutes": 10,
        "default_flows": {"welcome": "WelcomeFlow"},
    },
    "deployment": {"environment": "staging", "backend_mode": "mock"},
}


# ---------------------------------------------------------------------------
# knowledge base
# ---------------------------------------------------------------------------

def test_kb_document_valid_and_sanitized():
    kb = build_knowledge_base(SPEC)
    assert validate_acxd_asset("knowledge_base", kb) == []
    assert kb["name"] == "Acme FAQ"                      # '!' stripped
    assert len(kb["articles"]) == 1                      # incomplete dropped
    art = kb["articles"][0]
    assert art["question"]["text"].startswith("How do I return")
    assert art["responses"][0]["body"].startswith("Within 30 days")
    assert art["tags"] == ["returns", "policy"]


def test_kb_none_when_not_planned():
    assert build_knowledge_base({"business_profile": {}}) is None


def test_kb_article_generation_harness():
    prompt_seen = {}

    def invoke(prompt):
        prompt_seen["p"] = prompt
        return ('Here you go:\n[{"question": "Q1?", "answer": "A1.", '
                '"tags": ["t"]}, {"question": "", "answer": "x"}]')

    articles, problems = run_kb_article_generation(
        ["returns", "shipping"], SPEC, invoke)
    assert "returns" in prompt_seen["p"] and "Acme" in prompt_seen["p"]
    assert articles == [{"question": "Q1?", "answer": "A1.", "tags": ["t"]}]
    assert problems == ["article[1] missing question/answer"]

    articles, problems = run_kb_article_generation(["x"], SPEC, lambda p: "no json")
    assert articles == [] and problems


# ---------------------------------------------------------------------------
# guardrails
# ---------------------------------------------------------------------------

def test_guardrails_detection_and_enforcement_mapping():
    docs, problems = build_guardrails(SPEC)
    assert problems == []
    by_name = {d["name"]: d for d in docs}
    for doc in docs:
        assert validate_acxd_asset("guardrail", doc) == []

    pii = by_name["PII Filter"]["rules"][0]
    assert pii["detection"] == {"method": "regex", "pattern": r"[0-9]{16}"}
    assert pii["enforcement"]["action"] == "mask"

    abuse = by_name["Abuse Filter"]["rules"][0]
    assert abuse["detection"]["method"] == "keyword"          # auto → keyword
    assert abuse["detection"]["keywords"] == ["idiot", "stupid"]
    assert abuse["enforcement"] == {"action": "route",
                                    "behavior": {"flowId": "EscalationFlow"}}

    legal = by_name["Legal Advice"]["rules"][0]
    assert legal["detection"]["method"] == "llmJudge"         # auto, no examples
    assert "legal advice" in legal["detection"]["prompt"]
    assert legal["enforcement"]["behavior"]["message"].startswith("I can't advise")


def test_guardrail_route_without_target_is_repaired_not_rejected():
    """Rejecting blocked the whole step forever.

    Live: one unusable guardrail made generate_acxd_guardrails return an error
    and store NOTHING, on every retry, because the caller only saved documents
    when the problem list was empty. The spec kept the bad entry, so the agent
    could not proceed and said so ("도구 버그로 막혀 있다").
    """
    # An escalation flow exists → route there.
    spec = {"flows": [{"flow_id": "EscalationFlow", "role": "escalation"}],
            "guardrails": [{"name": "X", "policy": "p", "action": "route"}]}
    docs, problems = build_guardrails(spec)
    assert [d["name"] for d in docs] == ["X"], problems
    assert problems == []

    # No escalation flow → drop the route action rather than fail.
    spec_no_esc = {"flows": [], "guardrails": [
        {"name": "X", "policy": "p", "action": "route"}]}
    docs2, problems2 = build_guardrails(spec_no_esc)
    assert [d["name"] for d in docs2] == ["X"], problems2
    assert problems2 == []


def test_nameless_guardrail_gets_a_derived_name():
    """A guardrail's identity is its policy; the label can be derived."""
    spec = {"guardrails": [
        {"policy": "never promise a refund", "trigger": "output"},
        {"policy": "금액 임의 안내 금지", "trigger": "output"},   # no ASCII to derive from
    ]}
    docs, problems = build_guardrails(spec)
    assert problems == []
    names = [d["name"] for d in docs]
    assert names[0] == "neverPromiseARefund"
    assert names[1] == "guardrail2"          # positional fallback
    assert all(d["rules"][0]["name"] == d["name"] for d in docs)


def test_one_bad_entry_does_not_discard_the_good_ones():
    spec = {"guardrails": [
        {"name": "pii", "policy": "mask card numbers", "trigger": "output"},
        "garbage",
    ]}
    docs, problems = build_guardrails(spec)
    assert [d["name"] for d in docs] == ["pii"]
    assert any("not an object" in p for p in problems)


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

def test_application_document():
    app = build_application(SPEC)
    assert validate_acxd_asset("application", app) == []
    assert app["name"] == "Acme Assistant"
    # FollowUpFlow and RequestAgentFlow are emitted by the flow generator whether
    # or not the interview planned them (R2/R3), so the application must attach
    # them too — an unattached flow is neither routable nor redirectable.
    assert {f["flowId"] for f in app["flows"]} == {
        "RefundFlow", "WelcomeFlow", "EscalationFlow", "FaqFlow",
        "FollowUpFlow", "RequestAgentFlow"}

    s = app["settings"]
    assert s["languageCode"] == "ko-KR"
    # voice / useNativeLanguage are silently dropped by the service, so the
    # bundle must not claim to set them (TTS is a Connect "Set voice" block).
    assert "voice" not in s["languageSettings"][0]
    assert "useNativeLanguage" not in s["languageSettings"][0]
    assert "childDirected" not in s
    assert s["conversationTTL"] == 10
    # explicit mapping + role-based fills
    assert s["defaultFlows"]["welcome"]["flowId"] == "WelcomeFlow"
    assert s["defaultFlows"]["escalation"]["flowId"] == "EscalationFlow"
    assert s["defaultFlows"]["unknown"]["flowId"] == "FaqFlow"
    # KB placeholder attached to 'unknown'
    assert s["defaultFlows"]["unknown"]["knowledgeBaseId"] == "{KB:Acme FAQ}"
    # guardrail placeholder refs
    assert {g["guardrailId"] for g in s["guardrails"]} == {
        "{GUARDRAIL:PII Filter}", "{GUARDRAIL:Abuse Filter}",
        "{GUARDRAIL:Legal Advice}"}
    assert app["deploymentSettings"]["environment"] == "staging"


def test_application_ttl_clamped_and_defaults():
    spec = {"business_profile": {"company_name": "Acme"},
            "application": {"conversation_ttl_minutes": 999}, "flows": []}
    app = build_application(spec)
    assert app["settings"]["conversationTTL"] == 60
    assert app["name"] == "Acme Assistant"
    assert app["settings"]["languageCode"] == "en-US"


# ---------------------------------------------------------------------------
# cross-consistency: everything built from one spec hangs together
# ---------------------------------------------------------------------------

def test_full_generated_set_is_cross_consistent():
    guardrails, _ = build_guardrails(SPEC)
    from tools.acxd_system_flows import build_follow_up_flow, build_request_agent_flow

    bundle = {
        "flows": [  # minimal generated flows standing in for Task 6 output
            {"flowId": p["flow_id"], "nodes": {
                "a0000000-0000-4000-8000-000000000001": {"nodeId": "a0000000-0000-4000-8000-000000000001", "type": "start",
                            "childNodes": [{"nodeId": "a0000000-0000-4000-8000-000000000002"}]},
                "a0000000-0000-4000-8000-000000000002": {"nodeId": "a0000000-0000-4000-8000-000000000002", "type": "end"},
            }} for p in SPEC["flows"]
        # ...plus the two system flows the generator always emits, which is why
        # build_application attaches them.
        ] + [build_follow_up_flow(SPEC), build_request_agent_flow(SPEC)],
        "slot_types": build_slot_types(SPEC)[0],
        "guardrails": guardrails,
        "knowledge_bases": [build_knowledge_base(SPEC)],
        "application": build_application(SPEC),
    }
    assert validate_acxd_consistency(bundle) == []


def test_korean_guardrail_names_get_distinct_files(monkeypatch):
    """Filenames must be distinct or guardrails silently overwrite each other.

    Live dev bucket: three Korean-named guardrails all slugged to '' under the
    ASCII-only rule and were stored as 'acxd_guardrail/.json', each overwriting
    the last — the run reported 'generated 3' while one file survived
    ("두 번 생성했으나 매번 마지막 한 개만 남는다").
    """
    import importlib

    builders = importlib.import_module("tools.acxd_resource_builders")

    stored = []
    monkeypatch.setattr(builders, "_store",
                        lambda sid, atype, fname, doc: stored.append(fname))

    class FakeSpec:
        def model_dump(self):
            return {"flows": [], "guardrails": [
                {"name": "조회 외 요청 제한", "policy": "p1"},
                {"name": "민감정보 마스킹", "policy": "p2"},
                {"name": "공격적 발언 처리", "policy": "p3"},
            ]}

    monkeypatch.setattr(builders, "get_acxd_spec", lambda: FakeSpec())
    raw = getattr(builders.generate_acxd_guardrails, "_raw",
                  None) or builders.generate_acxd_guardrails
    result = raw() if callable(raw) else None
    assert result["status"] == "success", result
    assert len(stored) == 3
    assert len(set(stored)) == 3, f"filenames collided: {stored}"
    assert ".json" not in {f.strip() for f in stored}  # no empty basename


def test_korean_guardrail_names_are_replaced_with_ascii():
    """The live API rejects non-ASCII guardrail/rule names.

    Live deploy (selc-voice-agent, step 7): '폭언 대응' → "name is not in the
    expected format". The policy stays Korean (prompt/messages allow it); only
    the name label must be derived.
    """
    spec = {"flows": [], "guardrails": [
        {"name": "폭언 대응", "policy": "detect abusive language and escalate politely"},
    ]}
    docs, problems = build_guardrails(spec)
    assert problems == []
    assert docs, "the guardrail must survive with a derived name"
    name = docs[0]["name"]
    assert not any(ord(c) > 126 for c in name), name
    assert not any(ord(c) > 126 for c in docs[0]["rules"][0]["name"])


def test_application_name_is_ascii():
    """'삼성전자로지텍㈜ (SELC) Assistant' must not ship as the app name."""
    spec = {"application": {"name": "삼성전자로지텍㈜ (SELC) Assistant"},
            "business_profile": {}, "flows": []}
    from tools.acxd_resource_builders import build_application
    app = build_application(spec)
    assert not any(ord(c) > 126 for c in app["name"]), app["name"]
    assert "SELC" in app["name"]


def test_application_language_follows_the_flows_not_en_us():
    """A Korean PoC was deployed as an English application.

    Live defect: settings.languageCode was the en-US default while every flow
    was ko-KR, so the deployment refused the language ("ko-KR is not included
    in this build") and the assistant answered nothing usable.
    """
    from tools.acxd_resource_builders import build_application

    spec = {"business_profile": {"company_name": "SELC", "language": "ko-KR"},
            "flows": [{"flow_id": "welcomeRouting", "role": "welcome", "language": "ko-KR"}],
            "application": {}}
    app = build_application(spec)
    settings = app.get("settings") or app
    assert settings["languageCode"] == "ko-KR", settings
    assert settings["languageCodes"] == ["ko-KR"], settings


# ---------------------------------------------------------------------------
# Application: all four system events, and no silently-dropped settings
# ---------------------------------------------------------------------------

def test_all_four_system_events_are_routed():
    """A missing default flow leaves the runtime nowhere to go for that event."""
    spec = {
        "flows": [{"flow_id": "MainFlow", "purpose": "main", "role": "welcome"},
                  {"flow_id": "EscFlow", "purpose": "handoff", "role": "escalation"}],
        "application": {"name": "Acme", "languages": ["ko-KR"]},
    }
    defaults = build_application(spec)["settings"]["defaultFlows"]
    assert set(defaults) >= {"welcome", "fallback", "unknown", "escalation"}
    assert defaults["welcome"]["flowId"] == "MainFlow"
    assert defaults["escalation"]["flowId"] == "EscFlow"
    # no dedicated fallback/unknown flow -> fall back to the entry flow
    assert defaults["fallback"]["flowId"] == "MainFlow"
    assert defaults["unknown"]["flowId"] == "MainFlow"


def test_silently_dropped_settings_are_not_emitted():
    """voice / useNativeLanguage / childDirected are discarded by the service.

    Emitting them yields a bundle that does not match what is stored. TTS is a
    Connect "Set voice" block; ASR and audio filler are on the Agentic CX block.
    """
    spec = {"flows": [{"flow_id": "MainFlow", "purpose": "m", "role": "welcome"}],
            "application": {"name": "Acme", "languages": ["ko-KR"],
                            "voice": "Seoyeon", "child_directed": True}}
    settings = build_application(spec)["settings"]
    assert settings["languageSettings"] == [{"languageCode": "ko-KR"}]
    assert "childDirected" not in settings


def test_lifecycle_hooks_pass_through():
    spec = {"flows": [{"flow_id": "MainFlow", "purpose": "m", "role": "welcome"}],
            "application": {"name": "Acme", "languages": ["en-US"],
                            "lifecycle_hooks": {"escalation": "MainFlow"}}}
    assert build_application(spec)["settings"]["lifecycleHooks"] == {
        "escalation": "MainFlow"}


def test_guardrails_generalise_literal_regex_and_localise_the_modify_message():
    """Live (Hanbit, 2026-09-13): a PII guardrail's 'regex' was the sample phone
    number itself, and an llmJudge output rule replaced Korean bot messages with
    the English 'I can't help with that.'"""
    from tools.acxd_resource_builders import build_guardrails

    spec = {"business_profile": {"language": "ko-KR"},
            "flows": [{"flow_id": "Escalation", "role": "escalation"}],
            "guardrails": [
                {"name": "pii", "trigger": "output", "policy": "개인정보 노출 금지", "detection_method": "regex",
                 "action": "mask", "examples": ["010-1111-2222"]},
                {"name": "medical", "trigger": "output", "policy": "진단 금지", "detection_method": "llmJudge",
                 "action": "modify", "examples": ["이 약 먹어도 되나요?"]},
                {"name": "explicit", "trigger": "input", "policy": "x", "detection_method": "regex",
                 "action": "flag", "pattern": r"^\d{10}$"},
            ]}
    spec["guardrails"].append({"name": "explicit_mask", "trigger": "output", "policy": "mask card numbers",
                               "detection_method": "regex", "action": "mask", "pattern": r"\d{4}-\d{4}-\d{4}-\d{4}"})
    docs, problems = build_guardrails(spec)
    assert problems == []
    by_name = {d["name"]: d["rules"][0] for d in docs}
    assert by_name["pii"]["detection"]["pattern"] == r"\d{3}-\d{4}-\d{4}"
    assert by_name["explicit"]["detection"]["pattern"] == r"^\d{10}$"      # a real regex is kept
    assert "never violate" in by_name["medical"]["detection"]["prompt"]
    # Live: derived OUTPUT rules replaced the greeting / redacted the bot's own
    # format hint. They stay advisory; an explicit interviewer regex keeps mask.
    assert by_name["medical"]["enforcement"] == {"action": "flag"}
    assert by_name["pii"]["enforcement"] == {"action": "flag"}
    assert by_name["explicit_mask"]["enforcement"]["action"] == "mask"
    # the localised replacement message is still what an INPUT modify rule says
    spec["guardrails"] = [{"name": "abuse", "trigger": "input", "policy": "욕설 금지", "detection_method": "llmJudge",
                           "action": "modify", "examples": ["…"]}]
    docs, _ = build_guardrails(spec)
    message = docs[0]["rules"][0]["enforcement"]["behavior"]["message"]
    assert message.startswith("죄송합니다") and "can't" not in message


def test_guardrail_names_are_project_scoped_and_the_application_references_them():
    """Live (2026-09-14): 'guardrail1' from three projects in one workspace kept
    overwriting each other."""
    import copy
    from tools.acxd_resource_builders import build_application, build_guardrails
    spec = copy.deepcopy(SPEC)
    spec["infrastructure"] = {"project_name": "green-cart"}
    docs, problems = build_guardrails(spec)
    assert problems == []
    names = {d["name"] for d in docs}
    assert "greencart-PII Filter" in names and "greencart-Abuse Filter" in names
    app = build_application(spec)
    refs = {g["guardrailId"] for g in app["settings"]["guardrails"]}
    assert refs == {f"{{GUARDRAIL:{n}}}" for n in names}
