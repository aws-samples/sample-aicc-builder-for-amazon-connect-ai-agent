"""ACXD asset schema validation (knowledge pack runtime).

Validates LLM-generated ACXD asset documents against the contract JSON
Schemas in ``src/schemas/acxd/``. This is the *structural* half of the
double safety net (the deploy-time ``CreateApplicationBuild`` server-side
validation is the other half). Cross-asset consistency (references between
flows, slot types, data requests, guardrails, applications) lives in
``validate_acxd_consistency`` — not here.

Design notes:
- No strands / agent imports: tests and CLI load this module standalone.
- Schemas are data (JSON files), so they can also be embedded in sub-agent
  system prompts and reused by the Node.js deploy runner.
- ``strict_subset`` (default on) additionally restricts flow node types to
  the AICC-Builder-supported subset (decision D9): node ``metadata`` shapes
  for the remaining types are not publicly documented, so we refuse to
  generate them rather than guess.

CLI:
    python -m tools.validate_acxd_flow <file.json> [--kind flow]
        [--no-strict-subset]
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path
import logging
from typing import Any

import jsonschema
from referencing import Registry, Resource

# The contract lives beside this module. Tests load these files directly with
# importlib (no package context), so resolve by path rather than by name.
try:
    from tools.acxd_contract import NODE_TYPES
except ImportError:  # pragma: no cover - depends on how the module was loaded
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _spec = _ilu.spec_from_file_location(
        "_acxd_contract", _Path(__file__).resolve().parent / "acxd_contract.py")
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    NODE_TYPES = _mod.NODE_TYPES

try:
    from tools.acxd_runtime_contract import runtime_contract_violations
except ImportError:  # pragma: no cover - depends on how the module was loaded
    import importlib.util as _ilu2
    from pathlib import Path as _Path2

    _spec2 = _ilu2.spec_from_file_location(
        "_acxd_runtime_contract",
        _Path2(__file__).resolve().parent / "acxd_runtime_contract.py")
    _mod2 = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)
    runtime_contract_violations = _mod2.runtime_contract_violations

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "acxd"

#: kind -> schema file
SCHEMA_FILES = {
    "flow": "flow.schema.json",
    "slot_type": "slot_type.schema.json",
    "data_request": "data_request.schema.json",
    "guardrail": "guardrail.schema.json",
    "application": "application.schema.json",
    "knowledge_base": "knowledge_base.schema.json",
    "kb_article": "kb_article.schema.json",
    "context_variable": "context_variable.schema.json",
}

#: Every node type the service accepts — read from the SDK-derived contract
#: rather than transcribed by hand. The transcription was wrong in both
#: directions: it invented `application_handoff` (rejected by the API) and
#: omitted eight real types.
ALL_NODE_TYPES = NODE_TYPES

#: The subset AICC Builder generates and can validate end-to-end (decision D9).
#: Widened from the original 12 once the real contract was available.
#: `generative_journey` is included because it is the node that makes the agent
#: agentic — an LLM loop with tool access to knowledge bases, data requests and
#: flows — and leaving it out meant shipping scripted decision trees from a
#: product called Agentic CX Designer. `multimodal` stays out: it needs channel
#: and modality configuration this interview never gathers.
SUPPORTED_NODE_TYPES = frozenset({
    "start", "end", "basic", "user_input", "user_choice", "choice", "split",
    "data_request", "knowledge_base", "generative_text", "generative_task",
    "escalate", "redirect", "wait", "note", "define", "transform",
    "intent_capture", "loop", "generative_journey",
}) & ALL_NODE_TYPES

#: Classification used by the interview / flow generator to explain and
#: enforce determinism decisions (decision D6). "Generative" means an LLM
#: decides the wording or the next step at runtime, so the customer is told
#: what is fixed and what is not.
DETERMINISTIC_NODE_TYPES = frozenset({
    "start", "end", "basic", "user_input", "user_choice", "choice", "split",
    "data_request", "escalate", "redirect", "wait", "note", "define",
    "transform", "loop", "multimodal",
}) & ALL_NODE_TYPES
GENERATIVE_NODE_TYPES = frozenset({
    "knowledge_base", "generative_text", "generative_task",
    "generative_journey", "intent_capture",
}) & ALL_NODE_TYPES


class UnknownAssetKind(ValueError):
    """Raised when an unknown asset kind is requested."""


@lru_cache(maxsize=None)
def _registry() -> Registry:
    """Registry with every knowledge-pack schema, for cross-file $refs."""
    resources = []
    for filename in SCHEMA_FILES.values():
        contents = json.loads((SCHEMA_DIR / filename).read_text())
        resource = Resource.from_contents(contents)
        # Register under both the declared $id and the bare filename so
        # relative refs like "kb_article.schema.json" resolve.
        resources.append((contents["$id"], resource))
        resources.append((filename, resource))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def load_schema(kind: str) -> dict:
    """Load the contract schema for an asset kind (cached)."""
    if kind not in SCHEMA_FILES:
        raise UnknownAssetKind(
            f"unknown asset kind {kind!r}; expected one of "
            f"{sorted(SCHEMA_FILES)}"
        )
    return json.loads((SCHEMA_DIR / SCHEMA_FILES[kind]).read_text())


def _format_error(error: jsonschema.ValidationError) -> str:
    path = "$" + "".join(
        f"[{p!r}]" if isinstance(p, str) else f"[{p}]"
        for p in error.absolute_path
    )
    return f"{path}: {error.message}"


logger = logging.getLogger(__name__)


def prune_to_schema(document: Any, kind: str = "flow") -> Any:
    """Drop properties the schema forbids, following `$ref`s and `$defs`.

    `additionalProperties: false` appears throughout the flow schema, and the
    model reliably decorates nodes with plausible-sounding extras. Measured over
    a live window, "Additional properties are not allowed" was the second most
    common generation failure (55 occurrences), and because the offending key
    carries no contract meaning there is nothing to preserve by failing — the
    only outcome was burning retry attempts.

    Only prunes where the schema is explicit; anything permissive is left alone.
    """
    schema = load_schema(kind)
    defs = schema.get("$defs", {})

    def resolve(node_schema: dict) -> dict:
        seen = 0
        while isinstance(node_schema, dict) and "$ref" in node_schema and seen < 10:
            ref = node_schema["$ref"]
            if not ref.startswith("#/$defs/"):
                return {}
            node_schema = defs.get(ref.split("/")[-1], {})
            seen += 1
        return node_schema if isinstance(node_schema, dict) else {}

    def collect(node_schema: dict) -> tuple[dict, Any]:
        """Allowed properties and the additionalProperties rule for a schema.

        The node schema is `properties` + `additionalProperties: false` + an
        `allOf` of if/then conditionals. Bailing out on any composition keyword
        meant never descending into nodes at all — which is exactly where the
        model puts its extra keys. Branches can only ADD allowed properties, so
        the union across them is the safe allow-list: a key allowed by no branch
        is invalid under every reading of the schema.
        """
        props: dict = dict(node_schema.get("properties") or {})
        extra = node_schema.get("additionalProperties", True)

        def absorb(sub: Any) -> None:
            sub = resolve(sub) if isinstance(sub, dict) else {}
            if not isinstance(sub, dict):
                return
            for key, value in (sub.get("properties") or {}).items():
                props.setdefault(key, value)
            for branch_key in ("then", "else"):
                if isinstance(sub.get(branch_key), dict):
                    absorb(sub[branch_key])

        for keyword in ("allOf", "anyOf", "oneOf"):
            for branch in node_schema.get(keyword) or []:
                absorb(branch)
        return props, extra

    def walk(value: Any, node_schema: Any) -> Any:
        node_schema = resolve(node_schema if isinstance(node_schema, dict) else {})
        if not node_schema:
            return value

        if isinstance(value, list):
            item_schema = node_schema.get("items")
            if item_schema is None:
                return value
            return [walk(v, item_schema) for v in value]

        if not isinstance(value, dict):
            return value

        props, extra_allowed = collect(node_schema)
        additional_schema = extra_allowed if isinstance(extra_allowed, dict) else None
        # A map-style schema (`nodes`) has no `properties` — every value is
        # described by `additionalProperties`. Returning early here skipped the
        # node bodies entirely, which is where the extra keys actually are.
        if not props and additional_schema is None and extra_allowed is not False:
            return value

        out = {}
        for key, val in value.items():
            if key in props:
                out[key] = walk(val, props[key])
            elif additional_schema is not None:
                out[key] = walk(val, additional_schema)
            elif extra_allowed is False:
                logger.info("[ACXDPrune] dropped unsupported property %r", key)
                continue
            else:
                out[key] = val
        return out

    return walk(document, schema)


def validate_acxd_asset(
    kind: str,
    document: Any,
    *,
    strict_subset: bool = True,
    runtime_contract: bool = True,
) -> list[str]:
    """Validate a document against its contract schema.

    Returns a list of human-readable violation strings (empty = valid).
    For flows, ``strict_subset=True`` (default) also rejects node types
    outside :data:`SUPPORTED_NODE_TYPES`, and ``runtime_contract=True``
    (default) adds the flow-scope half of the live-verified runtime contract
    (:mod:`tools.acxd_runtime_contract`): the encodings the service accepts,
    builds and then cannot execute. Only findings the normalizer refuses to
    guess at are reported, so a flow through ``apply_runtime_contract`` gates
    clean; the cross-asset half (S5/D3/M1/RX) runs in
    ``validate_acxd_consistency``, which has the rest of the bundle.
    """
    schema = load_schema(kind)
    validator = jsonschema.Draft202012Validator(schema, registry=_registry())
    errors = [
        _format_error(e)
        for e in sorted(validator.iter_errors(document), key=str)
    ]

    if kind == "flow" and strict_subset and isinstance(document, dict):
        nodes = document.get("nodes")
        if isinstance(nodes, dict):
            for node_id, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                node_type = node.get("type")
                if node_type in ALL_NODE_TYPES and node_type not in SUPPORTED_NODE_TYPES:
                    errors.append(
                        f"$['nodes'][{node_id!r}]['type']: node type "
                        f"{node_type!r} is valid ACXD but outside the "
                        f"AICC-Builder-supported subset (D9); use one of "
                        f"{sorted(SUPPORTED_NODE_TYPES)} or document a "
                        f"manual step instead"
                    )

    if kind == "flow" and runtime_contract and isinstance(document, dict):
        errors += [
            f"runtime contract: {problem}"
            for problem in runtime_contract_violations(document, scope="flow")
        ]
    return errors


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_acxd_flow",
        description="Validate an ACXD asset JSON document against the "
        "AICC Builder contract schemas.",
    )
    parser.add_argument("file", help="path to the JSON document")
    parser.add_argument(
        "--kind",
        default="flow",
        choices=sorted(SCHEMA_FILES),
        help="asset kind (default: flow)",
    )
    parser.add_argument(
        "--no-strict-subset",
        action="store_true",
        help="accept all 22 ACXD node types instead of the supported subset",
    )
    parser.add_argument(
        "--no-runtime-contract",
        action="store_true",
        help="skip the flow-scope runtime contract checks (schema only)",
    )
    args = parser.parse_args(argv)

    try:
        document = json.loads(Path(args.file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    errors = validate_acxd_asset(
        args.kind, document, strict_subset=not args.no_strict_subset,
        runtime_contract=not args.no_runtime_contract,
    )
    if errors:
        print(f"INVALID ({args.kind}): {len(errors)} violation(s)")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"VALID ({args.kind}): {args.file}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
