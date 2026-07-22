"""
Deterministic Infrastructure Fragment Merger

Merges base CloudFormation YAML with per-operation YAML fragments.
No LLM involved - pure Python string manipulation.

Reads fragments from the infrastructure_generator_agent's internal registry,
so the orchestrator LLM doesn't need to pass huge YAML strings as tool params.

Usage by Orchestrator:
    merge_infrastructure_fragments(project_name="sunny-hotel")
"""

import json
import logging
import re
from strands import tool

logger = logging.getLogger(__name__)

ANCHOR_COMMENT = "# --- ADDITIONAL RESOURCES ANCHOR ---"


def _clean_fragment(fragment: str) -> str:
    """Extract YAML content from a fragment, stripping markdown fences if present."""
    match = re.search(r'```(?:yaml|yml)?\s*\n(.*?)```', fragment, re.DOTALL)
    if match:
        return match.group(1).rstrip("\n")
    stripped = fragment.strip()
    return stripped


def _merge_at_anchor(base_yaml: str, fragments: list[str]) -> tuple[str, dict]:
    """Insert all fragments at the anchor comment position in base YAML.

    Returns (merged_yaml, info) where info records the insertion strategy used
    and how many fragments were empty/skipped — so the caller can surface a
    warning instead of silently producing a structurally-wrong template (the
    "append to end" fallback in particular usually means the base template was
    malformed).
    """
    combined = ""
    skipped = 0
    for i, frag in enumerate(fragments):
        cleaned = _clean_fragment(frag)
        if not cleaned:
            logger.warning(f"[MERGE] Fragment {i} is empty, skipping")
            skipped += 1
            continue
        combined += cleaned.rstrip("\n") + "\n\n"

    if not combined.strip():
        logger.warning("[MERGE] No valid fragments to merge")
        return base_yaml, {"strategy": "none", "skipped": skipped, "degraded": True}

    anchor_idx = base_yaml.find(ANCHOR_COMMENT)
    if anchor_idx != -1:
        merged = base_yaml[:anchor_idx] + combined + base_yaml[anchor_idx:]
        logger.info(f"[MERGE] Inserted {len(combined)} chars at anchor comment")
        return merged, {"strategy": "anchor", "skipped": skipped, "degraded": False}

    deploy_idx = base_yaml.find("\n  ApiDeployment:")
    if deploy_idx != -1:
        merged = base_yaml[:deploy_idx + 1] + combined + base_yaml[deploy_idx + 1:]
        logger.info(f"[MERGE] Inserted at ApiDeployment fallback")
        return merged, {"strategy": "apideployment_fallback", "skipped": skipped, "degraded": False}

    outputs_idx = base_yaml.find("\nOutputs:")
    if outputs_idx != -1:
        merged = base_yaml[:outputs_idx + 1] + combined + base_yaml[outputs_idx + 1:]
        logger.info(f"[MERGE] Inserted at Outputs fallback")
        return merged, {"strategy": "outputs_fallback", "skipped": skipped, "degraded": False}

    logger.warning("[MERGE] No insertion point found, appending to end (DEGRADED — base template likely malformed)")
    return base_yaml.rstrip("\n") + "\n\n" + combined, {"strategy": "append_eof", "skipped": skipped, "degraded": True}


def _remove_anchor_comment(yaml_str: str) -> str:
    """Remove the anchor comment line from final output."""
    return "\n".join(
        line for line in yaml_str.split("\n") if ANCHOR_COMMENT not in line
    )


# IAM actions for Amazon Connect AI agents (formerly Amazon Q in Connect) live
# under the `wisdom:` namespace. `qconnect:` is NOT a real IAM namespace and
# causes AccessDenied at runtime. LLMs recurrently emit `qconnect:` despite the
# prompt forbidding it (workshop QA caught this repeatedly), so fix it
# deterministically at merge time rather than relying on the reviewer.
_QCONNECT_ACTION_RE = re.compile(r'qconnect:([A-Za-z]\w*)')


# The update_q_session Lambda resolves the Wisdom session ARN via
# connect:DescribeContact and writes via wisdom:UpdateSessionData at runtime.
# The system prompt instructs the LLM to emit an inline policy with these
# actions on UpdateQSessionRole, but the LLM recurrently omits it, leaving only
# AWSLambdaBasicExecutionRole — which fails mid-call with AccessDeniedException
# (verified in live workshop QA). This deterministic backstop injects the
# inline policy whenever UpdateQSessionRole lacks the required actions.
_QSESSION_POLICY_BLOCK = """\
      Policies:
        - PolicyName: UpdateQSessionRuntimePolicy
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action:
                  - wisdom:UpdateSessionData
                  - wisdom:GetSession
                  - wisdom:ListSessions
                  - connect:DescribeContact
                  - connect:GetContactAttributes
                Resource: "*"
"""


def _ensure_qsession_role_permissions(yaml_str: str) -> str:
    """Ensure UpdateQSessionRole carries connect:DescribeContact + wisdom perms.

    If an UpdateQSessionRole resource exists but its section does not mention
    connect:DescribeContact, append the inline runtime policy right after its
    ManagedPolicyArns block (same indentation level as other Properties keys).
    """
    m = re.search(r'(^  UpdateQSessionRole:\n(?:^(?:    |\n).*\n?)*)', yaml_str, re.M)
    if not m:
        return yaml_str
    block = m.group(1)
    if 'connect:DescribeContact' in block:
        return yaml_str
    mp = re.search(
        r'(^      ManagedPolicyArns:\n(?:^        .*\n)+)', block, re.M)
    if not mp:
        return yaml_str
    new_block = block.replace(mp.group(1), mp.group(1) + _QSESSION_POLICY_BLOCK, 1)
    logger.info("[MERGE] Injected UpdateQSessionRuntimePolicy into UpdateQSessionRole "
                "(missing connect:DescribeContact — recurrent LLM omission)")
    return yaml_str.replace(block, new_block, 1)


def _fix_qconnect_namespace(yaml_str: str) -> str:
    """Rewrite `qconnect:Action` IAM actions to `wisdom:Action`.

    Only rewrites the action-namespace form (`qconnect:` followed by an action
    name), which is always wrong in IAM. Leaves any other occurrence untouched.
    """
    new_yaml, n = _QCONNECT_ACTION_RE.subn(r'wisdom:\1', yaml_str)
    if n:
        logger.info(f"[MERGE] Rewrote {n}x qconnect: → wisdom: (invalid IAM namespace)")
    return new_yaml


def _fix_customer_lookup_handler(yaml_str: str) -> str:
    """Force CustomerLookupFunction's Handler to index.handler.

    The Lambda Generator always emits ``def handler`` and deploy.sh replaces
    the placeholder code, but the LLM recurrently declares
    ``Handler: index.lambda_handler`` on CustomerLookupFunction — which only
    surfaces at call time as Runtime.HandlerNotFound (broke caller
    personalization in a live workshop). Other inline-code functions
    (seeder/api-key retriever) legitimately keep lambda_handler.
    """
    m = re.search(r'(^  CustomerLookupFunction:\n(?:^(?:    |\n).*\n?)*)', yaml_str, re.M)
    if not m:
        return yaml_str
    block = m.group(1)
    if 'Handler: index.lambda_handler' not in block:
        return yaml_str
    fixed = block.replace('Handler: index.lambda_handler', 'Handler: index.handler')
    logger.info("[MERGE] CustomerLookupFunction Handler: index.lambda_handler → index.handler "
                "(matches generated code entry point)")
    return yaml_str.replace(block, fixed, 1)


def _fix_common_property_hallucinations(yaml_str: str) -> str:
    """Fix common LLM-hallucinated CloudFormation property names.

    LLMs sometimes generate plausible-but-wrong property names.  This function
    applies deterministic regex replacements for known cases.
    """
    # (wrong_pattern, correct_replacement, label)
    FIXES = [
        # AWS::ApiGateway::Resource
        (r'(\s+)ParentResourceId:', r'\1ParentId:', 'ParentResourceId->ParentId'),
        # AWS::ApiGateway::Method / Resource — wrong ref names
        (r'(\s+)RestApiRef:', r'\1RestApiId:', 'RestApiRef->RestApiId'),
        (r'(\s+)ResourceRef:', r'\1ResourceId:', 'ResourceRef->ResourceId'),
        # AWS::Lambda::Permission
        (r'(\s+)FunctionRef:', r'\1FunctionName:', 'FunctionRef->FunctionName'),
        # Integration block
        (r'(\s+)IntegrationMethod:', r'\1IntegrationHttpMethod:', 'IntegrationMethod->IntegrationHttpMethod'),
        # British spelling
        (r'(\s+)PassthroughBehaviour:', r'\1PassthroughBehavior:', 'PassthroughBehaviour->PassthroughBehavior'),
        # AuthType shorthand
        (r'(\s+)AuthType:', r'\1AuthorizationType:', 'AuthType->AuthorizationType'),
    ]

    total_fixes = 0
    for pattern, replacement, label in FIXES:
        new_yaml = re.sub(pattern, replacement, yaml_str)
        if new_yaml != yaml_str:
            count = len(re.findall(pattern, yaml_str))
            total_fixes += count
            logger.info(f"[MERGE] Fixed {count}x {label}")
            yaml_str = new_yaml

    if total_fixes:
        logger.info(f"[MERGE] Total property hallucination fixes: {total_fixes}")
    return yaml_str


def _deduplicate_resources(yaml_str: str) -> str:
    """Remove duplicate CloudFormation resource blocks, keeping the first occurrence.

    Handles TWO types of duplicates:
    1. Same logical ID (e.g. two 'ToolsResource:' blocks)
    2. Same API Gateway Resource path — different logical IDs but identical
       (ParentId, PathPart) pairs.  e.g. 'ToolsResource' and
       'ToolsResourceForLookupBilling' both creating PathPart: tools under
       RootResourceId.  Keeps the first, removes the rest, and rewrites
       all !Ref / !GetAtt references that pointed at the removed duplicates.
    """
    # --- Phase 1: exact logical-ID dedup (original logic) ---
    lines = yaml_str.split("\n")
    seen_ids: set[str] = set()
    result_lines: list[str] = []
    skip_block = False
    removed: list[str] = []
    in_resources = False

    for line in lines:
        # Detect Resources: section
        if re.match(r'^Resources:\s*$', line):
            in_resources = True
            result_lines.append(line)
            continue

        # Detect end of Resources section (another top-level key like Outputs:)
        if in_resources and re.match(r'^[A-Z]\w+:', line) and not line.startswith('  '):
            in_resources = False
            skip_block = False

        if in_resources:
            # Top-level resource definition (2-space indent, e.g. "  MyResource:")
            m = re.match(r'^  (\w+):\s*$', line)
            if m:
                logical_id = m.group(1)
                if logical_id in seen_ids:
                    skip_block = True
                    removed.append(logical_id)
                    continue
                else:
                    seen_ids.add(logical_id)
                    skip_block = False
            elif skip_block:
                # Skip lines belonging to duplicate block
                # (lines with 4+ spaces indent, or blank lines within block)
                if line.strip() == '' or line.startswith('    '):
                    continue
                else:
                    # Non-indented or 2-space line = end of duplicate block
                    skip_block = False

        if not skip_block:
            result_lines.append(line)

    if removed:
        logger.info(f"[MERGE] Removed {len(removed)} duplicate resources: {removed}")

    yaml_str = "\n".join(result_lines)

    # --- Phase 2: API Gateway Resource path dedup ---
    # Each operation fragment may independently create a parent path resource
    # (e.g. ToolsResourceForBookMeeting with PathPart: tools) that duplicates
    # an earlier fragment's resource (e.g. ToolsResource with PathPart: tools).
    # CloudFormation rejects: "Another resource with the same parent already has this name".
    apigw_resources: list[dict] = []
    for m in re.finditer(
        r'^  (\w+):\s*\n'                     # logical ID
        r'\s+Type:\s*AWS::ApiGateway::Resource\s*\n'
        r'((?:\s+\S.*\n)*)',                   # property lines
        yaml_str, re.MULTILINE,
    ):
        logical_id = m.group(1)
        props_block = m.group(2)
        parent_match = re.search(r'ParentId:\s*(.+)', props_block)
        path_match = re.search(r'PathPart:\s*(\S+)', props_block)
        if parent_match and path_match:
            apigw_resources.append({
                "logical_id": logical_id,
                "parent_id": parent_match.group(1).strip(),
                "path_part": path_match.group(1).strip(),
            })

    # Group by (parent_id, path_part)
    path_groups: dict[tuple, list] = {}
    for res in apigw_resources:
        key = (res["parent_id"], res["path_part"])
        path_groups.setdefault(key, []).append(res)

    ref_rewrites: dict[str, str] = {}   # old_logical_id -> canonical_logical_id
    blocks_to_remove: list[str] = []

    for key, group in path_groups.items():
        if len(group) <= 1:
            continue
        canonical = group[0]
        for dup in group[1:]:
            ref_rewrites[dup["logical_id"]] = canonical["logical_id"]
            blocks_to_remove.append(dup["logical_id"])
            logger.info(
                f"[MERGE] Dedup API GW Resource: {dup['logical_id']} -> "
                f"{canonical['logical_id']} (PathPart: {key[1]})"
            )

    if blocks_to_remove:
        # Remove duplicate resource blocks
        for block_id in blocks_to_remove:
            yaml_str = re.sub(
                r'^\s*' + re.escape(block_id) + r':\s*\n'
                r'(?:\s+Type:\s*AWS::ApiGateway::Resource\s*\n)'
                r'(?:\s+\S.*\n)*',
                '',
                yaml_str,
                flags=re.MULTILINE,
            )

        # Rewrite !Ref / !GetAtt references to removed resources
        for old_id, new_id in ref_rewrites.items():
            yaml_str = yaml_str.replace(f'!Ref {old_id}', f'!Ref {new_id}')
            yaml_str = yaml_str.replace(f'!GetAtt {old_id}.', f'!GetAtt {new_id}.')

        logger.info(
            f"[MERGE] Removed {len(blocks_to_remove)} duplicate API GW Resource blocks, "
            f"rewrote {len(ref_rewrites)} references"
        )

    return yaml_str


_APIENDPOINT_TOOLS_RE = re.compile(
    r"(Value:\s*!Sub\s*['\"]https://\$\{RestApi\}\.execute-api\."
    r"\$\{AWS::Region\}\.amazonaws\.com/\$\{Environment\})/tools(['\"])"
)


def _strip_tools_from_api_endpoint(yaml_str: str) -> str:
    """Remove trailing `/tools` from the ApiEndpoint Output value.

    OpenAPI `paths` already carry `/tools/<op>`, and `deploy.sh` substitutes
    `ApiEndpoint` verbatim into `servers.url`. If the LLM appends `/tools` to
    the stage root, the composed runtime URL becomes `.../tools/tools/<op>`,
    producing API Gateway 403 "Missing Authentication Token" (route not found).

    Deterministic safety net: the base-mode prompt forbids this, but LLMs
    occasionally ignore it. Fix at merge time so the reviewer doesn't have to.
    """
    fixed = _APIENDPOINT_TOOLS_RE.sub(r"\1\2", yaml_str)
    if fixed != yaml_str:
        logger.info("[MERGE] Stripped trailing '/tools' from ApiEndpoint Output value")
    return fixed


def _fix_api_deployment_depends_on(yaml_str: str) -> str:
    """Rewrite ApiDeployment's DependsOn with actual Method/Options resources.

    Fixes both wrong resource names AND bad indentation from LLM output.
    """
    # Find all *Method and *Options resource definitions (2-space indented top-level)
    actual_resources = re.findall(r'^  (\w+(?:Method|Options)):$', yaml_str, re.MULTILINE)
    if not actual_resources:
        return yaml_str

    # Rebuild the entire ApiDeployment block line by line
    lines = yaml_str.split("\n")
    new_lines = []
    in_api_deployment = False
    in_depends_on = False
    done = False

    for line in lines:
        if done:
            new_lines.append(line)
            continue

        # Detect ApiDeployment start
        if re.match(r'^  ApiDeployment:\s*$', line):
            in_api_deployment = True
            new_lines.append(line)
            continue

        if in_api_deployment:
            stripped = line.strip()

            # Skip any existing DependsOn line (regardless of indentation)
            if stripped == 'DependsOn:':
                in_depends_on = True
                continue
            # Skip existing DependsOn items
            if in_depends_on and stripped.startswith('- '):
                continue
            # End of DependsOn items — inject corrected block here
            if in_depends_on:
                in_depends_on = False
                new_lines.append("    DependsOn:")
                for r in actual_resources:
                    new_lines.append(f"      - {r}")
                in_api_deployment = False
                done = True
                new_lines.append(line)
                continue

            # If we hit Properties before DependsOn, inject DependsOn before it
            if stripped.startswith('Properties:'):
                new_lines.append("    DependsOn:")
                for r in actual_resources:
                    new_lines.append(f"      - {r}")
                in_api_deployment = False
                done = True
                new_lines.append(line)
                continue

            new_lines.append(line)
        else:
            new_lines.append(line)

    result = "\n".join(new_lines)
    if result != yaml_str:
        logger.info(f"[MERGE] Fixed ApiDeployment DependsOn with {len(actual_resources)} resources")
    return result


@tool
def merge_infrastructure_fragments(project_name: str) -> dict:
    """
    Merge base CloudFormation template with operation YAML fragments.

    This is a deterministic tool (no LLM). It reads the base template and
    operation fragments from the infrastructure_generator_agent's internal
    registry (stored during mode="base" and mode="operation" calls),
    merges them, and streams the final result to the frontend.

    Args:
        project_name: Project name (must match the project_name used in
                      prior infrastructure_generator_agent calls)

    Returns:
        dict with success status and merge info
    """
    from agents.infrastructure_generator.agent import get_fragments, clear_fragments

    logger.info(f"[MERGE] Starting merge for {project_name}")

    data = get_fragments(project_name)
    if not data or not data.get("base"):
        return {"success": False, "error": f"No base template found for {project_name}. Call infrastructure_generator_agent(mode='base') first."}

    base_yaml = data["base"]
    fragment_keys = list(data.get("fragments", {}).keys())
    fragments = list(data.get("fragments", {}).values())

    if not fragments:
        return {"success": False, "error": f"No operation fragments found for {project_name}. Call infrastructure_generator_agent(mode='operation') first."}

    logger.info(f"[MERGE] Merging {len(fragments)} fragments into base ({len(base_yaml)} chars)")

    merged, merge_info = _merge_at_anchor(base_yaml, fragments)
    final_yaml = _remove_anchor_comment(merged)
    final_yaml = _fix_common_property_hallucinations(final_yaml)
    final_yaml = _fix_qconnect_namespace(final_yaml)
    final_yaml = _ensure_qsession_role_permissions(final_yaml)
    final_yaml = _fix_customer_lookup_handler(final_yaml)
    final_yaml = _deduplicate_resources(final_yaml)
    final_yaml = _fix_api_deployment_depends_on(final_yaml)
    final_yaml = _strip_tools_from_api_endpoint(final_yaml)

    logger.info(f"[MERGE] Final template: {len(final_yaml)} chars")

    # NOTE: cfn-lint runs AFTER streaming/saving (further below). cfn-lint is a
    # CPU-bound call that blocks the event loop for several seconds on a large
    # template; running it here (before stream_asset) delayed/dropped the
    # infrastructure.yaml asset_preview events on the frontend. We stream the
    # deterministically-merged template first (instant preview, original UX),
    # then lint and re-stream only if the autofix actually changed something.
    lint_result = {"errors": [], "warnings": [], "fixes_applied": [], "available": False}

    # --- Completeness verification (silent-failure backstop) ---
    # Confirm every fragment's top-level logical IDs survived into the final
    # template. If a fragment got dropped by a bad anchor/dedup, this surfaces it
    # instead of letting a silently-incomplete template through.
    missing_resources: list[str] = []
    try:
        for fk, frag in zip(fragment_keys, fragments):
            cleaned = _clean_fragment(frag)
            frag_ids = re.findall(r'^  (\w+):\s*$', cleaned, re.MULTILINE)
            for lid in frag_ids:
                # present as a top-level resource definition in the final yaml?
                if not re.search(r'^  ' + re.escape(lid) + r':\s*$', final_yaml, re.MULTILINE):
                    missing_resources.append(f"{fk}:{lid}")
        if missing_resources:
            logger.warning(
                f"[MERGE] {len(missing_resources)} fragment resource(s) missing from "
                f"final template: {missing_resources[:10]}"
            )
    except Exception as e:
        logger.warning(f"[MERGE] completeness check failed (non-fatal): {e}")

    if merge_info.get("degraded"):
        logger.warning(f"[MERGE] DEGRADED merge strategy={merge_info.get('strategy')}")

    # Stream to frontend + save to S3
    try:
        from tools.streaming_callback import stream_asset, clear_asset_preview_cache, get_session_id
        from tools.s3_asset_storage import save_asset_to_s3

        # Clear stale incomplete base/fragment previews (suppress_complete=True leaves them incomplete)
        # Without this, 2nd generation appends to existing incomplete previews
        clear_asset_preview_cache("cloudformation", "infrastructure-base.yaml", project_name)
        for fk in fragment_keys:
            clear_asset_preview_cache("cloudformation", f"{fk}-fragment.yaml", project_name)

        clear_asset_preview_cache("cloudformation", "infrastructure.yaml", project_name)

        MAX_CHUNK = 15000
        for i in range(0, len(final_yaml), MAX_CHUNK):
            chunk_end = min(i + MAX_CHUNK, len(final_yaml))
            is_last = chunk_end >= len(final_yaml)
            stream_asset("cloudformation", "infrastructure.yaml", final_yaml[:chunk_end],
                         operation_id=project_name, is_complete=is_last)

        session_id = get_session_id()
        if session_id:
            s3_key = save_asset_to_s3(
                session_id=session_id, asset_type="cloudformation",
                file_name="infrastructure.yaml", content=final_yaml,
                operation_id=project_name,
            )
            logger.info(f"[MERGE] Saved to S3: {s3_key}")

            # Save static update-q-session Lambda (fixed code, not LLM-generated)
            from .asset_packager import UPDATE_Q_SESSION_LAMBDA_CODE
            uqs_key = save_asset_to_s3(
                session_id=session_id, asset_type="lambda",
                file_name="index.js", content=UPDATE_Q_SESSION_LAMBDA_CODE,
                operation_id="update_q_session",
            )
            if uqs_key:
                logger.info(f"[MERGE] Saved static update-q-session to S3: {uqs_key}")
                stream_asset("lambda", "index.js", UPDATE_Q_SESSION_LAMBDA_CODE,
                             operation_id="update_q_session", is_complete=True)
    except Exception as e:
        logger.error(f"[MERGE] Streaming/S3 error: {e}")

    # cfn-lint gate (runs AFTER the initial stream/save so the preview is
    # instant). Auto-fixes recurring syntax issues (e.g. !Sub in a string-only
    # Description — E1004) and records remaining errors. If the autofix changed
    # the template, re-stream + re-save the corrected version. Fault-tolerant:
    # any failure leaves the already-streamed template in place.
    try:
        from .asset_linters import lint_and_autofix_cfn
        lint_result = lint_and_autofix_cfn(final_yaml)
        fixed_yaml = lint_result["fixed_yaml"]
        if fixed_yaml != final_yaml:
            final_yaml = fixed_yaml
            logger.info("[MERGE] cfn-lint autofix changed template — re-streaming")
            try:
                from tools.streaming_callback import stream_asset, clear_asset_preview_cache, get_session_id
                from tools.s3_asset_storage import save_asset_to_s3
                clear_asset_preview_cache("cloudformation", "infrastructure.yaml", project_name)
                MAX_CHUNK = 15000
                for i in range(0, len(final_yaml), MAX_CHUNK):
                    chunk_end = min(i + MAX_CHUNK, len(final_yaml))
                    stream_asset("cloudformation", "infrastructure.yaml", final_yaml[:chunk_end],
                                 operation_id=project_name, is_complete=chunk_end >= len(final_yaml))
                _sid = get_session_id()
                if _sid:
                    save_asset_to_s3(session_id=_sid, asset_type="cloudformation",
                                     file_name="infrastructure.yaml", content=final_yaml,
                                     operation_id=project_name)
            except Exception as e:
                logger.error(f"[MERGE] cfn-lint re-stream/save failed (non-fatal): {e}")
    except Exception as e:
        logger.error(f"[MERGE] cfn-lint gate failed (non-fatal): {e}")

    # Clean up registry
    clear_fragments(project_name)

    cfn_errors = lint_result.get("errors", [])
    merge_degraded = merge_info.get("degraded", False)
    result = {
        "success": True,
        "project_name": project_name,
        "fragment_count": len(fragments),
        "total_chars": len(final_yaml),
        "merge_strategy": merge_info.get("strategy"),
        "merge_degraded": merge_degraded,
        "fragments_skipped": merge_info.get("skipped", 0),
        "missing_resources": missing_resources[:20],
        "lint_available": lint_result.get("available", False),
        "lint_fixes_applied": lint_result.get("fixes_applied", []),
        "lint_errors": cfn_errors[:20],
        "lint_error_count": len(cfn_errors),
        "summary": (
            f"Merged {len(fragments)} operation fragments into infrastructure.yaml "
            f"({len(final_yaml)} chars). "
            f"cfn-lint: {len(lint_result.get('fixes_applied', []))} auto-fix(es), "
            f"{len(cfn_errors)} error(s) remaining"
            + (
                " — REVIEW/PATCH the lint_errors before deploying."
                if cfn_errors else "."
            )
            + (
                f" ⚠️ {len(missing_resources)} fragment resource(s) MISSING from the "
                "template — a fragment may have been dropped; inspect before deploying."
                if missing_resources else ""
            )
            + (
                f" ⚠️ DEGRADED merge ({merge_info.get('strategy')}): base template may be malformed."
                if merge_degraded else ""
            )
        ),
    }
    return result
