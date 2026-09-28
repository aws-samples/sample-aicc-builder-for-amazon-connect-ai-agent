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
#: Set to 1 once the greeting has been spoken. Live (voice, 2026-09-21): the
#: caller heard the greeting five times in 23 seconds — the voice channel
#: re-entered WelcomeFlow with structured requests while the greeting played,
#: and "안녕하세요" was routed back to it — so a re-entry skips the greeting and
#: goes straight to listening.
WELCOME_GREETED_VAR = "welcomeGreeted"
#: Set to 1 by a carrying journey's `done` exit (the customer said they need
#: nothing else). Live (sandbox, 2026-09-27): after "아니요 없어요" the journey
#: handed over to FollowUpFlow, which asked "더 도와드릴 일이 있을까요?" again.
#: FollowUpFlow closes at once when it is set.
JOURNEY_DONE_VAR = "journeyDone"
#: Output variable of a knowledge_base node (`metadata.knowledgeBase.name`); the
#: answer is `{<name>.answer:NLX.Local}` and is spoken only where a message
#: references it (live-verified 2026-09-27).
KB_ANSWER_VAR = "faqAnswer"


def kb_answer_placeholder(output_name: str) -> str:
    """The message placeholder that speaks a knowledge_base node's answer."""
    return f"{{{output_name}.answer:NLX.Local}}"


def _node_status(status: str) -> dict:
    return {"left": {"type": "node_status"}, "operator": "eq",
            "right": {"type": "constant", "value": status}}

#: Routing text every untrained system flow carries. Live (2026-09-21): the
#: welcome flow's aiDescription said it "greets the customer", and the router
#: sent "안녕하세요" and "뭐 해줄 수 있어요?" to it although it is untrained —
#: the greeting was repeated each time. A system flow's routing text must not
#: describe anything a caller might say.
SYSTEM_FLOW_AI_DESCRIPTION = "Internal system flow. Never select this flow for a customer utterance."

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
    roles: tuple[str, ...] = ()
    if knowledge_base_name(spec) and not plans_cover_faq(plans):
        roles += ("faq",)
    return roles + tuple(handoff_notice_roles(spec))


HANDOFF_NOTICE_ROLE_PREFIX = "handoff_notice:"


def handoff_notice_plans(spec: dict) -> list[dict]:
    """The route guardrails that carry a mandated sentence, with the id of the
    flow that says it: ``[{name, message, keywords, flow_id, role}]``.

    Live (Hanbit e2e, 2026-09-22): a keyword guardrail can route the caller to
    a flow, but the escalation flow speaks its generic line — the document's
    "응급 상황이면 119 또는 응급실(24시간)로 연락해 주세요" was never said on that
    path. Each such rule gets its own deterministic flow: the sentence, then a
    terminal escalate."""
    out: list[dict] = []
    seen: set[str] = set()
    for plan in spec.get("guardrails") or []:
        if not isinstance(plan, dict) or str(plan.get("action") or "") != "route":
            continue
        message = str(plan.get("message") or "").strip()
        if not message:
            continue
        flow_id = _notice_flow_id(str(plan.get("name") or ""), seen)
        seen.add(flow_id)
        out.append({"name": plan.get("name"), "message": message,
                    "keywords": [str(e) for e in (plan.get("examples") or []) if str(e).strip()],
                    "flow_id": flow_id, "role": f"{HANDOFF_NOTICE_ROLE_PREFIX}{flow_id}"})
    return out


_HANDOFF_FLOW_SUFFIX = "HandoffFlow"


def _letters_suffix(n: int) -> str:
    """'' for 0, then 'B', 'C', … 'Z', 'BA', … — base 26 in letters, digits never."""
    out = ""
    while n > 0:
        n, r = divmod(n, 26)
        out = chr(ord("A") + r) + out
    return out


def _notice_flow_id(name: str, taken: set[str]) -> str:
    """A letters-only flow id for a guardrail's hand-off flow.

    ACXD flow ids are ``^[A-Za-z]{3,64}$``. Live (Hanbit e2e, 2026-09-27): the
    guardrail "응급·통증 호소 즉시 이관" has no Latin letters, the id became
    ``Notice1HandoffFlow``, and the digit failed the schema — every flow
    generation attempt of the application failed with it."""
    slug = "".join(w.capitalize() for w in re.split(r"[^A-Za-z]+", name) if w)
    slug = slug[:64 - len(_HANDOFF_FLOW_SUFFIX) - 2] or "Notice"
    n = 0
    while True:
        flow_id = f"{slug}{_letters_suffix(n)}{_HANDOFF_FLOW_SUFFIX}"
        if flow_id not in taken:
            return flow_id
        n += 1


def handoff_notice_roles(spec: dict) -> list[str]:
    return [n["role"] for n in handoff_notice_plans(spec)]


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
        "fallback_handoff": "요청을 정확히 이해하지 못했습니다.",
        "more_help": "더 도와드릴 일이 있을까요?",
        "next_request": "무엇을 도와드릴까요?",
        "thanks": "이용해 주셔서 감사합니다. 좋은 하루 보내세요.",
        "escalation_intro": "상담사를 연결해드리겠습니다. 이전에 전달해 주신 정보는 "
                            "상담사에게 자동 전달됩니다.",
        "operation_joiner": ", ",
        "yes": "예",
        "yes_synonyms": ["네", "응", "그래", "그렇습니다", "좋아요", "동의", "동의합니다",
                         "동의해요", "있어요", "있습니다", "맞아요", "해주세요"],
        "no": "아니요",
        "no_synonyms": ["아니오", "아뇨", "아니", "싫어요", "없어요", "없습니다",
                        "괜찮아요", "동의하지 않아요", "동의 안 해요", "안 해요", "됐어요"],
        # FollowUpFlow only: an answer to "anything else?" that ends the call
        # without a "no" word, and a bare "yes" (the whole utterance).
        "closing_words": ["감사합니다", "고맙습니다", "수고하세요", "끊을게요", "끊겠습니다",
                          "끝낼게요", "그만할게요"],
        "affirmative_pattern": r"^\s*((네|예|응|넵|그래요|있어요|있습니다)[\s.!,~?]*)+$",
        "faq_intro": "문의하신 내용을 안내해 드릴게요.",
        "faq_label": "자주 묻는 질문",
        "format_retry": "말씀하신 값이 형식에 맞지 않습니다.",
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
        "fallback_handoff": "I could not understand your request.",
        "more_help": "Is there anything else I can help you with?",
        "next_request": "How can I help you?",
        "thanks": "Thank you for contacting us. Have a great day.",
        "escalation_intro": "I will connect you to an agent. Everything you have "
                            "told me is passed along automatically.",
        "operation_joiner": ", ",
        "yes": "yes",
        "yes_synonyms": ["yeah", "yep", "yes please", "sure", "ok", "okay",
                         "correct", "right", "i do", "please do", "agreed"],
        "no": "no",
        "no_synonyms": ["nope", "nah", "no thanks", "no thank you", "not now",
                        "that is all", "thats all", "nothing else", "i am done",
                        "im good", "no i dont"],
        "closing_words": ["thank you", "thanks", "goodbye", "bye"],
        "affirmative_pattern": (r"^\s*(([Yy]es|[Yy]eah|[Yy]ep|[Yy]up|[Ss]ure|[Oo]kay|[Oo][Kk]|"
                                r"[Ii] do|[Pp]lease)[\s.!,?]*)+$"),
        "faq_intro": "Here is what I found on that.",
        "faq_label": "general questions",
        "format_retry": "That does not look like the right format.",
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
        "fallback_handoff": "ご要望を正確に理解できませんでした。",
        "more_help": "他にお手伝いできることはございますか？",
        "next_request": "ご用件をお伺いします。",
        "thanks": "お問い合わせありがとうございました。よい一日をお過ごしください。",
        "escalation_intro": "オペレーターにお繋ぎいたします。これまでにいただいた"
                            "情報は自動で引き継がれます。",
        "operation_joiner": "、",
        "yes": "はい",
        "yes_synonyms": ["ええ", "うん", "そう", "そうです", "お願いします",
                         "あります", "はいお願いします", "了解", "同意します"],
        "no": "いいえ",
        "no_synonyms": ["いや", "いえ", "ありません", "大丈夫です", "結構です",
                        "いらない", "不要です", "以上です"],
        "closing_words": ["ありがとう", "失礼します"],
        "affirmative_pattern": r"^\s*((はい|ええ|うん|あります|お願いします)[\s、。!！?？]*)+$",
        "faq_intro": "お問い合わせの内容についてご案内します。",
        "faq_label": "よくあるご質問",
        "format_retry": "入力された値の形式が正しくありません。",
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
    for notice in handoff_notice_plans(spec):
        resolved[notice["role"]] = notice["flow_id"]
    for plan in spec.get("flows") or []:
        if not isinstance(plan, dict):
            continue
        role, flow_id = plan.get("role"), plan.get("flow_id")
        if role in resolved and flow_id:
            resolved[role] = flow_id
    return resolved


#: How a plan or a model names a system hand-off when it does not use the flow
#: id: the role, in any casing or separator style, with or without "Flow".
_ROLE_ALIASES = {
    "welcome": "welcome", "greeting": "welcome",
    "fallback": "fallback", "unknown": "fallback",
    "escalation": "escalation", "escalate": "escalation", "handoff": "escalation",
    "followup": "followup", "follow": "followup", "anythingelse": "followup",
    "agentrequest": "agent_request", "requestagent": "agent_request", "agent": "agent_request",
    "faq": "faq", "knowledgebase": "faq",
}


def _loose(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def resolve_flow_reference(target, known_flow_ids, spec: Optional[dict] = None) -> str:
    """The flow id a hand-off means, given how plans and models actually write it.

    Live (TableNow, 2026-09-20): the confirmed plan said ``redirect_flow_id:
    "followup"`` — the ROLE — while the generated flow redirected to
    ``FollowUpFlow``; the determinism gate compared the strings and failed the
    flow, the retry then redirected to the literal ``followup``, which is not a
    bundled flow, and the two gates ping-ponged for five attempts. Resolve both
    sides through the same function: an exact id wins; otherwise a case- and
    separator-insensitive match against the known ids (with or without a
    trailing ``Flow``); otherwise a role alias mapped onto the system flow ids
    the spec resolves to. Anything else is returned unchanged.
    """
    raw = str(target or "").strip()
    if not raw:
        return raw
    known = [str(f) for f in (known_flow_ids or []) if f]
    if raw in known:
        return raw
    wanted = _loose(raw)
    for candidate in known:
        loose = _loose(candidate)
        if loose == wanted or loose == wanted + "flow" or wanted == loose + "flow":
            return candidate
    role = _ROLE_ALIASES.get(wanted) or _ROLE_ALIASES.get(wanted.removesuffix("flow"))
    if role:
        system_ids = resolve_system_flow_ids(spec or {})
        return system_ids.get(role, raw)
    return raw


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


# ---------------------------------------------------------------------------
# approved wording of the system flows
# ---------------------------------------------------------------------------
# The interview stores the sentence the customer approved for each plan step in
# its `template` ("the generator uses it verbatim"). Live (SELC e2e, 2026-09-26):
# the escalation plan carried the document's hand-off line "…이전 전달주신 정보는
# …" and the bundle said the builder's own "…이전에 전달해 주신 정보는 …"; the
# fallback plan's approved sentence was dropped the same way. A system flow
# speaks its plan's approved text and falls back to the language pack only when
# the plan has none.

#: A sentence ends at 。！？ (no space follows in Japanese) or at . ! ? + space.
_SENTENCE_BREAK = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])\s+")


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_BREAK.finditer(text):
        if m.start() > start and text[start:m.start()].strip():
            spans.append((start, m.start()))
        start = max(start, m.end())
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def split_sentences(text: str) -> list[str]:
    """The sentences of a customer-facing line, in order, terminators kept."""
    cleaned = " ".join(str(text or "").split())
    return [cleaned[a:b].strip() for a, b in _sentence_spans(cleaned)]


def sentence_key(text: str) -> str:
    """Comparison form of a sentence: whitespace collapsed, final punctuation dropped."""
    return re.sub(r"[\s.!?。！？]+$", "", " ".join(str(text or "").split()))


def _without_sentences(text: str, unwanted: set[str]) -> str:
    """*text* with every sentence whose key is in *unwanted* removed, the rest untouched."""
    cleaned = " ".join(str(text or "").split())
    out = cleaned
    for a, b in reversed(_sentence_spans(cleaned)):
        if sentence_key(cleaned[a:b]) in unwanted:
            out = out[:a] + out[b:]
    return " ".join(out.split())


def _split_last_sentence(text: str) -> tuple[str, str]:
    """(everything before the last sentence, the last sentence)."""
    cleaned = " ".join(str(text or "").split())
    spans = _sentence_spans(cleaned)
    if len(spans) < 2:
        return "", cleaned
    start = spans[-1][0]
    return cleaned[:start].strip(), cleaned[start:].strip()


def _plan_for_role(spec: dict, role: str) -> Optional[dict]:
    for plan in spec.get("flows") or []:
        if isinstance(plan, dict) and str(plan.get("role") or "") == role:
            return plan
    return None


def _step_templates(plan: Optional[dict], *, only: tuple = (), skip: tuple = ()) -> str:
    """The plan's approved step sentences in step order, joined — optionally only
    (or all but) the steps of the given node types."""
    if not isinstance(plan, dict):
        return ""
    steps = sorted((s for s in plan.get("steps") or [] if isinstance(s, dict)),
                   key=lambda s: s.get("step") or 0)
    chosen: list[str] = []
    for step in steps:
        node_type = str(step.get("node_type") or "").lower()
        if (only and node_type not in only) or node_type in skip:
            continue
        template = " ".join(str(step.get("template") or "").split())
        if template:
            chosen.append(template)
    return " ".join(chosen)


def approved_handoff_line(spec: dict) -> str:
    """The hand-off sentence the customer approved (the escalation plan's
    template), without the sentence of any route guardrail.

    Live (Hanbit e2e, 2026-09-26): the escalation plan's template began with the
    document's emergency line "응급 상황이면 119 또는 응급실(24시간)로 연락해
    주세요." — spoken in EscalationFlow it would tell every caller who asked for a
    person to call 119. That sentence belongs to its guardrail, which routes to
    its own HandoffFlow and says it there (and J9 says it inside a journey)."""
    line = _step_templates(_plan_for_role(spec, "escalation"))
    if not line:
        return ""
    notices = {sentence_key(s) for notice in handoff_notice_plans(spec)
               for s in split_sentences(notice["message"])}
    return _without_sentences(line, notices)


def _handoff_line(spec: dict, text: dict) -> str:
    return approved_handoff_line(spec) or text["escalation_intro"]


def _approved_closing(spec: dict) -> str:
    """The closing sentence the customer approved (SessionFlowConfig.common_closing)."""
    profile = spec.get("business_profile") or {}
    for key in ("closing", "common_closing", "closing_message"):
        value = profile.get(key) if isinstance(profile, dict) else None
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
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
        # A `help` flow the interview planned (Hanbit: "진료과·진료시간 안내", which
        # the greeting offers) is routable like an operation — the re-guide names
        # it too, when it has a name.
        role = plan.get("role", "operation") if isinstance(plan, dict) else None
        if role not in ("operation", "help"):
            continue
        if plan.get("customer_initiated") is False or str(plan.get("flow_id")) in not_routable:
            continue  # internal operation (e.g. call-result logging): never offered
        label = re.sub(r"\s+", " ", str(plan.get("display_name") or "")).strip(" .,;:-·")
        if not label or len(label) > _MAX_OPERATION_LABEL:
            if role == "help":
                continue
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
_WELCOME_CONTEXT = [{"name": FALLBACK_ATTEMPTS_VAR, "type": "number"},
                    {"name": WELCOME_GREETED_VAR, "type": "number"}]
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
    # (SessionFlowConfig.common_greeting / ContactFlowSpec.welcome_message), or
    # at least in the welcome plan's greeting step.
    # Live (GAON): composing one from the profile said "안녕하세요, gaon-aicc입니다"
    # — the project slug — because the profile had no company name.
    greeting = (_approved_greeting(spec)
                or _step_templates(_plan_for_role(spec, "welcome"), skip=("redirect", "escalate"))
                or _composed_greeting(spec, text, company))

    start = _node_id(flow_id, "start")
    guard = _node_id(flow_id, "greetedGuard")
    greet = _node_id(flow_id, "greeting")
    listen = _node_id(flow_id, "intentCapture")
    recognized = _node_id(flow_id, "redirectRecognized")
    unrecognized = _node_id(flow_id, "redirectFallback")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=(f"Greets the customer once, captures the intent with a User input "
                     f"node and redirects to the recognized flow. Unrecognized "
                     f"input goes to {ids['fallback']}; a re-entry skips the greeting."),
        ai_description=SYSTEM_FLOW_AI_DESCRIPTION,
        context_variables=_WELCOME_CONTEXT,
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(guard, "toGuard")]},
        # Greet once per session: a re-entry (the voice channel's structured
        # requests, an utterance routed back here) only listens again.
        guard: {"nodeId": guard, "type": "choice",
                "childNodes": [
                    _child(listen, "alreadyGreeted", [{
                        "left": {"type": "context", "name": WELCOME_GREETED_VAR},
                        "operator": "gte",
                        "right": {"type": "constant", "value": 1},
                    }]),
                    _child(greet, "firstVisit", []),
                ]},
        greet: {"nodeId": greet, "type": "basic",
                "messages": [_message(greeting)],
                # Reset here, not in FallbackFlow: a session that starts over
                # must not inherit the previous caller's failure count.
                "metadata": {"stateModifications": [_set_context(FALLBACK_ATTEMPTS_VAR, 0),
                                                    _set_context(WELCOME_GREETED_VAR, 1)]},
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
    plan = _plan_for_role(spec, "fallback")
    # The re-guide the customer approved for this flow, else the composed menu.
    reguide = (_step_templates(plan, skip=("redirect", "escalate"))
               or (text["reguide"].format(operations=text["operation_joiner"].join(labels))
                   if labels else text["reguide_no_operations"]))
    # The third miss only says why; EscalationFlow says the hand-off line next,
    # so a "connecting you" here was heard twice in a row (three times with the
    # escalate node's own line, before 2026-09-27).
    handoff_sentences = {sentence_key(s) for s in split_sentences(_handoff_line(spec, text))}
    approved_handoff = _step_templates(plan, only=("redirect", "escalate"))
    handoff = (_without_sentences(approved_handoff, handoff_sentences) if approved_handoff
               else text["fallback_handoff"])

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
        ai_description=SYSTEM_FLOW_AI_DESCRIPTION,
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
                                 messages=[_message(handoff)] if handoff else None),
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
    is what turned a one-answer bot into a multi-turn conversation live.

    Listen FIRST (R6b). Live (2026-09-21): after a booking the caller said
    "음 바꿔도 되나요?" and, earlier, "세척 예약도 해주실 수 있나요 근데 세척요금이
    어떻게 돼요" — a yes/no capture cannot route either (captured_flow is only
    set by a user_input node), so each cost a failure count or an extra turn.
    Now the question is asked by a message and the answer goes to a User input
    node: a request is routed at once; an answer that names no flow is tested
    for the language's "no" words (System.utterance contains, the operand E2
    verified live) and ends the session; anything else is asked "anything
    else? yes/no" once, the way it was before, so a bare "yes" still leads to
    "how can I help you?" and a listen.

    The attached slot is cleared at the START of the flow (R6): slot values
    persist for the whole session, and a filled ``moreHelp`` would answer the
    capture node for the customer on its second visit.

    An answer that names no flow still CAPTURES one. Live (sandbox,
    2026-09-27): after an FAQ answer, "아니요 없어요" took the ``captured_flow
    exists`` edge with ``System.capturedFlow`` = ``FallbackFlow`` — the
    application's ``unknown`` default — and got the re-guide ("죄송합니다, 잘
    이해하지 못했습니다…"); the "no" words were never tested. The listen
    therefore compares the captured flow with the unknown default first
    (``captured_flow eq <flow id>``, live-verified) and treats it as "no flow".
    A bare "yes" (the whole utterance matches the language's affirmative
    pattern, ``matches_regex`` on ``System.utterance``, live-verified) is asked
    what they need; a second User input in the same flow on the same turn is
    evaluated against the same utterance instead of waiting (live: "네" →
    "무엇을 도와드릴까요?" → the re-guide at once), so the next request is heard
    by the welcome flow's listen, which a re-entry reaches without greeting.
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids["followup"]
    language = system_flow_language(spec)
    text = _texts(language)
    root = str(language or "").replace("_", "-").split("-")[0].lower()

    start = _node_id(flow_id, "start")
    close_guard = _node_id(flow_id, "journeyDoneGuard")
    close_now = _node_id(flow_id, "closeAfterJourney")
    clear = _node_id(flow_id, "clearSlot")
    ask = _node_id(flow_id, "askMoreHelp")
    listen = _node_id(flow_id, "listenFirst")
    recognized = _node_id(flow_id, "redirectRecognized")
    decide = _node_id(flow_id, "decideOnAnswer")
    confirm = _node_id(flow_id, "askMoreHelpYesNo")
    branch = _node_id(flow_id, "branchOnAnswer")
    prompt = _node_id(flow_id, "askNextRequest")
    next_listen = _node_id(flow_id, "redirectWelcomeListen")
    thanks = _node_id(flow_id, "thanks")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=("Asked after an operation completes: offers further help, "
                     "routes the next request at once, or ends the session politely."),
        ai_description=SYSTEM_FLOW_AI_DESCRIPTION,
        context_variables=_FALLBACK_ATTEMPTS_CONTEXT + [{"name": JOURNEY_DONE_VAR, "type": "number"}],
        slot_types=[{
            "name": MORE_HELP_SLOT_NAME,
            "type": YES_NO_SLOT_TYPE_ID,
            "sensitive": False,
            "aiDescription": "Whether the customer wants further help: yes or no",
        }],
    )
    # `contains` is a substring test: an English "no" would match "I don't know",
    # so only words of three characters or more are tested (Korean and Japanese
    # negatives are distinctive at two characters). The language is a locale
    # ("ko-KR"), so compare its root: before 2026-09-27 "아뇨" and "아니" were
    # dropped from every Korean flow.
    closing_words = []
    for word in [text["no"], *text.get("no_synonyms", []), *text.get("closing_words", [])]:
        if word and (len(word) >= 3 or root in ("ko", "ja")) and word not in closing_words:
            closing_words.append(word)
    closing = _approved_closing(spec) or text["thanks"]
    unknown_edges = [
        _child(decide, f"unknownDefault:{target}", [{
            "left": {"type": "captured_flow"}, "operator": "eq",
            "right": {"type": "constant", "value": target}}])
        for target in unknown_default_flow_ids(spec, ids)
    ]
    yes_edges = ([_child(prompt, "yes", [{
        "left": {"type": "system", "name": "System.utterance"},
        "operator": "matches_regex",
        "right": {"type": "constant", "value": text["affirmative_pattern"]}}])]
        if text.get("affirmative_pattern") else [])
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(close_guard, "toGuard")]},
        # A carrying journey that already heard "no, nothing else" hands over
        # with journeyDone = 1: close at once instead of asking again.
        close_guard: {"nodeId": close_guard, "type": "choice",
                      "childNodes": [
                          _child(close_now, "customerIsDone", [{
                              "left": {"type": "context", "name": JOURNEY_DONE_VAR},
                              "operator": "gte",
                              "right": {"type": "constant", "value": 1},
                          }]),
                          _child(clear, "askMoreHelp", []),
                      ]},
        close_now: {"nodeId": close_now, "type": "basic",
                    "messages": [_message(closing)],
                    "metadata": {"stateModifications": [_set_context(JOURNEY_DONE_VAR, 0),
                                                        _set_context(FALLBACK_ATTEMPTS_VAR, 0)]},
                    "childNodes": [_child(end, "toEnd")]},
        clear: {"nodeId": clear, "type": "basic",
                "metadata": {"stateModifications": [
                    {"type": "slot", "name": MORE_HELP_SLOT_NAME,
                     "modification": "clear"},
                    # The customer got an answer, so the failure streak is over.
                    _set_context(FALLBACK_ATTEMPTS_VAR, 0)]},
                "childNodes": [_child(ask, "next")]},
        ask: {"nodeId": ask, "type": "basic",
              "messages": [_message(text["more_help"])],
              "childNodes": [_child(listen, "listen")]},
        # Every edge tests captured_flow, so the node waits for the answer.
        listen: {"nodeId": listen, "type": "user_input",
                 "childNodes": [
                     *unknown_edges,
                     _child(recognized, "flowRecognized", _captured_flow_condition(True)),
                     _child(decide, "noFlowRecognized", _captured_flow_condition(False)),
                 ]},
        recognized: _redirect_node(recognized, CAPTURED_FLOW_PLACEHOLDER, end),
        decide: {"nodeId": decide, "type": "choice",
                 "childNodes": [
                     *[_child(thanks, f"no:{word}", [_utterance_contains(word)]) for word in closing_words],
                     *yes_edges,
                     _child(confirm, "unclear", []),
                 ]},
        confirm: {"nodeId": confirm, "type": "user_choice",
                  "messages": [_message(text["more_help"])],
                  "metadata": {
                      # S2: slotTypeId is stored verbatim as the internal slotId, so
                      # it must be the attached slot's NAME, not the slot type id.
                      "choice": {"source": "slotType", "slotTypeId": MORE_HELP_SLOT_NAME},
                  },
                  "childNodes": [
                      _child(branch, "captured", _slot_condition(MORE_HELP_SLOT_NAME, True)),
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
                 "childNodes": [_child(next_listen, "listen")]},
        # The welcome flow's re-entry skips the greeting and listens (and waits).
        next_listen: _redirect_node(next_listen, ids["welcome"], end),
        thanks: {"nodeId": thanks, "type": "basic",
                 "messages": [_message(closing)],
                 "childNodes": [_child(end, "toEnd")]},
        end: {"nodeId": end, "type": "end"},
    }
    return flow


def unknown_default_flow_ids(spec: dict, ids: Optional[dict] = None) -> list[str]:
    """Flow ids the application's ``unknown`` default behaviour resolves to — the
    flow a User input "captures" when the utterance matches no flow (live
    2026-09-27). Mirrors :func:`tools.acxd_resource_builders.build_application`:
    an explicit ``application.default_flows.unknown``, a flow planned with role
    ``unknown``, then the fallback flow."""
    ids = ids or _ids(spec, None)
    out: list[str] = []
    explicit = ((spec.get("application") or {}).get("default_flows") or {}).get("unknown")
    planned = [p.get("flow_id") for p in (spec.get("flows") or [])
               if isinstance(p, dict) and p.get("role") == "unknown"]
    for candidate in (explicit, *planned, ids.get("fallback")):
        if candidate and str(candidate) not in out:
            out.append(str(candidate))
    return out


def _utterance_contains(word: str) -> dict:
    """The one operand shape a `System.utterance` test matches at runtime (E2)."""
    return {"left": {"type": "system", "name": "System.utterance"},
            "operator": "contains",
            "right": {"type": "constant", "value": word}}


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
    kb_name = knowledge_base_name(spec) or "FAQ"

    start = _node_id(flow_id, "start")
    answer = _node_id(flow_id, "knowledgeBase")
    say = _node_id(flow_id, "sayAnswer")
    follow_up = _node_id(flow_id, "redirectFollowUp")
    no_answer = _node_id(flow_id, "redirectFallback")
    end = _node_id(flow_id, "end")

    flow = _flow_shell(
        flow_id, language,
        untrained=False,
        description=f"Routable FAQ entry; answers from knowledge base {kb_name} and hands over to {ids['followup']}.",
        ai_description=FAQ_AI_DESCRIPTION,
        context_variables=[],
    )
    # The knowledge base node speaks nothing itself: its answer lands in the
    # output variable `metadata.knowledgeBase.name`, and a message downstream
    # must reference it (`{<name>.answer:NLX.Local}`). Live (sandbox, 2026-09-27):
    # the node retrieved "소아청소년과의 진료시간은 …" (confidence 95) and the caller
    # heard only the follow-up question — every FAQ answer was dropped.
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(answer, "toKnowledgeBase")]},
        answer: {"nodeId": answer, "type": "knowledge_base",
                 "metadata": {"knowledgeBase": {"knowledgeBaseId": f"{{KB:{kb_name}}}", "name": KB_ANSWER_VAR}},
                 # no_match first: the service takes a `success` edge on no_match
                 # too, with an empty answer (deployed runtime, 2026-09-28).
                 "childNodes": [_child(no_answer, "noMatch", [_node_status("no_match")]),
                                _child(say, "answered", [_node_status("success")]),
                                _child(no_answer, "notAnswered", [_node_status("failure")]),
                                _child(no_answer, "timedOut", [_node_status("timeout")])]},
        say: {"nodeId": say, "type": "basic",
              "messages": [_message(kb_answer_placeholder(KB_ANSWER_VAR))],
              "childNodes": [_child(follow_up, "toFollowUp")]},
        follow_up: _redirect_node(follow_up, ids["followup"], end),
        # No answer in the knowledge base: the fallback re-guides and counts the miss.
        no_answer: _redirect_node(no_answer, ids["fallback"], end),
        end: {"nodeId": end, "type": "end"},
    }
    return flow


# ---------------------------------------------------------------------------
# EscalationFlow (R7)
# ---------------------------------------------------------------------------

def build_escalation_flow(spec: dict, *, flow_ids: Optional[dict] = None) -> dict:
    """The hand-off line, then a TERMINAL escalate node.

    No ``end`` after the escalate: with one, Connect saw the Agentic CX block's
    Success branch instead of Escalation and the caller was never transferred
    (live, R7). The queue is chosen by the contact flow's Escalation branch, so
    the node carries only messages and the failReason handover.

    The line is the one the customer approved in the escalation plan
    (:func:`approved_handoff_line`), else the language pack's. Its LAST sentence
    rides on the escalate node itself and anything before it on a basic node, so
    the caller hears the approved text once and nothing else — the builder's
    own "connecting you now, please hold" after it said the same thing twice.
    A one-sentence line is carried by the escalate node alone.
    """
    ids = _ids(spec, flow_ids)
    flow_id = ids["escalation"]
    language = system_flow_language(spec)
    text = _texts(language)
    lead, last = _split_last_sentence(_handoff_line(spec, text))

    start = _node_id(flow_id, "start")
    intro = _node_id(flow_id, "handoffMessage")
    escalate = _node_id(flow_id, "escalate")

    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=("Plays the handoff message and escalates to a human agent; "
                     "failReason is passed to the contact flow."),
        ai_description=SYSTEM_FLOW_AI_DESCRIPTION,
        context_variables=_FAIL_REASON_CONTEXT,
    )
    nodes: dict = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(intro if lead else escalate, "start")]},
    }
    if lead:
        nodes[intro] = {"nodeId": intro, "type": "basic",
                        "messages": [_message(lead)],
                        "childNodes": [_child(escalate, "toEscalate")]}
    # TERMINAL — no childNodes.
    nodes[escalate] = _escalate_node(escalate, last)
    flow["nodes"] = nodes
    return flow


def _escalate_node(node_id: str, line: str) -> dict:
    return {"nodeId": node_id, "type": "escalate",
            "messages": [_message(line)],
            "metadata": {"stateModifications": [{
                "type": "context", "name": FAIL_REASON_VAR,
                "modification": "set",
                "value": {"type": "variable", "name": FAIL_REASON_VAR},
            }]}}


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


def format_retry_message(language: str) -> str:
    """'That value does not fit the format', in the project language. The capture
    node re-asks with its own prompt right after, so the hint is not repeated."""
    return _texts(language)["format_retry"]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def build_handoff_notice_flow(spec: dict, role: str, *, flow_ids: Optional[dict] = None) -> Optional[dict]:
    """The mandated sentence of one route guardrail, then a TERMINAL escalate.

    Same shape as the escalation flow (no ``end`` after the escalate — R7), so
    the caller who said a trigger word hears the sentence the requirements
    dictate and reaches the contact flow's Escalation branch.
    """
    notice = next((n for n in handoff_notice_plans(spec) if n["role"] == role), None)
    if notice is None:
        return None
    flow_id = notice["flow_id"]
    language = system_flow_language(spec)
    text = _texts(language)
    start = _node_id(flow_id, "start")
    intro = _node_id(flow_id, "notice")
    escalate = _node_id(flow_id, "escalate")
    # description is ASCII-only metadata: Korean trigger words would leave
    # "(trigger words: , , )" behind (Hanbit review, 2026-09-27).
    words = ", ".join(k for k in notice["keywords"][:6] if k.isascii() and k.strip())
    name = str(notice["name"] or "")
    flow = _flow_shell(
        flow_id, language, untrained=True,
        description=((f"Says the mandated notice for guardrail {name!r}" if name.isascii() and name.strip()
                      else "Says a route guardrail's mandated notice")
                     + (f" (trigger words: {words})" if words else "")
                     + " and escalates to a human agent."),
        ai_description=SYSTEM_FLOW_AI_DESCRIPTION,
        context_variables=_FAIL_REASON_CONTEXT,
    )
    flow["nodes"] = {
        start: {"nodeId": start, "type": "start",
                "childNodes": [_child(intro, "start")]},
        intro: {"nodeId": intro, "type": "basic",
                "messages": [_message(notice["message"])],
                "childNodes": [_child(escalate, "toEscalate")]},
        # TERMINAL — no childNodes. The same hand-off line as EscalationFlow:
        # the notice, then the sentence every transfer uses.
        escalate: _escalate_node(escalate, _handoff_line(spec, text)),
    }
    return flow


SYSTEM_FLOW_BUILDERS: dict[str, Callable[..., dict]] = {
    "welcome": build_welcome_flow,
    "fallback": build_fallback_flow,
    "escalation": build_escalation_flow,
    "followup": build_follow_up_flow,
    "agent_request": build_request_agent_flow,
    "faq": build_faq_flow,
}


def is_system_flow_role(role: Optional[str]) -> bool:
    return str(role or "") in SYSTEM_FLOW_BUILDERS or str(role or "").startswith(HANDOFF_NOTICE_ROLE_PREFIX)


def build_system_flow(role: str, spec: dict, *,
                      flow_ids: Optional[dict] = None) -> Optional[dict]:
    """Deterministic flow document for *role*, or ``None`` for an unknown role."""
    if str(role or "").startswith(HANDOFF_NOTICE_ROLE_PREFIX):
        return build_handoff_notice_flow(spec, str(role), flow_ids=flow_ids)
    builder = SYSTEM_FLOW_BUILDERS.get(str(role or ""))
    if builder is None:
        return None
    return builder(spec, flow_ids=flow_ids)
