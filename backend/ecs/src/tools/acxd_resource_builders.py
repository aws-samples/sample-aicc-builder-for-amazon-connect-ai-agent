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

    Non-ASCII names (e.g. Korean '삼성전자로지텍 FAQ') are stripped by the
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

def build_slot_types(spec: dict) -> tuple[list[dict], list[str]]:
    """Validate adapter-derived ACXD SlotType documents deterministically."""
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
        errors = validate_acxd_asset("slot_type", doc)
        if errors:
            problems.extend(f"slot_types[{index}] ({slot_type_id}): {error}" for error in errors)
            continue
        docs.append(doc)
    return docs, problems


def build_context_variables(spec: dict) -> list[dict]:
    """Render the bounded Agentic CX context-variable contract."""
    app = spec.get("application") or {}
    variables: list[dict] = []
    for raw in (app.get("context_variables") or [])[:10]:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        variables.append({
            "name": raw["name"],
            "type": raw.get("type") or "string",
            "description": raw.get("description") or "",
            "fromContactAttribute": raw.get("from_contact_attribute"),
        })
    return variables


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
        return {"method": "regex", "pattern": pattern[:200]}
    if method == "keyword" or (
        method == "auto" and examples and all(len(e) <= 50 for e in examples)
    ):
        keywords = [e[:50] for e in examples][:200] or ["placeholder"]
        return {"method": "keyword", "keywords": keywords}
    prompt = (
        "Decide whether the following message violates this policy. "
        f"Policy: {plan.get('policy', '')}"
    )
    return {"method": "llmJudge", "prompt": prompt[:4000], "threshold": 0.8}


def _resolve_enforcement(plan: dict) -> dict:
    action = plan.get("action", "flag")
    if action == "route":
        return {"action": "route",
                "behavior": {"flowId": plan.get("route_flow_id") or ""}}
    if action == "mask":
        return {"action": "mask", "behavior": {"maskText": "[REDACTED]"}}
    if action == "modify":
        return {"action": "modify",
                "behavior": {"message": (plan.get("message")
                                         or "I can't help with that.")[:500]}}
    return {"action": "flag"}


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

        action = plan.get("action")
        route_target = plan.get("route_flow_id")
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
            "trigger": plan.get("trigger", "input"),
            "description": (plan.get("policy") or "")[:100],
            "active": True,
            "rules": [{
                "name": name,
                "detection": _resolve_detection(plan),
                "enforcement": _resolve_enforcement(plan),
                "active": True,
            }],
            "fallbackBehavior": {"type": "continue"},
        }
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


def build_application(spec: dict) -> dict:
    """Application document: flows attach, defaultFlows, guardrail refs."""
    app = spec.get("application") or {}
    profile = spec.get("business_profile") or {}
    deployment = spec.get("deployment") or {}
    flow_plans = spec.get("flows") or []

    name = app.get("name") or f"{profile.get('company_name', 'AICC')} Assistant"
    # Live API: the application name must be ASCII too ('삼성전자로지텍㈜ (SELC)
    # Assistant' would die at compose-application the way Korean guardrail
    # names died at upsert-guardrails). Keep the ASCII words if any survive,
    # else fall back to a neutral name.
    if any(ord(c) > 126 for c in name):
        ascii_words = re.findall(r"[\x20-\x7E]+", name)
        cleaned = " ".join(w.strip() for w in ascii_words if w.strip(" ()-_.")).strip()
        name = cleaned if len(re.sub(r"[^A-Za-z]", "", cleaned)) >= 3 else "AICC Assistant"
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
    default_flows: dict = {}
    for event, flow_id in (app.get("default_flows") or {}).items():
        if flow_id:
            default_flows[event] = {"flowId": flow_id}
    for plan in flow_plans:
        role = plan.get("role")
        if role and role != "operation" and role not in default_flows:
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
        guardrail_refs.append({"guardrailId": f"{{GUARDRAIL:{guardrail_name}}}"})
    if guardrail_refs:
        settings["guardrails"] = guardrail_refs

    return {
        "name": name[:100],
        "description": (app.get("description")
                        or profile.get("description") or "")[:200],
        "flows": [{"flowId": p["flow_id"]} for p in flow_plans if p.get("flow_id")],
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
