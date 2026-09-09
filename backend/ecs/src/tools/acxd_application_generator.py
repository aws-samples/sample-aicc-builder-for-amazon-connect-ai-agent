"""Phase-4 ACXD application generation for the Classic Full pipeline.

FAQ generation remains the Classic phase-6 responsibility.  This tool consumes
its persisted ``faq`` assets through ``acxd_generation_context`` and renders
those documents as Knowledge Base articles; it never asks the FAQ generator to
create a second, divergent document set.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from strands import tool

from agents.acxd_flow_generator.agent import generate_acxd_flows
from tools.acxd_bundle import bundle_summary, load_acxd_bundle
from tools.acxd_data_request_builder import build_all_data_requests, build_secret_assets
from tools.acxd_flow_spec import acxd_flow_spec_ready
from tools.acxd_generation_context import get_acxd_spec
from tools.acxd_resource_builders import (
    build_application,
    build_context_variables,
    build_guardrails,
    build_knowledge_base,
    build_slot_types,
)
from tools.clues_format import format_clues
from tools.session_context import current_session_id
from tools.validate_acxd_consistency import validate_acxd_consistency
from tools.validate_acxd_flow import validate_acxd_asset

logger = logging.getLogger(__name__)


def _raw_callable(candidate: Callable[..., Any]) -> Callable[..., Any]:
    return getattr(candidate, "_raw", candidate)


def _save_asset(
    session_id: str,
    asset_type: str,
    file_name: str,
    document: dict | list,
    *,
    preview_operation_id: str | None = None,
) -> None:
    """Save in the shared root layout and emit the standard asset preview."""
    from tools.s3_asset_storage import save_asset_to_s3
    from tools.streaming_callback import stream_asset

    content = json.dumps(document, indent=2, ensure_ascii=False)
    s3_key = save_asset_to_s3(session_id, asset_type, file_name, content)
    stream_asset(
        asset_type,
        file_name,
        content,
        operation_id=preview_operation_id,
        is_complete=True,
        s3_key=s3_key,
    )


def _result(
    status: str,
    counts: dict,
    problems: list[str],
    *,
    action: str = "",
) -> dict:
    artifacts = ", ".join(f"{key}={value}" for key, value in counts.items()) or "None"
    findings = "FAQ assets were rendered into the ACXD Knowledge Base."
    if action:
        findings = action
    clues = format_clues(
        status=status.upper(),
        agent_name="acxd_application_generator",
        operation_id="acxd_application",
        findings=findings,
        artifacts=artifacts,
        issues="; ".join(problems) if problems else "None",
    )
    return {
        "status": status,
        "counts": counts,
        "problems": problems,
        "clues": clues,
        "faq_to_knowledge_base": "FAQ assets are consumed here; FAQ generation is unchanged.",
    }


@tool
def generate_acxd_application(modification_request: str = None) -> dict:
    """Generate the phase-4 ACXD application bundle.

    Refuses until every ACXD flow plan and deterministic/generative decision is
    confirmed.  For a modification request this is deliberately a PATCH-ONLY
    entry point: callers must use ``read_acxd_asset`` then ``patch_acxd_asset``
    with an exact old/new replacement; no generated asset is replaced wholesale.
    """
    if modification_request:
        return _result(
            "patch_required",
            {},
            [
                "ACXD modification requests are patch-only. Read the target with "
                "read_acxd_asset and apply the exact change with patch_acxd_asset; "
                "the application bundle was not regenerated."
            ],
            action=modification_request,
        )

    ready, readiness_problems = acxd_flow_spec_ready()
    if not ready:
        return _result("error", {}, readiness_problems)

    session_id = current_session_id.get() or "default"
    spec = get_acxd_spec().model_dump()
    problems: list[str] = []

    flow_result = _raw_callable(generate_acxd_flows)()
    failed = list(flow_result.get("failed") or []) if isinstance(flow_result, dict) else []
    if not isinstance(flow_result, dict) or flow_result.get("status") == "error" or failed:
        flow_problems = [
            f"{item.get('flow_id', 'flow')}: {' | '.join(item.get('problems') or [])}"
            for item in failed
        ] or ["ACXD flow generation did not return a successful result"]
        return _result("error", {"flows": 0}, flow_problems)

    slot_types, slot_problems = build_slot_types(spec)
    problems.extend(slot_problems)
    for document in slot_types:
        _save_asset(
            session_id,
            "acxd_slot_type",
            f"{document['slotTypeId']}.json",
            document,
            preview_operation_id=document["slotTypeId"],
        )

    data_requests, data_problems = build_all_data_requests(spec)
    problems.extend(data_problems)
    for document in data_requests:
        _save_asset(
            session_id,
            "acxd_data_request",
            f"{document['dataRequestId']}.json",
            document,
            preview_operation_id=document["dataRequestId"],
        )

    secrets = build_secret_assets(spec)
    for document in secrets:
        _save_asset(session_id, "acxd_secret", f"{document['name']}.json", document)

    guardrails, guardrail_problems = build_guardrails(spec)
    problems.extend(guardrail_problems)
    for index, document in enumerate(guardrails, start=1):
        safe_name = "".join(char for char in document["name"] if char.isalnum())
        file_name = f"{safe_name or f'guardrail{index}'}.json"
        _save_asset(session_id, "acxd_guardrail", file_name, document)

    knowledge_base = build_knowledge_base(spec)
    if knowledge_base:
        kb_errors = validate_acxd_asset("knowledge_base", knowledge_base)
        if kb_errors:
            problems.extend(f"knowledge_base: {error}" for error in kb_errors)
        else:
            _save_asset(
                session_id,
                "acxd_knowledge_base",
                "knowledge_base.json",
                knowledge_base,
            )

    application = build_application(spec)
    application_errors = validate_acxd_asset("application", application)
    if application_errors:
        problems.extend(f"application: {error}" for error in application_errors)
    else:
        _save_asset(session_id, "acxd_application", "application.json", application)

    context_variables = {"contextVariables": build_context_variables(spec)}
    _save_asset(
        session_id,
        "acxd_context_variable",
        "context_variables.json",
        context_variables,
    )

    bundle = load_acxd_bundle(session_id)
    problems.extend(str(problem) for problem in validate_acxd_consistency(bundle, spec=spec))
    counts = bundle_summary(bundle)
    status = "partial" if problems else "success"
    return _result(status, counts, problems)
