"""Bedrock ConverseStream message sanitizer (shared by SafeBedrockModel).

Extracted from app.py so the rules are unit-testable without importing the
FastAPI application.
"""
import logging

logger = logging.getLogger(__name__)


def _coerce_content_block(block):
    """Coerce one content block into a shape Bedrock accepts.

    strands passes a tool's return value straight through as an
    already-formed ToolResult when that dict carries BOTH ``status`` and
    ``content`` (``strands/tools/decorator.py::_wrap_tool_result``), so a
    tool returning ``{"status": "ok", "content": "<file text>"}`` puts a bare
    STRING where the toolResult's content list belongs. strands then iterates
    that string CHARACTER by character and raises
    ``TypeError: content_type=<{> | unsupported type``, killing the event loop
    mid-turn. Wrap anything that is not a dict in ``{"text": ...}``.
    """
    if not isinstance(block, dict):
        return {"text": str(block)}

    tool_result = block.get("toolResult")
    if not isinstance(tool_result, dict):
        return block

    inner = tool_result.get("content")
    if inner is None:
        return block
    coerced = [
        item if isinstance(item, dict) else {"text": str(item)}
        for item in (inner if isinstance(inner, list) else [inner])
    ]
    if coerced == inner:
        return block
    logger.warning("[SafeBedrockModel] Coerced malformed toolResult content into %d text block(s)", len(coerced))
    return {**block, "toolResult": {**tool_result, "content": coerced}}


def normalize_content_blocks(messages: list) -> list:
    """Make every message's ``content`` a list of content-block dicts.

    Runs before the pairing rules below, which all assume dict blocks.
    """
    normalized = []
    for msg in messages:
        if not isinstance(msg, dict):
            logger.warning("[SafeBedrockModel] Dropping non-dict message")
            continue
        content = msg.get("content")
        if content is None:
            normalized.append(msg)
            continue
        blocks = [
            _coerce_content_block(b)
            for b in (content if isinstance(content, list) else [content])
        ]
        normalized.append(msg if blocks == content else {**msg, "content": blocks})
    return normalized


def fix_messages_for_bedrock(messages: list) -> list:
    """Ensure messages satisfy Bedrock ConverseStream constraints:
    1. First message must be role=user
    2. Strict user/assistant alternation
    3. Every toolResult has matching toolUse in preceding assistant
    4. toolResult count <= toolUse count

    This runs on EVERY Bedrock API call during agent execution, not just
    on the initial history load.  Critical for catching issues that arise
    mid-conversation as the SDK mutates agent.messages.
    """
    if not messages:
        return messages

    # 0. Coerce malformed content blocks (a bare string where a content list
    #    belongs), then filter out system-role messages (Bedrock only accepts
    #    user/assistant)
    messages = normalize_content_blocks(messages)
    fixed = [m for m in messages if m.get("role") in ("user", "assistant")]

    # 1. Drop leading non-user messages
    while fixed and fixed[0].get("role") != "user":
        logger.warning("[SafeBedrockModel] Dropping leading non-user message")
        fixed.pop(0)

    if not fixed:
        return fixed

    # 2. Enforce role alternation — merge consecutive same-role messages
    merged = [fixed[0]]
    for msg in fixed[1:]:
        if msg.get("role") == merged[-1].get("role"):
            # Merge content blocks
            prev_content = merged[-1].get("content", [])
            curr_content = msg.get("content", [])
            if isinstance(prev_content, list) and isinstance(curr_content, list):
                merged[-1] = {"role": msg["role"], "content": prev_content + curr_content}
            # else: skip malformed
        else:
            merged.append(msg)
    fixed = merged

    # 3. Fix toolResult/toolUse pairing
    for i in range(len(fixed)):
        msg = fixed[i]
        content = msg.get("content")
        if not isinstance(content, list) or msg.get("role") != "user":
            continue

        tool_result_ids = {
            b["toolResult"]["toolUseId"]
            for b in content
            if isinstance(b, dict) and "toolResult" in b and b["toolResult"].get("toolUseId")
        }
        if not tool_result_ids:
            continue

        # Collect toolUse IDs from preceding assistant
        preceding_tool_use_ids = set()
        if i > 0 and fixed[i - 1].get("role") == "assistant":
            prev_content = fixed[i - 1].get("content", [])
            if isinstance(prev_content, list):
                preceding_tool_use_ids = {
                    b["toolUse"]["toolUseId"]
                    for b in prev_content
                    if isinstance(b, dict) and "toolUse" in b and b["toolUse"].get("toolUseId")
                }

        excess = tool_result_ids - preceding_tool_use_ids
        if excess:
            logger.warning(f"[SafeBedrockModel] Removing {len(excess)} excess toolResult blocks at msg {i}")
            cleaned = [
                b for b in content
                if not (isinstance(b, dict) and "toolResult" in b and b["toolResult"].get("toolUseId") in excess)
            ]
            if cleaned:
                fixed[i] = {"role": "user", "content": cleaned}
            else:
                fixed[i] = {"role": "user", "content": [{"text": "(tool results removed)"}]}

    # 4. Remove trailing assistant toolUse without following toolResult
    if fixed and fixed[-1].get("role") == "assistant":
        last_content = fixed[-1].get("content", [])
        if isinstance(last_content, list):
            has_tool_use = any(isinstance(b, dict) and "toolUse" in b for b in last_content)
            if has_tool_use:
                cleaned = [b for b in last_content if not (isinstance(b, dict) and "toolUse" in b)]
                if cleaned:
                    fixed[-1] = {"role": "assistant", "content": cleaned}
                else:
                    fixed.pop()
                    logger.warning("[SafeBedrockModel] Removed trailing assistant with only toolUse blocks")

    # 5. The request must end with a user message. A trailing assistant
    #    message (e.g. the partial text of a turn cancelled by a restart)
    #    is treated as prefill, which Opus 4.8 rejects with
    #    "This model does not support assistant message prefill" — drop it
    #    and let the model regenerate the turn.
    if len(fixed) > 1 and fixed[-1].get("role") == "assistant":
        fixed.pop()
        logger.warning("[SafeBedrockModel] Dropped trailing assistant message (prefill not supported)")

    # 6. A model that writes a tool call as TEXT ("<invoke name=...>") still
    #    stops with reason tool_use; strands then appends a user message
    #    with EMPTY content (no tool ran), which Bedrock rejects with the
    #    same prefill error. Give the model an explicit correction instead.
    if fixed and fixed[-1].get("role") == "user":
        last_content = fixed[-1].get("content")
        if isinstance(last_content, list) and not any(
            isinstance(b, dict) and (b.get("text") or "toolResult" in b or "image" in b or "document" in b)
            for b in last_content
        ):
            fixed[-1] = {"role": "user", "content": [{"text": (
                "No tool was invoked: the previous message described a tool call as text "
                "instead of using the tool-use interface. Call the tool through the tool "
                "interface now, or answer in plain text."
            )}]}
            logger.warning("[SafeBedrockModel] Replaced empty trailing user message with tool-call correction")

    return fixed
