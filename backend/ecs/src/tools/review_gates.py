"""Deterministic review gates → the *blocking* finding set.

Why: the reviewer is an LLM and each run surfaces a different list (SELC:
13 critical, five fixed, then 21). A loop whose blocking set moves on every
run never converges. The deterministic gates — cross-asset consistency
(D1–D8 + IAM), spec↔OpenAPI shape parity, ACXD D9 — are stable, so they are
the only findings that block packaging; the reviewer's own findings are
advisory. Every blocking finding carries a stable id so the orchestrator can
tell "fixed" from "new" between reviews without re-reading prose.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _stable_id(prefix: str, *parts: Any) -> str:
    bits = [str(p).strip() for p in parts if p not in (None, "")]
    return ":".join([prefix, *bits])[:200]


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


def collect_blocking_findings(session_id: str) -> dict:
    """Run every deterministic gate and return the blocking set.

    Returns {"findings": [...], "gates": {"consistency": n, "parity": n, "acxd-d9": n},
             "count": n}. Stable ids: D:<asset>:<op>:<field>:<issue>,
             PARITY:<path>:<reason>, D9-x:<asset>:<op>:<field>.
    """
    findings: list[dict] = []
    for collector in (_consistency_findings, _parity_findings):
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
