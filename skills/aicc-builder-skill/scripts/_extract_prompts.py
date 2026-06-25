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


def extract_prompts(repo_root: Path, skill_root: Path) -> int:
    """Extract prompts + schemas. Returns the count of un-extracted backend items
    found by the coverage pass (0 == full coverage)."""
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
    # Order mirrors how the live orchestrator composes phases (see
    # PHASE_PROMPTS / get_phase_system_prompt in system_prompt.py): the always-on
    # COMMON + TERMINOLOGY prefix, the per-phase bodies (interview → generation →
    # review → regeneration), then the shared reference sections. ATTACHMENT_HANDLING
    # is injected into every non-interview phase in the webapp, so it belongs here.
    phases = [
        "COMMON_PROMPT",
        "TERMINOLOGY_FACTS",
        "INTERVIEW_PROMPT",
        "GENERATION_PROMPT",
        "REVIEW_PROMPT",
        "REGENERATION_PROMPT",
        "ATTACHMENT_HANDLING",
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

    # ---------------- 3b) standalone orchestrator references ----------------
    # These two carry `{placeholder}` tokens (e.g. {document_content}) so they must
    # be written VERBATIM — never .format()'d — into their own files. They are not
    # part of the phase-composed system prompt above; the skill reads them on demand
    # (document-analysis entry path, OperationSpec authoring template).
    standalone_orch = {
        "DOCUMENT_ANALYSIS_PROMPT": "document_analysis.md",
        "OPERATION_SPEC_TEMPLATE": "operation_spec_template.md",
    }
    for var_name, fname in standalone_orch.items():
        v = sp_ns.get(var_name)
        if isinstance(v, str) and v:
            (out_orch / fname).write_text(v)
            print(f"[ok] orchestrator: {fname} <- {var_name} ({len(v)} chars)")
        else:
            print(f"[!] orchestrator: {var_name} missing or not a string")

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

    schema_models = (
        "OperationSpec",
        "InfrastructureSpec",
        "ToolSpec",
        "FieldSpec",
        "DataSourceSpec",
        "BusinessRule",
        "ErrorResponse",
        "SideEffect",
        "ConversationStep",
        # Session/flow contracts the orchestrator persists (save_session_flow_config /
        # save_contact_flow_spec) and the prompt + contact_flow generators consume.
        # Without these the skill's schema reference is materially incomplete.
        "SessionFlowConfig",
        "ContactFlowSpec",
        "FlowBehavior",
        "CustomerInfoVariable",
        "NoResponsePolicy",
    )
    for cls in schema_models:
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

    # ---------------- 6) COVERAGE ASSERTION (drift-gate blind-spot guard) ----------------
    # The --check drift gate diffs the extractor's output against itself, so it can
    # ONLY catch edits to sections/models the extractor already enumerates. A NEWLY
    # added backend prompt section or Pydantic model would silently never reach
    # resources/ and the gate would still report "no drift". This pass closes that
    # hole: it scans the backend for things that LOOK like they should be extracted
    # and warns about any the extractor's lists don't cover, so future additions
    # surface instead of rotting.
    return _report_coverage(sp_ns, spec_ns, phases, standalone_orch, schema_models)


# Backend prompt strings that are intentionally NOT extracted as standalone skill
# resources (they are compositions/aliases of sections we already emit).
_PROMPT_COVERAGE_IGNORE = {
    "SYSTEM_PROMPT",  # backward-compat concatenation of all sections
}
# Pydantic models that live only nested inside InfrastructureSpec (they surface as
# $defs in InfrastructureSpec.schema.json), so we don't emit standalone files.
_MODEL_COVERAGE_IGNORE = {
    "FlexibleBaseModel",  # shared base, not a contract
    "RdsConfig",
    "DynamoDbConfig",
    "LambdaConfig",
    "ApiGatewayConfig",
    "VpcConfig",
}


def _report_coverage(sp_ns, spec_ns, phases, standalone_orch, schema_models) -> int:
    """Warn about backend prompt sections / spec models the extractor doesn't cover.

    Returns the number of uncovered items found (0 == fully covered). The shell
    --check gate treats a non-zero return as drift so new backend additions can't
    silently bypass the skill.
    """
    covered_prompts = set(phases) | set(standalone_orch) | _PROMPT_COVERAGE_IGNORE
    uncovered = []

    # Any module-level UPPER_SNAKE name bound to a substantial string is a prompt
    # section the skill probably needs. We do NOT filter on a _PROMPT/_TEMPLATE
    # suffix — several real sections (TERMINOLOGY_FACTS, TOOLS_REFERENCE,
    # SCHEMA_REFERENCE, CONNECT_GUIDE, ATTACHMENT_HANDLING) don't use it.
    for name, val in sp_ns.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        if not isinstance(val, str) or len(val) < 200:
            continue
        if name not in covered_prompts:
            uncovered.append(("prompt", name))

    covered_models = set(schema_models) | _MODEL_COVERAGE_IGNORE
    for name, val in spec_ns.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
            continue
        # A FlexibleBaseModel subclass exposes model_json_schema(); the base itself
        # is in the ignore set.
        if not (hasattr(val, "model_json_schema") and isinstance(val, type)):
            continue
        if val.__module__ != "spec_manager_extracted":
            continue  # imported (e.g. pydantic BaseModel), not a spec contract
        if name not in covered_models:
            uncovered.append(("model", name))

    if not uncovered:
        print("[ok] coverage: all backend prompt sections + spec models are extracted.")
        return 0

    print("")
    print("[!] COVERAGE GAP — these backend items are NOT extracted into resources/:")
    for kind, name in uncovered:
        print(f"    - {kind}: {name}")
    print("    Add them to the extractor's `phases`/`standalone_orch`/`schema_models`")
    print("    lists (or to the *_COVERAGE_IGNORE sets if intentionally excluded).")
    return len(uncovered)


def main() -> int:
    argv = sys.argv[1:]
    strict = "--strict" in argv
    argv = [a for a in argv if a != "--strict"]
    if len(argv) != 2:
        print(
            f"Usage: {sys.argv[0]} [--strict] <repo_root> <skill_root>",
            file=sys.stderr,
        )
        return 2
    gaps = extract_prompts(Path(argv[0]).resolve(), Path(argv[1]).resolve())
    # In --strict mode (used by extract_prompts.sh --check) a coverage gap is a hard
    # failure; in plain extract mode it's a warning so a normal re-extract still works.
    return 1 if (strict and gaps) else 0


if __name__ == "__main__":
    sys.exit(main())
