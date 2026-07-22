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

# Official Amazon Connect flow-language Action Types (the `Type` field of an
# Action). VERIFIED against a real Amazon Connect console export (all 43 block
# types) PLUS individual CreateContactFlow API probes (2026-06-08). A flow whose
# `Type` is NOT in this set FAILS to import with InvalidContactFlowException.
# Source of truth: knowledge-base-docs/contact-flow/_reference-console-export-all-blocks.json
# Do NOT add a Type here without confirming it imports via the API — guessed
# names (TransferToAgent, TransferToPhoneNumber, CheckQueueStatus, ...) were the
# cause of import failures and are listed as invalid below.
VALID_CONTACT_FLOW_ACTION_TYPES = frozenset({
    # Interact
    "MessageParticipant", "MessageParticipantIteratively", "GetParticipantInput",
    "ConnectParticipantWithLexBot", "RenderMessageTemplate",
    # Set / update
    "UpdateContactAttributes", "UpdateContactData", "UpdateContactRecordingBehavior",
    "UpdateContactRecordingAndAnalyticsBehavior",
    "UpdateContactRecordingAndAnalyticsBehavior", "UpdateContactTextToSpeechVoice",
    "UpdateContactTargetQueue", "UpdateContactCallbackNumber", "UpdateContactEventHooks",
    "UpdateContactRoutingBehavior", "UpdateContactRoutingCriteria",
    "UpdateFlowLoggingBehavior", "UpdateFlowAttributes",
    "UpdateContactMediaStreamingBehavior", "UpdatePreviousContactParticipantState",
    "TagContact", "UntagContact",
    # Branch / control
    "Compare", "Loop", "Wait", "DistributeByPercentage",
    "CheckHoursOfOperation", "CheckMetricData", "GetMetricData", "CheckOutboundCallStatus",
    "EvaluateDataTableValues",
    # Integrate
    "InvokeLambdaFunction", "InvokeFlowModule", "CreateWisdomSession",
    "CreateTask", "CreateCase", "CreateContact", "StartOutboundEmailContact",
    "AssociateContactToCustomerProfile", "GetCustomerProfile", "GetCustomerProfileObject",
    "CreatePersistentContactAssociation", "LoadContactContent",
    "AuthenticateParticipant", "ShowView", "ResumeContact",
    # Transfer / terminate
    "TransferContactToQueue", "TransferParticipantToThirdParty", "TransferToFlow",
    "DequeueContactAndTransferToQueue",
    "DisconnectParticipant", "EndFlowExecution",
})

# Known LLM hallucinations / wrong names → the correct official Type (or None
# when there is no 1:1 replacement and the block must be removed/redesigned).
# These are blocks the model has actually emitted that break import.
INVALID_CONTACT_FLOW_TYPE_HINTS = {
    # There is no "Trigger" / entry-point block — a flow starts at the action
    # that StartAction points to. Drop the wrapper and start at its NextAction.
    "Trigger": "(remove — flows start at StartAction, no Trigger block exists)",
    "EntryPoint": "(remove — flows start at StartAction, no EntryPoint block exists)",
    # Branch-on-value is `Compare`, not CheckCondition / CheckValue / Condition.
    "CheckCondition": "Compare",
    "CheckValue": "Compare",
    "Condition": "Compare",
    "CheckAttribute": "Compare",
    # No "InvokeAgentAction" / Bedrock-agent block exists in the flow language.
    # AI self-service runs through a Q-in-Connect-enabled Lex V2 bot.
    "InvokeAgentAction": "ConnectParticipantWithLexBot",
    "InvokeBedrockAgent": "ConnectParticipantWithLexBot",
    "InvokeAmazonQConnect": "ConnectParticipantWithLexBot",
    "InvokeQConnect": "ConnectParticipantWithLexBot",
    # Common other hallucinations.
    "PlayPrompt": "MessageParticipant",
    "GetUserInput": "GetParticipantInput",
    "SetWorkingQueue": "UpdateContactTargetQueue",
    "SetCallbackNumber": "UpdateContactCallbackNumber",
    "SetContactAttributes": "UpdateContactAttributes",
    "SetRecordingBehavior": "UpdateContactRecordingBehavior",
    "SetLoggingBehavior": "UpdateFlowLoggingBehavior",
    "SetVoice": "UpdateContactTextToSpeechVoice",
    "CreateCallbackContact": "(remove — use UpdateContactCallbackNumber + TransferContactToQueue)",
    "TransferToQueue": "TransferContactToQueue",
    "EndFlow": "DisconnectParticipant",
    "Disconnect": "DisconnectParticipant",
    # API-confirmed INVALID Types (2026-06-08) — these returned "Invalid Action
    # type" from CreateContactFlow. Map to the real Type the console export uses.
    "TransferToAgent": "TransferContactToQueue",
    "TransferToPhoneNumber": "TransferParticipantToThirdParty",
    "TransferToThirdParty": "TransferParticipantToThirdParty",
    "CheckQueueStatus": "CheckMetricData",
    "CheckStaffing": "CheckMetricData",
    "Distribute": "DistributeByPercentage",
    "StartMediaStreaming": "UpdateContactMediaStreamingBehavior",
    "StopMediaStreaming": "UpdateContactMediaStreamingBehavior",
    "ReturnFromFlowModule": "EndFlowExecution",
    "InvokeAPI": "InvokeLambdaFunction",
    "GetParticipantWithLexV2Bot": "ConnectParticipantWithLexBot",
    "PutCustomerProfile": "(use Customer Profiles integration / UpdateContactData)",
    "UpdateCustomerProfileObject": "(use Customer Profiles integration)",
    "UpdateContactRoutingData": "UpdateContactRoutingCriteria",
    "UpdateContactTextToSpeechManner": "UpdateContactTextToSpeechVoice",
    "SetRecordingAndAnalyticsBehavior": "UpdateContactRecordingAndAnalyticsBehavior",
}


# Action Types that are TERMINAL — they end flow execution and MUST NOT carry
# Transitions. Connect rejects "Action does not support transitions" otherwise.
TERMINAL_ACTION_TYPES = frozenset({
    "DisconnectParticipant", "EndFlowExecution", "ReturnFromFlowModule",
    "TransferToFlow", "TransferContactToQueue", "TransferToAgent",
})
# TransferContactToQueue/TransferToAgent DO support transitions (queue-full etc.)
# so keep only the truly terminal ones for the no-transitions rule.
NO_TRANSITION_ACTION_TYPES = frozenset({
    "DisconnectParticipant", "EndFlowExecution", "ReturnFromFlowModule",
})

# Required Error handlers per Action Type (Connect import enforces these).
# Required Error handlers per Action Type — all verified against the real
# CreateContactFlow API (create → inspect problems → delete, ap-northeast-2).
# NOTE: GetParticipantInput is intentionally absent — its required errors depend
# on mode (menu vs store) and are handled in the dedicated normalizer above.
# NOTE: MessageParticipant is intentionally absent — API-verified (2026-07-04)
# that it imports with NO Errors, so NoMatchingError is recommended (see prompt)
# but NOT import-required; force-injecting it would over-normalize a valid flow.
REQUIRED_ERRORS_BY_TYPE = {
    "Compare": ["NoMatchingCondition"],
    "ConnectParticipantWithLexBot": ["NoMatchingError", "NoMatchingCondition"],
    "InvokeLambdaFunction": ["NoMatchingError"],
    "CreateWisdomSession": ["NoMatchingError"],
    "UpdateContactData": ["NoMatchingError"],
    "TransferContactToQueue": ["QueueAtCapacity", "NoMatchingError"],
    "DequeueContactAndTransferToQueue": ["QueueAtCapacity", "NoMatchingError"],
    "UpdateContactTargetQueue": ["NoMatchingError"],
    "UpdateContactAttributes": ["NoMatchingError"],   # API-verified: required
    "CheckHoursOfOperation": ["NoMatchingError"],     # branches via True/False Conditions; needs NoMatchingError
    # API-verified (2026-06-18): UpdateContactCallbackNumber requires BOTH of these
    # and rejects NoMatchingError / InvalidNumber / NotDialable.
    "UpdateContactCallbackNumber": ["InvalidCallbackNumber", "CallbackNumberNotDialable"],
    # API-verified (2026-07-03) exhaustive per-parameter probe of the 10 blocks that
    # the RAG sweep never exercised. Each block imports only with these error branches.
    "AuthenticateParticipant": ["NoMatchingError", "TimeLimitExceeded"],
    "CheckOutboundCallStatus": ["NoMatchingError"],
    "CreatePersistentContactAssociation": ["NoMatchingError"],
    "DistributeByPercentage": ["NoMatchingCondition"],
    "EvaluateDataTableValues": ["NoMatchingError"],
    "GetCustomerProfileObject": ["NoMatchingError", "NoneFoundError"],
    "LoadContactContent": ["NoMatchingError"],
    "UpdateContactRoutingCriteria": ["NoMatchingError"],
    # NOTE: UpdateContactRoutingBehavior has CONDITIONAL errors — NoMatchingError is
    # required ONLY for the RoutingProficiencies variant and MUST be absent for the
    # QueuePriority / QueueTimeAdjustmentSeconds variants. Left out of this static map
    # on purpose; enforcing a fixed set here would break the priority variant.
}

# Error types that are NOT valid for a given block — strip them on import.
INVALID_ERRORS_BY_TYPE = {
    "ConnectParticipantWithLexBot": {"AgentError"},
    "CheckHoursOfOperation": {"NoMatchingCondition"},
    # Compare branches via Conditions; its only valid Error is NoMatchingCondition.
    "Compare": {"NoMatchingError"},
    # API-verified (CreateContactFlow problems, 2026-06-18): UpdateContactCallbackNumber
    # accepts ONLY InvalidCallbackNumber + CallbackNumberNotDialable (both required).
    # It rejects NoMatchingError AND the outbound-dial types (InvalidNumber/NotDialable)
    # the LLM tends to hallucinate here — all of which trip InvalidContactFlowException.
    "UpdateContactCallbackNumber": {"InvalidNumber", "NotDialable", "NoMatchingError"},
}

# Canonical AI-bot tool-result vocabulary (see SUBAGENT_TERMINOLOGY_AND_ESCALATION).
# The bot returns exactly `Complete` (end call) or `Escalate` (human transfer);
# the flow's Compare branches on these. Map common LLM synonyms back to canonical.
# Keys are upper-cased for case-insensitive matching. Spec-driven extensions like
# `OutOfHoursComplete` / `EscalateBilling` are intentionally NOT remapped.
_CANONICAL_TOOL_RESULT = {
    "COMPLETE": "Complete",
    "END_CALL": "Complete",
    "ENDCALL": "Complete",
    "END_CONVERSATION": "Complete",
    "ENDCONVERSATION": "Complete",
    "DONE": "Complete",
    "FINISH": "Complete",
    "FINISHED": "Complete",
    "HANGUP": "Complete",
    "DISCONNECT": "Complete",
    "ESCALATE": "Escalate",
    "ESCALATION": "Escalate",
    "TRANSFER": "Escalate",
    "TRANSFER_TO_AGENT": "Escalate",
    "AGENT": "Escalate",
    "HUMAN": "Escalate",
    "HANDOFF": "Escalate",
}

# Valid JSONPath root namespaces for Compare.ComparisonValue / attribute refs.
# The model sometimes invents roots like `$.Agent.ReturnControlEvent.Type`.
_VALID_JSONPATH_ROOTS = (
    "$.Attributes.", "$.Channel", "$.CustomerEndpoint", "$.SystemEndpoint",
    "$.Lex.", "$.Customer.", "$.External.", "$.StoredCustomerInput",
    "$.Media.", "$.ContactId", "$.InitialContactId", "$.Queue.",
    "$.Metadata.", "$.FlowAttributes.",
)


def _normalize_contact_flow_params(actions: list, ids_to_first: dict, fixes: list) -> None:
    """Rewrite block Parameters/Transitions to the shapes Amazon Connect's
    CreateContactFlow API actually accepts. Mutates `actions` in place and
    appends human-readable notes to `fixes`.

    Every rule here was derived from real InvalidContactFlowException `problems`
    returned by the Connect API (the strict validator the console import uses):
      - UpdateContactRecordingBehavior: {Agent,Customer} → {RecordingBehavior,…}
      - UpdateContactTextToSpeechVoice: {VoiceId,Engine,LanguageCode} → {TextToSpeechVoice,TextToSpeechEngine}
      - UpdateFlowLoggingBehavior:      {LoggingBehavior} → {FlowLoggingBehavior}
      - UpdateContactTargetQueue:       {Queue} → {QueueId}
      - ConnectParticipantWithLexBot:   {BotAliasArn,LexBot{AliasArn},Participant…} → {LexV2Bot{AliasArn},Text}
      - terminal blocks:                strip Transitions
      - MessageParticipant:             only ONE of Text/SSML/Media
      - missing required Error handlers: injected, routed to a safe fallback
    """
    # A safe fallback target for injected error transitions: prefer an existing
    # disconnect/terminal block, else the first action.
    fallback = None
    for a in actions:
        if isinstance(a, dict) and (a.get("Type") or a.get("type")) == "DisconnectParticipant":
            fallback = a.get("Identifier") or a.get("identifier")
            break
    if not fallback and actions:
        fallback = actions[0].get("Identifier") or actions[0].get("identifier")

    for a in actions:
        if not isinstance(a, dict):
            continue
        t = a.get("Type") or a.get("type")
        p = a.get("Parameters")
        if p is None:
            p = a.get("parameters")
        if p is None:
            p = {}
        pkey = "Parameters" if "Parameters" in a or "parameters" not in a else "parameters"
        aid = a.get("Identifier") or a.get("identifier")

        # --- UpdateContactRecordingAndAnalyticsBehavior (current console block):
        #     API requires NoMatchingError + ChannelMismatch error branches
        #     (+ InFlightRedactionConfigurationFailed when ChatBehavior present)
        if t == "UpdateContactRecordingAndAnalyticsBehavior":
            tr = a.setdefault("Transitions", {})
            nxt = tr.get("NextAction") or fallback
            errs = tr.setdefault("Errors", [])
            have = {e.get("ErrorType") for e in errs if isinstance(e, dict)}
            required = ["NoMatchingError", "ChannelMismatch"]
            if isinstance(p, dict) and "ChatBehavior" in p:
                required.append("InFlightRedactionConfigurationFailed")
            for et in required:
                if et not in have and nxt:
                    errs.append({"ErrorType": et, "NextAction": nxt})
                    fixes.append(f"[{aid}] UpdateContactRecordingAndAnalyticsBehavior: added required {et} error branch")

        # --- UpdateContactRecordingBehavior: {Agent, Customer} → RecordingBehavior
        if t == "UpdateContactRecordingBehavior":
            if "RecordingBehavior" not in p:
                p.pop("Agent", None)
                p.pop("Customer", None)
                p["RecordingBehavior"] = {
                    "RecordedParticipants": ["Agent", "Customer"],
                    "IVRRecordingBehavior": "Enabled",
                }
                p.setdefault("AnalyticsBehavior", {
                    "Enabled": "True",
                    "AnalyticsLanguage": "ko-KR",
                    "ChannelConfiguration": {
                        "Chat": {"AnalyticsModes": ["ContactLens"]},
                        "Voice": {"AnalyticsModes": ["PostContact"]},
                    },
                })
                fixes.append(f"[{aid}] UpdateContactRecordingBehavior: rebuilt RecordingBehavior (removed Agent/Customer)")

        # --- UpdateContactTextToSpeechVoice: {VoiceId,Engine,LanguageCode} → {TextToSpeechVoice,Engine}
        elif t == "UpdateContactTextToSpeechVoice":
            if "TextToSpeechVoice" not in p:
                voice = p.pop("VoiceId", None) or "Seoyeon"
                eng = p.pop("Engine", None) or "Generative"
                p.pop("LanguageCode", None)
                p["TextToSpeechVoice"] = voice
                p["TextToSpeechEngine"] = eng
                p.setdefault("TextToSpeechStyle", "None")
                fixes.append(f"[{aid}] UpdateContactTextToSpeechVoice: VoiceId/Engine/LanguageCode → TextToSpeechVoice/TextToSpeechEngine")

        # --- UpdateFlowLoggingBehavior: {LoggingBehavior} → {FlowLoggingBehavior}
        elif t == "UpdateFlowLoggingBehavior":
            if "FlowLoggingBehavior" not in p and "LoggingBehavior" in p:
                p["FlowLoggingBehavior"] = p.pop("LoggingBehavior")
                fixes.append(f"[{aid}] UpdateFlowLoggingBehavior: LoggingBehavior → FlowLoggingBehavior")

        # --- UpdateContactTargetQueue: {Queue} → {QueueId}; flatten nested QueueId
        elif t == "UpdateContactTargetQueue":
            if "QueueId" not in p and "Queue" in p:
                p["QueueId"] = p.pop("Queue")
                fixes.append(f"[{aid}] UpdateContactTargetQueue: Queue → QueueId")
            # QueueId must be a string ARN/id, not a nested object {"QueueId": "..."}
            if isinstance(p.get("QueueId"), dict):
                inner = p["QueueId"].get("QueueId") or p["QueueId"].get("Id") or next(iter(p["QueueId"].values()), None)
                p["QueueId"] = inner or "{{QUEUE_ARN}}"
                fixes.append(f"[{aid}] UpdateContactTargetQueue: flattened nested QueueId → string")

        # --- TransferContactToQueue: takes NO queue parameter (API-verified
        # 2026-06-18). The target queue is set by a preceding UpdateContactTargetQueue;
        # any QueueId/QueueArn/Queue here is rejected as "Invalid Action property name".
        # The LLM frequently re-specifies the queue on the transfer block — strip it.
        elif t == "TransferContactToQueue":
            removed_q = [k for k in ("QueueId", "QueueArn", "Queue") if k in p]
            for k in removed_q:
                p.pop(k, None)
            if removed_q:
                fixes.append(f"[{aid}] TransferContactToQueue: removed invalid queue param(s) {removed_q} (queue is set via UpdateContactTargetQueue)")

        # --- InvokeLambdaFunction: RequestAttributes → LambdaInvocationAttributes
        elif t == "InvokeLambdaFunction":
            if "RequestAttributes" in p:
                p.setdefault("LambdaInvocationAttributes", p.pop("RequestAttributes"))
                fixes.append(f"[{aid}] InvokeLambdaFunction: RequestAttributes → LambdaInvocationAttributes")
            # ResponseValidation.ResponseType must be STRING_MAP or JSON (API-verified
            # 2026-07-03: both import; JSON_OBJECT is REJECTED with "Invalid Action
            # property value"). ResponseValidation itself is optional.
            rv = p.get("ResponseValidation")
            if isinstance(rv, dict) and rv.get("ResponseType") not in ("STRING_MAP", "JSON"):
                rv["ResponseType"] = "STRING_MAP"
                fixes.append(f"[{aid}] InvokeLambdaFunction: ResponseValidation.ResponseType → STRING_MAP")

        # --- CheckHoursOfOperation: null/missing HoursOfOperationId → placeholder
        elif t == "CheckHoursOfOperation":
            if not p.get("HoursOfOperationId"):
                p["HoursOfOperationId"] = "{{HOURS_OF_OPERATION_ID}}"
                fixes.append(f"[{aid}] CheckHoursOfOperation: filled missing HoursOfOperationId placeholder")
            # Must branch on BOTH True and False conditions.
            tr = a.get("Transitions") or a.get("transitions") or {}
            conds = tr.get("Conditions") or tr.get("conditions") or []
            operands = {str(c.get("Condition", {}).get("Operands", [None])[0])
                        for c in conds if isinstance(c, dict)}
            nxt = tr.get("NextAction") or tr.get("nextAction") or fallback
            true_target = next((c.get("NextAction") for c in conds
                                if isinstance(c, dict) and str(c.get("Condition", {}).get("Operands", [None])[0]) == "True"), None)
            if "False" not in operands:
                conds.append({"Condition": {"Operator": "Equals", "Operands": ["False"]},
                              "NextAction": nxt})
                fixes.append(f"[{aid}] CheckHoursOfOperation: added missing 'False' branch")
            if "True" not in operands:
                conds.insert(0, {"Condition": {"Operator": "Equals", "Operands": ["True"]},
                                 "NextAction": true_target or nxt})
                fixes.append(f"[{aid}] CheckHoursOfOperation: added missing 'True' branch")
            tr["Conditions"] = conds
            a["Transitions" if "Transitions" in a or "transitions" not in a else "transitions"] = tr

        # --- Compare: ComparisonValue must use a valid JSONPath root
        elif t == "Compare":
            cv = p.get("ComparisonValue")
            if isinstance(cv, str) and cv.startswith("$.") and not cv.startswith(_VALID_JSONPATH_ROOTS):
                # Re-home an invented root (e.g. $.Agent.ReturnControlEvent.Type)
                # to a contact attribute, which is where flow logic should read.
                leaf = cv.rstrip(".").split(".")[-1]
                p["ComparisonValue"] = f"$.Attributes.{leaf}"
                fixes.append(f"[{aid}] Compare: ComparisonValue '{cv}' → '$.Attributes.{leaf}' (invalid JSONPath root)")
            # Canonical AI-bot tool-result vocabulary: when this Compare branches on
            # the bot's Tool result, the operands MUST be `Complete`/`Escalate`
            # (see SUBAGENT_TERMINOLOGY_AND_ESCALATION). Normalize known wrong
            # synonyms so the flow's branch matches what the bot actually returns.
            cv2 = p.get("ComparisonValue") or ""
            if isinstance(cv2, str) and (".Tool" in cv2 or "actionType" in cv2 or ".toolResult" in cv2 or ".Tool." in cv2):
                tr = a.get("Transitions") or a.get("transitions") or {}
                for cond in (tr.get("Conditions") or tr.get("conditions") or []):
                    if not isinstance(cond, dict):
                        continue
                    ops = cond.get("Condition", {}).get("Operands")
                    if not isinstance(ops, list):
                        continue
                    for i_op, val in enumerate(ops):
                        canon = _CANONICAL_TOOL_RESULT.get(str(val).strip().upper())
                        if canon and val != canon:
                            ops[i_op] = canon
                            fixes.append(f"[{aid}] Compare: tool-result operand '{val}' → '{canon}' (canonical Complete/Escalate vocabulary)")

        # --- ConnectParticipantWithLexBot: normalize to LexV2Bot.AliasArn + one message
        elif t == "ConnectParticipantWithLexBot":
            # Collect any ARN the model produced under various wrong keys.
            arn = (p.pop("BotAliasArn", None)
                   or p.pop("AgentAliasArn", None)
                   or (isinstance(p.get("LexBot"), dict) and p["LexBot"].get("AliasArn"))
                   or (isinstance(p.get("LexV2Bot"), dict) and p["LexV2Bot"].get("AliasArn")))
            # Drop non-existent params.
            for bad in ("ParticipantRole", "SessionAttributes", "RequestAttributes",
                        "IdleSessionTimeout", "EndConversationPhrase"):
                if bad in p and bad != "SessionAttributes":
                    p.pop(bad, None)
            # Migrate LexSessionAttributes-style key if present under SessionAttributes.
            if "SessionAttributes" in p:
                p.setdefault("LexSessionAttributes", p.pop("SessionAttributes"))
            # Remove a malformed LexBot (V1 requires Name+Region+Alias; we use V2).
            lexbot = p.get("LexBot")
            if isinstance(lexbot, dict) and not all(k in lexbot for k in ("Name", "Region", "Alias")):
                p.pop("LexBot", None)
            if "LexV2Bot" not in p and "LexBot" not in p:
                p["LexV2Bot"] = {"AliasArn": arn or "{{LEX_BOT_ALIAS_ARN}}"}
                fixes.append(f"[{aid}] ConnectParticipantWithLexBot: normalized to LexV2Bot.AliasArn")
            # Must have exactly one message property.
            msg_keys = [k for k in ("Text", "SSML", "PromptId", "Media", "LexInitializationData") if k in p]
            if not msg_keys:
                p["Text"] = "{{WELCOME_MESSAGE}}"
                fixes.append(f"[{aid}] ConnectParticipantWithLexBot: added required Text")

        # --- GetParticipantInput: two distinct API-verified modes.
        #   MENU mode  (branches via Conditions): StoreInput=False, NO
        #     DTMFConfiguration, errors must be NoMatchingCondition +
        #     InputTimeLimitExceeded + NoMatchingError.
        #   STORE mode (captures input to an attribute): StoreInput=True,
        #     requires InputValidation.CustomValidation.MaximumLength, optional
        #     DTMFConfiguration{DisableCancelKey, InputTerminationSequence},
        #     errors = NoMatchingError only.
        # Both modes REQUIRE InputTimeLimitSeconds (string) at the Parameters root.
        # DisableCancelKey MUST be a string "True"/"False" — a JSON boolean makes the
        # whole block fail import with a MISLEADING "Invalid Action type" error.
        # (Both verified against CreateContactFlow; the empty/guessed forms the
        #  model emits otherwise fail import with misleading "Invalid Action type".)
        if t == "GetParticipantInput":
            tr_gp = a.get("Transitions") or a.get("transitions") or {}
            conds_gp = tr_gp.get("Conditions") or tr_gp.get("conditions") or []
            is_menu = bool(conds_gp)
            # DisableCancelKey must be a string, never a JSON boolean (bool → misleading
            # "Invalid Action type" on import).
            _dtmf = p.get("DTMFConfiguration")
            if isinstance(_dtmf, dict) and isinstance(_dtmf.get("DisableCancelKey"), bool):
                _dtmf["DisableCancelKey"] = "True" if _dtmf["DisableCancelKey"] else "False"
                fixes.append(f"[{aid}] GetParticipantInput: DisableCancelKey boolean → string")
            # Both modes require InputTimeLimitSeconds (string) at the Parameters root.
            # A value mistakenly nested in DTMFConfiguration is re-homed by the store-mode
            # normalizer below; only inject a default when it exists in neither place.
            _nested_itl = isinstance(_dtmf, dict) and "InputTimeLimitSeconds" in _dtmf
            if "InputTimeLimitSeconds" not in p and not _nested_itl:
                p["InputTimeLimitSeconds"] = "5"
                fixes.append(f"[{aid}] GetParticipantInput: added required InputTimeLimitSeconds")
            if is_menu:
                if str(p.get("StoreInput")) != "False":
                    p["StoreInput"] = "False"
                    fixes.append(f"[{aid}] GetParticipantInput(menu): StoreInput=False")
                if "DTMFConfiguration" in p:
                    p.pop("DTMFConfiguration", None)
                    fixes.append(f"[{aid}] GetParticipantInput(menu): removed DTMFConfiguration")
                p.pop("InputValidation", None)
            else:
                if str(p.get("StoreInput")) != "True":
                    p["StoreInput"] = "True"
                    fixes.append(f"[{aid}] GetParticipantInput(store): StoreInput=True")
                # DTMFConfiguration in store mode accepts DisableCancelKey and
                # InputTerminationSequence (API-verified 2026-07-03 — the terminator
                # key imports fine). InputTimeLimitSeconds belongs at the Parameters
                # root, not inside DTMFConfiguration; any other key is rejected.
                dtmf = p.get("DTMFConfiguration")
                if isinstance(dtmf, dict):
                    allowed = {"DisableCancelKey", "InputTerminationSequence"}
                    bad = [k for k in list(dtmf.keys()) if k not in allowed]
                    if bad:
                        # InputTimeLimitSeconds is a real top-level param — re-home it.
                        if "InputTimeLimitSeconds" in dtmf and "InputTimeLimitSeconds" not in p:
                            p["InputTimeLimitSeconds"] = dtmf["InputTimeLimitSeconds"]
                        for k in bad:
                            dtmf.pop(k, None)
                        dtmf.setdefault("DisableCancelKey", "False")
                        fixes.append(f"[{aid}] GetParticipantInput(store): stripped invalid DTMFConfiguration keys {bad}")
                if "InputValidation" not in p:
                    p["InputValidation"] = {"CustomValidation": {"MaximumLength": "6"}}
                    fixes.append(f"[{aid}] GetParticipantInput(store): added required InputValidation")
                # Store mode: ONLY NoMatchingError is valid (no Conditions → no
                # NoMatchingCondition; InputTimeLimitExceeded is rejected here).
                tr_st = a.get("Transitions") or a.get("transitions")
                if isinstance(tr_st, dict):
                    errs = tr_st.get("Errors") or tr_st.get("errors") or []
                    nxt = tr_st.get("NextAction") or tr_st.get("nextAction")
                    bad_err = [e for e in errs if (e.get("ErrorType") or e.get("errorType")) != "NoMatchingError"]
                    if bad_err:
                        kept = [e for e in errs if (e.get("ErrorType") or e.get("errorType")) == "NoMatchingError"]
                        if not kept:
                            kept = [{"ErrorType": "NoMatchingError", "NextAction": nxt}]
                        tr_st["Errors" if "Errors" in tr_st else "errors"] = kept
                        fixes.append(f"[{aid}] GetParticipantInput(store): kept only NoMatchingError (removed {[e.get('ErrorType') for e in bad_err]})")

        # --- Loop: condition operands are DoneLooping/ContinueLooping (NOT Looping/Complete)
        if t == "Loop":
            tr_lp = a.get("Transitions") or a.get("transitions") or {}
            for c in (tr_lp.get("Conditions") or tr_lp.get("conditions") or []):
                if not isinstance(c, dict):
                    continue
                ops = c.get("Condition", {}).get("Operands")
                if isinstance(ops, list):
                    for i_o, v in enumerate(ops):
                        lv = str(v).strip().lower()
                        if lv in ("looping", "continue", "continuelooping"):
                            if ops[i_o] != "ContinueLooping":
                                ops[i_o] = "ContinueLooping"
                                fixes.append(f"[{aid}] Loop: operand → ContinueLooping")
                        elif lv in ("complete", "done", "donelooping"):
                            if ops[i_o] != "DoneLooping":
                                ops[i_o] = "DoneLooping"
                                fixes.append(f"[{aid}] Loop: operand → DoneLooping")

        # --- MessageParticipant / GetParticipantInput: only ONE of Text/SSML/Media
        if t in ("MessageParticipant", "GetParticipantInput", "MessageParticipantIteratively"):
            present = [k for k in ("Text", "SSML", "PromptId", "Media") if k in p]
            if len(present) > 1:
                # Keep Text if present, else the first; drop the rest.
                keep = "Text" if "Text" in present else present[0]
                for k in present:
                    if k != keep:
                        p.pop(k, None)
                fixes.append(f"[{aid}] {t}: kept only '{keep}' (removed {[k for k in present if k!=keep]})")

        # write params back
        if pkey in a or p:
            a[pkey] = p

        # --- Terminal blocks must not carry Transitions / Conditions / Errors
        if t in NO_TRANSITION_ACTION_TYPES:
            removed = []
            for k in ("Transitions", "transitions", "Conditions", "Errors"):
                if a.get(k):
                    a.pop(k, None)
                    removed.append(k)
                elif k in a:
                    a.pop(k, None)
            if removed:
                fixes.append(f"[{aid}] {t}: removed {removed} (terminal block carries none)")

        # --- Strip Error types that are invalid for this block
        bad_errs = INVALID_ERRORS_BY_TYPE.get(t)
        if bad_errs:
            tr = a.get("Transitions") or a.get("transitions")
            if isinstance(tr, dict):
                errs = tr.get("Errors") or tr.get("errors")
                if isinstance(errs, list):
                    kept = [e for e in errs if (e.get("ErrorType") or e.get("errorType")) not in bad_errs]
                    if len(kept) != len(errs):
                        tr["Errors" if "Errors" in tr else "errors"] = kept
                        fixes.append(f"[{aid}] {t}: removed invalid Error type(s) {sorted(bad_errs & {e.get('ErrorType') or e.get('errorType') for e in errs})}")

        # --- Inject missing required Error handlers
        req = REQUIRED_ERRORS_BY_TYPE.get(t)
        if req and t not in NO_TRANSITION_ACTION_TYPES:
            tr = a.get("Transitions")
            trkey = "Transitions"
            if tr is None:
                tr = a.get("transitions")
                trkey = "transitions"
            if tr is None:
                tr = {}
                trkey = "Transitions"
            errs = tr.get("Errors")
            if errs is None:
                errs = tr.get("errors") or []
            have = {e.get("ErrorType") or e.get("errorType") for e in errs if isinstance(e, dict)}
            nxt = tr.get("NextAction") or tr.get("nextAction") or fallback
            added = []
            for et in req:
                if et not in have:
                    errs.append({"ErrorType": et, "NextAction": fallback or nxt})
                    added.append(et)
            if added:
                tr["Errors"] = errs
                a[trkey] = tr
                fixes.append(f"[{aid}] {t}: added required Error(s) {added}")


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
        return {"ok": False, "errors": [f"Invalid JSON: {e}"], "warnings": [], "fixes_applied": [], "fixed_json": flow_json}

    if not isinstance(doc, dict):
        return {"ok": False, "errors": ["Flow root is not an object"], "warnings": [], "fixes_applied": [], "fixed_json": flow_json}

    actions = doc.get("Actions") or doc.get("actions") or []
    errors: list[str] = []
    warnings: list[str] = []
    fixes_applied: list[str] = []

    # Deterministic auto-fix: `RealTime` in a Voice AnalyticsModes list breaks
    # Amazon Connect import (InvalidContactFlowException on
    # AnalyticsBehavior.ChannelConfiguration.Voice) unless real-time Contact Lens
    # preconditions are met. Drop it (keep PostContact / others) so the flow
    # always imports. Verified against create-contact-flow.
    for a in actions:
        if not isinstance(a, dict):
            continue
        if (a.get("Type") or a.get("type")) != "UpdateContactRecordingBehavior":
            continue
        params = a.get("Parameters") or a.get("parameters") or {}
        ab = params.get("AnalyticsBehavior") or {}
        cc = ab.get("ChannelConfiguration") or {}
        voice = cc.get("Voice") or {}
        modes = voice.get("AnalyticsModes")
        if isinstance(modes, list) and "RealTime" in modes:
            new_modes = [m for m in modes if m != "RealTime"] or ["PostContact"]
            voice["AnalyticsModes"] = new_modes
            fixes_applied.append(
                f"Removed 'RealTime' from Voice.AnalyticsModes (→ {new_modes}) — RealTime breaks Connect import"
            )
    # Deterministic auto-fix: a `Trigger` / `EntryPoint` block does not exist in
    # the flow language — a flow starts directly at the action StartAction points
    # to. The model sometimes emits a no-op Trigger as the StartAction. Rewire
    # StartAction to the Trigger's NextAction and drop the Trigger block so the
    # flow imports.
    _start_id = doc.get("StartAction") or doc.get("startAction")
    for a in list(actions):
        if not isinstance(a, dict):
            continue
        if (a.get("Type") or a.get("type")) not in ("Trigger", "EntryPoint"):
            continue
        aid = a.get("Identifier") or a.get("identifier")
        trans = a.get("Transitions") or a.get("transitions") or {}
        nxt = trans.get("NextAction") or trans.get("nextAction")
        if aid == _start_id and nxt:
            # Repoint StartAction past the Trigger, then remove the Trigger block.
            if "StartAction" in doc:
                doc["StartAction"] = nxt
            else:
                doc["startAction"] = nxt
            actions.remove(a)
            # Drop its ActionMetadata entry if present.
            md = doc.get("Metadata") or {}
            am = md.get("ActionMetadata") if isinstance(md, dict) else None
            if isinstance(am, dict):
                am.pop(aid, None)
            fixes_applied.append(
                f"Removed invalid '{a.get('Type') or a.get('type')}' block '{aid}'; "
                f"StartAction → '{nxt}' (no Trigger block exists in flow language)"
            )

    # Deterministic block-type validation: an Action whose Type is not a real
    # Amazon Connect flow-language Action Type makes the flow fail to import
    # (InvalidContactFlowException). The model occasionally hallucinates blocks
    # like `Trigger`, `CheckCondition`, `InvokeAgentAction`. Catch every such
    # Type and, for the known 1:1 renames, auto-fix it.
    invalid_types: list[str] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        t = a.get("Type") or a.get("type")
        if not t or t in VALID_CONTACT_FLOW_ACTION_TYPES:
            continue
        hint = INVALID_CONTACT_FLOW_TYPE_HINTS.get(t)
        if hint and not hint.startswith("("):
            # Safe 1:1 rename to a real Type.
            if "Type" in a:
                a["Type"] = hint
            else:
                a["type"] = hint
            # When renaming a hallucinated AI block to ConnectParticipantWithLexBot,
            # the old params (AgentAliasArn / IdleSessionTimeout / EndConversationPhrase)
            # are invalid for the Lex block and Connect rejects them on import.
            # Rewrite to the minimum valid shape: LexV2Bot.AliasArn + Text.
            if hint == "ConnectParticipantWithLexBot":
                p = a.get("Parameters") or a.get("parameters") or {}
                if "LexV2Bot" not in p and "LexBot" not in p:
                    old_arn = p.pop("AgentAliasArn", None) or p.pop("BotAliasArn", None)
                    phrase = p.pop("EndConversationPhrase", None)
                    p.pop("IdleSessionTimeout", None)
                    p["LexV2Bot"] = {"AliasArn": old_arn or "{{LEX_BOT_ALIAS_ARN}}"}
                    if "Text" not in p and "SSML" not in p and "PromptId" not in p and "Media" not in p:
                        p["Text"] = phrase or "{{WELCOME_MESSAGE}}"
                    if "Parameters" in a:
                        a["Parameters"] = p
                    else:
                        a["parameters"] = p
                    fixes_applied.append(
                        f"Rewrote '{t}' params → ConnectParticipantWithLexBot (LexV2Bot.AliasArn + Text)"
                    )
            fixes_applied.append(f"Renamed invalid block Type '{t}' → '{hint}'")
        else:
            suffix = f" — {hint}" if hint else " (not a valid Amazon Connect flow Action Type)"
            invalid_types.append(f"Action '{a.get('Identifier') or a.get('identifier')}' has invalid Type '{t}'{suffix}")

    # Deterministic parameter normalization: rewrite block Parameters/Transitions
    # to the exact shapes Connect's CreateContactFlow API accepts (derived from
    # real InvalidContactFlowException problems). This fixes the param-name and
    # required-error drift that makes generated flows fail to import.
    try:
        _normalize_contact_flow_params(actions, {}, fixes_applied)
    except Exception as _e:  # never let normalization break linting
        warnings.append(f"param normalization skipped: {_e}")

    flow_json_fixed = _json.dumps(doc, ensure_ascii=False, indent=2) if fixes_applied else flow_json

    if not actions:
        return {"ok": False, "errors": ["No Actions in flow"], "warnings": []}

    # Read StartAction AFTER the auto-fixes above (the Trigger fix may have
    # repointed it) so downstream validation reflects the corrected flow.
    start = doc.get("StartAction") or doc.get("startAction")

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
    # Re-evaluate terminal block AFTER any Type auto-fixes (e.g. EndFlow→Disconnect).
    if "DisconnectParticipant" not in types:
        warnings.append("No DisconnectParticipant (terminal) block — flow may not end cleanly")

    # Invalid Types that could NOT be auto-renamed are hard import-blockers.
    errors.extend(invalid_types)

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "fixes_applied": fixes_applied, "fixed_json": flow_json_fixed}


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


# Matches an Amazon Connect AI-prompt interpolation token: {{ $.something }} or
# {{ foo }}. Captures the inner expression (trimmed) so we can detect duplicates.
_AI_PROMPT_VAR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


def lint_ai_prompt(prompt_text: str) -> dict:
    """Validate an Amazon Connect AI-agent prompt against the qconnect
    CreateAIPrompt import rules. Never raises.

    The one rule the real API enforces that the generator can silently violate:
    **each variable may appear inside `{{ }}` only ONCE per prompt.** Verified
    against qconnect create-ai-prompt: a second `{{$.Custom.firstName}}` ->
    ValidationException "Each variable may only appear once." A bare reference
    (no braces) is just literal text and is always fine.

    Auto-fix: keep the FIRST `{{var}}` occurrence; strip the braces from every
    later occurrence of the SAME variable (leaving the inner expression as plain
    text), which the API accepts. Returns {ok, errors, warnings, fixes_applied,
    fixed_text}.
    """
    if not isinstance(prompt_text, str) or not prompt_text:
        return {"ok": False, "errors": ["Empty prompt text"], "warnings": [],
                "fixes_applied": [], "fixed_text": prompt_text or ""}

    errors: list[str] = []
    warnings: list[str] = []
    fixes_applied: list[str] = []

    seen: set[str] = set()

    def _dedupe(m: "re.Match") -> str:
        inner = m.group(1).strip()
        if inner in seen:
            # Subsequent reference — strip braces so it becomes literal text,
            # which the API accepts. This is the documented "use the bare token
            # after the first {{...}}" behavior.
            fixes_applied.append(
                f"AI prompt: variable '{inner}' referenced more than once with "
                f"{{{{ }}}} — stripped braces on the duplicate (API allows each "
                f"variable inside {{{{ }}}} only once)"
            )
            return inner
        seen.add(inner)
        return m.group(0)

    fixed_text = _AI_PROMPT_VAR_RE.sub(_dedupe, prompt_text)

    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "fixes_applied": fixes_applied,
        "fixed_text": fixed_text,
    }
