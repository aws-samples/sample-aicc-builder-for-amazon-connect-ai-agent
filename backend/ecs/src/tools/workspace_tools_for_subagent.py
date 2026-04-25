"""
Workspace Tools Factory for Sub-Agents (Modification Mode)

Creates session-bound workspace tools that sub-agents can use to directly
modify files via patch/write operations. The session_id and workspace path
are captured in closures so the LLM never needs to know them.

Streaming (file preview + diff preview) is built into the underlying
workspace_file_tools and requires no additional wiring.
"""

import logging
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)


def create_modification_tools(
    session_id: str,
    asset_type: str,
    file_name: str,
    operation_id: str | None = None,
) -> tuple[list, str]:
    """
    Create workspace tools bound to a specific session and asset file.

    Returns:
        (tools_list, ws_path) - list of @tool functions and the workspace-relative path.
        Returns ([], "") if setup fails (caller should fall back to legacy mode).
    """
    try:
        from .workspace_file_tools import (
            get_asset_workspace_path,
            read_workspace_file as _read,
            patch_workspace_file as _patch,
            write_workspace_file as _write,
            grep_workspace as _grep,
        )
    except ImportError as e:
        logger.warning(f"[workspace_tools_factory] import failed: {e}")
        return [], ""

    ws_path = get_asset_workspace_path(
        session_id, asset_type, file_name, operation_id=operation_id,
    )

    # ── Closure-bound tools ──────────────────────────────────────────

    @tool
    def read_current_file() -> dict[str, Any]:
        """Read the current file being modified. Call this first to see existing content."""
        return _read(session_id=session_id, path=ws_path)

    @tool
    def patch_file(search: str, replace: str) -> dict[str, Any]:
        """
        Find and replace text in the current file. Replaces ALL occurrences.

        Args:
            search: Exact text to find (literal match, NOT regex). Include 3-5 lines of surrounding context for uniqueness.
            replace: Text to replace with.

        Returns:
            Dict with success, replacements_made, and summary.
        """
        return _patch(session_id=session_id, path=ws_path, search=search, replace=replace)

    @tool
    def write_file(content: str) -> dict[str, Any]:
        """
        Overwrite the entire file with new content. Use ONLY when patch_file is
        insufficient (e.g. major restructuring). Prefer patch_file for targeted changes.

        Args:
            content: Complete new file content.
        """
        return _write(session_id=session_id, path=ws_path, content=content)

    @tool
    def search_workspace_files(pattern: str, path: str = "") -> dict[str, Any]:
        """
        Search for text across workspace files. Useful for finding references
        or understanding related code before making changes.

        Args:
            pattern: Text or regex pattern to search for.
            path: Relative directory to search in (empty = session root).
        """
        return _grep(session_id=session_id, pattern=pattern, path=path)

    tools = [read_current_file, patch_file, write_file, search_workspace_files]
    logger.info(
        f"[workspace_tools_factory] Created {len(tools)} tools for "
        f"session={session_id}, path={ws_path}"
    )
    return tools, ws_path


# ── Prompt template for modification mode with workspace tools ───────

WORKSPACE_TOOLS_MODIFICATION_PROMPT = """
## MODIFICATION MODE (WORKSPACE TOOLS)

You have workspace tools to modify the existing file directly.

**Modification request**: {modification_request}

### Step 0 — Spec-level classification (MANDATORY, do this FIRST)

Before touching any file, classify the request:

**Spec-level** = changes a domain rule that must survive regeneration:
  data model (field add/remove/rename), operating hours, slot granularity,
  retention policy, recording on/off, session greeting content, persona,
  identifier scheme (e.g. phone vs session-id based lookup).

**Asset-level** = wording or presentation of a single file, unrelated to other
  assets or to regeneration (a sentence reworded, order of lines, error
  message phrasing, TTS-friendly adjustments).

If the request is **spec-level**:
  DO NOT call read_current_file, patch_file, or write_file.
  Instead, return the following JSON as your final message and stop:

      {{"success": false,
       "escalation": "spec_level",
       "reason": "<which spec field/rule needs to change and why this file
                 alone cannot own the change>",
       "suggested_spec_updates": ["<optional: hints like update_operation_spec
                                   field X or save_session_flow_config
                                   recording_enabled=False>"]}}

  The orchestrator will update the spec, analyze downstream impact, confirm
  with the user, and re-call you with a refined request.

If the request is **asset-level**, proceed to Step 1.

### Step 1 — Read → patch → (write)

1. Call `read_current_file()` to see the current content.
2. Identify sections to change.
3. Call `patch_file(search="exact text", replace="new text")` for each
   targeted change. `search` must be an EXACT substring of the file — include
   3-5 lines of context so it is unique.
4. Only use `write_file(content)` if `patch_file` is insufficient (major
   restructuring).

### Rules
- ALWAYS prefer `patch_file` over `write_file`.
- Do NOT output code blocks — use tools instead.
- Each `patch_file` call is atomic and streams a diff to the frontend.
- If `patch_file` fails (search text not found), adjust the search and retry.
- Do NOT silently skip the request. If you cannot satisfy it with a patch and
  it is not spec-level, report the reason in your final message.
"""


# ── Escalation detector ─────────────────────────────────────────────

import json
import re

_ESCALATION_PATTERN = re.compile(
    r'\{[^{}]*"escalation"\s*:\s*"spec_level"[^{}]*\}',
    re.DOTALL,
)


def detect_spec_escalation(text: str) -> dict | None:
    """Return the parsed escalation dict if `text` contains the spec_level
    escalation JSON object defined in WORKSPACE_TOOLS_MODIFICATION_PROMPT,
    else None.

    Tolerates trailing/leading prose around the JSON, since a sub-agent may
    emit the object inside a broader message.
    """
    if not text or "spec_level" not in text:
        return None

    # Fast path: try to parse the whole thing as JSON
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and obj.get("escalation") == "spec_level":
            return obj
    except Exception:
        pass

    # Fallback: find the first JSON-looking substring with escalation=spec_level
    for match in _ESCALATION_PATTERN.finditer(text):
        candidate = match.group(0)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and obj.get("escalation") == "spec_level":
                return obj
        except Exception:
            continue
    return None
