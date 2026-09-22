"""ACXD resource builders: Knowledge Base, Guardrails, Application (Task 9).

Deterministic builders from the interview spec (D2). The only LLM-shaped
work here — writing KB articles from bare topics — goes through the same
injectable-``invoke`` harness pattern as the flow generator, so the
deterministic paths stay fully unit-tested.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from strands import tool

from tools.acxd_generation_context import get_acxd_spec
from tools.session_context import current_session_id
from tools.validate_acxd_flow import validate_acxd_asset

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------

def sanitize_kb_name(raw: str | None, fallback: str = "AICC FAQ") -> str:
    """Coerce a KB name into the ACXD charset (alphanumeric + space/-/_).

    Non-ASCII names (e.g. Korean '가온물류 FAQ') are stripped by the
    API's charset rule, which previously produced an empty/invalid name and
    blocked every flow-generation attempt (found in live QA). Transliterate
    what we can, then fall back.
    """
    if not raw:
        return fallback
    kept = re.sub(r"[^A-Za-z0-9 _-]", " ", raw)
    kept = re.sub(r"\s+", " ", kept).strip(" -_")
    return (kept or fallback)[:100]


def build_knowledge_base(spec: dict) -> Optional[dict]:
    """KB document from the spec (None when the interview defined no KB)."""
    plan = spec.get("knowledge_base") or {}
    articles = plan.get("articles") or []
    if not plan.get("name") and not articles and not plan.get("topics"):
        return None
    profile = spec.get("business_profile") or {}
    company = profile.get("company_name") or "AICC"
    name = sanitize_kb_name(plan.get("name"),
                            fallback=sanitize_kb_name(f"{company} FAQ", "AICC FAQ"))

    doc = {
        "name": name[:100],
        "type": "articles",
        "description": (plan.get("description")
                        or f"FAQ knowledge base for {profile.get('company_name', 'the business')}")[:200],
        "response": {"summarize": True, "minConfidenceScore": 70, "k": 3},
        "articles": [
            {
                "question": {"text": a["question"]},
                "responses": [{"type": "text", "body": a["answer"]}],
                **({"tags": a["tags"][:5]} if a.get("tags") else {}),
            }
            for a in articles
            if isinstance(a, dict) and a.get("question") and a.get("answer")
        ],
    }
    return doc


def build_kb_article_prompt(topics: list[str], spec: dict) -> str:
    profile = spec.get("business_profile") or {}
    return (
        "Write FAQ articles for these topics as a JSON array of "
        '{"question": "...", "answer": "...", "tags": ["..."]} objects. '
        "Answers must reflect the business profile exactly; do not invent "
        "policies that were not stated.\n\n"
        f"Business profile: {json.dumps(profile, ensure_ascii=False)}\n"
        f"Topics: {json.dumps(topics, ensure_ascii=False)}\n"
        "Return ONLY the JSON array."
    )


def run_kb_article_generation(
    topics: list[str], spec: dict, invoke: Callable[[str], str],
) -> tuple[list[dict], list[str]]:
    """Generate article plans from topics via the injectable LLM call."""
    response = invoke(build_kb_article_prompt(topics, spec))
    match = re.search(r"\[.*\]", str(response), re.DOTALL)
    if not match:
        return [], ["LLM response did not contain a JSON array"]
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return [], [f"invalid JSON from LLM: {e}"]
    articles, problems = [], []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("question") or not item.get("answer"):
            problems.append(f"article[{i}] missing question/answer")
            continue
        articles.append({"question": item["question"], "answer": item["answer"],
                         "tags": item.get("tags") or []})
    return articles, problems


# ---------------------------------------------------------------------------
# Slot types and Contact Flow context variables
# ---------------------------------------------------------------------------

def _is_single_placeholder_slot_type(doc: dict) -> bool:
    """True when this "slot type" holds no real value set.

    S5 (live-verified): a custom slot type with ONE sample value deploys as a
    one-item menu and is auto-selected without ever asking the customer — the
    order-number question was skipped and the flow ran with a literal
    ``orderNumber`` as the order number. The derivation no longer produces
    these (open values become an ``NLX.*`` attached slot with a regex); this
    guard catches hand-written or legacy specs that still carry one.
    """
    values = [str((v or {}).get("value") or "") for v in doc.get("values") or []
              if isinstance(v, dict)]
    if len(values) != 1:
        return False
    slot_type_id = str(doc.get("slotTypeId") or "")
    only = values[0]
    return only.lower() in {slot_type_id.lower(), "stub", "placeholder", ""}


def build_slot_types(spec: dict) -> tuple[list[dict], list[str]]:
    """Validate adapter-derived ACXD SlotType documents deterministically.

    Also emits the ``yesNo`` slot type: ACXD has no boolean built-in (S4) and
    ``FollowUpFlow`` — which every operation's success path redirects to — needs
    it for its "anything else?" question.
    """
    docs: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(spec.get("slot_types") or []):
        if not isinstance(raw, dict):
            problems.append(f"slot_types[{index}]: not an object")
            continue
        doc = {
            key: value for key, value in raw.items()
            if key in {"slotTypeId", "values", "sensitive", "description", "metadata",
                       "mainLanguageCode", "languageCodes"}
        }
        slot_type_id = doc.get("slotTypeId")
        if not slot_type_id or slot_type_id in seen:
            problems.append(f"slot_types[{index}]: missing or duplicate slotTypeId")
            continue
        seen.add(slot_type_id)
        if _is_single_placeholder_slot_type(doc):
            problems.append(
                f"slot_types[{index}] ({slot_type_id}): a custom slot type with one "
                "sample value deploys as a one-item menu and is auto-selected without "
                "asking (S5) — the slot must attach a built-in "
                "(NLX.AlphaNumeric / NLX.Number / NLX.PhoneNumber / NLX.Text) with a "
                "regex instead; not emitted")
            continue
        errors = validate_acxd_asset("slot_type", doc)
        if errors:
            problems.extend(f"slot_types[{index}] ({slot_type_id}): {error}" for error in errors)
            continue
        # Slot type `description` is ACXD metadata and must be ASCII; the values
        # and synonyms themselves stay in the project language.
        from tools.acxd_bundle import enforce_ascii_metadata
        docs.append(enforce_ascii_metadata(doc))

    # Only for a real generation context: the deterministic-repair path passes a
    # bare {"slot_types": [...]} with no language, and guessing English there
    # would overwrite a Korean project's yesNo values.
    if any(spec.get(key) for key in ("flows", "application", "business_profile")):
        from tools.acxd_system_flows import build_yes_no_slot_type

        yes_no = build_yes_no_slot_type(spec)
        if yes_no["slotTypeId"] not in seen:
            errors = validate_acxd_asset("slot_type", yes_no)
            if errors:  # pragma: no cover - deterministic document
                problems.extend(f"slot_types[yesNo]: {error}" for error in errors)
            else:
                docs.append(yes_no)
    return docs, problems


MAX_CONTEXT_VARIABLES = 10
HANDOFF_FAIL_REASON = "failReason"
#: Slot names an agent hand-off carries when the flows collect them — the
#: values a CRM screen-pop needs. Matched case-insensitively as substrings.
_HANDOFF_SLOT_HINTS = ("customername", "name", "phone", "address", "ordernumber", "orderid",
                       "reservationid", "reservationnumber", "email", "membernumber", "accountnumber")


def effective_context_variables(spec: dict) -> list[dict]:
    """The application's context variables: what the interview declared, plus
    what an agent hand-off needs — `failReason` (the EscalationFlow sets it) and
    the identity / reference slots the operation flows collect — within the
    Agentic CX block's limit of 10.

    Live (SELC, 2026-09-21): the Contact Flow's escalation payload read eight
    `$.AgenticCX.ContextVariables.*` names, the application declared one
    (`customerPhone`), and the review blocked on D9-6; the values themselves
    were never set by any flow. One list, used by the resource builder, the
    Contact Flow binding and the runtime contract (R9 copies the slots into
    these variables at hand-off), keeps the three in step."""
    app = spec.get("application") or {}
    variables: list[dict] = []
    seen: set[str] = set()

    def _add(name: str, description: str = "", var_type: str = "string", source=None) -> None:
        if not name or name in seen or len(variables) >= MAX_CONTEXT_VARIABLES:
            return
        seen.add(name)
        variables.append({"name": name, "type": var_type, "description": description,
                          "fromContactAttribute": source})

    for raw in (app.get("context_variables") or []):
        if isinstance(raw, dict) and raw.get("name"):
            _add(str(raw["name"]), str(raw.get("description") or ""), str(raw.get("type") or "string"),
                 raw.get("from_contact_attribute"))
    _add(HANDOFF_FAIL_REASON, "Why the conversation was handed to an agent (set by the escalation flow)")
    for plan in spec.get("flows") or []:
        if not isinstance(plan, dict) or str(plan.get("role") or "operation") != "operation":
            continue
        for slot in plan.get("slots") or []:
            name = str((slot or {}).get("name") or "") if isinstance(slot, dict) else ""
            lowered = re.sub(r"[^a-z0-9]", "", name.lower())
            if lowered and any(h in lowered for h in _HANDOFF_SLOT_HINTS) and lowered not in ("yesno",):
                _add(name, f"Collected by the conversation and passed to the agent on hand-off")
    return variables


def build_context_variables(spec: dict) -> list[dict]:
    """Render the bounded Agentic CX context-variable contract."""
    return effective_context_variables(spec)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def _resolve_detection(plan: dict) -> dict:
    """Deterministic detection-method selection.

    explicit regex   → regex (pattern required in plan.examples[0] or policy)
    explicit keyword → keyword from examples
    'auto'           → keyword when short examples exist, else llmJudge
    """
    method = plan.get("detection_method", "auto")
    examples = [e for e in plan.get("examples") or [] if isinstance(e, str) and e]
    if method == "regex":
        pattern = (examples[0] if examples else plan.get("pattern")) or ""
        # Live (Hanbit): the interview stored the SAMPLE phone number
        # "010-1111-2222" as the "regex" of a PII guardrail, so only that one
        # literal was ever masked. A literal (no regex metacharacters) is
        # generalised: every digit run becomes a digit class of the same width.
        if pattern and not re.search(r"[\\^$.*+?()\[\]{}|]", pattern):
            pattern = re.sub(r"\d+", lambda m: "\\d{%d}" % len(m.group(0)), pattern)
        return {"method": "regex", "pattern": pattern[:200]}
    if method == "keyword" or (
        method == "auto" and examples and all(len(e) <= 50 for e in examples)
    ):
        keywords = [e[:50] for e in examples][:200]
        if keywords:
            return {"method": "keyword", "keywords": keywords}
        # Live (2026-09-17): a keyword rule the interview left without examples
        # shipped as keywords ["placeholder"] — a rule that can never fire. With
        # nothing to match, judge the policy instead (a judged input route is
        # then kept advisory by _keep_derived_input_routes_advisory).
    prompt = (
        "Decide whether the following message violates this policy. "
        f"Policy: {plan.get('policy', '')} "
        "Judge only what the message itself does: it violates the policy only "
        "when its own content does what the policy forbids. Greetings, questions "
        "to the customer, confirmations, scheduling, look-up results and "
        "hand-offs to a human never violate it."
    )
    return {"method": "llmJudge", "prompt": prompt[:4000], "threshold": 0.8}


#: What the customer hears when an output guardrail replaces a message and the
#: interview gave no wording. Live (Hanbit): the English default "I can't help
#: with that." was spoken to Korean callers.
_GUARDRAIL_MODIFY_MESSAGES = {
    "ko": "죄송합니다. 해당 내용은 안내해 드릴 수 없습니다. 다른 도움이 필요하시면 말씀해 주세요.",
    "ja": "申し訳ありません。その内容はご案内できません。他にご用件があればお知らせください。",
    "en": "I'm sorry, I can't help with that. Let me know if there is anything else I can do.",
}


def _resolve_enforcement(plan: dict, spec: Optional[dict] = None) -> dict:
    action = plan.get("action", "flag")
    if action == "route":
        return {"action": "route",
                "behavior": {"flowId": plan.get("route_flow_id") or ""}}
    if action == "mask":
        return {"action": "mask", "behavior": {"maskText": "[REDACTED]"}}
    if action == "modify":
        message = plan.get("message")
        if not message or message.strip() == "I can't help with that.":
            try:
                from tools.acxd_system_flows import system_flow_language
                language = system_flow_language(spec or {})
            except Exception:  # pragma: no cover - defensive
                language = "en-US"
            message = _GUARDRAIL_MODIFY_MESSAGES.get(str(language)[:2].lower(), _GUARDRAIL_MODIFY_MESSAGES["en"])
        return {"action": "modify", "behavior": {"message": message[:500]}}
    return {"action": "flag"}


#: Written into a downgraded rule's ``description`` (schema cap: 100 chars) so a
#: reviewer or a later edit sees the flag is a decision, not an omission.
_ADVISORY_RULE_NOTE = ("AICC: derived output rule kept as flag (false positive would mute the bot); "
                       "plan.message => modify")
assert len(_ADVISORY_RULE_NOTE) <= 100
_ADVISORY_INPUT_RULE_NOTE = ("AICC: derived input rule kept as flag (a false positive hijacks the call); "
                             "hand-off = journey exits")
assert len(_ADVISORY_INPUT_RULE_NOTE) <= 100


def _mask_trigger(plan: dict) -> str:
    """The trigger a guardrail rule runs on.

    A PII ``mask`` belongs on what the CUSTOMER says: that is where a phone
    number or an address enters the transcript. On ``output`` the same regex
    rewrites the bot's own words — live (2026-09-14 and again 2026-09-17) it
    turned "010-1234-5678 형식으로 말씀해 주세요" into "[REDACTED] 형식으로", twice,
    the second time with a regex the review had written on purpose. So a mask
    planned on ``output`` runs on ``input`` unless the plan says the bot's own
    replies must be masked (``mask_bot_output: true``).
    """
    trigger = plan.get("trigger", "input")
    if (trigger == "output" and plan.get("action") == "mask"
            and not plan.get("mask_bot_output")):
        logger.info("[ACXDGuardrails] %s: PII mask moved from output to input — an output "
                    "mask redacts the bot's own format hints", plan.get("name"))
        return "input"
    return trigger


def _keep_derived_output_rules_advisory(doc: dict, plan: dict) -> None:
    """An output guardrail whose detection the builder DERIVED must not rewrite
    what the caller hears.

    Live (Hanbit, 2026-09-14): an llmJudge output rule replaced the greeting
    itself with the refusal message, and a regex generalised from a sample phone
    number redacted the bot's own "010-1234-5678 형식으로" hint. A false positive
    on an output rule silences the assistant; a missed one is only a log line. So
    a derived output rule (judge, keywords, or a generalised literal) with a
    message-altering action is kept as ``flag``; an explicit regex the interview
    wrote itself (``pattern``) keeps its ``mask`` / ``modify``.
    """
    if doc.get("trigger") != "output":
        return
    rule = doc["rules"][0]
    action = (rule.get("enforcement") or {}).get("action")
    if action not in ("mask", "modify", "block"):
        return
    if action == "modify" and isinstance(plan.get("message"), str) and plan["message"].strip():
        return  # the interview wrote the replacement itself: a deliberate design
    method = (rule.get("detection") or {}).get("method")
    if method == "regex":
        examples = [e for e in plan.get("examples") or [] if isinstance(e, str) and e]
        source = plan.get("pattern") or (examples[0] if examples else "")
        # a real regex (metacharacters present) was written on purpose; a plain
        # literal was generalised by the builder and is only a guess at intent
        if isinstance(source, str) and re.search(r"[\\^$.*+?()\[\]{}|]", source):
            return
    rule["enforcement"] = {"action": "flag"}
    # Leave the reason ON the rule: live (2026-09-16) the reviewer read the
    # flag as a defect ("the spec says modify"), the orchestrator hand-patched
    # the action back to modify without a behavior.message, and the service
    # rejected the guardrail at deploy time.
    rule["description"] = _ADVISORY_RULE_NOTE
    logger.info("[ACXDGuardrails] %s: output rule with derived %s detection kept advisory "
                "(flag) instead of %s — a false positive would silence the assistant",
                doc.get("name"), method, action)


def _keep_derived_input_routes_advisory(doc: dict, plan: dict) -> None:
    """An input guardrail whose detection the builder DERIVED must not take the
    conversation away from the flow.

    Live (2026-09-17): an llmJudge input rule derived from the escalation policy
    ("refund/claim requests go to an agent", threshold 0.8) fired on a customer
    describing a cleaning order in detail — "냄새가 나서 … 종합으로 세척받고 싶어요"
    — and ``route`` sent the caller to the escalation flow before the journey
    could collect anything. A false positive on a routing rule ends the
    self-service; a missed one costs only a log line. So an llmJudge input rule
    with ``route`` / ``block`` is kept as ``flag``; keyword and regex rules match
    deterministically and keep their action, and topic-based hand-off belongs to
    the journey's own exit conditions, judged with the conversation in view.
    """
    if doc.get("trigger", "input") != "input":
        return
    rule = doc["rules"][0]
    action = (rule.get("enforcement") or {}).get("action")
    if action not in ("route", "block"):
        return
    method = (rule.get("detection") or {}).get("method")
    if method != "llmJudge":
        return  # keywords and regexes match deterministically: the interview's own rule
    rule["enforcement"] = {"action": "flag"}
    rule["description"] = _ADVISORY_INPUT_RULE_NOTE
    logger.info("[ACXDGuardrails] %s: input rule with derived %s detection kept advisory "
                "(flag) instead of %s — a false positive would hijack the conversation",
                doc.get("name"), method, action)


def _project_scoped_name(spec: dict, name: str) -> str:
    """``<projectSlug>-<name>`` (ASCII, ≤100 chars) when the spec knows its project."""
    slug = re.sub(r"[^A-Za-z0-9]", "", str(((spec or {}).get("infrastructure") or {}).get("project_name") or ""))
    if not slug or name.lower().startswith(slug.lower()):
        return name[:100]
    return f"{slug}-{name}"[:100]


def _guardrail_name(plan: dict, index: int) -> str:
    """A usable name for a guardrail the interview left unnamed.

    Live: one nameless entry made `generate_acxd_guardrails` return an error and
    store NOTHING, on every retry, because the caller only saves docs when the
    problem list is empty. The spec kept the nameless entry, so the step was
    permanently blocked — the agent correctly reported "도구 버그로 막혀 있다"
    and stopped. A guardrail's identity is its policy, not its label, so derive
    the label rather than discarding the policy.
    """
    source = str(plan.get("policy") or plan.get("description")
                 or plan.get("trigger") or "").strip()
    words = re.findall(r"[A-Za-z0-9]+", source)[:4]
    if words:
        head, *rest = words
        derived = head.lower() + "".join(w[:1].upper() + w[1:] for w in rest)
        if len(derived) >= 3:
            return derived[:100]
    return f"guardrail{index + 1}"


def build_guardrails(spec: dict) -> tuple[list[dict], list[str]]:
    """Guardrail documents from the spec plans. Returns (docs, problems).

    One unusable entry must not sink the batch: every repairable plan is still
    emitted, and only genuinely unfixable ones are reported.
    """
    docs, problems = [], []
    for i, plan in enumerate(spec.get("guardrails") or []):
        if not isinstance(plan, dict):
            problems.append(f"guardrails[{i}]: not an object")
            continue

        name = plan.get("name")
        if not name:
            name = _guardrail_name(plan, i)
            logger.info("[ACXDGuardrails] guardrails[%d] had no name; using %r",
                        i, name)
        name = str(name)[:100]
        # LIVE-API constraint (found by a real deploy, 2026-09-08): guardrail
        # and rule names must be ASCII — a Korean name fails upsert-guardrails
        # with "name is not in the expected format". Derive an English name
        # from the policy text instead of shipping one that cannot deploy; the
        # Korean policy itself stays in the detection prompt and messages.
        if any(ord(c) > 126 for c in name):
            derived = _guardrail_name(plan, i)
            logger.info("[ACXDGuardrails] guardrails[%d] name %r is non-ASCII; "
                        "using %r (the live API rejects non-ASCII names)",
                        i, name, derived)
            name = derived

        # Guardrails are workspace-level resources keyed by name. Live: three
        # projects in one workspace all produced 'guardrail1'/'guardrail2' and
        # each deploy overwrote the others' rules. Prefix with the project slug.
        name = _project_scoped_name(spec, name)

        action = plan.get("action")
        route_target = plan.get("route_flow_id")
        if action == "route" and str(plan.get("message") or "").strip():
            # A route rule with a mandated sentence goes to the flow that says
            # it (built deterministically by tools.acxd_system_flows), not to
            # the generic escalation line.
            from tools.acxd_system_flows import handoff_notice_plans
            notice = next((n for n in handoff_notice_plans(spec) if n["name"] == plan.get("name")), None)
            if notice:
                route_target = notice["flow_id"]
                plan = {**plan, "route_flow_id": route_target}
        if action == "route" and not route_target:
            # Prefer an escalation flow the bundle actually has; otherwise stop
            # routing rather than failing — the detection still fires and the
            # conversation continues instead of jumping to a flow that is not
            # there.
            escalation = next(
                (f.get("flow_id") for f in (spec.get("flows") or [])
                 if isinstance(f, dict) and f.get("role") == "escalation"
                 and f.get("flow_id")),
                None,
            )
            if escalation:
                plan = {**plan, "route_flow_id": escalation}
                logger.info("[ACXDGuardrails] %s: route had no target; routing "
                            "to the escalation flow %r", name, escalation)
            else:
                plan = {k: v for k, v in plan.items() if k != "action"}
                logger.info("[ACXDGuardrails] %s: route had no target and no "
                            "escalation flow exists; dropped the route action",
                            name)

        doc = {
            "name": name,
            "trigger": _mask_trigger(plan),
            "description": (plan.get("policy") or "")[:100],
            "active": True,
            "rules": [{
                "name": name,
                "detection": _resolve_detection(plan),
                "enforcement": _resolve_enforcement(plan, spec),
                "active": True,
            }],
            "fallbackBehavior": {"type": "continue"},
        }
        _keep_derived_output_rules_advisory(doc, plan)
        _keep_derived_input_routes_advisory(doc, plan)
        errors = validate_acxd_asset("guardrail", doc)
        if errors:
            problems.extend(f"guardrails[{i}] ({name}): {e}" for e in errors)
        else:
            docs.append(doc)
    return docs, problems


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

_LANG_DEFAULT = "en-US"

#: The only ``settings.defaultFlows`` events the service (and the application
#: schema) accepts. Anything else is a flow role, not a default behaviour.
DEFAULT_FLOW_EVENTS = frozenset({
    "welcome", "fallback", "unknown", "escalation",
    "frustration", "help", "repeat", "resume",
})


def _attached_flows(flow_plans: list, spec: Optional[dict] = None) -> list[dict]:
    """Every flow the bundle ships, in plan order, system flows included.

    A flow that is not attached to the application is not routable and cannot be
    redirected to. The flow generator ALWAYS emits FollowUpFlow (R3, the
    "anything else?" flow every operation's success path redirects to) and
    RequestAgentFlow (R2, the routable "connect me to a human" entry) whether or
    not the interview planned them, so the application must attach them too —
    otherwise the deployment drops exactly the two flows that make the
    conversation multi-turn. The same holds for the FAQ flow the generator adds
    when the application ships a knowledge base.
    """
    from tools.acxd_system_flows import (
        ALWAYS_GENERATED_SYSTEM_ROLES,
        conditional_system_roles,
        resolve_system_flow_ids,
    )

    attached = [p["flow_id"] for p in flow_plans if isinstance(p, dict) and p.get("flow_id")]
    if attached:
        resolved = resolve_system_flow_ids({**(spec if isinstance(spec, dict) else {}), "flows": flow_plans})
        roles = ALWAYS_GENERATED_SYSTEM_ROLES + (
            conditional_system_roles(spec, flow_plans) if isinstance(spec, dict) else ())
        for role in roles:
            flow_id = resolved.get(role)
            if flow_id and flow_id not in attached:
                attached.append(flow_id)
    return [{"flowId": flow_id} for flow_id in attached]


def build_application(spec: dict) -> dict:
    """Application document: flows attach, defaultFlows, guardrail refs."""
    app = spec.get("application") or {}
    profile = spec.get("business_profile") or {}
    deployment = spec.get("deployment") or {}
    flow_plans = spec.get("flows") or []

    name = app.get("name") or f"{profile.get('company_name', 'AICC')} Assistant"
    # Live API: the application name must be ASCII too ('가온물류㈜ (GAON)
    # Assistant' would die at compose-application the way Korean guardrail
    # names died at upsert-guardrails). Keep the ASCII words if any survive,
    # else fall back to a neutral name.
    if any(ord(c) > 126 for c in name):
        ascii_words = re.findall(r"[\x20-\x7E]+", name)
        cleaned = " ".join(w.strip() for w in ascii_words if w.strip(" ()-_.")).strip()
        core = re.sub(r"\bAssistant\b", "", cleaned, flags=re.IGNORECASE)
        if len(re.sub(r"[^A-Za-z]", "", core)) >= 3:
            name = cleaned
        else:
            # A Korean/Japanese company name has no ASCII to keep. The Classic
            # infrastructure spec always carries an ASCII project name
            # (seoul-bright-eye, sakura-insurance) — build the application name
            # from it so two customers never both deploy as "AICC Assistant".
            infra = spec.get("infrastructure") or {}
            project = str(infra.get("project_name") or infra.get("projectName") or "").strip()
            words = [w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", project) if w]
            name = (" ".join(words) + " Assistant").strip() if words and re.search(r"[A-Za-z]{3}", project) \
                else "AICC Assistant"
    # The application's languages MUST match the flows'. Live defect: the app
    # was built with the en-US default while every flow was ko-KR, so the
    # deployment refused the language ("ko-KR is not included in this build")
    # and a Korean PoC deployed as an English application — the agent answered
    # nothing usable. Fall back to the flows' / business language, not en-US.
    locales = app.get("locales") or app.get("languages") or []
    if not locales:
        flow_langs = [f.get("language") for f in flow_plans
                      if isinstance(f, dict) and f.get("language")]
        business_lang = (profile.get("language") or profile.get("primary_language")
                         or (spec.get("business_profile") or {}).get("language"))
        candidate = next((lang for lang in flow_langs if lang), None) or business_lang
        if candidate:
            from tools.acxd_contract import canonical_language
            try:
                candidate = canonical_language(candidate)
            except Exception:
                pass
            locales = [candidate]
    if not locales:
        locales = [_LANG_DEFAULT]
    primary = app.get("primary_locale") or app.get("primary_language") or locales[0]

    # defaultFlows: explicit mapping first, then role-based flows fill gaps.
    # ONLY these eight events exist (application schema, additionalProperties
    # false). `followup` / `agent_request` are flow ROLES, not default
    # behaviours — FollowUpFlow is reached by redirect and RequestAgentFlow by
    # intent routing — so they must never be written here.
    default_flows: dict = {}
    for event, flow_id in (app.get("default_flows") or {}).items():
        if flow_id and event in DEFAULT_FLOW_EVENTS:
            default_flows[event] = {"flowId": flow_id}
    for plan in flow_plans:
        role = plan.get("role")
        if role in DEFAULT_FLOW_EVENTS and role not in default_flows:
            default_flows[role] = {"flowId": plan.get("flow_id")}

    # Every application must route all four system events. Fill any the
    # interview did not assign, so the bundle is complete by construction
    # rather than deploying an app with no greeting or no escalation path.
    if flow_plans:
        def _first(*roles):
            for role in roles:
                for plan in flow_plans:
                    if plan.get("role") == role and plan.get("flow_id"):
                        return plan["flow_id"]
            return None

        entry = (default_flows.get("welcome", {}).get("flowId")
                 or _first("welcome")
                 or next((p["flow_id"] for p in flow_plans if p.get("flow_id")), None))
        escalation = (default_flows.get("escalation", {}).get("flowId")
                      or _first("escalation") or entry)
        catch_all = _first("fallback", "unknown") or entry
        for role, target in (("welcome", entry), ("fallback", catch_all),
                             ("unknown", catch_all), ("escalation", escalation)):
            if target and role not in default_flows:
                default_flows[role] = {"flowId": target}
        # "customer expresses frustration → agent" is a hand-off condition every
        # interview confirms; the service detects frustration itself, so the
        # deterministic realisation is this default event — not an LLM-judged
        # guardrail, which fired on ordinary requests (live, 2026-09-17).
        if escalation and "frustration" not in default_flows:
            default_flows["frustration"] = {"flowId": escalation}

    kb = build_knowledge_base(spec)
    if kb and "unknown" in default_flows:
        default_flows["unknown"]["knowledgeBaseId"] = f"{{KB:{kb['name']}}}"

    settings: dict = {
        "languageCode": primary,
        "languageCodes": locales,
        # NOTE: the service silently DROPS languageSettings.voice,
        # languageSettings.useNativeLanguage and settings.childDirected.
        # Emitting them produces a bundle that does not match what is stored,
        # so they are omitted here and blocked in preflight. ASR/audio filler
        # are set on the Agentic CX block and TTS on the Connect "Set voice"
        # block — voice is a Connect concern, not an ACXD application setting.
        "languageSettings": [{"languageCode": lang} for lang in locales],
        "thresholds": {"incomprehensionCount": 2},
        "conversationTTL": min(max(app.get("conversation_ttl_minutes") or 5, 1), 60),
    }
    if default_flows:
        settings["defaultFlows"] = default_flows
    # Lifecycle hooks are authored per application; pass through what the
    # interview captured so they are not silently lost.
    hooks = app.get("lifecycle_hooks") or app.get("lifecycleHooks")
    if hooks:
        settings["lifecycleHooks"] = hooks
    guardrail_refs = []
    for index, guardrail in enumerate(spec.get("guardrails") or []):
        if not isinstance(guardrail, dict):
            continue
        guardrail_name = guardrail.get("name") or _guardrail_name(guardrail, index)
        if any(ord(char) > 126 for char in str(guardrail_name)):
            guardrail_name = _guardrail_name(guardrail, index)
        guardrail_name = _project_scoped_name(spec, str(guardrail_name)[:100])
        guardrail_refs.append({"guardrailId": f"{{GUARDRAIL:{guardrail_name}}}"})
    if guardrail_refs:
        settings["guardrails"] = guardrail_refs

    return {
        "name": name[:100],
        "description": (app.get("description")
                        or profile.get("description") or "")[:200],
        "flows": _attached_flows(flow_plans, spec),
        "settings": settings,
        "deploymentSettings": {
            "oneClickDeployEnabled": False,
            "environment": app.get("environment") or deployment.get("environment", "development"),
        },
    }


# ---------------------------------------------------------------------------
# orchestrator tools
# ---------------------------------------------------------------------------

def _store(session_id: str, asset_type: str, file_name: str, doc,
           preview_operation_id: str | None = None) -> None:
    """Persist one root-layout ACXD asset and emit the normal asset preview."""
    from tools.s3_asset_storage import save_asset_to_s3
    from tools.streaming_callback import stream_asset

    content = json.dumps(doc, indent=2, ensure_ascii=False)
    s3_key = save_asset_to_s3(session_id, asset_type, file_name, content)
    stream_asset(
        asset_type,
        file_name,
        content,
        operation_id=preview_operation_id,
        is_complete=True,
        s3_key=s3_key,
    )


@tool
def generate_acxd_knowledge_base() -> dict:
    """Generate the ACXD knowledge base document (articles) from the spec.

    Deterministic when the interview captured full articles. If only
    topics were captured, article text is written by the pooled agent
    first (faq-style) and then validated.
    """
    session_id = current_session_id.get() or "default"
    spec = get_acxd_spec().model_dump()
    plan = spec.get("knowledge_base") or {}
    if plan.get("topics") and not plan.get("articles"):
        # Per-call instance, not the shared pool singleton: concurrent sessions
        # on one task otherwise collide (see _make_llm_invoke in
        # agents/acxd_flow_generator/agent.py).
        from agents.agent_pool import get_agent_with_tools

        def invoke(prompt: str) -> str:
            agent = get_agent_with_tools("acxd_flow_generator", tools=None)
            return str(agent(prompt))

        articles, problems = run_kb_article_generation(
            plan["topics"], spec, invoke)
        if problems:
            return {"status": "error", "problems": problems}
        plan["articles"] = articles
        spec["knowledge_base"] = plan

    doc = build_knowledge_base(spec)
    if doc is None:
        return {"status": "success", "message": "no knowledge base in the spec"}
    errors = validate_acxd_asset("knowledge_base", doc)
    if errors:
        return {"status": "error", "problems": errors}
    _store(session_id, "acxd_knowledge_base", "knowledge_base.json", doc)
    return {"status": "success", "name": doc["name"],
            "articles": len(doc.get("articles") or [])}


@tool
def generate_acxd_guardrails() -> dict:
    """Generate ACXD guardrail documents from the interview policies.

    Deterministic: detection method selection (regex/keyword/llmJudge)
    follows fixed rules; 'route' actions must name an existing flow.
    """
    session_id = current_session_id.get() or "default"
    spec = get_acxd_spec().model_dump()
    docs, problems = build_guardrails(spec)
    seen_names: set = set()
    for index, doc in enumerate(docs):
        # \w is Unicode-aware, so Korean names survive. The ASCII-only slug
        # reduced every Korean name to "" — three guardrails all stored as
        # ".json", each overwriting the last. The live report was exact:
        # "3개를 두 번 생성했으나 매번 마지막 한 개만 남는다".
        slug = re.sub(r"[^\w]+", "-", doc["name"]).strip("-").lower()
        if not slug:
            slug = f"guardrail-{index + 1}"
        if slug in seen_names:  # distinct guardrails must never share a file
            slug = f"{slug}-{index + 1}"
        seen_names.add(slug)
        _store(session_id, "acxd_guardrail", f"{slug}.json", doc)
    if problems and not docs:
        return {"status": "error", "problems": problems, "generated": []}
    if problems:
        # Partial success is progress. Returning an error while discarding the
        # valid documents meant one unusable entry blocked the step on every
        # retry, with nothing to show for it.
        return {"status": "partial", "generated": [d["name"] for d in docs],
                "problems": problems}
    return {"status": "success", "generated": [d["name"] for d in docs]}


def store_acxd_application_asset(spec: dict, session_id: str) -> dict:
    """Store a validated application document for compatibility callers.

    The phase-4 orchestrator entry point is
    ``tools.acxd_application_generator.generate_acxd_application``; it owns
    generation order and patch-only behavior.
    """
    doc = build_application(spec)
    errors = validate_acxd_asset("application", doc)
    if errors:
        return {"status": "error", "problems": errors}
    _store(session_id, "acxd_application", "application.json", doc)
    return {"status": "success", "name": doc["name"],
            "flows_attached": len(doc["flows"]),
            "environment": doc["deploymentSettings"]["environment"]}
