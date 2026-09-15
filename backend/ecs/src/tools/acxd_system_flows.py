"""Deterministic builders for the ACXD system flows (no LLM).

Why these are code and not prompts
----------------------------------
A live Amazon Connect Customer / ACXD validation (2026-09-12, sandbox
a sandbox Connect Customer account) proved that the *routing* half of an application is not a design
question the model should re-answer per project: an LLM-authored welcome flow
that classified intent with a ``generative_journey`` never routed a single
customer utterance, and the session ended after one answer. The shapes below
are the ones that DID work over Connect chat, transcribed from the deployed
documents (``WelcomeFlow.json``, ``FallbackFlow.json``, ``FollowUpFlow.json``,
``RequestAgentFlow.json``, ``EscalationFlow.json``, ``slot-types/yesNo.json``):

* R1 intent routing is ``user_input`` + ``redirect`` to
  ``{System.capturedFlow:NLX.System}`` — never a generative node.
* R2 system flows are ``untrained: true`` (they are not routing targets).
  ``RequestAgentFlow`` is the one routable system flow, so a customer asking
  for a human is matched by IT and it redirects to ``EscalationFlow``.
* R3 an operation that succeeded redirects to ``FollowUpFlow``, which asks
  "anything else?" with a ``yesNo`` slot type (S4: there is no boolean
  built-in) and keeps the conversation alive.
* R4 ``FallbackFlow`` counts consecutive failures in a context variable and
  escalates on the third.
* R6 a slot must be cleared before the same capture node is revisited, so
  ``FollowUpFlow`` clears ``moreHelp`` at its own start.
* R7 the ``escalate`` node is TERMINAL — an ``end`` after it made Connect see
  Success instead of the Escalation branch.

Everything customer-facing is written in the project's language; ``description``
and ``aiDescription`` stay ASCII because the live API rejects non-ASCII there.
Node ids are deterministic v4-shaped UUIDs, so re-generating a project produces
byte-identical flows (a changed id is a new node to the service).
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from tools.acxd_flow_canonicalizer import _v4_like_id

# ---------------------------------------------------------------------------
# canonical ids and placeholders
# ---------------------------------------------------------------------------

WELCOME_FLOW_ID = "WelcomeFlow"
FALLBACK_FLOW_ID = "FallbackFlow"
ESCALATION_FLOW_ID = "EscalationFlow"
FOLLOW_UP_FLOW_ID = "FollowUpFlow"
REQUEST_AGENT_FLOW_ID = "RequestAgentFlow"
FAQ_FLOW_ID = "FaqFlow"

#: NLX placeholder for "whichever flow the application just recognized".
CAPTURED_FLOW_PLACEHOLDER = "{System.capturedFlow:NLX.System}"

#: Slot type + attached slot name FollowUpFlow uses. There is no boolean
#: built-in (S4), so yes/no needs a real custom slot type.
YES_NO_SLOT_TYPE_ID = "yesNo"
MORE_HELP_SLOT_NAME = "moreHelp"

#: Context variables the system flows declare and use.
FALLBACK_ATTEMPTS_VAR = "fallbackAttempts"
FAIL_REASON_VAR = "failReason"

#: Consecutive unrecognized turns before FallbackFlow hands over to a human.
MAX_FALLBACK_ATTEMPTS = 3

#: Flow roles produced by this module instead of by the LLM.
SYSTEM_FLOW_ROLES = ("welcome", "fallback", "escalation", "followup", "agent_request", "faq")

#: Roles that must exist in EVERY generated application, planned or not: the
#: conversation cannot continue after an answer without FollowUpFlow, and a
#: customer asking for a human has nothing to match without RequestAgentFlow.
ALWAYS_GENERATED_SYSTEM_ROLES = ("followup", "agent_request")

DEFAULT_SYSTEM_FLOW_IDS = {
    "welcome": WELCOME_FLOW_ID,
    "fallback": FALLBACK_FLOW_ID,
    "escalation": ESCALATION_FLOW_ID,
    "followup": FOLLOW_UP_FLOW_ID,
    "agent_request": REQUEST_AGENT_FLOW_ID,
    "faq": FAQ_FLOW_ID,
}


def knowledge_base_name(spec: dict) -> Optional[str]:
    """Name of the knowledge base the application ships, or None."""
    try:
        from tools.acxd_resource_builders import build_knowledge_base  # lazy: that module imports this one
        kb = build_knowledge_base(spec)
    except Exception:  # pragma: no cover - resource builders unavailable in a stub
        return None
    return str(kb["name"]) if isinstance(kb, dict) and kb.get("name") else None


def plans_cover_faq(plans) -> bool:
    """True when the interview already planned a flow that answers from the knowledge base."""
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        if str(plan.get("role") or "") == "faq":
            return True
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and str(step.get("node_type") or "").lower() in ("knowledge_base", "knowledgebase", "kb"):
                return True
    return False


def conditional_system_roles(spec: dict, plans=None) -> tuple[str, ...]:
    """System roles the application needs because of what it ships.

    Live (2026-09-15): an application had a knowledge base, and the greeting
    promised policy questions, but no flow reached the knowledge base — the only
    path was the application's `unknown` default behaviour, and the NLU sent
    "what is your return policy?" to the return-request flow instead. A
    knowledge base therefore gets a routable FAQ flow unless the interview
    planned one itself.
    """
    plans = plans if plans is not None else (spec.get("flows") or [])
    if knowledge_base_name(spec) and not plans_cover_faq(plans):
        return ("faq",)
    return ()


# ---------------------------------------------------------------------------
# language pack
# ---------------------------------------------------------------------------

#: Customer-facing wording per language root. Korean is the live-verified set;
#: the others follow the same script. Unknown languages fall back to English
#: rather than shipping Korean to an English caller.
_TEXTS: dict[str, dict] = {
    "ko": {
        "greeting": "안녕하세요, {company}입니다. 무엇을 도와드릴까요?",
        "greeting_no_company": "안녕하세요. 무엇을 도와드릴까요?",
        "greeting_ops": "안녕하세요, {company}입니다. {operations} 등을 도와드릴 수 있어요. 무엇을 도와드릴까요?",
        "greeting_ops_no_company": "안녕하세요. {operations} 등을 도와드릴 수 있어요. 무엇을 도와드릴까요?",
        "reguide": "죄송합니다, 잘 이해하지 못했습니다. {operations} 중 무엇을 "
                   "도와드릴까요? 상담사 연결도 가능합니다.",
        "reguide_no_operations": "죄송합니다, 잘 이해하지 못했습니다. 무엇을 "
                                 "도와드릴까요? 상담사 연결도 가능합니다.",
        "fallback_handoff": "요청을 정확히 이해하지 못해 상담사에게 연결해 드리겠습니다.",
        "more_help": "더 도와드릴 일이 있을까요?",
        "next_request": "무엇을 도와드릴까요?",
        "thanks": "이용해 주셔서 감사합니다. 좋은 하루 보내세요.",
        "escalation_intro": "상담사를 연결해드리겠습니다. 이전에 전달해 주신 정보는 "
                            "상담사에게 자동 전달됩니다.",
        "escalation_wait": "지금 상담사에게 연결하고 있습니다. 잠시만 기다려 주세요.",
        "operation_joiner": ", ",
        "yes": "예",
        "yes_synonyms": ["네", "응", "그래", "그렇습니다", "좋아요", "동의", "동의합니다",
                         "동의해요", "있어요", "있습니다", "맞아요", "해주세요"],
        "no": "아니요",
        "no_synonyms": ["아니오", "아뇨", "아니", "싫어요", "없어요", "없습니다",
                        "괜찮아요", "동의하지 않아요", "동의 안 해요", "안 해요", "됐어요"],
        "faq_intro": "문의하신 내용을 안내해 드릴게요.",
        "faq_label": "자주 묻는 질문",
    },
    "en": {
        "greeting": "Hello, this is {company}. How can I help you today?",
        "greeting_no_company": "Hello. How can I help you today?",
        "greeting_ops": "Hello, this is {company}. I can help with {operations}. How can I help you today?",
        "greeting_ops_no_company": "Hello. I can help with {operations}. How can I help you today?",
        "reguide": "Sorry, I did not catch that. I can help with {operations}. "
                   "Which one would you like? I can also connect you to an agent.",
        "reguide_no_operations": "Sorry, I did not catch that. How can I help "
                                 "you? I can also connect you to an agent.",
        "fallback_handoff": "I could not understand your request, so I will "
                            "connect you to an agent.",
        "more_help": "Is there anything else I can help you with?",
        "next_request": "How can I help you?",
        "thanks": "Thank you for contacting us. Have a great day.",
        "escalation_intro": "I will connect you to an agent. Everything you have "
                            "told me is passed along automatically.",
        "escalation_wait": "Connecting you to an agent now. Please hold.",
        "operation_joiner": ", ",
        "yes": "yes",
        "yes_synonyms": ["yeah", "yep", "yes please", "sure", "ok", "okay",
                         "correct", "right", "i do", "please do", "agreed"],
        "no": "no",
        "no_synonyms": ["nope", "nah", "no thanks", "no thank you", "not now",
                        "that is all", "thats all", "nothing else", "i am done",
                        "im good", "no i dont"],
        "faq_intro": "Here is what I found on that.",
        "faq_label": "general questions",
    },
    "ja": {
        "greeting": "こんにちは、{company}です。ご用件をお伺いします。",
        "greeting_no_company": "こんにちは。ご用件をお伺いします。",
        "greeting_ops": "こんにちは、{company}です。{operations}をご案内できます。ご用件をお伺いします。",
        "greeting_ops_no_company": "こんにちは。{operations}をご案内できます。ご用件をお伺いします。",
        "reguide": "申し訳ございません、うまく理解できませんでした。{operations} の"
                   "うち、どのご用件でしょうか。オペレーターへのお繋ぎも可能です。",
        "reguide_no_operations": "申し訳ございません、うまく理解できませんでした。"
                                 "ご用件をお伺いします。オペレーターへのお繋ぎも可能です。",
        "fallback_handoff": "ご要望を正確に理解できませんでしたので、オペレーターに"
                            "お繋ぎいたします。",
        "more_help": "他にお手伝いできることはございますか？",
        "next_request": "ご用件をお伺いします。",
        "thanks": "お問い合わせありがとうございました。よい一日をお過ごしください。",
        "escalation_intro": "オペレーターにお繋ぎいたします。これまでにいただいた"
                            "情報は自動で引き継がれます。",
        "escalation_wait": "ただ今オペレーターにお繋ぎしています。少しお待ちください。",
        "operation_joiner": "、",
        "yes": "はい",
        "yes_synonyms": ["ええ", "うん", "そう", "そうです", "お願いします",
                         "あります", "はいお願いします", "了解", "同意します"],
        "no": "いいえ",
        "no_synonyms": ["いや", "いえ", "ありません", "大丈夫です", "結構です",
                        "いらない", "不要です", "以上です"],
        "faq_intro": "お問い合わせの内容についてご案内します。",
        "faq_label": "よくあるご質問",
    },
}

_FALLBACK_LANGUAGE = "en"


def system_flow_language(spec: dict) -> str:
    """Full locale code the system flows are written in (e.g. ``ko-KR``).

    Precedence mirrors :func:`tools.acxd_resource_builders.build_application`
    so the flows and the application never disagree about the language — a live
    defect deployed a Korean PoC as an English application and the assistant
    answered nothing usable.
    """
    app = spec.get("application") or {}
    profile = spec.get("business_profile") or {}
    candidates = [
        app.get("primary_locale"), app.get("primary_language"),
        *(app.get("locales") or []), *(app.get("languages") or []),
        *[f.get("language") for f in (spec.get("flows") or []) if isinstance(f, dict)],
        profile.get("language"), profile.get("primary_language"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            from tools.acxd_contract import canonical_language
            return canonical_language(str(candidate))
        except Exception:  # pragma: no cover - contract unavailable
            return str(candidate)
    return "en-US"


def _texts(language: str) -> dict:
    root = str(language or "").replace("_", "-").split("-")[0].lower()
    return _TEXTS.get(root) or _TEXTS[_FALLBACK_LANGUAGE]


def resolve_system_flow_ids(spec: dict) -> dict:
    """role -> flow id, honouring what the interview actually planned.

    The interview may have named the welcome flow ``Welcome`` rather than
    ``WelcomeFlow``; every redirect this module emits must point at the id the
    bundle really ships, so resolve once and pass the mapping around.
    """
    resolved = dict(DEFAULT_SYSTEM_FLOW_IDS)
    for plan in spec.get("flows") or []:
        if not isinstance(plan, dict):
            continue
        role, flow_id = plan.get("role"), plan.get("flow_id")
        if role in resolved and flow_id:
            resolved[role] = flow_id
    return resolved


def _ids(spec: dict, flow_ids: Optional[dict]) -> dict:
    resolved = resolve_system_flow_ids(spec)
    if flow_ids:
        resolved.update({k: v for k, v in flow_ids.items() if v})
    return resolved


def _company(spec: dict) -> str:
    profile = spec.get("business_profile") or {}
    for key in ("company_name", "companyName"):
        value = str(profile.get(key) or "").strip()
        # A project slug ("gaon-aicc") is not a name to greet a customer with.
        if value and value != str(profile.get("project_name") or "").strip():
            return value
    return ""


def _composed_greeting(spec: dict, text: dict, company: str) -> str:
    """Greeting built from what the interview did record: the company name when
    it is a real name, and the operations the assistant can help with (the same
    display names the re-guide uses), so a first-time caller hears the menu."""
    labels = operation_labels(spec)
    operations = text["operation_joiner"].join(labels) if labels else ""
    if operations and company:
        return text["greeting_ops"].format(company=company, operations=operations)
    if operations:
        return text["greeting_ops_no_company"].format(operations=operations)
    if company:
        return text["greeting"].format(company=company)
    return text["greeting_no_company"]


def _approved_greeting(spec: dict) -> str:
    """The greeting sentence the customer approved during the interview, when
    the generation context carries one (business_profile.greeting, set from
    SessionFlowConfig.common_greeting / ContactFlowSpec.welcome_message)."""
    profile = spec.get("business_profile") or {}
    application = spec.get("application") or {}
    for source in (profile, application):
        for key in ("greeting", "welcome_message", "welcomeMessage", "common_greeting"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


#: How many operations the re-guide message lists before it stops. A caller
#: cannot hold a ten-item menu in their head, and the message is spoken.
_MAX_LISTED_OPERATIONS = 6
_MAX_OPERATION_LABEL = 40


def operation_labels(spec: dict) -> list[str]:
    """Short customer-facing names of the business operations, in the project
    language, for the fallback re-guidance ("I can help with A, B or C").

    Only the plan's ``display_name`` (the interview asks for it) is used: a
    label cut out of a purpose sentence reads as a fragment ("customer name"
    for "find the order by customer name, phone and address"), and a menu that
    names some operations and drops others misleads the caller. When any
    operation flow lacks a display name the list is empty and the caller gets
    the generic re-guidance instead."""
    labels: list[str] = []
    # Flows the generator marked ``untrained`` (the model recognised an internal
    # utility flow the plan did not flag) are not routing targets either; a menu
    # that names them offers something the caller cannot reach (live: "통화 결과
    # 기록" listed in the re-guide).
    not_routable = {str(f) for f in (spec.get("untrained_flow_ids") or [])}
    for plan in spec.get("flows") or []:
        if not isinstance(plan, dict) or plan.get("role", "operation") != "operation":
            continue
        if plan.get("customer_initiated") is False or str(plan.get("flow_id")) in not_routable:
            continue  # internal operation (e.g. call-result logging): never offered
        label = re.sub(r"\s+", " ", str(plan.get("display_name") or "")).strip(" .,;:-·")
        if not label or len(label) > _MAX_OPERATION_LABEL:
            return []
        labels.append(label)
        if len(labels) >= _MAX_LISTED_OPERATIONS:
            break
    # The deterministic FAQ flow is a routing target too: offer it by its own
    # short name so a caller with a policy question knows it is on the menu.
    if labels and "faq" in conditional_system_roles(spec) and len(labels) < _MAX_LISTED_OPERATIONS:
        labels.append(_texts(system_flow_language(spec))["faq_label"])
    return labels


# ---------------------------------------------------------------------------
# small node helpers
# ---------------------------------------------------------------------------

def _node_id(flow_id: str, name: str) -> str:
    return _v4_like_id(f"acxd-system-flow/{flow_id}#{name}")


def _message(body: str) -> dict:
    return {"type": "text", "body": body}


def _child(node_id: str, name: str, conditions: Optional[list] = None) -> dict:
    child = {"nodeId": node_id, "name": name}
    if conditions is not None:
        child["conditions"] = conditions
    return child


def _captured_flow_condition(exists: bool) -> list:
    return [{"left": {"type": "captured_flow"},
             "operator": "exists" if exists else "not_exists"}]


def _slot_condition(name: str, exists: bool) -> list:
    return [{"left": {"type": "slot", "name": name},
             "operator": "exists" if exists else "not_exists"}]


def _set_context(name: str, value) -> dict:
    return {"type": "context", "name": name, "modification": "set",
            "value": {"type": "constant", "value": value}}


def _redirect_node(node_id: str, flow_id: str, next_id: Optional[str] = None,
                   *, messages: Optional[list] = None,
                   state_modifications: Optional[list] = None) -> dict:
    metadata: dict = {"redirect": {"type": "flow", "flowId": flow_id}}
    if state_modifications:
        metadata["stateModifications"] = state_modifications
    node: dict = {"nodeId": node_id, "type": "redirect", "metadata": metadata}
    if messages:
        node["messages"] = messages
    if next_id:
        node["childNodes"] = [_child(next_id, "next")]
    return node


def _flow_shell(flow_id: str, language: str, *, untrained: bool,
                description: str, ai_description: str,
                context_variables: list, slot_types: Optional[list] = None) -> dict:
    """Common flow header. ``description`` / ``aiDescription`` MUST be ASCII —
    the live API rejects anything else on these two fields."""
    return {
        "flowId": flow_id,
        "untrained": untrained,
        "description": _ascii(description)[:200],
        "aiDescription": _ascii(ai_description)[:1000],
        "mainLanguageCode": language,
        "languageCodes": [language],
        "slotTypes": list(slot_types or []),
        "contextVariables": list(context_variables),
        "nodes": {},
    }


def _ascii(text: str) -> str:
    cleaned = re.sub(r"[^\x20-\x7E]", " ", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


_FALLBACK_ATTEMPTS_CONTEXT = [{"name": FALLBACK_ATTEMPTS_VAR, "type": "number"}]
_FAIL_REASON_CONTEXT = [{"name": FAIL_REASON_VAR, "type": "text"}]


# ---------------------------------------------------------------------------
# WelcomeFlow (R1)
# ---------------------------------------------------------------------------

def build_welcome_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """start -> greeting -> user_input -> redirect(recognized flow | FallbackFlow).

    This is the whole of intent routing. The application recognizes which
    attached flow matches the utterance and exposes it as
    ``{System.capturedFlow:NLX.System}``; the flow's only job is to listen and
    hand over. A ``generative_journey`` here recognized nothing (live).
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids["welcome"]
    language = system_flow_language(spec)
    text = _texts(language)
    company = _company(spec)
    # The interview records the greeting the customer approved verbatim
    # (SessionFlowConfig.common_greeting / ContactFlowSpec.welcome_message).
    # Live (GAON): composing one from the profile said "안녕하세요, gaon-aicc입니다"
    # — the project slug — because the profile had no company name.
    greeting = _approved_greeting(spec) or _composed_greeting(spec, text, company)

    start = _node_id(flow_id, "start")
    greet = _node_id(flow_id, "greeting")
    listen = _node_id(flow_id, "intentCapture")
    recognized = _node_id(flow_id, "redirectRecognized")
    unrecognized = _node_id(flow_id, "redirectFallback")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=(f"Greets the customer, captures the intent with a User input "
                     f"node and redirects to the recognized flow. Unrecognized "
                     f"input goes to {ids['fallback']}."),
        ai_description=("System welcome flow. Not a routing target: it greets the "
                        "customer once, resets the fallback counter, listens with a "
                        "User input node and redirects to whichever attached flow "
                        "the application recognized."),
        context_variables=_FALLBACK_ATTEMPTS_CONTEXT,
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(greet, "toGreeting")]},
        greet: {"nodeId": greet, "type": "basic",
                "messages": [_message(greeting)],
                # Reset here, not in FallbackFlow: a session that starts over
                # must not inherit the previous caller's failure count.
                "metadata": {"stateModifications": [_set_context(FALLBACK_ATTEMPTS_VAR, 0)]},
                "childNodes": [_child(listen, "toIntentCapture")]},
        listen: {"nodeId": listen, "type": "user_input",
                 "childNodes": [
                     _child(recognized, "flowRecognized", _captured_flow_condition(True)),
                     _child(unrecognized, "noFlowRecognized", _captured_flow_condition(False)),
                 ]},
        recognized: _redirect_node(recognized, CAPTURED_FLOW_PLACEHOLDER, end),
        unrecognized: _redirect_node(unrecognized, ids["fallback"], end),
        end: {"nodeId": end, "type": "end"},
    }
    return flow


# ---------------------------------------------------------------------------
# FallbackFlow (R4)
# ---------------------------------------------------------------------------

def build_fallback_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """Count the failure, re-guide once, listen again, escalate on the third.

    The counter lives in a context variable because the flow is re-entered from
    scratch each time (its own not-recognized edge redirects back to itself), so
    there is no in-flow state to keep.
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids["fallback"]
    language = system_flow_language(spec)
    text = _texts(language)
    labels = operation_labels(spec)
    reguide = (text["reguide"].format(operations=text["operation_joiner"].join(labels))
               if labels else text["reguide_no_operations"])

    start = _node_id(flow_id, "start")
    count = _node_id(flow_id, "countAttempt")
    check = _node_id(flow_id, "checkAttempts")
    guide = _node_id(flow_id, "reguide")
    listen = _node_id(flow_id, "listenAgain")
    recognized = _node_id(flow_id, "redirectRecognized")
    escalate = _node_id(flow_id, "redirectEscalation")
    retry = _node_id(flow_id, "redirectFallback")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=("Re-guides the customer when nothing was recognized, keeps "
                     "listening, and escalates on the third consecutive failure."),
        ai_description=("System fallback flow. Not a routing target: increments the "
                        "fallback counter, re-guides the customer with the supported "
                        "services, listens again with a User input node and redirects "
                        "to the recognized flow; after "
                        f"{MAX_FALLBACK_ATTEMPTS} consecutive failures it hands over "
                        f"to {ids['escalation']}."),
        context_variables=_FALLBACK_ATTEMPTS_CONTEXT,
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(count, "toCount")]},
        # A message-free basic node is the only place a state modification can
        # hang without saying anything to the caller.
        count: {"nodeId": count, "type": "basic",
                "metadata": {"stateModifications": [
                    {"type": "context", "name": FALLBACK_ATTEMPTS_VAR,
                     "modification": "increment"}]},
                "childNodes": [_child(check, "toCheck")]},
        check: {"nodeId": check, "type": "choice",
                "childNodes": [
                    _child(escalate, "escalateOnThirdFailure", [{
                        "left": {"type": "context", "name": FALLBACK_ATTEMPTS_VAR},
                        "operator": "gte",
                        "right": {"type": "constant", "value": MAX_FALLBACK_ATTEMPTS},
                    }]),
                    # The default branch carries an EMPTY condition list, which
                    # is how the console encodes "otherwise".
                    _child(guide, "reguide", []),
                ]},
        guide: {"nodeId": guide, "type": "basic",
                "messages": [_message(reguide)],
                "childNodes": [_child(listen, "listenAgain")]},
        listen: {"nodeId": listen, "type": "user_input",
                 "childNodes": [
                     _child(recognized, "flowRecognized", _captured_flow_condition(True)),
                     _child(retry, "noFlowRecognized", _captured_flow_condition(False)),
                 ]},
        recognized: _redirect_node(recognized, CAPTURED_FLOW_PLACEHOLDER, end),
        escalate: _redirect_node(escalate, ids["escalation"], end,
                                 messages=[_message(text["fallback_handoff"])]),
        retry: _redirect_node(retry, flow_id, end),
        end: {"nodeId": end, "type": "end"},
    }
    return flow


# ---------------------------------------------------------------------------
# FollowUpFlow (R3 + R6)
# ---------------------------------------------------------------------------

def build_follow_up_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """"Anything else?" — the node that keeps the session alive after an answer.

    Every operation flow's success path redirects here instead of ending, which
    is what turned a one-answer bot into a multi-turn conversation live. The
    attached slot is cleared at the START of the flow (R6): slot values persist
    for the whole session, and a filled ``moreHelp`` would answer the capture
    node for the customer on its second visit.
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids["followup"]
    language = system_flow_language(spec)
    text = _texts(language)

    start = _node_id(flow_id, "start")
    clear = _node_id(flow_id, "clearSlot")
    ask = _node_id(flow_id, "askMoreHelp")
    branch = _node_id(flow_id, "branchOnAnswer")
    prompt = _node_id(flow_id, "askNextRequest")
    listen = _node_id(flow_id, "listenAgain")
    recognized = _node_id(flow_id, "redirectRecognized")
    thanks = _node_id(flow_id, "thanks")
    unrecognized = _node_id(flow_id, "redirectFallback")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=("Asked after an operation completes: offers further help, "
                     "routes the next request, or ends the session politely."),
        ai_description=("System follow-up flow. Not a routing target: asks whether "
                        "the customer needs anything else (yes/no), listens for the "
                        "next request and redirects to the recognized flow, or thanks "
                        "the customer and exits the application."),
        context_variables=_FALLBACK_ATTEMPTS_CONTEXT,
        slot_types=[{
            "name": MORE_HELP_SLOT_NAME,
            "type": YES_NO_SLOT_TYPE_ID,
            "sensitive": False,
            "aiDescription": "Whether the customer wants further help: yes or no",
        }],
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(clear, "toAsk")]},
        clear: {"nodeId": clear, "type": "basic",
                "metadata": {"stateModifications": [
                    {"type": "slot", "name": MORE_HELP_SLOT_NAME,
                     "modification": "clear"}]},
                "childNodes": [_child(ask, "next")]},
        ask: {"nodeId": ask, "type": "user_choice",
              "messages": [_message(text["more_help"])],
              "metadata": {
                  # S2: slotTypeId is stored verbatim as the internal slotId, so
                  # it must be the attached slot's NAME, not the slot type id.
                  "choice": {"source": "slotType", "slotTypeId": MORE_HELP_SLOT_NAME},
                  # The customer got an answer, so the failure streak is over.
                  "stateModifications": [_set_context(FALLBACK_ATTEMPTS_VAR, 0)],
              },
              "childNodes": [
                  _child(branch, "captured", _slot_condition(MORE_HELP_SLOT_NAME, True)),
                  # "Anything else?" is often answered with the next request
                  # itself ("반품 신청하고 싶어요", live) rather than yes/no. A
                  # user_choice cannot route it — captured_flow is only set by a
                  # user_input node (live) — so treat the answer as "yes, and…":
                  # ask what they need and listen, instead of the fallback's
                  # "I did not understand" and a failure count.
                  _child(prompt, "notCaptured",
                         _slot_condition(MORE_HELP_SLOT_NAME, False)),
              ]},
        branch: {"nodeId": branch, "type": "choice",
                 "childNodes": [
                     _child(prompt, "yes", [{
                         "left": {"type": "slot", "name": MORE_HELP_SLOT_NAME},
                         "operator": "eq",
                         "right": {"type": "constant", "value": text["yes"]},
                     }]),
                     _child(thanks, "no", []),
                 ]},
        prompt: {"nodeId": prompt, "type": "basic",
                 "messages": [_message(text["next_request"])],
                 "childNodes": [_child(listen, "listen")]},
        listen: {"nodeId": listen, "type": "user_input",
                 "childNodes": [
                     _child(recognized, "flowRecognized", _captured_flow_condition(True)),
                     _child(unrecognized, "noFlowRecognized",
                            _captured_flow_condition(False)),
                 ]},
        recognized: _redirect_node(recognized, CAPTURED_FLOW_PLACEHOLDER, end),
        thanks: {"nodeId": thanks, "type": "basic",
                 "messages": [_message(text["thanks"])],
                 "childNodes": [_child(end, "toEnd")]},
        unrecognized: _redirect_node(unrecognized, ids["fallback"], end),
        end: {"nodeId": end, "type": "end"},
    }
    return flow


# ---------------------------------------------------------------------------
# RequestAgentFlow (R2)
# ---------------------------------------------------------------------------

#: The one routing description a system flow carries. EscalationFlow is wired to
#: the application's escalation default behaviour and is therefore NOT a routing
#: target, so "connect me to a human" needs its own routable flow.
REQUEST_AGENT_AI_DESCRIPTION = (
    "Use this flow when the customer explicitly asks to talk to a human agent, "
    "counselor or representative, wants a person instead of the bot, or wants to "
    "file a complaint."
)


def build_request_agent_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """Routable "connect me to a human" entry that redirects to EscalationFlow."""
    ids = _ids(spec, flow_ids)
    flow_id = ids["agent_request"]
    language = system_flow_language(spec)

    start = _node_id(flow_id, "start")
    handover = _node_id(flow_id, "redirectEscalation")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language,
        # The ONLY trained system flow: it has to be matched from an utterance.
        untrained=False,
        description=(f"Routable entry for customers who ask for a human agent; "
                     f"hands over to {ids['escalation']}."),
        ai_description=REQUEST_AGENT_AI_DESCRIPTION,
        context_variables=_FAIL_REASON_CONTEXT,
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(handover, "next")]},
        handover: _redirect_node(
            handover, ids["escalation"], end,
            state_modifications=[_set_context(FAIL_REASON_VAR,
                                              "customer_requested_agent")]),
        end: {"nodeId": end, "type": "end"},
    }
    return flow


# ---------------------------------------------------------------------------
# FaqFlow — routable entry to the knowledge base
# ---------------------------------------------------------------------------

#: Routing description of the FAQ flow. It has to WIN against the transactional
#: flows for an information question ("what is your return policy?" was routed to
#: the return-request flow, live), so it names the question kinds explicitly and
#: says what it is not.
FAQ_AI_DESCRIPTION = (
    "Use this flow when the customer asks a general information question that is "
    "answered from the FAQ: policies and rules, fees and prices in general, "
    "opening hours, delivery areas and times, membership benefits, receipts and "
    "documents, how something works or what is allowed. Do not use it when the "
    "customer asks to look up, create, change or cancel their own order, booking, "
    "return or account — those have their own flows."
)


def build_faq_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """Routable FAQ entry: one knowledge_base node, then FollowUpFlow.

    A knowledge base attached only to the application's `unknown` default
    behaviour is reached solely when NO flow matches; an information question
    that resembles an operation is routed to that operation instead. A trained
    flow with its own routing description gives the NLU a target to prefer.
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids.get("faq", FAQ_FLOW_ID)
    language = system_flow_language(spec)
    text = _texts(language)
    kb_name = knowledge_base_name(spec) or "FAQ"

    start = _node_id(flow_id, "start")
    answer = _node_id(flow_id, "knowledgeBase")
    follow_up = _node_id(flow_id, "redirectFollowUp")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language,
        untrained=False,
        description=f"Routable FAQ entry; answers from knowledge base {kb_name} and hands over to {ids['followup']}.",
        ai_description=FAQ_AI_DESCRIPTION,
        context_variables=[],
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(answer, "toKnowledgeBase")]},
        answer: {"nodeId": answer, "type": "knowledge_base",
                 "messages": [_message(text["faq_intro"])],
                 "metadata": {"knowledgeBase": {"knowledgeBaseId": f"{{KB:{kb_name}}}", "name": kb_name}},
                 "childNodes": [_child(follow_up, "toFollowUp")]},
        follow_up: _redirect_node(follow_up, ids["followup"], end),
        end: {"nodeId": end, "type": "end"},
    }
    return flow


# ---------------------------------------------------------------------------
# EscalationFlow (R7)
# ---------------------------------------------------------------------------

def build_escalation_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """Handoff message, then a TERMINAL escalate node.

    No ``end`` after the escalate: with one, Connect saw the Agentic CX block's
    Success branch instead of Escalation and the caller was never transferred
    (live, R7). The queue is chosen by the contact flow's Escalation branch, so
    the node carries only messages and the failReason handover.
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids["escalation"]
    language = system_flow_language(spec)
    text = _texts(language)

    start = _node_id(flow_id, "start")
    intro = _node_id(flow_id, "handoffMessage")
    escalate = _node_id(flow_id, "escalate")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=("Plays the handoff message and escalates to a human agent; "
                     "failReason is passed to the contact flow."),
        ai_description=("System escalation flow. Not a routing target: it is reached "
                        f"by redirect from {ids['agent_request']} or "
                        f"{ids['fallback']}, plays the handoff message and escalates "
                        "to a human agent queue."),
        context_variables=_FAIL_REASON_CONTEXT,
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(intro, "start")]},
        intro: {"nodeId": intro, "type": "basic",
                "messages": [_message(text["escalation_intro"])],
                "childNodes": [_child(escalate, "toEscalate")]},
        # TERMINAL — no childNodes.
        escalate: {"nodeId": escalate, "type": "escalate",
                   "messages": [_message(text["escalation_wait"])],
                   "metadata": {"stateModifications": [{
                       "type": "context", "name": FAIL_REASON_VAR,
                       "modification": "set",
                       "value": {"type": "variable", "name": FAIL_REASON_VAR},
                   }]}},
    }
    return flow


# ---------------------------------------------------------------------------
# yesNo slot type (S4)
# ---------------------------------------------------------------------------

def build_yes_no_slot_type(spec: dict) -> dict:
    """The yes/no slot type FollowUpFlow's ``moreHelp`` slot attaches.

    ACXD has no boolean built-in (S4) and an attached ``type: "boolean"``
    silently disables flow recognition for the WHOLE application (S1), so the
    yes/no answer needs a real custom slot type with synonyms.
    """
    text = _texts(system_flow_language(spec))
    return {
        "slotTypeId": YES_NO_SLOT_TYPE_ID,
        "values": [
            {"value": text["yes"], "synonyms": list(text["yes_synonyms"])},
            {"value": text["no"], "synonyms": list(text["no_synonyms"])},
        ],
        "sensitive": False,
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

SYSTEM_FLOW_BUILDERS: dict[str, Callable[..., dict]] = {
    "welcome": build_welcome_flow,
    "fallback": build_fallback_flow,
    "escalation": build_escalation_flow,
    "followup": build_follow_up_flow,
    "agent_request": build_request_agent_flow,
    "faq": build_faq_flow,
}


def is_system_flow_role(role: Optional[str]) -> bool:
    return str(role or "") in SYSTEM_FLOW_BUILDERS


def build_system_flow(role: str, spec: dict, *,
                      flow_ids: Optional[dict] = None) -> Optional[dict]:
    """Deterministic flow document for *role*, or ``None`` for an unknown role."""
    builder = SYSTEM_FLOW_BUILDERS.get(str(role or ""))
    if builder is None:
        return None
    return builder(spec, flow_ids=flow_ids)
