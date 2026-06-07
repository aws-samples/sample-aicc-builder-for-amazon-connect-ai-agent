"""
Deterministic asset linters: CloudFormation + OpenAPI 3.0.

Run automatically after the deterministic merge steps (merge_infrastructure /
merge_openapi) and also exposed as standalone @tool functions so the
orchestrator can re-lint after a manual patch.

Design principles
------------------
1. **Fault-tolerant.** Linting must NEVER crash the generation pipeline. If a
   lint library is missing, or the parser throws on malformed input, we log and
   return a benign result (``available=False`` / ``ok=True``) instead of raising.
2. **Deterministic auto-fix first.** A small set of unambiguous, well-understood
   issues (e.g. ``!Sub`` used where CloudFormation only accepts a literal string,
   ``!Sub`` with no variables) are fixed in-place by regex. These are the issues
   that recur every workshop and are safe to fix without an LLM.
3. **Surface the rest.** Remaining errors are returned as a structured list so
   the caller (orchestrator / sub-agent) can patch them — we do NOT silently
   swallow them.

The two public tools are :func:`lint_cloudformation` and :func:`lint_openapi`.
The two library helpers used by the merge functions are
:func:`lint_and_autofix_cfn` and :func:`lint_and_autofix_openapi`.
"""

from __future__ import annotations

import logging
import re

from strands import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CloudFormation
# ---------------------------------------------------------------------------

# Top-level (column-0) ``Description:`` only accepts a literal string — cfn-lint
# E1004. The infra agent occasionally emits ``Description: !Sub "..."`` here.
_TOP_LEVEL_DESC_SUB_RE = re.compile(
    r'^(Description:[ \t]+)!Sub[ \t]+(.+)$', re.MULTILINE
)

# ``!Sub`` with no ``${...}`` variables is redundant (cfn-lint W1020) and, in a
# handful of string-only fields, an outright error. Converting a single-line
# ``!Sub "literal"`` (no variables) back to a plain quoted string is always safe.
_SUB_NO_VAR_RE = re.compile(
    r'!Sub[ \t]+(?P<q>[\'"])(?P<body>(?:(?!(?P=q)).)*)(?P=q)'
)


def _autofix_cfn(yaml_str: str) -> tuple[str, list[str]]:
    """Apply deterministic, always-safe CloudFormation fixes.

    Returns (fixed_yaml, list_of_human_readable_fix_descriptions).
    """
    fixes: list[str] = []

    # 1. Strip !Sub from the template-level Description (E1004). The Description
    #    field is literal-only and cannot interpolate, so we also collapse any
    #    embedded ${...} placeholders to their bare token text to avoid the
    #    follow-on E1029 ("embedded parameter outside Fn::Sub").
    def _strip_top_desc(m: re.Match) -> str:
        literal = m.group(2)
        # Collapse ${Foo::Bar} or ${Foo} -> Foo::Bar / Foo (plain text).
        literal = re.sub(r'\$\{([^}]*)\}', r'\1', literal)
        return m.group(1) + literal

    new_yaml, n = _TOP_LEVEL_DESC_SUB_RE.subn(_strip_top_desc, yaml_str)
    if n:
        fixes.append(f"Removed !Sub from {n} top-level Description field(s) (E1004/E1029)")
        yaml_str = new_yaml

    # 2. Convert `!Sub "literal"` (no ${} variables) to a plain quoted string.
    def _strip_useless_sub(m: re.Match) -> str:
        body = m.group("body")
        if "${" in body:  # has a real variable — leave the !Sub intact
            return m.group(0)
        q = m.group("q")
        return f"{q}{body}{q}"

    new_yaml, n2 = _SUB_NO_VAR_RE.subn(_strip_useless_sub, yaml_str)
    # subn counts all matches incl. ones we left untouched; recount real changes
    if new_yaml != yaml_str:
        changed = sum(
            1 for m in _SUB_NO_VAR_RE.finditer(yaml_str) if "${" not in m.group("body")
        )
        fixes.append(f"Converted {changed} variable-free !Sub to literal string (W1020)")
        yaml_str = new_yaml

    return yaml_str, fixes


def _run_cfn_lint(yaml_str: str) -> tuple[bool, list[dict]]:
    """Run cfn-lint. Returns (available, issues).

    issues: list of {id, level, message, line} where level is 'error'|'warning'.
    Fault-tolerant: any failure -> (False, []).
    """
    try:
        import cfnlint.api as cfn_api
    except Exception as e:  # library missing
        logger.warning(f"[LINT_CFN] cfn-lint unavailable, skipping: {e}")
        return False, []

    try:
        matches = cfn_api.lint(yaml_str)
    except Exception as e:
        logger.warning(f"[LINT_CFN] cfn-lint raised on input, skipping: {e}")
        return True, []

    issues: list[dict] = []
    for m in matches:
        try:
            rid = getattr(getattr(m, "rule", None), "id", "?") or "?"
            # cfn-lint severity: error ids start with E, warnings W, info I
            level = "error" if str(rid).startswith("E") else (
                "warning" if str(rid).startswith("W") else "info"
            )
            issues.append({
                "id": str(rid),
                "level": level,
                "message": str(getattr(m, "message", m)),
                "line": getattr(m, "linenumber", None),
            })
        except Exception:
            continue
    return True, issues


def lint_and_autofix_cfn(yaml_str: str) -> dict:
    """Library entry point used by merge_infrastructure_fragments.

    Returns dict: {available, fixed_yaml, fixes_applied, errors, warnings}.
    Never raises.
    """
    try:
        fixed_yaml, fixes = _autofix_cfn(yaml_str)
        available, issues = _run_cfn_lint(fixed_yaml)
        errors = [i for i in issues if i["level"] == "error"]
        warnings = [i for i in issues if i["level"] == "warning"]
        if fixes:
            logger.info(f"[LINT_CFN] Auto-fixes applied: {fixes}")
        if errors:
            logger.warning(
                f"[LINT_CFN] {len(errors)} error(s) remain after autofix: "
                + "; ".join(f"{e['id']}@L{e['line']}: {e['message']}" for e in errors[:8])
            )
        return {
            "available": available,
            "fixed_yaml": fixed_yaml,
            "fixes_applied": fixes,
            "errors": errors,
            "warnings": warnings,
        }
    except Exception as e:
        logger.error(f"[LINT_CFN] Unexpected failure, passing through: {e}")
        return {"available": False, "fixed_yaml": yaml_str, "fixes_applied": [],
                "errors": [], "warnings": []}


# ---------------------------------------------------------------------------
# OpenAPI 3.0
# ---------------------------------------------------------------------------


def _autofix_openapi(doc: dict) -> tuple[dict, list[str]]:
    """Apply deterministic, always-safe OpenAPI fixes to a parsed doc.

    Targets the recurring workshop issues:
      - ``null`` where a string/object is required (drop the key)
      - missing ``responses`` on an operation (add a minimal 200)
      - ``type: "null"`` (invalid in OpenAPI 3.0) — drop the offending schema key
    Returns (fixed_doc, fix_descriptions).
    """
    fixes: list[str] = []

    def _scrub_nulls(node, path="$"):
        """Recursively drop dict keys whose value is literally None."""
        if isinstance(node, dict):
            null_keys = [k for k, v in node.items() if v is None]
            for k in null_keys:
                del node[k]
                fixes.append(f"Dropped null field '{k}' at {path}")
            for k, v in list(node.items()):
                _scrub_nulls(v, f"{path}.{k}")
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _scrub_nulls(item, f"{path}[{idx}]")

    _scrub_nulls(doc)

    # Ensure every operation has a responses object (OpenAPI requires it).
    paths = doc.get("paths")
    if isinstance(paths, dict):
        for p, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for verb, op in methods.items():
                if verb.lower() not in {
                    "get", "post", "put", "delete", "patch", "head", "options", "trace"
                }:
                    continue
                if isinstance(op, dict) and not op.get("responses"):
                    op["responses"] = {
                        "200": {"description": "Successful response"}
                    }
                    fixes.append(f"Added minimal 200 response to {verb.upper()} {p}")

    return doc, fixes


def _run_openapi_validate(doc: dict) -> tuple[bool, list[str]]:
    """Validate an OpenAPI 3.0 doc. Returns (available, error_messages)."""
    try:
        from openapi_spec_validator import OpenAPIV30SpecValidator
    except Exception as e:
        logger.warning(f"[LINT_OAS] openapi-spec-validator unavailable, skipping: {e}")
        return False, []

    try:
        errors = [
            f"{'/'.join(str(p) for p in err.absolute_path)}: {err.message}"
            for err in OpenAPIV30SpecValidator(doc).iter_errors()
        ]
        return True, errors
    except Exception as e:
        logger.warning(f"[LINT_OAS] validator raised, skipping: {e}")
        return True, []


def lint_and_autofix_openapi(yaml_str: str) -> dict:
    """Library entry point used by merge_openapi_fragments.

    Parses YAML, scrubs nulls / adds missing responses, validates against the
    OpenAPI 3.0 schema, and re-serialises. Never raises.
    Returns {available, fixed_yaml, fixes_applied, errors}.
    """
    try:
        import yaml as _yaml
    except Exception as e:
        logger.error(f"[LINT_OAS] PyYAML unavailable: {e}")
        return {"available": False, "fixed_yaml": yaml_str, "fixes_applied": [], "errors": []}

    try:
        doc = _yaml.safe_load(yaml_str)
    except Exception as e:
        logger.warning(f"[LINT_OAS] YAML parse failed, cannot lint: {e}")
        return {"available": True, "fixed_yaml": yaml_str, "fixes_applied": [],
                "errors": [f"YAML parse error: {e}"]}

    if not isinstance(doc, dict):
        return {"available": True, "fixed_yaml": yaml_str, "fixes_applied": [], "errors": []}

    try:
        doc, fixes = _autofix_openapi(doc)
        available, errors = _run_openapi_validate(doc)

        if fixes:
            # Re-serialise only if we actually changed something.
            fixed_yaml = _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=4096)
            logger.info(f"[LINT_OAS] Auto-fixes applied: {fixes}")
        else:
            fixed_yaml = yaml_str

        if errors:
            logger.warning(
                f"[LINT_OAS] {len(errors)} validation error(s) remain: "
                + "; ".join(errors[:8])
            )
        return {
            "available": available,
            "fixed_yaml": fixed_yaml,
            "fixes_applied": fixes,
            "errors": errors,
        }
    except Exception as e:
        logger.error(f"[LINT_OAS] Unexpected failure, passing through: {e}")
        return {"available": False, "fixed_yaml": yaml_str, "fixes_applied": [], "errors": []}


# ---------------------------------------------------------------------------
# Standalone tools (orchestrator can re-lint after a manual patch)
# ---------------------------------------------------------------------------


@tool
def lint_cloudformation(session_id: str = "", project_name: str = "") -> dict:
    """Lint the current CloudFormation template for a session and auto-fix
    common syntax issues (e.g. !Sub used where only a literal string is allowed).

    Loads ``infrastructure.yaml`` from the session workspace, runs cfn-lint,
    applies deterministic safe fixes, saves the fixed template back, and returns
    any remaining errors so they can be patched.

    Args:
        session_id: Session id (defaults to the active streaming session).
        project_name: Optional project name used as the asset operation_id.

    Returns:
        dict with: ok (bool — no errors remain), fixes_applied, errors, warnings.
    """
    from tools.streaming_callback import get_session_id, stream_asset, clear_asset_preview_cache
    from tools.s3_asset_storage import build_s3_key, get_asset_from_s3, save_asset_to_s3

    sid = session_id or get_session_id() or ""
    if not sid:
        return {"ok": False, "error": "No session_id available"}

    try:
        key = build_s3_key(sid, "cloudformation", "infrastructure.yaml", project_name or None)
        content = get_asset_from_s3(key)
    except Exception as e:
        return {"ok": False, "error": f"Could not load infrastructure.yaml: {e}"}

    if not content:
        return {"ok": False, "error": "infrastructure.yaml not found for session"}

    result = lint_and_autofix_cfn(content)
    fixed = result["fixed_yaml"]

    if fixed != content:
        try:
            clear_asset_preview_cache("cloudformation", "infrastructure.yaml", project_name)
            save_asset_to_s3(
                session_id=sid, asset_type="cloudformation",
                file_name="infrastructure.yaml", content=fixed,
                operation_id=project_name or None,
            )
            stream_asset("cloudformation", "infrastructure.yaml", fixed,
                         operation_id=project_name or sid, is_complete=True)
        except Exception as e:
            logger.error(f"[LINT_CFN] save-back failed: {e}")

    return {
        "ok": not result["errors"],
        "lint_available": result["available"],
        "fixes_applied": result["fixes_applied"],
        "errors": result["errors"],
        "warnings": result["warnings"][:20],
        "summary": (
            f"cfn-lint: {len(result['errors'])} error(s), "
            f"{len(result['warnings'])} warning(s); "
            f"{len(result['fixes_applied'])} auto-fix(es) applied"
        ),
    }


@tool
def lint_openapi(session_id: str = "", api_title: str = "") -> dict:
    """Lint the current OpenAPI 3.0 spec for a session and auto-fix common
    issues (null fields, missing operation responses), then validate against
    the OpenAPI 3.0 schema.

    Loads ``openapi.yaml`` from the session workspace, fixes + validates, saves
    the fixed spec back, and returns any remaining validation errors.

    Args:
        session_id: Session id (defaults to the active streaming session).
        api_title: Optional api title used to derive the asset operation_id.

    Returns:
        dict with: ok (bool — valid), fixes_applied, errors.
    """
    from tools.streaming_callback import get_session_id, stream_asset, clear_asset_preview_cache
    from tools.s3_asset_storage import build_s3_key, get_asset_from_s3, save_asset_to_s3

    sid = session_id or get_session_id() or ""
    if not sid:
        return {"ok": False, "error": "No session_id available"}

    op_id = api_title.replace(" ", "_").lower() if api_title else None
    try:
        key = build_s3_key(sid, "openapi", "openapi.yaml", op_id)
        content = get_asset_from_s3(key)
    except Exception as e:
        return {"ok": False, "error": f"Could not load openapi.yaml: {e}"}

    if not content:
        return {"ok": False, "error": "openapi.yaml not found for session"}

    result = lint_and_autofix_openapi(content)
    fixed = result["fixed_yaml"]

    if fixed != content:
        try:
            clear_asset_preview_cache("openapi", "openapi.yaml", op_id)
            save_asset_to_s3(
                session_id=sid, asset_type="openapi",
                file_name="openapi.yaml", content=fixed, operation_id=op_id,
            )
            stream_asset("openapi", "openapi.yaml", fixed,
                         operation_id=op_id or sid, is_complete=True)
        except Exception as e:
            logger.error(f"[LINT_OAS] save-back failed: {e}")

    return {
        "ok": not result["errors"],
        "lint_available": result["available"],
        "fixes_applied": result["fixes_applied"],
        "errors": result["errors"][:30],
        "summary": (
            f"openapi 3.0 validation: {len(result['errors'])} error(s); "
            f"{len(result['fixes_applied'])} auto-fix(es) applied"
        ),
    }


# ---------------------------------------------------------------------------
# Lambda (Python) — syntax check via the compiler (no execution)
# ---------------------------------------------------------------------------


def lint_python_source(code: str) -> dict:
    """Compile-check Python Lambda source WITHOUT running it.

    Catches syntax errors a generator might emit (unterminated strings, bad
    indentation, stray markdown fences) before they reach a 500 at runtime.
    Never raises. Returns {ok, errors:[{line, message}]}.
    """
    try:
        # Strip stray markdown fences if a generator left them in.
        src = code
        m = re.search(r'```(?:python|py)?\s*\n(.*?)```', src, re.DOTALL)
        if m:
            src = m.group(1)
        compile(src, "<lambda>", "exec")
        return {"ok": True, "errors": []}
    except SyntaxError as e:
        return {"ok": False, "errors": [{"line": e.lineno, "message": f"{e.msg}: {(e.text or '').strip()[:80]}"}]}
    except Exception as e:
        # Non-syntax failure (e.g. null bytes) — report but don't crash.
        return {"ok": False, "errors": [{"line": None, "message": str(e)[:120]}]}


@tool
def lint_lambda(session_id: str = "", operation_id: str = "", file_name: str = "index.py") -> dict:
    """Syntax-check a generated Lambda handler (Python) for a session.

    Loads the Lambda source from the session workspace and compile-checks it
    (no execution). Reports syntax errors with line numbers so they can be
    patched before packaging. Node.js handlers are skipped (returns ok).

    Args:
        session_id: Session id (defaults to the active streaming session).
        operation_id: The Lambda's operation/tool id (its asset subfolder).
        file_name: Handler file name (default index.py).

    Returns:
        dict with ok (bool), errors, summary.
    """
    from tools.streaming_callback import get_session_id
    from tools.s3_asset_storage import build_s3_key, get_asset_from_s3

    sid = session_id or get_session_id() or ""
    if not sid:
        return {"ok": False, "error": "No session_id available"}
    if not file_name.endswith(".py"):
        return {"ok": True, "skipped": "non-Python handler", "errors": []}
    try:
        key = build_s3_key(sid, "lambda", file_name, operation_id or None)
        content = get_asset_from_s3(key)
    except Exception as e:
        return {"ok": False, "error": f"Could not load lambda: {e}"}
    if not content:
        return {"ok": False, "error": f"lambda {operation_id}/{file_name} not found"}

    result = lint_python_source(content)
    return {
        "ok": result["ok"],
        "errors": result["errors"],
        "summary": (
            f"Python syntax {'OK' if result['ok'] else 'ERROR'} for {operation_id}/{file_name}"
            + ("" if result["ok"] else f": {result['errors'][0]['message']}")
        ),
    }


# ---------------------------------------------------------------------------
# Contact Flow (Amazon Connect flow JSON) — structural integrity
# ---------------------------------------------------------------------------


def lint_contact_flow(flow_json: str) -> dict:
    """Validate Amazon Connect Contact Flow JSON structural integrity.

    Catches the issues that make a flow fail to IMPORT or run:
      - invalid JSON
      - missing StartAction / Actions
      - transitions (NextAction / Errors / Conditions) pointing at action ids
        that don't exist (dangling references)
      - actions unreachable from StartAction (orphans)
      - terminal blocks (DisconnectParticipant) present
    Never raises. Returns {ok, errors:[...], warnings:[...]}.
    """
    import json as _json
    try:
        doc = _json.loads(flow_json)
    except Exception as e:
        return {"ok": False, "errors": [f"Invalid JSON: {e}"], "warnings": []}

    if not isinstance(doc, dict):
        return {"ok": False, "errors": ["Flow root is not an object"], "warnings": []}

    actions = doc.get("Actions") or doc.get("actions") or []
    start = doc.get("StartAction") or doc.get("startAction")
    errors: list[str] = []
    warnings: list[str] = []

    if not actions:
        return {"ok": False, "errors": ["No Actions in flow"], "warnings": []}

    ids = set()
    for a in actions:
        if isinstance(a, dict):
            aid = a.get("Identifier") or a.get("identifier")
            if aid:
                ids.add(aid)

    if not start:
        errors.append("Missing StartAction")
    elif start not in ids:
        errors.append(f"StartAction '{start}' is not a defined action")

    def _targets(action: dict):
        t = action.get("Transitions") or action.get("transitions") or {}
        out = []
        if isinstance(t, dict):
            nxt = t.get("NextAction") or t.get("nextAction")
            if nxt:
                out.append(nxt)
            for err in (t.get("Errors") or t.get("errors") or []):
                if isinstance(err, dict):
                    n = err.get("NextAction") or err.get("nextAction")
                    if n:
                        out.append(n)
            for cond in (t.get("Conditions") or t.get("conditions") or []):
                if isinstance(cond, dict):
                    n = cond.get("NextAction") or cond.get("nextAction")
                    if n:
                        out.append(n)
        return out

    # Dangling reference check + adjacency for reachability.
    adj: dict[str, list] = {}
    for a in actions:
        if not isinstance(a, dict):
            continue
        aid = a.get("Identifier") or a.get("identifier")
        tgts = _targets(a)
        adj[aid] = tgts
        for tg in tgts:
            if tg not in ids:
                errors.append(f"Action '{aid}' transitions to undefined action '{tg}'")

    # Reachability from StartAction (orphans → warning, not fatal).
    if start in ids:
        seen = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adj.get(cur, []))
        orphans = ids - seen
        if orphans:
            warnings.append(f"{len(orphans)} action(s) unreachable from StartAction: {sorted(orphans)[:5]}")

    types = {(a.get("Type") or a.get("type")) for a in actions if isinstance(a, dict)}
    if "DisconnectParticipant" not in types:
        warnings.append("No DisconnectParticipant (terminal) block — flow may not end cleanly")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


@tool
def lint_contact_flow_asset(session_id: str = "", flow_name: str = "", file_name: str = "contact_flow.json") -> dict:
    """Validate a generated Contact Flow's structural integrity for a session.

    Loads the flow JSON from the workspace and checks JSON validity, StartAction,
    dangling transition targets, orphan actions, and a terminal block — the
    things that make an Amazon Connect flow fail to import.

    Args:
        session_id: Session id (defaults to active streaming session).
        flow_name: The flow's operation id (asset subfolder).
        file_name: Flow file name (default contact_flow.json).

    Returns:
        dict with ok, errors, warnings, summary.
    """
    from tools.streaming_callback import get_session_id
    from tools.s3_asset_storage import build_s3_key, get_asset_from_s3

    sid = session_id or get_session_id() or ""
    if not sid:
        return {"ok": False, "error": "No session_id available"}
    try:
        key = build_s3_key(sid, "contact_flow", file_name, flow_name or None)
        content = get_asset_from_s3(key)
    except Exception as e:
        return {"ok": False, "error": f"Could not load contact flow: {e}"}
    if not content:
        return {"ok": False, "error": "contact_flow.json not found"}

    result = lint_contact_flow(content)
    return {
        "ok": result["ok"],
        "errors": result["errors"][:20],
        "warnings": result["warnings"][:10],
        "summary": (
            f"Contact Flow integrity: {len(result['errors'])} error(s), "
            f"{len(result['warnings'])} warning(s)"
        ),
    }
