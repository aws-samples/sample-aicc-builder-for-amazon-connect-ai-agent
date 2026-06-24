"""
Orchestrator tools for conversationally importing an externally-created asset.

These replace the old auto-firing `importAsset` WS pipeline. The orchestrator
now decides *when* to import (after acknowledging the upload and, for images,
*asking* the user first); these tools do the deterministic *what*: lint/repair,
seed the file at the canonical workspace path, and flip the session into
modification (post_generation) mode so subsequent edits are patch-only.

Two tools:
  - import_uploaded_asset_tool(asset_type, content, name)  — for a flow JSON or
    prompt YAML the user pasted/attached (delivered inline as message text).
  - draft_flow_from_image_tool(image_format, company_name, hint) — transcribe a
    stashed flow-diagram image into a draft Contact Flow, then import it.

Both seed to assets/{type}/{operation_id}/{file} with a STABLE operation_id so the
generators' patch-only modification tools (create_modification_tools, keyed on
operation_id) target exactly the seeded file. The orchestrator MUST pass
flow_name/agent_name == the returned operation_id when it later edits the asset.

This is the relocated, conversational form of app.py:handle_import_asset_ws.
"""
import logging
from typing import Any, Dict

from strands import tool

logger = logging.getLogger(__name__)

# asset_type → (file name, completion tool name, stable operation id)
_IMPORT_MAP = {
    "contact_flow": ("contact_flow.json", "contact_flow_generator_agent", "imported_flow"),
    "prompt": ("ai_agent_prompt.yaml", "prompt_generator_agent", "imported_agent"),
}


def _seed_imported_asset(asset_type: str, content: str, name: str = "") -> Dict[str, Any]:
    """Lint → seed file → record completion → post_generation → stream to UI.

    Returns {ok, operation_id, file_name, asset_type, lint_summary, error}.
    Runs in the orchestrator's tool-call context, so current_session_id is set
    and stream_asset / workspace writes resolve to the right session.
    """
    from tools.session_context import current_session_id
    from tools.workspace_file_tools import write_workspace_file, get_asset_workspace_path
    from tools.streaming_callback import stream_asset
    from context.generation_progress import (
        record_tool_completion,
        set_generation_scope,
        mark_imported_session,
        update_phase,
    )

    session_id = current_session_id.get()
    if not session_id:
        return {"ok": False, "error": "no active session"}

    if asset_type not in _IMPORT_MAP:
        return {"ok": False, "error": f"unsupported asset type: {asset_type}"}
    if not content or not content.strip():
        return {"ok": False, "error": "imported asset content is empty"}

    file_name, tool_name, op_id = _IMPORT_MAP[asset_type]

    # 1. Validate + repair against the real Connect / qconnect import rules.
    lint_summary: Dict[str, Any] = {"ok": True, "errors": [], "warnings": [], "fixesApplied": 0}
    final_content = content
    try:
        if asset_type == "contact_flow":
            from tools.asset_linters import lint_contact_flow
            result = lint_contact_flow(content)
            if result.get("fixed_json"):
                final_content = result["fixed_json"]
            lint_summary = {
                "ok": result.get("ok", False),
                "errors": result.get("errors", []),
                "warnings": result.get("warnings", []),
                "fixesApplied": result.get("fixes_applied", []),
            }
        elif asset_type == "prompt":
            # qconnect CreateAIPrompt: each variable may appear inside {{ }} only once.
            from tools.asset_linters import lint_ai_prompt
            result = lint_ai_prompt(content)
            if result.get("fixed_text"):
                final_content = result["fixed_text"]
            lint_summary = {
                "ok": result.get("ok", True),
                "errors": result.get("errors", []),
                "warnings": result.get("warnings", []),
                "fixesApplied": result.get("fixes_applied", []),
            }
    except Exception as e:  # noqa: BLE001 — lint is best-effort, never block import
        logger.warning(f"[asset_import] lint failed (non-critical): {e}")

    # 2. Seed the file at the canonical workspace path (op_id is the patch target).
    ws_path = get_asset_workspace_path(session_id, asset_type, file_name, operation_id=op_id)
    try:
        write_result = write_workspace_file(session_id, ws_path, final_content)
        if not write_result.get("success", True):
            return {"ok": False, "error": f"failed to seed asset: {write_result.get('error')}"}
    except Exception as e:  # noqa: BLE001
        logger.error(f"[asset_import] write_workspace_file failed: {e}")
        return {"ok": False, "error": "failed to write imported asset to workspace"}

    # 3. Seed progress state + force post_generation (modification mode, no interview).
    try:
        record_tool_completion(session_id, tool_name, status="completed")
        set_generation_scope(session_id, [asset_type])
        mark_imported_session(session_id)
        update_phase(session_id, "post_generation", "imported_asset")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[asset_import] progress seed failed: {e}")

    # 4. Stream into the right-hand asset workspace (same as a generator on complete).
    try:
        stream_asset(
            asset_type,
            file_name,
            final_content,
            operation_id=op_id,
            is_complete=True,
            force_full=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[asset_import] stream_asset failed: {e}")

    logger.info(
        f"[asset_import] seeded {asset_type} op_id={op_id} "
        f"({len(final_content)} chars, fixes={lint_summary.get('fixesApplied')})"
    )
    return {
        "ok": True,
        "operation_id": op_id,
        "file_name": file_name,
        "asset_type": asset_type,
        "lint_summary": lint_summary,
    }


@tool
def import_uploaded_asset_tool(asset_type: str, content: str, name: str = "") -> dict:
    """Import an externally-provided Contact Flow JSON or AI Prompt YAML so it can be edited.

    Call this ONLY after acknowledging the user's upload in conversation. It
    validates/repairs the asset against the real Amazon Connect import rules,
    seeds it into the workspace, and puts the session into modification mode so
    your next edit is a patch (never a full regeneration).

    After this returns, edit the asset by calling the matching generator with
    flow_name / agent_name set to the returned operation_id and a
    modification_request — do NOT regenerate from scratch.

    Args:
        asset_type: "contact_flow" (JSON) or "prompt" (AI Prompt YAML).
        content: The raw file content (the JSON or YAML text the user provided).
        name: Optional original file name (for logging only).

    Returns:
        {ok, operation_id, file_name, asset_type, lint_summary} on success,
        or {ok: false, error} on failure.
    """
    return _seed_imported_asset(asset_type, content, name)


@tool
def draft_flow_from_image_tool(image_format: str = "png", company_name: str = "", hint: str = "") -> dict:
    """Transcribe the uploaded flow-diagram image into an Amazon Connect Contact Flow.

    Call this ONLY after the user has confirmed they want the attached image (a
    whiteboard sketch, draw.io export, screenshot, …) turned into a Contact Flow.
    A vision model reads the diagram into a draft flow, which is then validated/
    repaired and imported — after which the session is in modification mode and
    further edits are patches.

    The image bytes are taken from the current message (stashed server-side); you
    do not pass them. Acknowledge the upload and ASK before calling this.

    Args:
        image_format: png / jpeg / gif / webp (defaults to png).
        company_name: Optional company name to use in the greeting.
        hint: Optional hint about what the diagram represents.

    Returns:
        {ok, operation_id, file_name, asset_type, lint_summary} on success,
        or {ok: false, error} on failure (e.g. no image attached / unreadable).
    """
    from tools.session_context import current_session_id
    from tools.import_context import get_pending_import_image, clear_pending_import_image
    from agents.contact_flow_generator.vision_import import draft_flow_from_image

    session_id = current_session_id.get()
    if not session_id:
        return {"ok": False, "error": "no active session"}

    stashed = get_pending_import_image(session_id)
    if not stashed:
        return {"ok": False, "error": "no image is available — ask the user to re-attach the flow diagram"}
    image_bytes, stashed_fmt = stashed

    vres = draft_flow_from_image(
        image_bytes,
        image_format=(image_format or stashed_fmt or "png"),
        company_name=company_name,
        hint=hint,
    )
    if not vres.get("ok") or not vres.get("flow_json"):
        return {"ok": False, "error": f"couldn't read a flow from the image: {vres.get('error') or 'no flow detected'}"}

    result = _seed_imported_asset("contact_flow", vres["flow_json"], name="flow_from_image")
    # Consume the stash once we've successfully drafted+seeded from it.
    if result.get("ok"):
        clear_pending_import_image(session_id)
    return result
