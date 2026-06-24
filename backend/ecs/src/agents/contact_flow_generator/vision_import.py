"""Whiteboard / sketch image → Contact Flow draft (vision import).

Lets a user import a hand-drawn or whiteboarded flow as a photo. A vision-capable
Claude model reads the image and emits a best-effort Amazon Connect Contact Flow
JSON, which the caller then runs through the same lint/repair + seed path as a
JSON import (so the deterministic linter still guarantees import-safety).

Kept separate from agent.py so it has no streaming/callback dependencies — it's a
single synchronous vision call that returns the raw JSON string.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from strands import Agent
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

from tools.model_selection import resolve_model_id, build_model_kwargs
from tools.asset_linters import VALID_CONTACT_FLOW_ACTION_TYPES

logger = logging.getLogger(__name__)

# A tight, single-purpose prompt. We do NOT reuse the full generator system prompt
# (it assumes specs + RAG tools); instead we give the vision model the valid block
# vocabulary and the exact output contract so its draft lints cleanly.
_VALID_TYPES_LIST = ", ".join(sorted(VALID_CONTACT_FLOW_ACTION_TYPES))

VISION_IMPORT_SYSTEM_PROMPT = f"""You are an Amazon Connect Contact Flow architect. The user has uploaded an IMAGE
of a contact flow — it may be a whiteboard sketch, a hand-drawn diagram, a
screenshot of another tool, or a photo of a flow chart.

Your job: read the diagram and produce a best-effort **Amazon Connect Contact Flow
JSON** that captures the logic shown (greeting, menus/DTMF, conditions, transfers,
disconnects, etc.).

OUTPUT CONTRACT — follow exactly:
- Output ONLY the JSON object. No prose, no markdown fences, no explanation.
- Top-level keys: "Version" (always "2019-10-30"), "StartAction", "Actions".
- Each action: "Identifier" (unique kebab-case string), "Type", "Parameters",
  "Transitions" ({{"NextAction": ..., "Errors": [...], "Conditions": [...]}}).
- Use ONLY these block Types (anything else is invalid):
  {_VALID_TYPES_LIST}
- Map common diagram intent to blocks: greeting/announcement → MessageParticipant;
  menu/press-a-digit → GetParticipantInput; if/branch/condition → Compare;
  business hours → CheckHoursOfOperation; "transfer to <queue>" → set the queue with
  UpdateContactTargetQueue then TransferContactToQueue (the transfer block takes NO
  queue parameter); hang up/end → DisconnectParticipant.
- For anything ambiguous in the drawing, choose a reasonable default and keep the
  flow structurally valid (every NextAction points to a real Identifier; include a
  terminal DisconnectParticipant). It's fine to be approximate — the user will
  refine it afterward.
- Use {{{{PLACEHOLDER}}}} tokens (e.g. {{{{FRONT_DESK_QUEUE_ARN}}}}, {{{{LAMBDA_ARN}}}})
  for any ARN/id the diagram references but doesn't provide.

Return the JSON now."""


def _extract_json(text: str) -> Optional[str]:
    """Pull the JSON object out of the model response (handles stray fences/prose)."""
    if not text:
        return None
    # Strip markdown fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        # Otherwise take from the first { to the last }.
        start, end = text.find("{"), text.rfind("}")
        candidate = text[start : end + 1] if start != -1 and end > start else text
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        return None


def draft_flow_from_image(
    image_bytes: bytes,
    image_format: str = "png",
    company_name: str = "",
    hint: str = "",
    model_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a vision model over a flow image and return a draft Contact Flow JSON.

    Returns: {"ok": bool, "flow_json": str|None, "error": str|None}
    The returned flow_json is a best-effort draft — the caller MUST still run it
    through lint_contact_flow for import-safety.
    """
    if not image_bytes:
        return {"ok": False, "flow_json": None, "error": "empty image"}
    fmt = (image_format or "png").lower()
    if fmt == "jpg":
        fmt = "jpeg"

    model_id = model_id or resolve_model_id()
    try:
        model = BedrockModel(**build_model_kwargs(
            model_id,
            temperature=0,  # deterministic transcription (dropped for 4.7/4.8)
            max_tokens=64000,
            boto_client_config=BotocoreConfig(read_timeout=300),
        ))
        agent = Agent(model=model, system_prompt=VISION_IMPORT_SYSTEM_PROMPT)
        ctx = []
        if company_name:
            ctx.append(f"Company: {company_name}.")
        if hint:
            ctx.append(f"User note: {hint}.")
        ctx.append("Transcribe this contact flow diagram into Amazon Connect Contact Flow JSON.")
        content = [
            {"image": {"source": {"bytes": image_bytes}, "format": fmt}},
            {"text": " ".join(ctx)},
        ]
        result = agent([{"role": "user", "content": content}])
        text = str(result)
        flow_json = _extract_json(text)
        if not flow_json:
            logger.warning("[vision_import] model did not return parseable JSON")
            return {"ok": False, "flow_json": None, "error": "vision model did not return valid JSON"}
        return {"ok": True, "flow_json": flow_json, "error": None}
    except Exception as e:
        logger.error(f"[vision_import] draft_flow_from_image failed: {e}")
        return {"ok": False, "flow_json": None, "error": str(e)}
