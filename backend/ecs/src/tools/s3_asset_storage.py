"""
S3 Asset Storage Utility

Stores generated assets to S3 for persistent storage and download.
Path format: assets/{session_id}/{asset_type}/{operation_id}/{file_name}

Supports NFS fast-path when S3FILES_MOUNT_PATH is set (ECS mode).
In NFS mode, files are written to local NFS mount. S3 upload still happens
as a backup.

This module is used by streaming_callback.py to persist assets when generation
is complete (is_complete=True).

Usage:
    from tools.s3_asset_storage import save_asset_to_s3, get_asset_from_s3

    # Save an asset
    s3_key = save_asset_to_s3(
        session_id="session-abc123",
        asset_type="lambda",
        file_name="handler.py",
        content="def handler(event, context): ...",
        operation_id="create_reservation"
    )

    # Retrieve an asset
    content = get_asset_from_s3(s3_key)
"""
import os
import boto3
import logging
from typing import Optional
from pathlib import Path
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Lazy-initialized S3 client
_s3_client = None

# NFS fast-path configuration
S3FILES_MOUNT = os.environ.get("S3FILES_MOUNT_PATH", "")


def _nfs_available() -> bool:
    """Check if NFS mount is available."""
    return bool(S3FILES_MOUNT) and os.path.isdir(S3FILES_MOUNT)


def _nfs_asset_path(session_id: str, asset_type: str, file_name: str,
                     operation_id: Optional[str] = None) -> Path:
    """Build NFS path for an asset (flat structure, no versioning)."""
    safe_session = session_id.replace("..", "_").replace("/", "_")
    safe_type = asset_type.replace("..", "_").replace("/", "_")
    safe_file = file_name.replace("..", "_")

    base = Path(S3FILES_MOUNT) / "sessions" / safe_session / "assets" / safe_type
    if operation_id:
        safe_op = operation_id.replace("..", "_").replace("/", "_")
        base = base / safe_op

    return base / safe_file


def _save_to_nfs(session_id: str, asset_type: str, file_name: str,
                  content: str, operation_id: Optional[str] = None) -> bool:
    """Save asset to NFS mount (fast-path)."""
    if not _nfs_available():
        return False
    try:
        path = _nfs_asset_path(session_id, asset_type, file_name, operation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.debug(f"[NFS] Saved: {path} ({len(content)} bytes)")
        return True
    except Exception as e:
        logger.warning(f"[NFS] Save failed: {e}")
        return False


def _get_from_nfs(session_id: str, asset_type: str, file_name: str,
                   operation_id: Optional[str] = None) -> Optional[str]:
    """Read asset from NFS mount (fast-path)."""
    if not _nfs_available():
        return None
    try:
        path = _nfs_asset_path(session_id, asset_type, file_name, operation_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"[NFS] Read failed: {e}")
    return None


def _parse_s3_key_to_nfs_components(s3_key: str):
    """
    Parse an S3 key into NFS path components.

    Expected formats:
        assets/{session_id}/{asset_type}/{file_name}
        assets/{session_id}/{asset_type}/{operation_id}/{file_name}

    Returns:
        Tuple of (session_id, asset_type, file_name, operation_id) or
        (None, None, None, None) if key cannot be parsed.
    """
    if not s3_key:
        return (None, None, None, None)

    parts = s3_key.split('/')

    # Must start with "assets" and have at least 4 parts
    if len(parts) < 4 or parts[0] != "assets":
        return (None, None, None, None)

    session_id = parts[1]
    asset_type = parts[2]

    # Skip state keys (e.g., assets/{sid}/state/...) — handled by S3 only
    if asset_type == "state":
        return (None, None, None, None)

    if len(parts) == 4:
        # assets/{sid}/{type}/{file}
        file_name = parts[3]
        return (session_id, asset_type, file_name, None)
    elif len(parts) >= 5:
        # assets/{sid}/{type}/{op_id}/{file}
        operation_id = parts[3]
        file_name = parts[-1]
        return (session_id, asset_type, file_name, operation_id)

    return (None, None, None, None)


def _list_nfs_assets(session_id: str):
    """
    Walk NFS workspace directory and return S3-key-format strings.

    Returns:
        List of S3-key-format strings if NFS is available, None if unavailable.
        Returns [] if NFS is available but directory is empty or doesn't exist.
    """
    if not _nfs_available():
        return None

    try:
        safe_session = session_id.replace("..", "_").replace("/", "_")
        session_dir = Path(S3FILES_MOUNT) / "sessions" / safe_session

        # If session directory doesn't exist on NFS at all, fall back to S3
        # (session may have been created on a different instance)
        if not session_dir.exists():
            return None

        assets_dir = session_dir / "assets"

        if not assets_dir.exists():
            # Assets dir missing — may not have been written to NFS yet
            # (e.g., session restored from S3 after redeployment).
            # Fall through to S3 to check there.
            return None

        keys = []
        for root, dirs, files in os.walk(assets_dir):
            for file_name in files:
                # Skip hidden/temp files
                if file_name.startswith('.') or file_name.endswith('.tmp'):
                    continue

                file_path = Path(root) / file_name
                # Build relative path from the assets directory
                rel_path = file_path.relative_to(assets_dir)
                rel_parts = rel_path.parts

                # Use sanitized session_id to match S3 key format from build_s3_key()
                if len(rel_parts) == 2:
                    # {type}/{file}
                    s3_key = f"assets/{safe_session}/{rel_parts[0]}/{rel_parts[1]}"
                elif len(rel_parts) >= 3:
                    # {type}/{op_id}/{file}
                    s3_key = f"assets/{safe_session}/{'/'.join(rel_parts)}"
                else:
                    # File directly in assets dir (unexpected) — skip
                    continue

                keys.append(s3_key)

        logger.debug(f"[NFS] Listed {len(keys)} assets for session {session_id}")
        return keys

    except (IOError, OSError) as e:
        logger.warning(f"[NFS] Failed to list assets: {e}")
        return None
    except Exception as e:
        logger.warning(f"[NFS] Unexpected error listing assets: {e}")
        return None


def _get_binary_from_nfs(session_id: str, asset_type: str, file_name: str,
                          operation_id: Optional[str] = None) -> Optional[bytes]:
    """Read binary asset from NFS mount."""
    if not _nfs_available():
        return None
    try:
        path = _nfs_asset_path(session_id, asset_type, file_name, operation_id)
        if path.exists():
            with open(path, "rb") as f:
                return f.read()
    except (IOError, OSError) as e:
        logger.warning(f"[NFS] Binary read failed: {e}")
    return None


def get_s3_client():
    """Get or create S3 client (lazy initialization)."""
    global _s3_client
    if _s3_client is None:
        region = os.environ.get('AWS_REGION', 'ap-northeast-1')
        _s3_client = boto3.client('s3', region_name=region)
        logger.info(f"S3 client initialized for region: {region}")
    return _s3_client


def get_bucket_name() -> Optional[str]:
    """Get the assets bucket name from environment variable."""
    bucket = os.environ.get('ASSETS_BUCKET_NAME')
    if not bucket:
        logger.warning("ASSETS_BUCKET_NAME environment variable not set")
    return bucket


def build_s3_key(
    session_id: str,
    asset_type: str,
    file_name: str,
    operation_id: Optional[str] = None
) -> str:
    """
    Build S3 key for asset storage.

    Args:
        session_id: User session ID
        asset_type: Type of asset (lambda, openapi, prompt, contact_flow, etc.)
        file_name: Name of the file
        operation_id: Optional operation identifier for grouping

    Returns:
        S3 key path

    Examples:
        - assets/session-abc/lambda/create_reservation/handler.py
        - assets/session-abc/openapi/openapi.yaml
        - assets/session-abc/prompt/ai_agent_prompt.yaml
    """
    # Sanitize inputs to prevent path traversal
    session_id = session_id.replace('/', '_').replace('..', '_')
    asset_type = asset_type.replace('/', '_').replace('..', '_')
    file_name = file_name.replace('/', '_').replace('..', '_')

    if operation_id:
        operation_id = operation_id.replace('/', '_').replace('..', '_')
        return f"assets/{session_id}/{asset_type}/{operation_id}/{file_name}"

    return f"assets/{session_id}/{asset_type}/{file_name}"


def _get_content_type(asset_type: str, file_name: str) -> str:
    """Determine content type based on asset type and file name."""
    # Check file extension first
    if file_name.endswith('.py'):
        return 'text/x-python'
    elif file_name.endswith('.yaml') or file_name.endswith('.yml'):
        return 'text/yaml'
    elif file_name.endswith('.json'):
        return 'application/json'
    elif file_name.endswith('.md'):
        return 'text/markdown'
    elif file_name.endswith('.txt'):
        return 'text/plain'
    elif file_name.endswith('.zip'):
        return 'application/zip'

    # Fall back to asset type
    content_type_map = {
        'lambda': 'text/x-python',
        'openapi': 'text/yaml',
        'prompt': 'text/markdown',
        'contact_flow': 'application/json',
        'cloudformation': 'text/yaml',
        'infrastructure': 'text/yaml',
        'cdk': 'text/yaml',  # Legacy - now using cloudformation
        'faq': 'text/plain',
        'operations': 'application/json',
        'company': 'application/json',
    }
    return content_type_map.get(asset_type, 'text/plain')


def save_asset_to_s3(
    session_id: str,
    asset_type: str,
    file_name: str,
    content: str,
    operation_id: Optional[str] = None,
    content_type: Optional[str] = None
) -> Optional[str]:
    """
    Save asset content to S3.

    Args:
        session_id: User session ID
        asset_type: Type of asset
        file_name: Name of the file
        content: Content to save
        operation_id: Optional operation identifier
        content_type: Optional content type (auto-detected if not provided)

    Returns:
        S3 key if successful, None otherwise
    """
    bucket = get_bucket_name()
    if not bucket:
        logger.warning("ASSETS_BUCKET_NAME not configured, skipping S3 storage")
        return None

    if not content:
        logger.warning("Empty content, skipping S3 storage")
        return None

    s3_key = build_s3_key(session_id, asset_type, file_name, operation_id)

    # NFS fast-path (ECS mode)
    _save_to_nfs(session_id, asset_type, file_name, content, operation_id)

    # Auto-detect content type if not provided
    if content_type is None:
        content_type = _get_content_type(asset_type, file_name)

    try:
        s3 = get_s3_client()
        # S3 metadata only supports ASCII - encode non-ASCII to safe format
        def to_ascii(s: str) -> str:
            return s.encode('ascii', 'replace').decode('ascii') if s else ''

        s3.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=content.encode('utf-8'),
            ContentType=content_type,
            Metadata={
                'session-id': to_ascii(session_id),
                'asset-type': to_ascii(asset_type),
                'operation-id': to_ascii(operation_id or ''),
                'file-name': to_ascii(file_name),
            }
        )
        logger.info(f"[S3] Saved asset: s3://{bucket}/{s3_key} ({len(content)} bytes)")
        return s3_key

    except ClientError as e:
        logger.error(f"[S3] Failed to save asset to S3: {e}")
        return None
    except Exception as e:
        logger.error(f"[S3] Unexpected error saving to S3: {e}")
        return None


def save_binary_asset_to_s3(
    session_id: str,
    asset_type: str,
    file_name: str,
    content: bytes,
    operation_id: Optional[str] = None,
    content_type: Optional[str] = None
) -> Optional[str]:
    """
    Save binary asset content (e.g., ZIP files) to S3.

    Args:
        session_id: User session ID
        asset_type: Type of asset
        file_name: Name of the file
        content: Binary content to save (bytes)
        operation_id: Optional operation identifier
        content_type: Optional content type (auto-detected if not provided)

    Returns:
        S3 key if successful, None otherwise
    """
    bucket = get_bucket_name()
    if not bucket:
        logger.warning("ASSETS_BUCKET_NAME not configured, skipping S3 storage")
        return None

    if not content:
        logger.warning("Empty content, skipping S3 storage")
        return None

    s3_key = build_s3_key(session_id, asset_type, file_name, operation_id)

    # Auto-detect content type if not provided
    if content_type is None:
        content_type = _get_content_type(asset_type, file_name)

    try:
        s3 = get_s3_client()
        # S3 metadata only supports ASCII - encode non-ASCII to safe format
        def to_ascii(s: str) -> str:
            return s.encode('ascii', 'replace').decode('ascii') if s else ''

        s3.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=content,  # Binary content directly
            ContentType=content_type,
            Metadata={
                'session-id': to_ascii(session_id),
                'asset-type': to_ascii(asset_type),
                'operation-id': to_ascii(operation_id or ''),
                'file-name': to_ascii(file_name),
            }
        )
        logger.info(f"[S3] Saved binary asset: s3://{bucket}/{s3_key} ({len(content)} bytes)")
        return s3_key

    except ClientError as e:
        logger.error(f"[S3] Failed to save binary asset to S3: {e}")
        return None
    except Exception as e:
        logger.error(f"[S3] Unexpected error saving binary to S3: {e}")
        return None


def get_binary_asset_from_s3(s3_key: str, s3_only: bool = False) -> Optional[bytes]:
    """
    Retrieve binary asset content.

    Args:
        s3_key: S3 key path
        s3_only: If True, skip the NFS fast-path and read directly from S3.
            The NFS layer is mountpoint-s3, which caches directory/object
            metadata for several seconds after a write. ZIP packaging and
            other download paths that must see the *latest* authoritative
            version should pass ``s3_only=True``.

    Returns:
        Binary content if successful, None otherwise
    """
    if not s3_only:
        # NFS-first: try to read binary from NFS workspace
        session_id, asset_type, file_name, operation_id = _parse_s3_key_to_nfs_components(s3_key)
        if session_id is not None:
            try:
                nfs_content = _get_binary_from_nfs(session_id, asset_type, file_name, operation_id)
                if nfs_content is not None:
                    logger.info(f"[NFS] Retrieved binary asset: {s3_key} ({len(nfs_content)} bytes)")
                    return nfs_content
            except (IOError, OSError) as e:
                logger.warning(f"[NFS] Binary read failed for {s3_key}, falling back to S3: {e}")

    # S3 fallback (or forced S3-only)
    bucket = get_bucket_name()
    if not bucket:
        logger.warning("ASSETS_BUCKET_NAME not configured")
        return None

    try:
        s3 = get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=s3_key)
        content = response['Body'].read()
        logger.info(f"[S3] Retrieved binary asset: s3://{bucket}/{s3_key} ({len(content)} bytes)")
        return content

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == 'NoSuchKey':
            logger.warning(f"[S3] Binary asset not found: {s3_key}")
        else:
            logger.error(f"[S3] Failed to retrieve binary asset from S3: {e}")
        return None
    except Exception as e:
        logger.error(f"[S3] Unexpected error retrieving binary from S3: {e}")
        return None


def _is_binary_file(file_name: str) -> bool:
    """Check if file is binary based on extension."""
    binary_extensions = {'.zip', '.tar', '.gz', '.tgz', '.bz2', '.7z', '.rar',
                         '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp',
                         '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                         '.exe', '.dll', '.so', '.dylib', '.bin', '.dat'}
    file_lower = file_name.lower()
    return any(file_lower.endswith(ext) for ext in binary_extensions)


def get_asset_from_s3(s3_key: str, allow_binary: bool = False, s3_only: bool = False) -> Optional[str]:
    """
    Retrieve asset content.

    Args:
        s3_key: S3 key path
        allow_binary: If True, return placeholder for binary files instead of None
        s3_only: If True, skip the NFS fast-path and read directly from S3.
            The NFS layer is mountpoint-s3, which caches directory/object
            metadata for several seconds after a write. Download/packaging
            paths that must see the *latest* authoritative version should
            pass ``s3_only=True``.

    Returns:
        Content string if successful, None otherwise.
        For binary files, returns "[BINARY FILE]" if allow_binary=True, None otherwise.
    """
    # Check if file is binary based on extension
    file_name = s3_key.split('/')[-1] if '/' in s3_key else s3_key
    if _is_binary_file(file_name):
        if allow_binary:
            logger.info(f"Binary file detected, returning placeholder: {s3_key}")
            return f"[BINARY FILE: {file_name}]"
        else:
            logger.info(f"Skipping binary file: {s3_key}")
            return None

    if not s3_only:
        # NFS-first: try to read from NFS workspace
        session_id, asset_type, nfs_file_name, operation_id = _parse_s3_key_to_nfs_components(s3_key)
        if session_id is not None:
            try:
                nfs_content = _get_from_nfs(session_id, asset_type, nfs_file_name, operation_id)
                if nfs_content is not None:
                    logger.info(f"[NFS] Retrieved asset: {s3_key} ({len(nfs_content)} bytes)")
                    return nfs_content
            except (IOError, OSError) as e:
                logger.warning(f"[NFS] Read failed for {s3_key}, falling back to S3: {e}")

    # S3 fallback (or forced S3-only)
    bucket = get_bucket_name()
    if not bucket:
        logger.warning("ASSETS_BUCKET_NAME not configured")
        return None

    try:
        s3 = get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=s3_key)
        raw_content = response['Body'].read()

        # Try multiple encodings
        encodings_to_try = ['utf-8', 'utf-8-sig', 'latin-1', 'cp949', 'euc-kr']
        content = None
        used_encoding = None

        for encoding in encodings_to_try:
            try:
                content = raw_content.decode(encoding)
                used_encoding = encoding
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is not None:
            if used_encoding != 'utf-8':
                logger.info(f"[S3] Retrieved asset with {used_encoding} encoding: s3://{bucket}/{s3_key} ({len(content)} bytes)")
            else:
                logger.info(f"[S3] Retrieved asset: s3://{bucket}/{s3_key} ({len(content)} bytes)")
            return content
        else:
            # All encodings failed - likely binary
            logger.warning(f"[S3] Could not decode content (tried {encodings_to_try}): {s3_key}")
            if allow_binary:
                return f"[BINARY FILE: {file_name}]"
            return None

    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logger.warning(f"[S3] Asset not found: {s3_key}")
        else:
            logger.error(f"[S3] Failed to get asset from S3: {e}")
        return None
    except Exception as e:
        logger.error(f"[S3] Unexpected error reading from S3: {e}")
        return None


def delete_asset_from_s3(s3_key: str) -> bool:
    """
    Delete asset from S3.

    Args:
        s3_key: S3 key path

    Returns:
        True if successful, False otherwise
    """
    bucket = get_bucket_name()
    if not bucket:
        return False

    try:
        s3 = get_s3_client()
        s3.delete_object(Bucket=bucket, Key=s3_key)
        logger.info(f"[S3] Deleted asset: s3://{bucket}/{s3_key}")
        return True

    except Exception as e:
        logger.error(f"[S3] Failed to delete asset from S3: {e}")
        return False


def list_session_assets(session_id: str, s3_only: bool = False) -> list:
    """
    List all assets for a session.

    Args:
        session_id: User session ID
        s3_only: If True, skip NFS listing (mountpoint-s3 metadata cache can
            return stale directory contents for up to the cache TTL window).
            Download/packaging paths that must enumerate the latest
            authoritative state should pass ``s3_only=True``.

    Returns:
        List of S3 keys for the session
    """
    if not s3_only:
        # NFS-first: try listing from NFS workspace
        nfs_keys = _list_nfs_assets(session_id)
        if nfs_keys is not None:
            logger.info(f"[NFS] Found {len(nfs_keys)} assets for session {session_id}")
            return nfs_keys

    # S3 fallback (or forced S3-only)
    bucket = get_bucket_name()
    if not bucket:
        return []

    # Match the sanitization done by build_s3_key() when assets were written.
    safe_session = session_id.replace('/', '_').replace('..', '_')
    prefix = f"assets/{safe_session}/"

    try:
        s3 = get_s3_client()
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)

        keys = []
        for obj in response.get('Contents', []):
            keys.append(obj['Key'])

        # Handle pagination
        while response.get('IsTruncated'):
            response = s3.list_objects_v2(
                Bucket=bucket,
                Prefix=prefix,
                ContinuationToken=response['NextContinuationToken']
            )
            for obj in response.get('Contents', []):
                keys.append(obj['Key'])

        logger.info(f"[S3] Found {len(keys)} assets for session {session_id}")
        return keys

    except Exception as e:
        logger.error(f"[S3] Failed to list session assets: {e}")
        return []


def _copy_s3_key_to_nfs(s3_key: str, nfs_path: Path) -> bool:
    """Download an S3 object and write it to *nfs_path* atomically.

    Used by hydrate_session_workspace to repopulate the NFS view from the
    durable S3 copy when lazy mountpoint-s3 import didn't bring the file
    back (file >10MB, dir not yet read, or evicted after 30 days).
    """
    bucket = get_bucket_name()
    if not bucket:
        return False
    try:
        s3 = get_s3_client()
        body = s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "NoSuchKey":
            logger.warning(f"[HYDRATE] S3 GET failed {s3_key}: {e}")
        return False
    except Exception as e:
        logger.warning(f"[HYDRATE] S3 GET error {s3_key}: {e}")
        return False
    try:
        nfs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = nfs_path.with_suffix(nfs_path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(body)
        os.rename(tmp, nfs_path)
        return True
    except OSError as e:
        logger.warning(f"[HYDRATE] NFS write failed {nfs_path}: {e}")
        return False


def _list_s3_prefix(prefix: str) -> list:
    """List every S3 key under *prefix* (handles pagination)."""
    bucket = get_bucket_name()
    if not bucket:
        return []
    keys: list = []
    try:
        s3 = get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except Exception as e:
        logger.warning(f"[HYDRATE] list {prefix} failed: {e}")
    return keys


def hydrate_session_workspace(session_id: str) -> dict:
    """
    Repopulate the NFS view from S3 so every asset for a session is visible.

    S3 Files (managed NFS over S3) lazily imports each directory on first
    access (ON_DIRECTORY_FIRST_ACCESS) and only for files ≤10MB. Files larger
    than that, or older than the 30-day eviction window, never come back via
    auto-import — even though the underlying S3 objects still exist. Every
    asset is dual-written: NFS at sessions/{sid}/assets/{type}/{file} AND a
    durable PutObject at assets/{sid}/{type}/{file}. This helper bridges the
    two: it lists the authoritative durable copy and copies any missing files
    into the NFS view so the workspace tree shows the full session contents.

    Covers three S3 prefixes:
      - assets/{sid}/{type}/...      → /mnt/s3/sessions/{sid}/assets/{type}/...
      - assets/{sid}/specs/{op}.json → /mnt/s3/sessions/{sid}/assets/specs/...
      - assets/{sid}/state/...       → /mnt/s3/sessions/{sid}/state/...

    Idempotent: files already present on NFS are skipped.

    Args:
        session_id: User session ID to hydrate.

    Returns:
        dict with:
          - listed:   total S3 keys discovered for the session
          - files:    files already present on NFS (no work needed)
          - restored: files newly copied from S3 to NFS
          - skipped:  files we couldn't restore (S3 read or NFS write failed)
    """
    result = {"listed": 0, "files": 0, "restored": 0, "skipped": 0}

    if not _nfs_available():
        logger.info(f"[HYDRATE] NFS not available, skipping for {session_id}")
        return result

    safe_session = session_id.replace("..", "_").replace("/", "_")
    mount_root = Path(S3FILES_MOUNT) / "sessions" / safe_session

    # Map each S3 key to its target NFS path. Three durable prefixes:
    #   - assets/{sid}/state/...   → state/...
    #   - assets/{sid}/specs/...   → assets/specs/...
    #   - assets/{sid}/{type}/...  → assets/{type}/...
    plan: list[tuple[str, Path]] = []

    # Asset files (lambda, openapi, contact_flow, infrastructure, prompt, faq, …).
    try:
        asset_keys = list_session_assets(session_id, s3_only=True)
    except Exception as e:
        logger.warning(f"[HYDRATE] list assets failed for {session_id}: {e}")
        asset_keys = []
    for key in asset_keys:
        _, asset_type, file_name, op_id = _parse_s3_key_to_nfs_components(key)
        if asset_type is None:
            continue
        target = mount_root / "assets" / asset_type
        if op_id:
            target = target / op_id
        target = target / file_name
        plan.append((key, target))

    # State files (project.json, progress.json, requirements/, schemas/).
    state_prefix = f"assets/{safe_session}/state/"
    for key in _list_s3_prefix(state_prefix):
        rel = key[len(state_prefix):]
        if not rel:
            continue
        plan.append((key, mount_root / "state" / rel))

    result["listed"] = len(plan)
    if not plan:
        return result

    for s3_key, nfs_path in plan:
        try:
            if nfs_path.exists():
                result["files"] += 1
                continue
        except OSError as e:
            logger.warning(f"[HYDRATE] stat failed for {nfs_path}: {e}")
            result["skipped"] += 1
            continue
        if _copy_s3_key_to_nfs(s3_key, nfs_path):
            result["restored"] += 1
        else:
            result["skipped"] += 1

    logger.info(
        f"[HYDRATE] session={session_id} "
        f"listed={result['listed']} files={result['files']} "
        f"restored={result['restored']} skipped={result['skipped']}"
    )
    return result


def delete_session_assets(session_id: str) -> int:
    """
    Delete all assets for a session.

    Args:
        session_id: User session ID

    Returns:
        Number of deleted assets
    """
    bucket = get_bucket_name()
    if not bucket:
        return 0

    keys = list_session_assets(session_id)
    if not keys:
        return 0

    try:
        s3 = get_s3_client()

        # Delete in batches of 1000 (S3 limit)
        deleted_count = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            delete_objects = [{'Key': key} for key in batch]

            s3.delete_objects(
                Bucket=bucket,
                Delete={'Objects': delete_objects}
            )
            deleted_count += len(batch)

        logger.info(f"[S3] Deleted {deleted_count} assets for session {session_id}")
        return deleted_count

    except Exception as e:
        logger.error(f"[S3] Failed to delete session assets: {e}")
        return 0
