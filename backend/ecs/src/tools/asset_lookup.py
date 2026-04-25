"""
Asset Lookup Tool

Retrieves generated assets from S3 for review and validation.
Used by Orchestrator Agent and Sub-Agents (especially reviewer_agent).

Supported asset types:
- lambda: Lambda handler.py files
- openapi: OpenAPI specification YAML
- prompt: AI agent prompt markdown
- contact_flow: Contact Flow JSON
- cloudformation: CloudFormation YAML (also: infrastructure, cdk)
- mermaid: Mermaid diagram markdown
- faq: Knowledge Base FAQ documents

S3 Key Format:
- assets/{session_id}/{asset_type}/{file_name}
- assets/{session_id}/{asset_type}/{operation_id}/{file_name}

Usage:
    from tools.asset_lookup import asset_lookup

    # List all assets for a session
    result = asset_lookup(session_id="session-abc123")

    # Filter by asset type
    result = asset_lookup(session_id="session-abc123", asset_type="lambda")

    # Get specific Lambda function
    result = asset_lookup(
        session_id="session-abc123",
        asset_type="lambda",
        operation_id="create_reservation"
    )
"""

import logging
from typing import Optional, List, Dict, Any
from collections import defaultdict
from strands import tool

from .s3_asset_storage import (
    list_session_assets,
    get_asset_from_s3,
    _is_binary_file,
)

logger = logging.getLogger(__name__)

# Asset type aliases
ASSET_TYPE_ALIASES = {
    "infrastructure": "cloudformation",
    "cdk": "cloudformation",
    "contact-flow": "contact_flow",
    "mermaid": "contact_flow",  # Mermaid diagrams are stored with contact_flow
}


def _parse_s3_key(s3_key: str) -> Dict[str, str]:
    """
    Parse S3 key to extract asset metadata.

    S3 key formats:
    - assets/{session_id}/{asset_type}/{file_name}
    - assets/{session_id}/{asset_type}/{operation_id}/{file_name}

    Args:
        s3_key: S3 key path

    Returns:
        Dict with session_id, asset_type, operation_id, file_name
    """
    parts = s3_key.split('/')

    # Minimum: assets/session/type/filename
    if len(parts) < 4:
        return {}

    # parts[0] = 'assets'
    # parts[1] = session_id
    # parts[2] = asset_type
    # parts[3+] = operation_id/file_name or just file_name

    result = {
        "s3_key": s3_key,
        "session_id": parts[1],
        "asset_type": parts[2],
    }

    if len(parts) == 4:
        # No operation_id: assets/session/type/filename
        result["operation_id"] = None
        result["file_name"] = parts[3]
    elif len(parts) >= 5:
        # With operation_id: assets/session/type/op_id/filename
        result["operation_id"] = parts[3]
        result["file_name"] = parts[-1]

    return result


def _normalize_asset_type(asset_type: str) -> str:
    """Normalize asset type using aliases."""
    asset_type = asset_type.lower().strip()
    return ASSET_TYPE_ALIASES.get(asset_type, asset_type)


def _truncate_content(content: str, max_length: int) -> tuple:
    """
    Truncate content if it exceeds max_length.

    Returns:
        Tuple of (truncated_content, was_truncated, remaining_chars)
    """
    if len(content) <= max_length:
        return content, False, 0

    remaining = len(content) - max_length
    truncated = content[:max_length] + f"\n\n[TRUNCATED: {remaining} characters remaining]"
    return truncated, True, remaining


def _group_assets_by_type(assets: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """
    Group assets by type for summary.

    Returns:
        Dict mapping asset_type to list of identifiers (operation_id or file_name)
    """
    grouped = defaultdict(list)
    for asset in assets:
        asset_type = asset.get("asset_type", "unknown")
        operation_id = asset.get("operation_id")
        file_name = asset.get("file_name", "")

        identifier = operation_id if operation_id else file_name
        if identifier and identifier not in grouped[asset_type]:
            grouped[asset_type].append(identifier)

    return dict(grouped)


def _generate_summary(grouped: Dict[str, List[str]], total_count: int) -> str:
    """Generate human-readable summary of assets."""
    if total_count == 0:
        return "No assets found"

    parts = []
    for asset_type, items in grouped.items():
        count = len(items)
        parts.append(f"{count} {asset_type}")

    return f"Found {total_count} assets: " + ", ".join(parts)


@tool
def asset_lookup(
    session_id: str,
    asset_type: Optional[str] = None,
    operation_id: Optional[str] = None,
    file_name: Optional[str] = None,
    max_content_length: int = 50000,
    include_content: bool = True
) -> dict:
    """
    Look up generated assets from S3 storage for review or validation.

    Use this tool when you need to:
    - Review generated code before packaging
    - Validate consistency across assets
    - Check field names or API paths
    - Compare Lambda code with OpenAPI spec

    Args:
        session_id: Session identifier to look up assets for
        asset_type: Filter by asset type (lambda, openapi, prompt, contact_flow, cloudformation, faq).
                   Use None to list all types.
        operation_id: Filter by operation ID (mainly for lambda functions).
                     Example: "create_reservation", "cancel_reservation"
        file_name: Specific file name to retrieve.
                  Example: "handler.py", "openapi.yaml"
        max_content_length: Maximum content length per asset (default 50KB).
                           Large files will be truncated with [TRUNCATED] marker.
        include_content: Whether to include file content (default True).
                        Set to False to just list assets without content.

    Returns:
        dict with:
        - success: bool - Whether lookup succeeded
        - assets: List of asset details with content
        - summary: Brief overview of what was found
        - count: Total number of assets found
        - grouped: Assets grouped by type for easy navigation
    """
    logger.info(f"[asset_lookup] Looking up assets for session: {session_id}, type: {asset_type}")

    if not session_id:
        return {
            "success": False,
            "error": "session_id is required",
            "assets": [],
            "summary": "Error: session_id is required",
            "count": 0,
        }

    try:
        # List all assets for the session
        s3_keys = list_session_assets(session_id)

        if not s3_keys:
            return {
                "success": True,
                "assets": [],
                "summary": f"No assets found for session {session_id}",
                "count": 0,
                "grouped": {},
            }

        # Parse and filter assets
        assets = []
        normalized_type = _normalize_asset_type(asset_type) if asset_type else None

        for s3_key in s3_keys:
            parsed = _parse_s3_key(s3_key)
            if not parsed:
                continue

            # Apply filters
            if normalized_type and parsed.get("asset_type") != normalized_type:
                continue

            if operation_id and parsed.get("operation_id") != operation_id:
                continue

            if file_name and parsed.get("file_name") != file_name:
                continue

            # Check if file is binary
            file_name_check = parsed.get("file_name", "")
            is_binary = _is_binary_file(file_name_check)
            parsed["is_binary"] = is_binary

            # Optionally include content
            if include_content:
                if is_binary:
                    # For binary files, just mark them without trying to read content
                    parsed["content"] = f"[BINARY FILE: {file_name_check}]"
                    parsed["truncated"] = False
                    parsed["content_length"] = 0
                else:
                    content = get_asset_from_s3(s3_key, allow_binary=True)
                    if content:
                        # Check if it's a binary placeholder
                        if content.startswith("[BINARY FILE:"):
                            parsed["content"] = content
                            parsed["truncated"] = False
                            parsed["content_length"] = 0
                            parsed["is_binary"] = True
                        else:
                            content, was_truncated, remaining = _truncate_content(content, max_content_length)
                            parsed["content"] = content
                            parsed["truncated"] = was_truncated
                            parsed["content_length"] = len(content)
                    else:
                        parsed["content"] = None
                        parsed["error"] = "Failed to retrieve content"

            assets.append(parsed)

        # Generate summary
        grouped = _group_assets_by_type(assets)
        summary = _generate_summary(grouped, len(assets))

        logger.info(f"[asset_lookup] {summary}")

        return {
            "success": True,
            "assets": assets,
            "summary": summary,
            "count": len(assets),
            "grouped": grouped,
        }

    except Exception as e:
        logger.error(f"[asset_lookup] Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "assets": [],
            "summary": f"Error looking up assets: {str(e)}",
            "count": 0,
        }


# Utility function for internal use (not a tool)
def get_assets_for_review(session_id: str) -> Dict[str, Any]:
    """
    Get all assets for a session organized for review.

    This is a helper function for the reviewer_agent that returns
    assets organized by type with full content.

    Args:
        session_id: Session identifier

    Returns:
        Dict organized by asset type with file contents
    """
    result = asset_lookup(
        session_id=session_id,
        include_content=True,
        max_content_length=100000,  # Higher limit for review
    )

    if not result.get("success"):
        return {"error": result.get("error", "Unknown error")}

    organized = {
        "lambda": {},
        "openapi": None,
        "prompt": None,
        "contact_flow": None,
        "cloudformation": None,
        "faq": {},
    }

    for asset in result.get("assets", []):
        asset_type = asset.get("asset_type")
        content = asset.get("content")
        operation_id = asset.get("operation_id")
        file_name = asset.get("file_name")

        if asset_type == "lambda" and operation_id:
            organized["lambda"][operation_id] = content
        elif asset_type == "openapi":
            organized["openapi"] = content
        elif asset_type == "prompt":
            organized["prompt"] = content
        elif asset_type == "contact_flow" and file_name and file_name.endswith(".json"):
            organized["contact_flow"] = content
        elif asset_type in ("cloudformation", "infrastructure"):
            organized["cloudformation"] = content
        elif asset_type == "faq" and file_name:
            organized["faq"][file_name] = content

    return organized
