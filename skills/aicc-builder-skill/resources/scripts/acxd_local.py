#!/usr/bin/env python3
"""ACXD runtime target for the CLI skill — the deterministic half.

The webapp builds an Agentic CX Designer application in two halves: a model
writes the operation flows, and code does everything else — the system flows,
slot types, data requests, secrets, guardrails, knowledge bases, context
variables and application; the live-verified runtime contract that normalises a
model-written flow; the cross-asset (D9) gates; the deploy manifest; and the
bundle with its Node runner and ``deploy.sh``. That code is ~10k lines and is
verified against the live service, so this script does not re-implement it: it
imports the same modules from an AICC Builder checkout and runs them against the
skill's local output directory (the NFS session layout the webapp uses is
emulated in a scratch mount).

    acxd_local.py [--output-dir ./aicc-output] [--repo /path/to/aicc-builder]
                  [--project <slug>] <command>

    check       validate state/acxd_flow_spec.json against ACXDFlowSpec and list what is missing
    system      write the deterministic resources into assets/v1/acxd/ (system flows, slot types,
                data requests, secrets, guardrails, knowledge bases, context variables, application)
    normalize   canonicalise + runtime contract + validation for every model-written operation
                flow in assets/v1/acxd/flows/ (in place); exit 1 while any flow still has problems
    validate    the full D9 / manifest gate over the bundle; exit 1 on findings
    package     write the deployable bundle to <output-dir>/bundle/<project>/ (assets/acxd/*,
                contact-flow, cloudformation, lambda, openapi, knowledge-base, deploy-manifest.json,
                runner, deploy.sh, README.md, WIRING-GUIDE.md)

Requirements: Python 3.11+, the backend's Python dependencies (``pip install -r
backend/ecs/requirements.txt`` or the repo's ``backend/.venv``) and Node 18+
only when you go on to deploy. The repo is found from ``--repo``,
``$AICC_BUILDER_REPO`` or, when the skill is used from inside the checkout,
by walking up from this file.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
import types
from pathlib import Path
from typing import Iterable, Optional

SESSION_ID = "local"

#: skill layout (assets/v1/acxd/<dir>) -> NFS asset type the engine reads
ACXD_DIRS = {
    "flows": "acxd_flow",
    "slot-types": "acxd_slot_type",
    "data-requests": "acxd_data_request",
    "guardrails": "acxd_guardrail",
    "knowledge-bases": "acxd_knowledge_base",
    "secrets": "acxd_secret",
}
ACXD_SINGLE_FILES = {
    "application.json": ("acxd_application", "application.json"),
    "context-variables.json": ("acxd_context_variable", "context_variables.json"),
}
CLASSIC_DIRS = ("lambda", "openapi", "infrastructure", "contact_flow", "faq", "prompt", "cloudformation")


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------

def find_repo(explicit: Optional[str]) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("AICC_BUILDER_REPO"):
        candidates.append(Path(os.environ["AICC_BUILDER_REPO"]))
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    for c in candidates:
        if (c / "backend/ecs/src/tools/acxd_runtime_contract.py").is_file():
            return c.resolve()
    sys.exit("acxd_local: cannot find an AICC Builder checkout — pass --repo or set AICC_BUILDER_REPO "
             "(the ACXD engine lives in backend/ecs/src and is not copied into the skill)")


def _light_package(name: str, path: Path) -> None:
    """Register a package without running its __init__ (the backend's package
    inits import every generator, strands and boto3)."""
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = pkg


def bootstrap(repo: Path, mount: Path) -> None:
    os.environ["S3FILES_MOUNT_PATH"] = str(mount)
    os.environ.setdefault("SESSION_STORE_BACKEND", "s3files")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
    os.environ.setdefault("ASSETS_BUCKET", "local-skill-no-bucket")
    os.environ.setdefault("ASSETS_BUCKET_NAME", "local-skill-no-bucket")
    src = repo / "backend/ecs/src"
    sys.path.insert(0, str(src))
    try:
        import strands  # noqa: F401
    except Exception:
        stub = types.ModuleType("strands")
        setattr(stub, "tool", lambda f: f)
        setattr(stub, "Agent", type("Agent", (), {}))
        sys.modules["strands"] = stub
    _light_package("tools", src / "tools")
    _light_package("prompts", src / "prompts")
    _light_package("agents", src / "agents")
    _light_package("agents.acxd_flow_generator", src / "agents/acxd_flow_generator")
    session = importlib.import_module("tools.session_context")
    session.current_session_id.set(SESSION_ID)
    # Everything is read from the emulated mount; the S3 fallbacks the webapp
    # keeps for a cold container are noise here.
    import logging
    logging.getLogger("tools.s3_asset_storage").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# the skill's output dir <-> the engine's session layout
# ---------------------------------------------------------------------------

def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return
    for f in src.rglob("*"):
        if f.is_file() and not f.name.startswith("."):
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)


def materialize(output_dir: Path, mount: Path) -> Path:
    """Copy the skill workspace into ``mount/sessions/local`` the way the webapp
    stores a session on NFS, so every engine reader finds its files."""
    session = mount / "sessions" / SESSION_ID
    if session.exists():
        shutil.rmtree(session)
    state_src = output_dir / "state"
    _copy_tree(state_src, session / "state")
    _copy_tree(state_src / "specs", session / "assets" / "specs")
    assets = output_dir / "assets" / "v1"
    for skill_dir, asset_type in ACXD_DIRS.items():
        (session / "assets" / asset_type).mkdir(parents=True, exist_ok=True)  # present, even if empty: no S3 fallback
        _copy_tree(assets / "acxd" / skill_dir, session / "assets" / asset_type)
    for file_name, (asset_type, stored_name) in ACXD_SINGLE_FILES.items():
        (session / "assets" / asset_type).mkdir(parents=True, exist_ok=True)
        _copy_tree(assets / "acxd" / file_name, session / "assets" / asset_type / stored_name)
    for classic in CLASSIC_DIRS:
        (session / "assets" / classic).mkdir(parents=True, exist_ok=True)
        _copy_tree(assets / classic, session / "assets" / classic)
    return session


def load_spec_dict(output_dir: Path) -> dict:
    """The generation context the webapp hands its builders: the confirmed ACXD
    flow spec merged with the OperationSpecs (data integrations, response
    fields, slot constraints)."""
    path = output_dir / "state" / "acxd_flow_spec.json"
    if not path.is_file():
        sys.exit(f"acxd_local: {path} is missing — the interview must have saved the ACXD flow spec "
                 "(resources/schemas/ACXDFlowSpec.schema.json) before generation")
    context = importlib.import_module("tools.acxd_generation_context")
    return context.get_acxd_spec(SESSION_ID).model_dump()


def write_json(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_check(output_dir: Path, mount: Path, project: str) -> int:
    materialize(output_dir, mount)
    spec = load_spec_dict(output_dir)
    flow_spec = importlib.import_module("tools.acxd_flow_spec")
    ready, problems = flow_spec.acxd_flow_spec_ready()
    plans = spec.get("flows") or []
    print(f"ACXD flow spec: {len(plans)} flow plan(s), {len(spec.get('data_integrations') or [])} data integration(s), "
          f"{len(spec.get('slot_types') or [])} custom slot type(s)")
    for plan in plans:
        print(f"  - {plan.get('flow_id')} [{plan.get('role') or 'operation'}] confirmed={plan.get('confirmed')}")
    if not ready:
        print("NOT READY:")
        for p in problems:
            print(f"  ! {p}")
        return 1
    print("ready for generation")
    return 0


def cmd_system(output_dir: Path, mount: Path, project: str) -> int:
    materialize(output_dir, mount)
    spec = load_spec_dict(output_dir)
    sf = importlib.import_module("tools.acxd_system_flows")
    rb = importlib.import_module("tools.acxd_resource_builders")
    drb = importlib.import_module("tools.acxd_data_request_builder")
    out = output_dir / "assets" / "v1" / "acxd"
    problems: list[str] = []

    ids = sf.resolve_system_flow_ids(spec)
    roles = set(sf.ALWAYS_GENERATED_SYSTEM_ROLES) | set(sf.conditional_system_roles(spec))
    roles |= {str(p.get("role")) for p in spec.get("flows") or []
              if isinstance(p, dict) and sf.is_system_flow_role(p.get("role"))}
    for role in sorted(roles):
        flow = sf.build_system_flow(role, spec, flow_ids=ids)
        if flow is None:
            continue
        write_json(out / "flows" / f"{flow['flowId']}.json", flow)
        print(f"  + flow {flow['flowId']} ({role})")

    slot_types, p = rb.build_slot_types(spec)
    problems += p
    for doc in slot_types:
        write_json(out / "slot-types" / f"{doc['slotTypeId']}.json", doc)
    print(f"  + {len(slot_types)} slot type(s)")

    data_requests, p = drb.build_all_data_requests(spec)
    problems += p
    for doc in data_requests:
        write_json(out / "data-requests" / f"{doc['dataRequestId']}.json", doc)
    print(f"  + {len(data_requests)} data request(s)")

    secrets = drb.build_secret_assets(spec)
    for doc in secrets:
        write_json(out / "secrets" / f"{doc['name']}.json", doc)
    print(f"  + {len(secrets)} secret(s)")

    guardrails, p = rb.build_guardrails(spec)
    problems += p
    for doc in guardrails:
        write_json(out / "guardrails" / f"{doc['name']}.json", doc)
    print(f"  + {len(guardrails)} guardrail(s)")

    kb = rb.build_knowledge_base(spec)
    if kb:
        write_json(out / "knowledge-bases" / f"{kb['name']}.json", kb)
        print(f"  + knowledge base {kb['name']} (articles: write them with the faq generator, then re-run package)")

    write_json(out / "context-variables.json", rb.build_context_variables(spec))
    write_json(out / "application.json", rb.build_application(spec))
    print("  + context-variables.json, application.json")
    for problem in problems:
        print(f"  ! {problem}")
    return 1 if problems else 0


def _operation_plans(spec: dict) -> dict:
    sf = importlib.import_module("tools.acxd_system_flows")
    return {p["flow_id"]: p for p in spec.get("flows") or []
            if isinstance(p, dict) and p.get("flow_id") and not sf.is_system_flow_role(p.get("role"))}


def cmd_normalize(output_dir: Path, mount: Path, project: str) -> int:
    materialize(output_dir, mount)
    spec = load_spec_dict(output_dir)
    agent = importlib.import_module("agents.acxd_flow_generator.agent")
    plans = _operation_plans(spec)
    flows_dir = output_dir / "assets" / "v1" / "acxd" / "flows"
    failed = 0
    seen: set[str] = set()
    for path in sorted(flows_dir.glob("*.json")) if flows_dir.is_dir() else []:
        flow = json.loads(path.read_text(encoding="utf-8"))
        flow_id = str(flow.get("flowId") or path.stem)
        plan = plans.get(flow_id)
        if plan is None:
            continue  # a system flow, or a flow no plan asked for
        seen.add(flow_id)
        flow, problems, notes, changes = agent.finalize_generated_flow(flow, plan, spec)
        write_json(path, flow)
        print(f"{flow_id}: {len(changes)} canonicalization(s), {len(notes)} contract change(s), {len(problems)} problem(s)")
        for note in notes:
            print(f"    ~ {note}")
        for problem in problems:
            print(f"    ! {problem}")
        failed += bool(problems)
    for flow_id in sorted(set(plans) - seen):
        print(f"{flow_id}: MISSING — write assets/v1/acxd/flows/{flow_id}.json with the acxd_flow_generator prompt")
        failed += 1
    return 1 if failed else 0


def _prepare(output_dir: Path, mount: Path, project: str):
    materialize(output_dir, mount)
    packager = importlib.import_module("tools.asset_packager")
    return packager, packager._prepare_acxd_package(SESSION_ID, project)


def cmd_validate(output_dir: Path, mount: Path, project: str) -> int:
    _, (bundle, manifest, problems) = _prepare(output_dir, mount, project)
    counts = {k: len(v) for k, v in bundle.items() if isinstance(v, list) and v}
    print("bundle:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty")
    print(f"manifest steps: {len(manifest.get('steps') or [])}")
    for problem in problems:
        print(f"  ! {problem}")
    print("OK — no findings" if not problems else f"{len(problems)} finding(s)")
    return 1 if problems else 0


def cmd_package(output_dir: Path, mount: Path, project: str, skill_root: Path) -> int:
    packager, (bundle, manifest, problems) = _prepare(output_dir, mount, project)
    if problems:
        for problem in problems:
            print(f"  ! {problem}")
        print("refusing to package while the gate has findings")
        return 1
    dest = output_dir / "bundle" / project
    if dest.exists():
        shutil.rmtree(dest)
    for relative_path, payload, executable in packager.build_acxd_zip_entries(project, bundle, manifest):
        target = dest / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        if executable:
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assets = output_dir / "assets" / "v1"
    infra = assets / "infrastructure" / "template.yaml"
    if infra.is_file():
        _copy_tree(infra, dest / "cloudformation" / "infrastructure.yaml")
    for lambda_dir in sorted((assets / "lambda").glob("*")) if (assets / "lambda").is_dir() else []:
        if lambda_dir.is_dir():
            _copy_tree(lambda_dir, dest / "lambda" / lambda_dir.name)
    _copy_tree(assets / "openapi", dest / "openapi")
    _copy_tree(assets / "faq", dest / "knowledge-base")
    _copy_tree(output_dir / "state" / "specs", dest / "specs")
    deploy = skill_root / "resources" / "templates" / "deploy_workshop.sh"
    if deploy.is_file():
        shutil.copyfile(deploy, dest / "deploy.sh")
        (dest / "deploy.sh").chmod(0o755)
    readme = dest / "README.md"
    readme.write_text(f"# {project}\n\n" + packager._generate_acxd_readme_section(), encoding="utf-8")
    (dest / "WIRING-GUIDE.md").write_text(packager._generate_wiring_guide(bundle), encoding="utf-8")
    files = sum(1 for f in dest.rglob("*") if f.is_file())
    print(f"bundle written: {dest} ({files} files)")
    print("next: cd into it, export CONNECT_INSTANCE_ID / ACXD_WORKSPACE_ID / ACXD_API_KEY, ./deploy.sh --dry-run")
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "system", "normalize", "validate", "package"))
    parser.add_argument("--output-dir", default="./aicc-output")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--project", default=None, help="bundle/project slug (default: state/project.json company slug or 'aicc-poc')")
    args = parser.parse_args(list(argv) if argv is not None else None)

    output_dir = Path(args.output_dir).resolve()
    if not output_dir.is_dir():
        sys.exit(f"acxd_local: {output_dir} does not exist")
    repo = find_repo(args.repo)
    skill_root = Path(__file__).resolve().parents[2]
    project = args.project
    if not project:
        project_file = output_dir / "state" / "project.json"
        if project_file.is_file():
            try:
                meta = json.loads(project_file.read_text(encoding="utf-8"))
                project = str(meta.get("project_slug") or meta.get("slug") or meta.get("company") or "").strip()
            except (OSError, json.JSONDecodeError):
                project = ""
        project = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (project or "aicc-poc")).strip("-").lower() or "aicc-poc"

    with tempfile.TemporaryDirectory(prefix="acxd-local-") as tmp:
        mount = Path(tmp)
        bootstrap(repo, mount)
        if args.command == "check":
            return cmd_check(output_dir, mount, project)
        if args.command == "system":
            return cmd_system(output_dir, mount, project)
        if args.command == "normalize":
            return cmd_normalize(output_dir, mount, project)
        if args.command == "validate":
            return cmd_validate(output_dir, mount, project)
        return cmd_package(output_dir, mount, project, skill_root)


if __name__ == "__main__":
    sys.exit(main())
