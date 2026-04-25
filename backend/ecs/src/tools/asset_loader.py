"""
Asset Loader Utility

Loads existing assets from S3 for modification workflows.
When a generator receives a modification_request, it uses this to load
the current version of the asset so the LLM can modify it in-place
rather than regenerating from scratch (which causes schema/structure loss).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def load_existing_asset(
    asset_type: str,
    operation_id: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Optional[str]:
    """
    Load an existing asset from S3 for the current session.

    Args:
        asset_type: Asset type (lambda, openapi, prompt, contact_flow, cloudformation)
        operation_id: Operation ID (for lambda, cloudformation fragments)
        file_name: Specific file name to match (e.g., "handler.py", "openapi.yaml")

    Returns:
        Asset content string, or None if not found
    """
    try:
        from tools.streaming_callback import get_session_id
        from tools.s3_asset_storage import list_session_assets, get_asset_from_s3

        session_id = get_session_id()
        if not session_id:
            logger.warning("[ASSET_LOADER] No session_id available")
            return None

        keys = list_session_assets(session_id)
        if not keys:
            return None

        for key in keys:
            parts = key.split('/')
            if len(parts) < 4:
                continue

            key_type = parts[2]
            if key_type != asset_type:
                continue

            # Match operation_id if specified
            if operation_id:
                key_op = parts[3] if len(parts) >= 5 else None
                if key_op != operation_id:
                    continue

            # Match file_name if specified
            key_file = parts[-1]
            if file_name and key_file != file_name:
                continue

            content = get_asset_from_s3(key)
            if content:
                logger.info(f"[ASSET_LOADER] Loaded existing asset: {key} ({len(content)} chars)")
                return content

        logger.info(f"[ASSET_LOADER] No existing asset found: type={asset_type}, op={operation_id}, file={file_name}")
        return None

    except Exception as e:
        logger.warning(f"[ASSET_LOADER] Failed to load: {e}")
        return None
