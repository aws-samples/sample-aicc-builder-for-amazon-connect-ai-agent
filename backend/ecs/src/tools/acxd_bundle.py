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
    safe = session_id.replace("..", "_").replace("/", "_")
    return Path(mount) / "sessions" / safe / "assets"


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
    docs: list[dict] = []
    root = _assets_root(session_id)
    if root is not None and (root / asset_type).is_dir():
        for path in sorted((root / asset_type).rglob("*.json")):
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
        bundle[key] = _read_json_docs(session_id, asset_type)
    bundle["flows"] = [normalize_flow_for_service(f) for f in bundle["flows"]]

    apps = _read_json_docs(session_id, ACXD_APPLICATION_TYPE)
    bundle["application"] = apps[0] if apps else None

    cvs: list[dict] = []
    for doc in _read_json_docs(session_id, ACXD_CONTEXT_VARIABLE_TYPE):
        if isinstance(doc, list):
            cvs.extend(d for d in doc if isinstance(d, dict))
        elif isinstance(doc, dict):
            cvs.extend(d for d in (doc.get("contextVariables") or doc.get("context_variables") or [doc])
                       if isinstance(d, dict))
    bundle["context_variables"] = cvs

    bundle["contact_flows"] = _read_json_docs(session_id, CLASSIC_CONTACT_FLOW_TYPE)

    inventory = _backend_inventory(session_id)
    bundle["infrastructure"] = inventory["infrastructure"]
    bundle["lambdas"] = inventory["lambdas"]
    bundle["openapi"] = inventory["openapi"]
    return bundle


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
