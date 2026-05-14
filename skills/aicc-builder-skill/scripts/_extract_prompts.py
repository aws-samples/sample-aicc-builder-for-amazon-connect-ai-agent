#!/usr/bin/env python3
"""Re-extract orchestrator + sub-agent prompts and JSON Schemas from the
ECS backend into this skill's resources/ tree.

Called by extract_prompts.sh. Do not run directly unless you know where
your args point.

Args:
    argv[1]: repo root (contains backend/ecs/...)
    argv[2]: skill root (contains resources/, claude/, kiro/)
"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path


def extract_prompts(repo_root: Path, skill_root: Path) -> None:
    src = repo_root / "backend/ecs/src"
    agents_dir = src / "agents"
    out_sub = skill_root / "resources/sub-agents"
    out_orch = skill_root / "resources/orchestrator"
    out_schemas = skill_root / "resources/schemas"
    for d in (out_sub, out_orch, out_schemas):
        d.mkdir(parents=True, exist_ok=True)

    # ---------------- 1) shared consistency rules ----------------
    cr_ns: dict = {}
    exec((agents_dir / "_consistency_rules.py").read_text(), cr_ns)
    consistency_rules = cr_ns.get("CONSISTENCY_RULES", "")
    terminology = cr_ns.get("SUBAGENT_TERMINOLOGY_AND_ESCALATION", "")
    shared = []
    for n, v in (
        ("CONSISTENCY_RULES", consistency_rules),
        ("SUBAGENT_TERMINOLOGY_AND_ESCALATION", terminology),
    ):
        if isinstance(v, str) and v:
            shared.append(f"<!-- ============ {n} ============ -->\n\n{v}\n")
    (out_sub / "_shared_rules.md").write_text("\n".join(shared))
    print(f"[ok] _shared_rules.md ({len(consistency_rules) + len(terminology)} chars)")

    # ---------------- 2) sub-agent prompts ----------------
    agents = [
        "lambda_generator",
        "openapi_generator",
        "prompt_generator",
        "contact_flow_generator",
        "infrastructure_generator",
        "faq_generator",
        "research_agent",
        "reviewer_agent",
    ]
    for a in agents:
        sp = agents_dir / a / "system_prompt.py"
        if not sp.exists():
            print(f"[!] {a}: no system_prompt.py")
            continue
        text = sp.read_text()
        # Strip relative imports; we'll inject replacements in the exec namespace
        text = re.sub(r"from \.\.[^\n]+import[^\n]+\n", "", text)
        text = re.sub(r"from \.[^\n]+import[^\n]+\n", "", text)
        ns = {
            "CONSISTENCY_RULES": consistency_rules,
            "SUBAGENT_TERMINOLOGY_AND_ESCALATION": terminology,
        }
        try:
            exec(text, ns)
        except Exception as e:
            print(f"[!] {a}: exec failed: {e}")
            continue
        picked = None
        payload: str | None = None
        for name, val in ns.items():
            if name.startswith("_") or not isinstance(val, str):
                continue
            if (
                name.endswith("_PROMPT") or name.endswith("_SYSTEM_PROMPT")
            ) and len(val) > 300:
                picked = name
                payload = val
                break
        if payload is None:
            print(f"[!] {a}: no prompt string found")
            continue
        (out_sub / f"{a}.md").write_text(payload)
        print(f"[ok] {a}.md <- {picked} ({len(payload)} chars)")

    # ---------------- 3) orchestrator prompt (phase-split) ----------------
    sys.path.insert(0, str(repo_root / "backend/ecs"))
    sp_text = (src / "prompts/system_prompt.py").read_text()
    sp_ns: dict = {}
    exec(sp_text, sp_ns)
    phases = [
        "COMMON_PROMPT",
        "TERMINOLOGY_FACTS",
        "INTERVIEW_PROMPT",
        "GENERATION_PROMPT",
        "REVIEW_PROMPT",
        "TOOLS_REFERENCE",
        "SCHEMA_REFERENCE",
        "CONNECT_GUIDE",
    ]
    parts = []
    for p in phases:
        v = sp_ns.get(p)
        if isinstance(v, str):
            parts.append(f"\n\n<!-- ============ {p} ============ -->\n\n{v}")
            print(f"[ok] orchestrator: appended {p} ({len(v)} chars)")
    (out_orch / "system_prompt.md").write_text("".join(parts))

    # ---------------- 4) interview agent prompt ----------------
    ia_text = (src / "prompts/interview_agent_prompt.py").read_text()
    ia_ns: dict = {}
    exec(ia_text, ia_ns)
    for n, v in ia_ns.items():
        if (
            isinstance(v, str)
            and n.endswith("_PROMPT")
            and not n.startswith("_")
            and len(v) > 500
        ):
            (out_orch / "interview_agent.md").write_text(v)
            print(f"[ok] interview_agent.md <- {n} ({len(v)} chars)")
            break

    # ---------------- 5) OperationSpec JSON Schemas ----------------
    # Stub 'strands' and 'tools.*' so spec_manager.py imports cleanly
    strands_stub = types.ModuleType("strands")
    setattr(strands_stub, "tool", lambda f: f)
    sys.modules["strands"] = strands_stub
    tools_stub = types.ModuleType("tools")
    sys.modules["tools"] = tools_stub
    for mod_name, attrs in {
        "tools.session_context": (
            "current_session_id",
            "operation_specs_bucket",
            "get_infrastructure_spec_for",
            "set_infrastructure_spec_for",
            "get_session_flow_config_for",
            "set_session_flow_config_for",
            "session_tools_bucket",
            "get_session_context",
            "get_or_create_session",
        ),
        "tools.s3_asset_storage": (
            "save_asset_to_s3",
            "get_asset_from_s3",
            "list_session_assets",
            "delete_asset_from_s3",
            "save_spec_to_s3",
        ),
    }.items():
        m = types.ModuleType(mod_name)
        for a_name in attrs:
            setattr(m, a_name, lambda *a, **k: None)
        sys.modules[mod_name] = m

    spec_text = (src / "tools/spec_manager.py").read_text()
    spec_ns: dict = {"__name__": "spec_manager_extracted"}
    exec(spec_text, spec_ns)
    # Resolve forward refs before calling model_json_schema()
    for cls in spec_ns.values():
        try:
            if hasattr(cls, "model_rebuild") and callable(getattr(cls, "model_rebuild")):
                cls.model_rebuild(_types_namespace=spec_ns, raise_errors=False)
        except Exception:
            pass

    for cls in (
        "OperationSpec",
        "InfrastructureSpec",
        "ToolSpec",
        "FieldSpec",
        "DataSourceSpec",
        "BusinessRule",
        "ErrorResponse",
        "SideEffect",
        "ConversationStep",
    ):
        model = spec_ns.get(cls)
        if model is None:
            print(f"[!] missing {cls}")
            continue
        try:
            schema = model.model_json_schema()
        except Exception as e:
            print(f"[!] {cls}: schema generation failed: {e}")
            continue
        p = out_schemas / f"{cls}.schema.json"
        p.write_text(json.dumps(schema, indent=2))
        print(f"[ok] {cls}.schema.json ({p.stat().st_size} bytes)")


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <repo_root> <skill_root>", file=sys.stderr)
        return 2
    extract_prompts(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
