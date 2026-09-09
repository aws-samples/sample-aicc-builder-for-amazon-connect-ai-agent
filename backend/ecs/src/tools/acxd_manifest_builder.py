"""Build and validate deterministic ACXD deployment manifests.

The manifest is a compact contract between the generated asset bundle and the
static Node.js runner.  It is derived from ``load_acxd_bundle`` inventory;
models never choose deployment order or emit executable deployment logic.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import jsonschema
from strands import tool

from tools.session_context import current_session_id

logger = logging.getLogger(__name__)

SDK_VERSION = "0.1.0"
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "acxd" / "deploy_manifest.schema.json"

#: Bundle-relative files.  The contact-flow asset deliberately remains in the
#: Classic location so an ACXD bundle keeps the same Connect import artifact.
FILES = {
    "slot_types": "assets/acxd/slot-types/*.json",
    "context_variables": "assets/acxd/context-variables.json",
    "data_requests": "assets/acxd/data-requests/*.json",
    "flows": "assets/acxd/flows/*.json",
    "knowledge_bases": "assets/acxd/knowledge-bases/*.json",
    "guardrails": "assets/acxd/guardrails/*.json",
    "application": "assets/acxd/application.json",
    "contact_flows": "contact-flow/*.json",
    "secrets": "assets/acxd/secrets/*.json",
}

#: This is the only permitted order for deployed resources.  Checks use the
#: relative ordering of entries that are present, so a mock-only bundle can
#: omit its backend without weakening the ordering of its ACXD assets.
_CANONICAL_STEP_ORDER = (
    "deploy-cfn-backend",
    "wire-webhook-urls",
    "upsert-secrets",
    "upsert-slot-types",
    "upsert-context-variables",
    "upsert-data-requests",
    "upsert-knowledge-bases",
    "upsert-guardrails",
    "upsert-flows",
    "compose-application",
    "build-application",
    "deploy-application",
    "import-contact-flows",
)

#: ACXD CreateApplicationBuild rejects longer version strings.
MAX_BUILD_VERSION_LENGTH = 16


def validate_deploy_manifest(manifest: dict) -> list[str]:
    """Return Draft 2020-12 schema errors for *manifest*.

    Kept here rather than in the removed ACXD-only packager: both the single
    asset packager and the manifest-generation tool need exactly the same
    schema gate.
    """
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"could not load deploy manifest schema: {exc}"]
    validator = jsonschema.Draft202012Validator(schema)
    return [
        "$" + "".join(f"[{part!r}]" for part in error.absolute_path)
        + f": {error.message}"
        for error in sorted(validator.iter_errors(manifest), key=str)
    ]


def _project_slug(value: Any) -> str:
    """Make a schema-valid project name when callers omit one."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    slug = slug[:64].strip("-")
    if len(slug) < 3:
        return "aicc-acxd"
    return slug


def _derive_lambda_dirs(bundle: dict) -> list[str]:
    """Return generated handler directories for external Data Requests."""
    dirs: list[str] = []
    for data_request in bundle.get("data_requests") or []:
        if not isinstance(data_request, dict):
            continue
        if (data_request.get("webhook") or {}).get("implementation") != "external":
            continue
        request_id = data_request.get("dataRequestId")
        if request_id:
            directory = f"lambda/{request_id}"
            if directory not in dirs:
                dirs.append(directory)
    return dirs


def _needs_webhook_backend(bundle: dict) -> bool:
    return "{WEBHOOK_URL}" in json.dumps(bundle.get("data_requests") or [])


def _needs_secrets(bundle: dict) -> bool:
    return bool(re.search(r"\{\{secrets\.[A-Za-z0-9_]+\}\}",
                          json.dumps(bundle.get("data_requests") or [])))


def build_manifest(
    bundle: dict,
    *,
    project_name: str | None = None,
    environment: str = "development",
    region: str | None = None,
    cfn_template_path: str = "cloudformation/infrastructure.yaml",
    lambda_dirs: list[str] | None = None,
    build_version: str = "1.0",
) -> dict:
    """Build a schema-ready manifest from one ``load_acxd_bundle`` result.

    Dependency order is fixed: backend/webhook, secrets, slot types, context
    variables, data requests, knowledge bases, guardrails, flows, application
    compose/build/deploy, then the Connect contact-flow import.
    """
    if len(str(build_version)) > MAX_BUILD_VERSION_LENGTH:
        raise ValueError(
            f"build version {build_version!r} exceeds the live API limit of "
            f"{MAX_BUILD_VERSION_LENGTH} characters")

    application = bundle.get("application") or {}
    project = _project_slug(project_name or application.get("name") or "aicc-acxd")
    steps: list[dict] = []
    backend_lambda_dirs = (lambda_dirs if lambda_dirs is not None
                           else _derive_lambda_dirs(bundle))

    if _needs_webhook_backend(bundle):
        params: dict[str, Any] = {
            "templatePath": cfn_template_path,
            "stackName": f"{project}-stack",
        }
        if backend_lambda_dirs:
            params["lambdaDirs"] = backend_lambda_dirs
        steps.append({
            "type": "deploy-cfn-backend",
            "description": "Deploy the Classic backend for external ACXD data requests",
            "params": params,
        })
        steps.append({"type": "wire-webhook-urls"})

    if bundle.get("secrets") or _needs_secrets(bundle):
        steps.append({"type": "upsert-secrets", "params": {"files": [FILES["secrets"]]}})
    if bundle.get("slot_types"):
        steps.append({"type": "upsert-slot-types", "params": {"files": [FILES["slot_types"]]}})
    if bundle.get("context_variables"):
        steps.append({"type": "upsert-context-variables",
                      "params": {"files": [FILES["context_variables"]]}})
    if bundle.get("data_requests"):
        steps.append({"type": "upsert-data-requests",
                      "params": {"files": [FILES["data_requests"]]}})
    if bundle.get("knowledge_bases"):
        steps.append({"type": "upsert-knowledge-bases",
                      "params": {"files": [FILES["knowledge_bases"]]}})
    if bundle.get("guardrails"):
        steps.append({"type": "upsert-guardrails",
                      "params": {"files": [FILES["guardrails"]]}})
    if application:
        steps.extend([
            {"type": "upsert-flows", "params": {"files": [FILES["flows"]]}},
            {"type": "compose-application", "params": {"file": FILES["application"]}},
            {"type": "build-application", "params": {"version": str(build_version)}},
            {"type": "deploy-application", "params": {"environment": environment}},
        ])
    elif bundle.get("flows"):
        # Retain the flow step so coverage tells the operator that the missing
        # application is the root cause instead of silently dropping flows.
        steps.append({"type": "upsert-flows", "params": {"files": [FILES["flows"]]}})
    if bundle.get("contact_flows"):
        steps.append({"type": "import-contact-flows",
                      "params": {"files": [FILES["contact_flows"]]}})

    manifest: dict[str, Any] = {
        "manifestVersion": "1.0",
        "project": project,
        "sdk": {"package": "amazon-connect-acxd-sdk", "version": SDK_VERSION},
        "steps": steps,
    }
    if region:
        manifest["region"] = region
    return manifest


def build_deploy_manifest(
    project: str,
    bundle: dict,
    *,
    environment: str = "development",
    region: str | None = None,
    cfn_template_path: str = "cloudformation/infrastructure.yaml",
    lambda_dirs: list[str] | None = None,
    build_version: str = "1.0",
) -> dict:
    """Backward-compatible name for callers written before target unification."""
    return build_manifest(
        bundle,
        project_name=project,
        environment=environment,
        region=region,
        cfn_template_path=cfn_template_path,
        lambda_dirs=lambda_dirs,
        build_version=build_version,
    )


def check_manifest_coverage(manifest: dict, bundle: dict) -> list[str]:
    """Return missing-asset coverage and dependency-order problems."""
    problems: list[str] = []
    step_types = [step.get("type") for step in manifest.get("steps") or []]

    required = {
        "secrets": "upsert-secrets",
        "slot_types": "upsert-slot-types",
        "context_variables": "upsert-context-variables",
        "data_requests": "upsert-data-requests",
        "knowledge_bases": "upsert-knowledge-bases",
        "guardrails": "upsert-guardrails",
        "flows": "upsert-flows",
        "contact_flows": "import-contact-flows",
    }
    for bundle_key, step_type in required.items():
        if bundle.get(bundle_key) and step_type not in step_types:
            problems.append(
                f"bundle has {bundle_key} but the manifest has no '{step_type}' step")

    if bundle.get("application"):
        for step_type in ("compose-application", "build-application", "deploy-application"):
            if step_type not in step_types:
                problems.append(
                    f"bundle has an application but the manifest has no '{step_type}' step")
    elif bundle.get("flows"):
        problems.append("bundle has flows but no ACXD application asset")

    if _needs_webhook_backend(bundle):
        for step_type in ("deploy-cfn-backend", "wire-webhook-urls"):
            if step_type not in step_types:
                problems.append(
                    "data requests use {WEBHOOK_URL} but the manifest has no "
                    f"'{step_type}' step")

    latest_index = -1
    latest_step = None
    for step_type in _CANONICAL_STEP_ORDER:
        if step_type not in step_types:
            continue
        current_index = step_types.index(step_type)
        if current_index < latest_index:
            problems.append(
                f"step '{latest_step}' must run before '{step_type}'")
        else:
            latest_index = current_index
            latest_step = step_type
    return problems


@tool
def generate_acxd_deploy_manifest(
    project_name: str,
    environment: str = "development",
) -> dict:
    """Build and persist the deploy manifest for the current ACXD session."""
    from tools.acxd_bundle import load_acxd_bundle
    from tools.s3_asset_storage import save_asset_to_s3

    session_id = current_session_id.get() or "default"
    bundle = load_acxd_bundle(session_id)
    manifest = build_manifest(bundle, project_name=project_name, environment=environment)
    problems = validate_deploy_manifest(manifest) + check_manifest_coverage(manifest, bundle)
    if problems:
        return {"status": "error", "problems": problems}

    save_asset_to_s3(
        session_id,
        "acxd_deploy_manifest",
        "deploy-manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return {
        "status": "success",
        "steps": [step["type"] for step in manifest["steps"]],
        "project": manifest["project"],
        "environment": environment,
    }
