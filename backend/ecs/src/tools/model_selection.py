"""
Model selection — central resolver for the Bedrock Claude model used by every agent.

Users can pick among the top Bedrock Claude models from the frontend. The choice
flows through a ContextVar (per-request) and is persisted to NFS (per-session), so
the orchestrator and all sub-agents construct their ``BedrockModel`` against the
same id.

API quirk this module exists to handle:
  * Opus 4.6 **accepts** ``temperature``.
  * Opus 4.7 and 4.8 **removed** ``temperature`` — sending it returns HTTP 400.

So model construction must conditionally include ``temperature``. ``build_model_kwargs``
centralizes that branch: every construction site routes through it, passing whatever
temperature it wants, and the kwarg is dropped automatically for the models that
reject it.

Kept dependency-light to avoid circular imports — ``session_context`` is imported
lazily inside ``resolve_model_id``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Allowlisted model ids the frontend may request. These are the EXACT Bedrock
# inference-profile ids verified ACTIVE in ap-northeast-2 (the project's default
# region) — note 4.6 carries a `-v1` suffix while 4.7/4.8 do not. Using the wrong
# form 404s at invoke time.
ALLOWED_MODEL_IDS = {
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-7",
    "global.anthropic.claude-opus-4-6-v1",
}

# Default when nothing is selected / persisted / configured.
DEFAULT_MODEL_ID = "global.anthropic.claude-opus-4-8"

# Models that REMOVED the `temperature` parameter (sending it → HTTP 400).
# Opus 4.6 still ACCEPTS temperature; 4.7 and 4.8 removed it.
MODELS_WITHOUT_TEMPERATURE = {
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-7",
}

# Valid values for the Anthropic `effort` control (output_config.effort).
# Stable API for Claude 4.6+ — no beta header. "high" equals omitting the
# parameter; lower levels trade capability for speed/cost. "max" spends even
# more tokens than high (hardest reasoning problems).
ALLOWED_EFFORT_LEVELS = {"low", "medium", "high", "max"}


def validate_effort(effort: Optional[str]) -> Optional[str]:
    """Return *effort* lower-cased if it is a valid level, else None."""
    if isinstance(effort, str) and effort.lower() in ALLOWED_EFFORT_LEVELS:
        return effort.lower()
    return None


def resolve_effort() -> Optional[str]:
    """Resolve the effective effort level (None = model default / omit).

    Precedence:
      1. ``current_selected_effort`` ContextVar (user's pick for this request)
      2. ``os.environ["BEDROCK_EFFORT"]`` (deployment default)
      3. None — parameter omitted entirely (model default behavior).
    """
    try:
        from tools.session_context import current_selected_effort
        ctx_val = validate_effort(current_selected_effort.get())
        if ctx_val:
            return ctx_val
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[model_selection] effort ContextVar lookup failed: {e}")
    return validate_effort(os.environ.get("BEDROCK_EFFORT"))


def validate_model_id(model_id: Optional[str]) -> Optional[str]:
    """Return *model_id* if it is in the allowlist, else None."""
    if model_id and model_id in ALLOWED_MODEL_IDS:
        return model_id
    return None


def supports_temperature(model_id: str) -> bool:
    """True if *model_id* accepts the ``temperature`` parameter (Opus 4.6 and older)."""
    return model_id not in MODELS_WITHOUT_TEMPERATURE


def resolve_model_id(override_env: Optional[str] = None) -> str:
    """Resolve the effective model id.

    Precedence:
      1. ``os.environ[override_env]`` if *override_env* is given AND its value is valid
         (per-agent escape hatch, e.g. INFRA_MODEL_ID / FAQ_MODEL_ID).
      2. ``current_selected_model`` ContextVar value (if valid) — the user's pick for
         this request.
      3. ``os.environ["BEDROCK_MODEL_ID"]`` (if valid) — deployment default.
      4. ``DEFAULT_MODEL_ID``.
    """
    if override_env:
        env_val = validate_model_id(os.environ.get(override_env))
        if env_val:
            return env_val

    # Lazy import keeps this module free of import-time dependencies on
    # session_context (which several agents already import) and avoids any
    # circular-import surprises.
    try:
        from tools.session_context import current_selected_model
        ctx_val = validate_model_id(current_selected_model.get())
        if ctx_val:
            return ctx_val
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[model_selection] ContextVar lookup failed: {e}")

    env_default = validate_model_id(os.environ.get("BEDROCK_MODEL_ID"))
    if env_default:
        return env_default

    return DEFAULT_MODEL_ID


def build_model_kwargs(
    model_id: str,
    temperature: Optional[float] = None,
    **rest: Any,
) -> dict[str, Any]:
    """Build the kwargs dict for ``BedrockModel(...)``.

    Always includes ``model_id`` and everything in *rest*. Includes ``temperature``
    ONLY when a temperature was provided AND the model accepts it — this is the one
    place the 4.6-vs-4.7/4.8 branch lives.

    Also injects the user-selected Anthropic ``effort`` level (if any) via
    ``additional_request_fields.output_config.effort`` — merged non-destructively
    with any caller-provided ``additional_request_fields``.
    """
    kwargs: dict[str, Any] = {"model_id": model_id, **rest}
    if temperature is not None and supports_temperature(model_id):
        kwargs["temperature"] = temperature

    effort = resolve_effort()
    if effort:
        arf = dict(kwargs.get("additional_request_fields") or {})
        out_cfg = dict(arf.get("output_config") or {})
        out_cfg.setdefault("effort", effort)
        arf["output_config"] = out_cfg
        kwargs["additional_request_fields"] = arf
    return kwargs
