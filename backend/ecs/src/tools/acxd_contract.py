"""Single source of truth for the ACXD service contract.

Loads ``schemas/acxd/contract.json``, which is generated from the official
SDK's TypeScript model by ``scripts/extract_acxd_contract.py``. Everything that
needs to know what the service accepts — the flow generator, the validator, the
system prompts — reads it from here, so the SDK and this codebase cannot drift
apart quietly.

Before this existed the constants were hand-written from live API probing, and
they were wrong in ways that mattered: 12 of 21 node types, and ``eq`` as the
only condition operator, which turned "refund over $500 needs approval" into
"refund equals $500".
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "schemas" / "acxd" / "contract.json"


@lru_cache(maxsize=1)
def load_contract() -> dict:
    """Read the generated contract. Cached — it is immutable at runtime."""
    try:
        return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - deploy issue
        logger.error("[ACXDContract] cannot read %s: %s", CONTRACT_PATH, exc)
        return {"enums": {}, "implicitEdges": {}, "unaryOperators": [],
                "terminalNodeTypes": ["end"]}


def enum(name: str) -> frozenset[str]:
    """Members of an SDK enum, e.g. ``enum("FlowNodeType")``."""
    return frozenset(load_contract().get("enums", {}).get(name, ()))


# --- flow authoring ---------------------------------------------------------

#: All 21 node types the service knows.
NODE_TYPES = enum("FlowNodeType")

#: All 14 comparison operators.
CONDITION_OPERATORS = enum("ConditionOperator")

#: Operand `type` values, including service-observed extras absent from the SDK.
OPERAND_TYPES = enum("OperandType")

#: Operators that must NOT carry a right-hand operand.
UNARY_OPERATORS = frozenset(load_contract().get("unaryOperators", ("exists", "not_exists")))

#: Node types with no outgoing edge.
TERMINAL_NODE_TYPES = frozenset(load_contract().get("terminalNodeTypes", ("end",)))

#: Edge sets the service materializes itself; both halves need targets or live
#: conversations leak to the application Fallback flow.
IMPLICIT_EDGES: dict[str, list[dict]] = load_contract().get("implicitEdges", {})

#: `left.type` values that identify a node's own typed edge rather than a data
#: comparison — these conditions must be preserved verbatim.
TYPED_EDGE_LEFT_TYPES = frozenset(
    cond["left"]["type"]
    for edges in IMPLICIT_EDGES.values()
    for edge in edges
    for cond in edge.get("conditions", ())
)

#: Full locales the service accepts (87 in SDK 0.1.0).
LANGUAGE_CODES = enum("LanguageCode")

MESSAGE_TYPES = enum("MessageType")
DEPLOYMENT_ENVIRONMENTS = enum("DeploymentEnvironment")

#: Common shorthands the model emits for operators, mapped onto real ones.
OPERATOR_ALIASES = {
    "equals": "eq", "equal": "eq", "==": "eq", "=": "eq", "is": "eq",
    "not_equals": "neq", "notequals": "neq", "!=": "neq", "ne": "neq",
    "greater_than": "gt", ">": "gt", "greater_than_or_equal": "gte", ">=": "gte",
    "less_than": "lt", "<": "lt", "less_than_or_equal": "lte", "<=": "lte",
    "is_set": "exists", "isset": "exists", "present": "exists",
    "is_not_set": "not_exists", "isnotset": "not_exists", "absent": "not_exists",
    "regex": "matches_regex", "matches": "matches_regex",
    "starts_with": "prefix", "startswith": "prefix",
    "ends_with": "suffix", "endswith": "suffix",
    "includes": "contains", "not_includes": "not_contains",
}


#: Default region per language, for when the model supplies a bare code. Chosen
#: by prevalence in contact centers, not alphabetically — resolving "en" to
#: "en-AE" (the first match) instead of "en-US" would be a silent downgrade.
PREFERRED_LOCALE = {
    "en": "en-US", "ko": "ko-KR", "ja": "ja-JP", "zh": "zh-CN",
    "pt": "pt-BR", "es": "es-US", "fr": "fr-FR", "de": "de-DE",
    "it": "it-IT", "ar": "ar-AE", "hi": "hi-IN", "nl": "nl-NL",
    "sv": "sv-SE", "id": "id-ID", "th": "th-TH", "vi": "vi-VN",
}


def canonical_operator(raw: str | None, has_right_operand: bool) -> str:
    """Map a model-supplied operator onto one the service accepts."""
    candidate = str(raw or "").strip()
    candidate = OPERATOR_ALIASES.get(candidate.lower(), candidate)
    if candidate in CONDITION_OPERATORS:
        return candidate
    return "eq" if has_right_operand else "exists"


def canonical_language(code: str | None) -> str:
    """Normalize to a locale the service accepts ('ko' -> 'ko-KR').

    Bare language codes are rejected live with "mainLanguageCode is not a
    supported value", so resolve them against the SDK's LanguageCode list
    rather than a hand-kept table.
    """
    raw = str(code or "").strip().replace("_", "-")
    if not raw:
        return raw
    if raw in LANGUAGE_CODES:
        return raw
    lang, _, region = raw.partition("-")
    if region:
        qualified = f"{lang.lower()}-{region.upper()}"
        if qualified in LANGUAGE_CODES:
            return qualified
    preferred = PREFERRED_LOCALE.get(lang.lower())
    if preferred and preferred in LANGUAGE_CODES:
        return preferred
    prefix = f"{lang.lower()}-"
    matches = sorted(c for c in LANGUAGE_CODES if c.lower().startswith(prefix))
    return matches[0] if matches else raw
