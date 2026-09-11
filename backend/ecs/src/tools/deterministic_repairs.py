"""Deterministic repairs the orchestrator can run instead of patching by hand.

Both act on stored assets of the current session and re-run the same code the
generators now use, so a session generated before these rules existed can be
brought to zero blocking findings in one call each:

* `enforce_openapi_contract_tool` — re-project openapi.yaml's request/response
  schemas from the OperationSpecs (tools/response_contract.py). Clears every
  PARITY finding by construction.
* `rebuild_acxd_slot_types_tool` — derive the custom slot types again from the
  confirmed flow plans + FieldSpec constraints (one per field) and re-save them.
  Clears the D9-4 "requires a generated custom slot type" findings.
"""
from __future__ import annotations

import json
import logging

from strands import tool

logger = logging.getLogger(__name__)


def _session_id() -> str:
    from tools.session_context import current_session_id
    return current_session_id.get() or "default"


@tool
def enforce_openapi_contract_tool() -> dict:
    """Re-project openapi.yaml's schemas from the OperationSpecs (deterministic).

    Use when the review's blocking findings include PARITY:* items. Request
    schemas become the spec's input_fields, success responses become the shared
    envelope (success/errorCode/message) + output_fields at the spec's status
    code; the generator's descriptions are kept. Nothing is invented.
    """
    from tools.asset_loader import load_existing_asset
    from tools.response_contract import enforce_openapi_yaml
    from tools.s3_asset_storage import save_asset_to_s3
    from tools.streaming_callback import stream_asset

    session_id = _session_id()
    yaml_text = load_existing_asset("openapi", file_name="openapi.yaml")
    if not yaml_text:
        return {"status": "error", "problems": ["openapi.yaml not found for this session"]}
    new_text, changes = enforce_openapi_yaml(yaml_text)
    if new_text == yaml_text:
        return {"status": "unchanged", "changes": changes,
                "summary": "openapi.yaml already matches the OperationSpecs"}
    # keep the existing operation folder (the API title slug) so the file has one home
    op_id = None
    try:
        from tools.s3_asset_storage import list_session_assets
        for key in list_session_assets(session_id) or []:
            parts = str(key).split("/")
            if len(parts) >= 5 and parts[2] == "openapi" and parts[-1] == "openapi.yaml":
                op_id = parts[3]
                break
    except Exception:
        pass
    save_asset_to_s3(session_id=session_id, asset_type="openapi", file_name="openapi.yaml",
                     content=new_text, operation_id=op_id)
    try:
        stream_asset("openapi", "openapi.yaml", new_text, operation_id=op_id, is_complete=True, force_full=True)
    except Exception as exc:  # preview is best-effort
        logger.debug("[repair] openapi re-stream skipped: %s", exc)
    return {"status": "updated", "changes": changes,
            "summary": f"openapi.yaml re-projected from the spec ({len(changes)} change(s)); "
                       "re-run the review — PARITY findings should be gone"}


@tool
def rebuild_acxd_slot_types_tool() -> dict:
    """Rebuild the ACXD slot types from the confirmed flow plans + FieldSpecs (deterministic).

    Use when the review's blocking findings include D9-4 "requires a generated
    custom slot type" items: one slot type per constrained field, values from
    the spec's enum_values, regex/length constraints carried over. Flows are
    NOT regenerated; only slot type assets are re-saved.
    """
    from tools.acxd_flow_spec import get_acxd_flow_spec, is_acxd_target
    from tools.acxd_generation_context import _derive_slot_types
    from tools.acxd_resource_builders import build_slot_types
    from tools.s3_asset_storage import save_asset_to_s3
    from tools.spec_manager import get_all_specs
    from tools.streaming_callback import stream_asset

    session_id = _session_id()
    if not is_acxd_target(session_id):
        return {"status": "error", "problems": ["this session's runtime target is not ACXD"]}
    flow_spec = get_acxd_flow_spec()
    if flow_spec is None:
        return {"status": "error", "problems": ["ACXD flow spec not saved"]}
    plans = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in (flow_spec.flows or [])]
    operations = {op_id: (s.model_dump() if hasattr(s, "model_dump") else dict(s))
                  for op_id, s in (get_all_specs() or {}).items()}
    derived = _derive_slot_types(plans, operations)
    documents, problems = build_slot_types({"slot_types": derived})
    saved = []
    for document in documents:
        file_name = f"{document['slotTypeId']}.json"
        content = json.dumps(document, ensure_ascii=False, indent=2)
        save_asset_to_s3(session_id=session_id, asset_type="acxd_slot_type", file_name=file_name, content=content)
        try:
            stream_asset("acxd_slot_type", file_name, content, operation_id=document["slotTypeId"],
                         is_complete=True, force_full=True)
        except Exception as exc:
            logger.debug("[repair] slot type re-stream skipped: %s", exc)
        saved.append(document["slotTypeId"])
    return {"status": "updated" if saved else "unchanged", "slot_types": saved, "problems": problems,
            "summary": f"rebuilt {len(saved)} slot type(s) from the flow plans: {', '.join(saved)}. "
                       "Flows that still reference an old shared slot type id (e.g. 'enum') need "
                       "patch_acxd_asset to point at the per-field id; then re-run the review."}
