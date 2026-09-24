"""Assemble a session's generated ACXD assets into one bundle dict.

The bundle is the shared currency of the D9 validators
(``validate_acxd_consistency``), the deploy-manifest builder, the packager
and the patch tool. Assets are read through the same storage layer every
Classic generator writes through (``s3_asset_storage``: NFS fast path,
S3 fallback), so a Classic asset and an ACXD asset are found the same way.

Asset layout (``assets/{sid}/{asset_type}/[{group}/]{file}``)::

    acxd_flow/<flowId>.json                 LLM flow generator (+ repair loop)
    acxd_slot_type/<slotTypeId>.json        code, from OperationSpec FieldSpecs
    acxd_data_request/<dataRequestId>.json  code, from the OpenAPI asset
    acxd_guardrail/<slug>.json              code, from ACXDFlowSpec.guardrails
    acxd_knowledge_base/knowledge_base.json code, articles from the FAQ asset
    acxd_application/application.json       code, from ACXDFlowSpec.application
    acxd_context_variable/context_variables.json
    acxd_secret/<name>.json                 {name, description, valueEnv} — never a value
    contact_flow/contact_flow.json          Classic Contact Flow generator (Agentic CX block)
    infrastructure/…, lambda/…, openapi/…   Classic backend the Data Requests call
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: asset_type → bundle key (list-valued)
ACXD_LIST_TYPES = {
    "acxd_flow": "flows",
    "acxd_slot_type": "slot_types",
    "acxd_data_request": "data_requests",
    "acxd_guardrail": "guardrails",
    "acxd_knowledge_base": "knowledge_bases",
    "acxd_secret": "secrets",
}
ACXD_APPLICATION_TYPE = "acxd_application"
ACXD_CONTEXT_VARIABLE_TYPE = "acxd_context_variable"
ACXD_ASSET_TYPES = tuple(ACXD_LIST_TYPES) + (ACXD_APPLICATION_TYPE, ACXD_CONTEXT_VARIABLE_TYPE)

#: Classic asset types the bundle also reports on (backend + contact flow).
CLASSIC_CONTACT_FLOW_TYPE = "contact_flow"
CLASSIC_BACKEND_TYPES = ("infrastructure", "cloudformation", "cdk", "lambda", "openapi")

#: Metadata fields the live ACXD API restricts to ASCII (verified 2026-09-05):
#: a non-ASCII ``description`` on any asset kind fails with "description is
#: not in the expected format", while customer-facing text (messages[].body,
#: KB content, guardrail messages) accepts any language. Field-scoped, never global.
ASCII_ONLY_METADATA_FIELDS = ("description", "aiDescription")


def enforce_ascii_metadata(doc: Any) -> Any:
    """Strip non-ASCII from metadata fields only, recursively (new structure).

    Drops a field when no readable ASCII word survives — every such field is
    optional and dropping beats a failed deploy.
    """
    if isinstance(doc, list):
        return [enforce_ascii_metadata(v) for v in doc]
    if not isinstance(doc, dict):
        return doc
    out: dict = {}
    for key, val in doc.items():
        if key in ASCII_ONLY_METADATA_FIELDS and isinstance(val, str):
            cleaned = re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7E]", " ", val)).strip()
            if cleaned and re.search(r"[A-Za-z]{3}", cleaned):
                cleaned = re.sub(r"\s+([,.:;!?])", r"\1", cleaned)
                cleaned = re.sub(r"[\"']\s+[\"']", " ", cleaned)
                cleaned = re.sub(r"^[\s.,:;!?\"'-]+", "", cleaned)
                cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
                if cleaned:
                    out[key] = cleaned
            continue
        out[key] = enforce_ascii_metadata(val)
    return out


# amazon-connect-acxd-sdk KnowledgeBaseNodeConfig keys; the service requires
# `name` (live-verified 2026-09-10) and rejects fields outside this set.
KB_NODE_KEYS = frozenset({
    "name", "knowledgeBaseId", "prompt", "question", "includeCitation",
    "timeout", "minConfidenceScore", "brandId", "filters",
})
_KB_PLACEHOLDER = re.compile(r"^\{KB:([^}]+)\}$")


def normalize_flow_for_service(flow: Any) -> Any:
    """Deterministic service-contract repairs applied whenever a bundle is
    loaded (packaging, D9, runner). Mechanical only — it never changes a
    flow's behaviour:

    - knowledge_base nodes: `metadata.knowledgeBase.name` is required by the
      service (the SDK type says optional); derive it from the `{KB:<name>}`
      placeholder, and drop keys KnowledgeBaseNodeConfig does not have.

    Flows generated before a contract fix stay deployable; the generator is
    fixed at the source as well, so this is a safety net, not the fix.
    """
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), dict):
        return flow
    # Encoding drift the SDK serializer would silently drop (slot capture in
    # metadata.userInput, messages under metadata, define.assignments, `{{x}}`
    # placeholders, text-typed booleans) is mapped onto the contract first, so
    # bundles generated before the canonicalizer existed deploy correctly too.
    from tools.acxd_flow_canonicalizer import canonicalize_flow
    canonical = canonicalize_flow(flow)
    if canonical.changes:
        logger.info("[acxd_bundle] %s: canonicalized %d encoding(s) on load",
                    flow.get("flowId"), len(canonical.changes))
    flow = canonical.flow
    for node in flow["nodes"].values():
        if not isinstance(node, dict) or node.get("type") != "knowledge_base":
            continue
        meta = node.get("metadata")
        kb = meta.get("knowledgeBase") if isinstance(meta, dict) else None
        if not isinstance(kb, dict):
            continue
        if not kb.get("name"):
            match = _KB_PLACEHOLDER.match(str(kb.get("knowledgeBaseId") or ""))
            if match:
                kb["name"] = match.group(1)
        for key in [k for k in kb if k not in KB_NODE_KEYS]:
            kb.pop(key, None)
    return flow


def _assets_root(session_id: str) -> Optional[Path]:
    mount = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    if not session_id or not os.path.isdir(mount):
        return None
    from tools.path_safety import path_under
    return path_under(Path(mount) / "sessions", session_id, "assets")


def _session_keys(session_id: str) -> list[str]:
    """Every asset key of the session, NFS first then S3 (best effort)."""
    try:
        from tools.s3_asset_storage import _list_nfs_assets, list_session_assets
        keys = _list_nfs_assets(session_id)
        if not keys:
            keys = list_session_assets(session_id)
        return [str(k) for k in (keys or [])]
    except Exception as e:  # pragma: no cover - storage outage
        logger.warning("[ACXDBundle] could not list session assets: %s", e)
        return []


def _read_json_docs(session_id: str, asset_type: str) -> list[dict]:
    """All JSON documents of one asset type, via NFS when mounted, else S3."""
    from tools.path_safety import path_under
    docs: list[dict] = []
    root = _assets_root(session_id)
    type_dir = path_under(root, asset_type) if root is not None else None
    if type_dir is not None and type_dir.is_dir():
        for path in sorted(type_dir.rglob("*.json")):
            try:
                docs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("[ACXDBundle] unreadable asset %s: %s", path, e)
        return [enforce_ascii_metadata(d) for d in docs]
    try:
        from tools.s3_asset_storage import get_asset_from_s3
        prefix = f"/{asset_type}/"
        for key in _session_keys(session_id):
            if prefix in key and key.endswith(".json"):
                text = get_asset_from_s3(key)
                if text:
                    try:
                        docs.append(json.loads(text))
                    except json.JSONDecodeError as e:
                        logger.warning("[ACXDBundle] invalid JSON at %s: %s", key, e)
    except Exception as e:  # pragma: no cover
        logger.warning("[ACXDBundle] S3 read failed for %s: %s", asset_type, e)
    return [enforce_ascii_metadata(d) for d in docs]


def _backend_inventory(session_id: str) -> dict:
    """Which Classic backend assets exist — the Data Requests' `{WEBHOOK_URL}` target."""
    infra: set[str] = set()
    lambdas: set[str] = set()
    openapi: set[str] = set()
    contact_flows: set[str] = set()
    for key in _session_keys(session_id):
        parts = key.split("/")
        if any(p in ("infrastructure", "cloudformation", "cdk") for p in parts):
            infra.add(parts[-1])
        elif any(p == "lambda" or p.startswith("lambda") for p in parts):
            lambdas.add(key)
        elif "openapi" in parts:
            openapi.add(parts[-1])
        elif CLASSIC_CONTACT_FLOW_TYPE in parts and key.endswith(".json"):
            contact_flows.add(key)
    return {
        "infrastructure": {"files": sorted(infra)} if infra else None,
        "lambdas": [{"name": k} for k in sorted(lambdas)],
        "openapi": {"files": sorted(openapi)} if openapi else None,
        "_contact_flow_keys": sorted(contact_flows),
    }


def _dedupe_contact_flows(docs: list[dict]) -> list[dict]:
    """One Contact Flow per name.

    A session can hold the same flow twice — the generator's copy under the
    flow-name folder and a root-level copy the model wrote by hand (live:
    GAON shipped `contact_flow.json` and `contact_flow-1.json`, and the deploy
    imported two flows). Identical documents collapse; documents sharing a
    flow name keep the last one read (sorted read order puts the operation
    folder after the root file, i.e. the generator's copy wins).
    """
    by_key: dict[str, dict] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        name = (doc.get("Name") or doc.get("name")
                or ((doc.get("Metadata") or {}).get("name") if isinstance(doc.get("Metadata"), dict) else None))
        key = f"name:{name}" if name else "hash:" + json.dumps(doc, sort_keys=True, ensure_ascii=False)
        if key in by_key and by_key[key] != doc:
            logger.info("[ACXDBundle] duplicate Contact Flow %r collapsed (keeping the later copy)", name)
        by_key[key] = doc
    return list(by_key.values())


_BUILTIN_SLOT_TYPE_IDS = frozenset({
    "text", "string", "number", "integer", "int", "boolean", "bool",
    "date", "datetime", "time", "email", "phone",
})


def _rebind_slot_types(flow: Any, slot_types: list) -> Any:
    """Point a flow's attached slots at the per-field slot type when the id it
    names is not in the bundle.

    Live (GAON): three slots were attached with `type: "enum"` — one shared
    slot type that no longer exists once the slot types are rebuilt per field.
    When the bundle holds a slot type whose id equals the slot's name, the slot
    (and the user_choice node capturing it) is rebound to it; anything else is
    left for D9-4 to report.
    """
    if not isinstance(flow, dict):
        return flow
    available = {str(st.get("slotTypeId")) for st in slot_types or [] if isinstance(st, dict) and st.get("slotTypeId")}
    # old shared id → the per-field slot names that replaced it (several slots
    # may have shared one id, e.g. 'enum')
    renames: dict[str, list[str]] = {}
    for slot in flow.get("slotTypes") or []:
        if not isinstance(slot, dict):
            continue
        type_id, name = str(slot.get("type") or ""), str(slot.get("name") or "")
        if type_id.startswith("NLX."):
            # A service built-in (the runtime contract's S1/S5 result, e.g.
            # NLX.AlphaNumeric + regex for an order number). Live: this rebind
            # pointed it back at a stale one-item custom slot type of the same
            # name, re-creating the auto-selecting menu the normalizer removed.
            continue
        if type_id and type_id.lower() not in _BUILTIN_SLOT_TYPE_IDS and type_id not in available and name in available:
            renames.setdefault(type_id, []).append(name)
            slot["type"] = name
            logger.info("[ACXDBundle] %s: slot %r rebound from missing slot type %r to %r",
                        flow.get("flowId"), name, type_id, name)
    if renames:
        for node in (flow.get("nodes") or {}).values():
            meta = (node or {}).get("metadata") if isinstance(node, dict) else None
            choice = (meta or {}).get("choice") if isinstance(meta, dict) else None
            if isinstance(choice, dict) and choice.get("slotTypeId") in renames:
                candidates = renames[choice["slotTypeId"]]
                target = _slot_for_choice(node, set(candidates))
                if target:
                    choice["slotTypeId"] = target
                elif len(candidates) == 1:
                    choice["slotTypeId"] = candidates[0]
                # else: ambiguous — left for D9-4 to report against this node
    return flow


def _slot_for_choice(node: dict, candidates: set[str]) -> Optional[str]:
    """Which rebound slot a user_choice node captures: its display name (the
    canonicalizer sets metadata.name to the slot name), else a slot the node's
    placeholders or branch conditions reference — only when unambiguous."""
    import re
    meta_name = str(((node.get("metadata") or {}).get("name")) or "")
    if meta_name in candidates:
        return meta_name
    text = json.dumps(node, ensure_ascii=False)
    referenced = {m for m in re.findall(r"\{([A-Za-z]{3,30}):NLX\.Slot\}", text) if m in candidates}
    referenced |= {m for m in re.findall(r'"type":\s*"slot",\s*"name":\s*"([A-Za-z]{3,30})"', text) if m in candidates}
    return referenced.pop() if len(referenced) == 1 else None


_DOC_ID_KEYS = ("flowId", "slotTypeId", "dataRequestId", "guardrailId", "knowledgeBaseId", "secretId", "name")


def _collapse_identical_docs(docs: list, asset_type: str) -> list:
    """Keep one copy of a document that exists twice with the same id AND the
    same content (a tool once saved `<type>/<id>/<id>.json` beside
    `<type>/<id>.json`). `rglob` sorts the flat file first, so the canonical
    path wins. Two copies that DIFFER are both kept — D9-1 reports the
    duplicate id, which is the right outcome for a real conflict."""
    seen: dict[str, dict] = {}
    out: list = []
    for doc in docs:
        if not isinstance(doc, dict):
            out.append(doc)
            continue
        doc_id = next((str(doc[k]) for k in _DOC_ID_KEYS if doc.get(k)), None)
        if doc_id is None:
            out.append(doc)
            continue
        first = seen.get(doc_id)
        if first is not None and first == doc:
            logger.info("[ACXDBundle] %s %r: identical duplicate copy ignored", asset_type, doc_id)
            continue
        seen.setdefault(doc_id, doc)
        out.append(doc)
    return out


def load_acxd_bundle(session_id: str) -> dict:
    """Load every generated asset the ACXD target needs into one dict.

    Keys: flows, slot_types, data_requests, guardrails, knowledge_bases,
    secrets (lists); application, context_variables; contact_flows (Classic
    contact_flow asset, list); infrastructure / lambdas / openapi
    (presence inventory of the Classic backend). Missing pieces are empty,
    never absent, so validators can report what is missing.
    """
    bundle: dict = {key: [] for key in ACXD_LIST_TYPES.values()}
    for asset_type, key in ACXD_LIST_TYPES.items():
        bundle[key] = _collapse_identical_docs(_read_json_docs(session_id, asset_type), asset_type)
    bundle["flows"] = [normalize_flow_for_service(f) for f in bundle["flows"]]
    bundle["flows"] = [_rebind_slot_types(f, bundle["slot_types"]) for f in bundle["flows"]]
    # A slot type no flow attaches is a leftover of an earlier generation —
    # live: the one-item 'orderNumber'/'phoneNumber' types the runtime contract
    # replaced with NLX built-ins stayed on disk and would have been deployed
    # as stray resources. Drop them from the bundle and say so.
    referenced = {
        str(slot.get("type"))
        for flow in bundle["flows"] if isinstance(flow, dict)
        for slot in (flow.get("slotTypes") or []) if isinstance(slot, dict) and slot.get("type")
    }
    kept, dropped = [], []
    for slot_type in bundle["slot_types"]:
        type_id = slot_type.get("slotTypeId") if isinstance(slot_type, dict) else None
        (kept if not type_id or type_id in referenced or not bundle["flows"] else dropped).append(slot_type)
    if dropped:
        bundle["dropped_slot_types"] = [st.get("slotTypeId") for st in dropped]
        logger.info("[ACXDBundle] %d slot type(s) no flow attaches left out of the bundle: %s",
                    len(dropped), bundle["dropped_slot_types"])
    bundle["slot_types"] = kept
    # D1/D2 (live 2026-09-13): a data request whose secret header still uses the
    # {{secrets.X}} spelling, or that carries no environment blocks, gets a 403
    # from the backend on every call. Repair on load so a session generated
    # before the contract was known still deploys.
    from tools.acxd_data_request_builder import repair_data_request_contract
    bundle["data_requests"] = [repair_data_request_contract(d)
                               for d in bundle["data_requests"]]
    # A Data Request left behind for an operation the knowledge base answers
    # (e2e 2026-09-22: `answerFaq` / `departmentFaq` — the interview had saved
    # an OperationSpec for FAQ, and nothing could delete the file) or a file
    # emptied to `{}` by hand is not part of the application.
    try:
        from tools.spec_manager import is_kb_native_spec, operation_specs_bucket
        from tools.acxd_generation_context import _normalise_data_request_id
        kb_native = {_normalise_data_request_id(str(op_id)).lower()
                     for op_id, spec in (operation_specs_bucket(session_id) or {}).items()
                     if is_kb_native_spec(spec)}
    except Exception:  # pragma: no cover - the bucket is optional here
        kb_native = set()
    kept_requests, dropped_requests = [], []
    for doc in bundle["data_requests"]:
        rid = str((doc or {}).get("dataRequestId") or "") if isinstance(doc, dict) else ""
        if not rid or rid.lower() in kb_native:
            dropped_requests.append(rid or "<no dataRequestId>")
            continue
        kept_requests.append(doc)
    if dropped_requests:
        bundle["dropped_data_requests"] = dropped_requests
        logger.info("[ACXDBundle] %d data request(s) left out of the bundle (knowledge-base "
                    "operation or empty document): %s", len(dropped_requests), dropped_requests)
    bundle["data_requests"] = kept_requests

    apps = _read_json_docs(session_id, ACXD_APPLICATION_TYPE)
    bundle["application"] = apps[0] if apps else None

    cvs: list[dict] = []
    for doc in _read_json_docs(session_id, ACXD_CONTEXT_VARIABLE_TYPE):
        if isinstance(doc, list):
            cvs.extend(d for d in doc if isinstance(d, dict))
        elif isinstance(doc, dict):
            # Live (2026-09-17): a wrapper {"contextVariables": []} — the normal
            # shape for a project that defines no variable — fell through an
            # `or` chain to [doc], so the wrapper itself was packaged as a
            # variable and the runner failed with "key is required".
            if "contextVariables" in doc or "context_variables" in doc:
                items = doc.get("contextVariables")
                if items is None:
                    items = doc.get("context_variables")
                items = items if isinstance(items, list) else []
            else:
                items = [doc]
            cvs.extend(d for d in items if isinstance(d, dict))
    bundle["context_variables"] = cvs

    bundle["contact_flows"] = _dedupe_contact_flows(_read_json_docs(session_id, CLASSIC_CONTACT_FLOW_TYPE))
    # The Contact Flow was generated once; the binding contract (language block
    # before the Agentic CX block, chat analytics the API rejects, branch
    # targets) has grown since. Re-apply the linter and the binding on load so a
    # bundle packaged today carries today's contract — both are idempotent.
    bundle["contact_flows"] = [_refresh_contact_flow(doc, session_id) for doc in bundle["contact_flows"]]
    # Guardrails the application does not reference and secrets no Data Request
    # uses are leftovers of earlier generations (live: an unprefixed
    # 'BackendApiKey' secret shipped next to the project-scoped one and, once
    # deployed, overwrote the workspace-level value every other project read).
    bundle["guardrails"], dropped_guardrails = _referenced_guardrails(bundle["guardrails"], bundle["application"])
    bundle["secrets"], dropped_secrets = _referenced_secrets(bundle["secrets"], bundle["data_requests"])
    if dropped_guardrails:
        bundle["dropped_guardrails"] = dropped_guardrails
        logger.info("[ACXDBundle] %d guardrail(s) the application does not reference left out: %s",
                    len(dropped_guardrails), dropped_guardrails)
    if dropped_secrets:
        bundle["dropped_secrets"] = dropped_secrets
        logger.info("[ACXDBundle] %d secret(s) no data request uses left out", len(dropped_secrets))

    inventory = _backend_inventory(session_id)
    bundle["infrastructure"] = inventory["infrastructure"]
    bundle["lambdas"] = inventory["lambdas"]
    bundle["openapi"] = inventory["openapi"]
    return bundle


def _refresh_contact_flow(document: Any, session_id: str) -> Any:
    """Run the Contact Flow linter and the ACXD binding on a stored flow."""
    if not isinstance(document, dict) or not isinstance(document.get("Actions"), list):
        return document
    refreshed = document
    try:
        from tools.asset_linters import lint_contact_flow
        result = lint_contact_flow(json.dumps(refreshed, ensure_ascii=False))
        fixed = result.get("fixed_json")
        if result.get("fixes_applied") and fixed:
            refreshed = json.loads(fixed)
            logger.info("[ACXDBundle] contact flow re-linted on load: %s", "; ".join(result["fixes_applied"][:4]))
    except Exception as exc:  # pragma: no cover - the stored flow is still returned
        logger.debug("[ACXDBundle] contact flow re-lint skipped: %s", exc)
    try:
        from tools.acxd_contact_flow_binding import normalize_acxd_contact_flow
        from tools.acxd_flow_spec import get_acxd_flow_spec
        refreshed = normalize_acxd_contact_flow(refreshed, get_acxd_flow_spec(session_id))
    except Exception as exc:  # pragma: no cover - the stored flow is still returned
        logger.debug("[ACXDBundle] contact flow binding refresh skipped: %s", exc)
    return refreshed


def _referenced_guardrails(guardrails: list, application: Any) -> tuple[list, list]:
    """(kept, dropped names): only guardrails the application references."""
    settings = (application or {}).get("settings") if isinstance(application, dict) else None
    refs = (settings or {}).get("guardrails") if isinstance(settings, dict) else None
    if not isinstance(refs, list) or not refs:
        return list(guardrails), []
    wanted: set[str] = set()
    for ref in refs:
        value = ref.get("guardrailId") if isinstance(ref, dict) else ref
        match = re.fullmatch(r"\{GUARDRAIL:(.+)\}", str(value or ""))
        wanted.add(match.group(1) if match else str(value))
    kept, dropped = [], []
    for doc in guardrails:
        name = doc.get("name") if isinstance(doc, dict) else None
        (kept if not name or name in wanted else dropped).append(doc)
    return kept, [d.get("name") for d in dropped]


def _referenced_secrets(secrets: list, data_requests: list) -> tuple[list, list]:
    """(kept, dropped names): only secrets some Data Request header names."""
    if not data_requests:
        return list(secrets), []
    used: set[str] = set()
    for doc in data_requests:
        text = json.dumps(doc, ensure_ascii=False) if isinstance(doc, dict) else ""
        used.update(re.findall(r"\{([A-Za-z0-9_]+):NLX\.Secret\}", text))
        used.update(re.findall(r"\{\{secrets\.([A-Za-z0-9_]+)\}\}", text))
    kept, dropped = [], []
    for doc in secrets:
        name = doc.get("name") if isinstance(doc, dict) else None
        (kept if not name or name in used else dropped).append(doc)
    return kept, [d.get("name") for d in dropped]


def bundle_summary(bundle: dict) -> dict:
    """Counts per asset family — what the progress UI and README show."""
    return {
        "flows": len(bundle.get("flows") or []),
        "slot_types": len(bundle.get("slot_types") or []),
        "data_requests": len(bundle.get("data_requests") or []),
        "guardrails": len(bundle.get("guardrails") or []),
        "knowledge_bases": len(bundle.get("knowledge_bases") or []),
        "secrets": len(bundle.get("secrets") or []),
        "application": bool(bundle.get("application")),
        "context_variables": len(bundle.get("context_variables") or []),
        "contact_flows": len(bundle.get("contact_flows") or []),
        "backend": bool(bundle.get("infrastructure")) and bool(bundle.get("lambdas")),
    }
