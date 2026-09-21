"""Deterministic review gates → the *blocking* finding set.

Why: the reviewer is an LLM and each run surfaces a different list (GAON:
13 critical, five fixed, then 21). A loop whose blocking set moves on every
run never converges. The deterministic gates — cross-asset consistency
(D1–D8 + IAM), spec↔OpenAPI shape parity, ACXD D9 — are stable, so they are
the only findings that block packaging; the reviewer's own findings are
advisory. Every blocking finding carries a stable id so the orchestrator can
tell "fixed" from "new" between reviews without re-reading prose.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _stable_id(prefix: str, *parts: Any) -> str:
    bits = [str(p).strip() for p in parts if p not in (None, "")]
    return ":".join([prefix, *bits])[:200]


def _session_tool_ids() -> list[str]:
    """tool_ids declared in the session flow config's `session_tools`."""
    try:
        from tools.spec_manager import get_session_flow_config
        cfg = get_session_flow_config()
    except Exception as exc:
        logger.debug("[review_gates] session flow config unavailable: %s", exc)
        return []
    ids: list[str] = []
    for tool in (getattr(cfg, "session_tools", None) or []) if cfg else []:
        tool_id = getattr(tool, "tool_id", None) or (tool.get("tool_id") if isinstance(tool, dict) else None)
        if tool_id:
            ids.append(str(tool_id))
    return ids


def _consistency_findings(session_id: str) -> list[dict]:
    from tools.validate_consistency import _validate_parameter_consistency_impl
    try:
        result = _validate_parameter_consistency_impl(session_id)
    except Exception as exc:  # gate unavailable is itself blocking-worthy news
        return [{"id": "GATE:consistency:unavailable", "gate": "consistency", "severity": "error",
                 "message": f"cross-asset consistency gate could not run: {exc}"}]
    findings: list[dict] = []
    for m in result.get("mismatches") or []:
        if not isinstance(m, dict):
            continue
        if str(m.get("id", "")).startswith("D9-"):
            fid = _stable_id(m["id"], m.get("asset_type"), m.get("operation_id"), m.get("field"), m.get("code"))
            gate = "acxd-d9"
        else:
            fid = _stable_id("D", m.get("asset_type"), m.get("operation_id"), m.get("field"),
                             (m.get("issue") or m.get("message") or "")[:60])
            gate = "consistency"
        findings.append({
            "id": fid, "gate": gate, "severity": m.get("severity", "error"),
            "message": m.get("issue") or m.get("message") or str(m),
            "asset_type": m.get("asset_type"), "operation_id": m.get("operation_id"), "field": m.get("field"),
        })
    return findings


def _parity_findings(session_id: str) -> list[dict]:
    import yaml
    from tools.asset_loader import load_existing_asset
    from tools.shape_parity import ShapeParityError, validate_shape_parity
    from tools.spec_manager import get_all_specs

    specs = get_all_specs() or {}
    if not specs:
        return []
    openapi_yaml = load_existing_asset("openapi", file_name="openapi.yaml")
    if not openapi_yaml:
        return []  # no OpenAPI yet: completeness is the consistency gate's job
    try:
        doc = yaml.safe_load(openapi_yaml)
    except Exception as exc:
        return [{"id": "PARITY:openapi:unparsable", "gate": "parity", "severity": "error",
                 "message": f"openapi.yaml did not parse: {exc}", "asset_type": "openapi"}]
    findings: list[dict] = []
    for op_id, spec in specs.items():
        spec_dict = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
        try:
            mismatches = validate_shape_parity(spec_dict, doc)
        except ShapeParityError as exc:
            findings.append({"id": _stable_id("PARITY", op_id, "refused"), "gate": "parity", "severity": "error",
                             "message": f"{op_id}: {exc}", "asset_type": "openapi", "operation_id": op_id})
            continue
        for m in mismatches:
            d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
            findings.append({
                "id": _stable_id("PARITY", d.get("path"), d.get("reason")), "gate": "parity", "severity": "error",
                "message": f"{d.get('path')}: {d.get('detail') or d.get('reason')}",
                "asset_type": "openapi", "operation_id": op_id,
            })
    return findings


def _orphan_operation_findings(session_id: str) -> list[dict]:
    """An operation that has assets but no OperationSpec is outside every other
    gate (consistency, parity and D9 iterate over the specs). Live: a fifth
    operation `log_call_result` was hand-built during generation — Lambda, OpenAPI
    path and Data Request — and none of the gates ever looked at it.

    Reports one finding per (asset kind, operation) whose id matches no spec in
    any spelling (snake / camel / lowercase). Stable id SPEC:<asset>:<op>."""
    from tools.spec_manager import get_all_specs
    specs = get_all_specs() or {}
    if not specs:
        return []          # scoped runs without operation specs are not judged here
    known: set[str] = set()
    for op_id, spec in specs.items():
        known |= _spellings(op_id)
        # A tool declared INSIDE an OperationSpec (role helper/session — "each
        # tool = 1 Lambda + 1 API path") is covered by that spec's gates; live
        # (2026-09-17) a quote helper of a reservation operation was reported
        # here and the review round registered a duplicate spec for it.
        for tool in getattr(spec, "tools", None) or []:
            tool_id = getattr(tool, "tool_id", None) or (tool.get("tool_id") if isinstance(tool, dict) else None)
            if tool_id:
                known |= _spellings(str(tool_id))
    # A session tool (flow config `session_tools`, role=session — log_call_result,
    # get_outbound_targets) has no OperationSpec BY DESIGN: it is declared once for
    # the whole session and the count gate expects its Lambda and API path from
    # that declaration. Live (2026-09-20) this gate reported the session tool's
    # OpenAPI operation as an orphan and told the orchestrator to remove it; the
    # count gate then reported the removed path as missing — two blocking findings
    # that could not both be satisfied, and the review never converged.
    for tool_id in _session_tool_ids():
        known |= _spellings(tool_id)

    findings: list[dict] = []

    def _report(asset: str, op_id: str, where: str) -> None:
        if not op_id or _spellings(op_id) & known:
            return
        findings.append({
            "id": _stable_id("SPEC", asset, op_id), "gate": "spec", "severity": "error",
            "asset_type": asset, "operation_id": op_id, "field": None,
            "message": f"{where} {op_id!r} has no OperationSpec — register it with save_operation_spec "
                       "(then regenerate its assets from the spec) or remove the asset; an operation "
                       "without a spec is checked by no gate",
        })

    try:
        from tools.s3_asset_storage import list_session_assets
        seen: set[tuple[str, str]] = set()
        supporting: set[str] = set()      # lambda folders that ship index.py/.js — not business operations
        for key in list_session_assets(session_id) or []:
            parts = [p for p in str(key).split("/") if p]
            # assets/<session>/<type>/<op>/<file>
            if len(parts) >= 5 and parts[2] == "lambda":
                # Same convention as the consistency gates: a business operation
                # is a `handler.py`; `index.py` / `index.js` are supporting
                # Lambdas (customer_lookup, update_q_session, …) with no spec by design.
                if parts[-1] == "handler.py":
                    seen.add(("lambda", parts[3]))
                elif parts[-1] in ("index.py", "index.js"):
                    supporting.add(parts[3])
            elif len(parts) == 4 and parts[2] == "acxd_data_request" and parts[3].endswith(".json"):
                seen.add(("acxd_data_request", parts[3][:-5]))
        for asset, op_id in sorted(seen):
            if asset == "lambda" and op_id in supporting:
                continue
            _report(asset, op_id, "Lambda folder" if asset == "lambda" else "ACXD Data Request")
    except Exception as exc:
        logger.debug("[review_gates] orphan asset scan skipped: %s", exc)

    try:
        import yaml
        from tools.asset_loader import load_existing_asset
        text = load_existing_asset("openapi", file_name="openapi.yaml")
        doc = yaml.safe_load(text) if text else None
        for path, item in ((doc or {}).get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if isinstance(op, dict) and op.get("operationId"):
                    _report("openapi", str(op["operationId"]), f"OpenAPI operation {method.upper()} {path}")
    except Exception as exc:
        logger.debug("[review_gates] orphan openapi scan skipped: %s", exc)
    return findings


def _missing_asset_findings(session_id: str) -> list[dict]:
    """The mirror image of the orphan gate: a spec operation whose Lambda folder,
    OpenAPI operationId or (ACXD) Data Request does not exist. Live (GAON,
    2026-09-12): the orchestrator went from infrastructure to OpenAPI and on to
    the ACXD application without ever calling the Lambda generator; the review
    reported 0 blocking and only the packager refused ('manifest references
    lambda dir ... but the bundle has no generated handler')."""
    from tools.spec_manager import get_all_specs
    specs = get_all_specs() or {}
    if not specs:
        return []
    try:
        from tools.acxd_flow_spec import is_acxd_target
        acxd = bool(is_acxd_target(session_id))
    except Exception:
        acxd = False

    lambda_folders: set[str] = set()
    data_requests: set[str] = set()
    try:
        from tools.s3_asset_storage import list_session_assets
        for key in list_session_assets(session_id) or []:
            parts = [p for p in str(key).split("/") if p]
            if len(parts) >= 5 and parts[2] == "lambda" and parts[-1] in ("handler.py", "index.py", "index.js"):
                lambda_folders |= _spellings(parts[3])
            elif len(parts) == 4 and parts[2] == "acxd_data_request" and parts[3].endswith(".json"):
                data_requests |= _spellings(parts[3][:-5])
    except Exception as exc:
        logger.debug("[review_gates] missing-asset scan skipped: %s", exc)
        return []

    openapi_ops: set[str] = set()
    openapi_seen = False
    try:
        import yaml
        from tools.asset_loader import load_existing_asset
        text = load_existing_asset("openapi", file_name="openapi.yaml")
        if text:
            openapi_seen = True
            doc = yaml.safe_load(text) or {}
            for item in (doc.get("paths") or {}).values():
                if isinstance(item, dict):
                    for op in item.values():
                        if isinstance(op, dict) and op.get("operationId"):
                            openapi_ops |= _spellings(str(op["operationId"]))
    except Exception as exc:
        logger.debug("[review_gates] missing-asset openapi scan skipped: %s", exc)

    findings: list[dict] = []
    for op_id in specs:
        names = _spellings(op_id)
        checks = [("lambda", not (names & lambda_folders), "Lambda handler",
                   "run lambda_generator_agent for this operation")]
        if openapi_seen:
            checks.append(("openapi", not (names & openapi_ops), "OpenAPI path",
                           "run openapi_generator_agent / merge_openapi_fragments"))
        if acxd:
            checks.append(("acxd_data_request", not (names & data_requests), "ACXD Data Request",
                           "run rebuild_acxd_slot_types_tool (rebuilds the Data Requests from the spec)"))
        for asset, missing, label, remedy in checks:
            if missing:
                findings.append({
                    "id": _stable_id("MISSING", asset, op_id), "gate": "spec", "severity": "error",
                    "asset_type": asset, "operation_id": op_id, "field": None,
                    "message": f"Operation {op_id!r} has a spec but no {label} — {remedy}; "
                               "a spec without its asset ships nothing for that operation",
                })
    findings.extend(_missing_knowledge_findings(session_id, acxd))
    return findings


def _missing_knowledge_findings(session_id: str, acxd: bool) -> list[dict]:
    """The FAQ / knowledge-base family. Live (2026-09-20): the document listed
    seven FAQ topics, the FAQ generator was never called, the ACXD bundle had no
    knowledge base and no flow reading one, and the review reported 0 blocking —
    every gate iterated over operations, and the FAQ is not an operation. The
    family is required when the plan has KB topics or the document has an FAQ
    section the customer did not exclude."""
    planned_topics: list[str] = []
    if acxd:
        try:
            from tools.acxd_flow_spec import get_acxd_flow_spec
            fs = get_acxd_flow_spec()
            planned_topics = list(getattr(getattr(fs, "knowledge_base", None), "topics", None) or [])
        except Exception:
            planned_topics = []
    documented = False
    try:
        from tools.requirement_items import load_ledger
        ledger = load_ledger() or {}
        faq = (ledger.get("signals") or {}).get("faq") or {}
        mappings = ledger.get("mappings") or {}
        ids = list(faq.get("item_ids") or [])
        documented = bool(ids) and not all(
            str((mappings.get(i) or {}).get("target", "")).startswith("excluded") for i in ids)
    except Exception:
        documented = False
    if not planned_topics and not documented:
        return []

    faq_files = kb_files = 0
    kb_flows = 0
    try:
        from tools.s3_asset_storage import list_session_assets
        for key in list_session_assets(session_id) or []:
            parts = [p for p in str(key).split("/") if p]
            if len(parts) >= 4 and parts[2] == "faq":
                faq_files += 1
            elif len(parts) >= 5 and parts[2] == "package" and parts[3] == "knowledge_base":
                faq_files += 1          # the FAQ generator's knowledge-base zip
            elif len(parts) >= 4 and parts[2] == "acxd_knowledge_base":
                kb_files += 1
    except Exception as exc:
        logger.debug("[review_gates] knowledge scan skipped: %s", exc)
        return []
    if acxd:
        try:
            from tools.acxd_flow_spec import get_acxd_flow_spec
            fs = get_acxd_flow_spec()
            for f in (getattr(fs, "flows", None) or []):
                if getattr(f, "uses_knowledge_base", False) or any(
                        "knowledge_base" in [str(t) for t in (getattr(st, "journey_tools", None) or [])]
                        for st in (getattr(f, "steps", None) or [])):
                    kb_flows += 1
        except Exception:
            kb_flows = 0

    why = (f"the plan has {len(planned_topics)} knowledge-base topic(s)" if planned_topics
           else "the requirements document has an FAQ section")
    findings: list[dict] = []
    if faq_files == 0:
        findings.append({
            "id": _stable_id("MISSING", "faq", "__all__"), "gate": "spec", "severity": "error",
            "asset_type": "faq", "operation_id": "__all__", "field": None,
            "message": f"No FAQ asset was generated although {why} — run faq_generator_agent; "
                       "without it the caller's side questions all end in the fallback",
        })
    if acxd and kb_files == 0:
        findings.append({
            "id": _stable_id("MISSING", "acxd_knowledge_base", "__all__"), "gate": "spec", "severity": "error",
            "asset_type": "acxd_knowledge_base", "operation_id": "__all__", "field": None,
            "message": f"No ACXD knowledge base was generated although {why} — save_acxd_policies(kb_name, "
                       "kb_topics) if the plan lacks them, then generate the FAQ and re-run "
                       "generate_acxd_application (it builds the knowledge base from the FAQ asset)",
        })
    if acxd and kb_flows == 0:
        findings.append({
            "id": _stable_id("MISSING", "acxd_flow", "faq"), "gate": "spec", "severity": "error",
            "asset_type": "acxd_flow", "operation_id": "__all__", "field": None,
            "message": f"No flow uses the knowledge base although {why} — plan a FAQ flow "
                       "(uses_knowledge_base=true, or a journey with journey_tools=['knowledge_base']) so "
                       "questions outside the operations are answered instead of falling back",
        })
    return findings


def _spellings(name: str) -> set[str]:
    raw = str(name or "")
    snake = re.sub(r"(?<!^)([A-Z])", r"_\1", raw).lower()
    camel = re.sub(r"_+([a-zA-Z0-9])", lambda m: m.group(1).upper(), raw)
    lower_camel = camel[:1].lower() + camel[1:] if camel else camel
    return {raw, raw.lower(), snake, camel, lower_camel, snake.replace("_", "")}


def collect_blocking_findings(session_id: str) -> dict:
    """Run every deterministic gate and return the blocking set.

    Returns {"findings": [...], "gates": {"consistency": n, "parity": n, "acxd-d9": n},
             "count": n}. Stable ids: D:<asset>:<op>:<field>:<issue>,
             PARITY:<path>:<reason>, D9-x:<asset>:<op>:<field>.
    """
    findings: list[dict] = []
    for collector in (_consistency_findings, _parity_findings, _orphan_operation_findings, _missing_asset_findings):
        try:
            findings.extend(collector(session_id))
        except Exception as exc:
            logger.warning("[review_gates] %s failed for %s: %s", collector.__name__, session_id, exc)
    # de-duplicate by id, keep first
    seen: set[str] = set()
    unique: list[dict] = []
    for f in findings:
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        unique.append(f)
    gates: dict[str, int] = {}
    for f in unique:
        gates[f["gate"]] = gates.get(f["gate"], 0) + 1
    return {"findings": unique, "gates": gates, "count": len(unique)}


def diff_findings(previous_ids: Optional[list[str]], current: list[dict]) -> dict:
    """Which blocking ids were fixed since the last review, which are new."""
    prev = set(previous_ids or [])
    now = {f["id"] for f in current}
    return {"fixed": sorted(prev - now), "new": sorted(now - prev), "remaining": sorted(prev & now)}


def format_blocking_summary(report: dict, language: str = "en") -> str:
    count = report.get("count", 0)
    gates = report.get("gates") or {}
    parts = ", ".join(f"{g} {n}" for g, n in gates.items()) or "-"
    if str(language).startswith("ko"):
        return (f"차단 항목(결정론 검사) {count}건 — {parts}. 이 항목이 0건이어야 패키징이 됩니다. "
                "리뷰어의 다른 지적은 권고이며 사용자 선택에 따라 고칩니다.")
    return (f"Blocking findings (deterministic gates): {count} — {parts}. Packaging requires 0. "
            "Everything else the reviewer wrote is advisory and fixed only if the user asks.")
