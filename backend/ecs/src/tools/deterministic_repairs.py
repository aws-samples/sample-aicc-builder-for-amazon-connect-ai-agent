"""Deterministic repairs the orchestrator can run instead of patching by hand.

Both act on stored assets of the current session and re-run the same code the
generators now use, so a session generated before these rules existed can be
brought to zero blocking findings in one call each:

* `enforce_openapi_contract_tool` — re-project openapi.yaml's request/response
  schemas from the OperationSpecs (tools/response_contract.py). Clears every
  PARITY finding by construction.
* `rebuild_acxd_slot_types_tool` — derive the custom slot types again from the
  confirmed flow plans + FieldSpec constraints (one per field) and re-save them,
  and rebuild the Data Requests from the spec (envelope + output_fields).
  Clears the D9-4 "requires a generated custom slot type" and D9-3 findings.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

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
    s3_key = save_asset_to_s3(session_id=session_id, asset_type="openapi", file_name="openapi.yaml",
                              content=new_text, operation_id=op_id)
    try:
        # pass the saved key so the preview stream does not save a second copy
        stream_asset("openapi", "openapi.yaml", new_text, operation_id=op_id, is_complete=True,
                     force_full=True, s3_key=s3_key)
    except Exception as exc:  # preview is best-effort
        logger.debug("[repair] openapi re-stream skipped: %s", exc)
    return {"status": "updated", "changes": changes,
            "summary": f"openapi.yaml re-projected from the spec ({len(changes)} change(s)); "
                       "re-run the review — PARITY findings should be gone"}


@tool
def rebuild_acxd_slot_types_tool() -> dict:
    """Rebuild the ACXD slot types and Data Requests from the spec (deterministic).

    Use when the review's blocking findings include D9-4 "requires a generated
    custom slot type" or D9-3 (Data Request ↔ OpenAPI) items: one slot type per
    constrained field (values from enum_values, regex/length carried over) and
    Data Requests with the shared response envelope. Flows are NOT regenerated.
    """
    from tools.acxd_flow_spec import get_acxd_flow_spec, is_acxd_target
    from tools.acxd_generation_context import _derive_slot_types
    from tools.acxd_resource_builders import build_slot_types
    from tools.asset_loader import load_existing_asset
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

    def _write(asset_type: str, doc_id: str, document: dict) -> str:
        """Write the canonical flat file once and report 'updated' / 'unchanged'.

        The preview stream is given the saved key: without it, stream_asset saves
        the document AGAIN under `<type>/<id>/<file>` (a live run produced a second
        copy of every slot type and data request, and the loader then failed the
        bundle on duplicate ids).
        """
        file_name = f"{doc_id}.json"
        content = json.dumps(document, ensure_ascii=False, indent=2)
        previous = load_existing_asset(asset_type, file_name=file_name)
        same = _same_json(previous, content)
        s3_key = save_asset_to_s3(session_id=session_id, asset_type=asset_type, file_name=file_name, content=content)
        try:
            stream_asset(asset_type, file_name, content, operation_id=doc_id,
                         is_complete=True, force_full=True, s3_key=s3_key)
        except Exception as exc:
            logger.debug("[repair] %s re-stream skipped: %s", asset_type, exc)
        return "unchanged" if same else "updated"

    # Data Requests are deterministic too (spec → webhook/request/response
    # schema, envelope included); rebuilding them realigns D9-3 with the
    # re-projected OpenAPI without touching any LLM-authored flow.
    data_requests_status: dict[str, str] = {}
    try:
        from tools.acxd_data_request_builder import build_all_data_requests
        from tools.acxd_generation_context import get_acxd_spec
        acxd_spec = get_acxd_spec().model_dump()
        data_requests, dr_problems = build_all_data_requests(acxd_spec)
        problems.extend(dr_problems)
        for document in data_requests:
            data_requests_status[document["dataRequestId"]] = _write(
                "acxd_data_request", document["dataRequestId"], document)
    except Exception as exc:
        problems.append(f"data requests not rebuilt: {exc}")

    slot_types_status: dict[str, str] = {}
    for document in documents:
        if not document.get("values"):
            # An ACXD slot type without values recognises nothing; refusing to
            # ship a placeholder keeps the gap visible for the interview/plan.
            problems.append(f"slot type {document['slotTypeId']!r}: the plan gives no values/examples "
                            "and the FieldSpec has no enum_values — add them to the flow plan "
                            "(upsert_acxd_flow_plan) and rebuild")
            continue
        slot_types_status[document["slotTypeId"]] = _write("acxd_slot_type", document["slotTypeId"], document)

    # What the gate sees AFTER the rebuild is the only honest status.
    remaining = _remaining_d9(session_id, ("D9-1", "D9-3", "D9-4"))
    changed = [k for k, v in {**slot_types_status, **data_requests_status}.items() if v == "updated"]
    status = "blocked" if remaining else ("updated" if changed else "unchanged")
    return {
        "status": status,
        "slot_types": slot_types_status,
        "data_requests": data_requests_status,
        "problems": problems,
        "remaining_findings": remaining,
        "summary": (
            f"{len(changed)} file(s) changed, "
            f"{len(slot_types_status) + len(data_requests_status) - len(changed)} already matched the spec. "
            + (f"{len(remaining)} D9 finding(s) still open — see remaining_findings. A regex/enum mismatch "
               "means the SPEC and the plan disagree (fix the spec or the plan, then rebuild); a duplicate id "
               "means two copies of one asset exist (remove_duplicate_asset_copies_tool)."
               if remaining else "D9-1/3/4 are clear.")
        ),
    }


def _same_json(previous: Optional[str], content: str) -> bool:
    if not previous:
        return False
    try:
        return json.loads(previous) == json.loads(content)
    except (TypeError, ValueError):
        return previous == content


@tool
def remove_duplicate_asset_copies_tool() -> dict:
    """Remove byte-identical duplicate copies of asset files (deterministic).

    Use when the review's blocking findings include D9-1 DUP_SLOT_TYPE_ID /
    DUP_DATA_REQUEST_ID (or a bundle validation says the same id exists twice)
    and the workspace shows `<type>/<id>/<id>.json` next to `<type>/<id>.json`.
    Only a nested copy whose content equals the canonical flat file is deleted
    (NFS and the S3 mirror); a copy that differs is reported, never removed.
    """
    from tools.s3_asset_storage import (
        _nfs_asset_path, _nfs_available, delete_asset_from_s3, get_asset_from_s3, list_session_assets,
    )

    session_id = _session_id()
    keys = [str(k) for k in (list_session_assets(session_id) or [])]
    flat: dict[tuple[str, str], str] = {}
    nested: list[tuple[str, str, str, str]] = []   # (asset_type, op, file, key)
    for key in keys:
        parts = [p for p in key.split("/") if p]
        # assets/<session>/<type>/[<op>/]<file> — ACXD types only: their canonical
        # file is the flat one. Classic assets (lambda/<op>/handler.py,
        # openapi/<op>/openapi.yaml) are canonical INSIDE the operation folder.
        if len(parts) >= 4 and not parts[2].startswith("acxd_"):
            continue
        if len(parts) == 4:
            flat[(parts[2], parts[3])] = key
        elif len(parts) == 5:
            nested.append((parts[2], parts[3], parts[4], key))

    removed: list[str] = []
    kept: list[str] = []
    for asset_type, op_id, file_name, key in nested:
        canonical_key = flat.get((asset_type, file_name))
        if not canonical_key:
            continue
        try:
            canonical = get_asset_from_s3(canonical_key)
            copy = get_asset_from_s3(key)
        except Exception as exc:
            kept.append(f"{key}: unreadable ({exc})")
            continue
        if not canonical or not copy or not (copy == canonical or _same_json(canonical, copy)):
            kept.append(f"{key}: differs from {canonical_key} — not removed")
            continue
        if _nfs_available():
            path = _nfs_asset_path(session_id, asset_type, file_name, op_id)
            try:
                if path.exists():
                    path.unlink()
                if path.parent.exists() and not any(path.parent.iterdir()):
                    path.parent.rmdir()
            except OSError as exc:
                kept.append(f"{key}: NFS delete failed ({exc})")
                continue
        delete_asset_from_s3(key)
        removed.append(key)
        logger.info("[repair] removed identical duplicate copy %s (canonical %s)", key, canonical_key)

    return {
        "status": "updated" if removed else "unchanged",
        "removed": removed,
        "kept": kept,
        "summary": f"removed {len(removed)} identical duplicate cop(y/ies); "
                   f"{len(kept)} nested file(s) left in place (differ or unreadable). "
                   + ("Re-run the review — the duplicate-id findings should be gone." if removed else ""),
    }


def _remaining_d9(session_id: str, prefixes: tuple[str, ...]) -> list[str]:
    """Re-run the blocking gates and return the findings whose id starts with
    one of `prefixes`, as one-line strings."""
    try:
        from tools.review_gates import collect_blocking_findings
        report = collect_blocking_findings(session_id)
        return [f"{f['id']}: {f['message']}" for f in report.get("findings", [])
                if str(f.get("id", "")).startswith(prefixes)]
    except Exception as exc:  # pragma: no cover - the gate failing must not hide the rebuild
        return [f"gate re-check failed: {exc}"]
