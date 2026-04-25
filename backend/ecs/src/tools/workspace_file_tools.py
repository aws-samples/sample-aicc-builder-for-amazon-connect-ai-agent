"""
NFS Workspace File Tools

Provides direct file system access for agents within session workspace directories.
All paths are relative to /mnt/s3/sessions/{session_id}/ with security protections
against path traversal, absolute paths, and symlink escapes.

Tools:
- read_workspace_file: Read file contents
- write_workspace_file: Write/overwrite file (atomic)
- append_workspace_file: Append content to file
- list_workspace_dir: List directory entries
- patch_workspace_file: Find-and-replace text in file
- find_workspace_files: Recursive file search with glob patterns
- grep_workspace: Search text across multiple files
"""

import fnmatch
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)

# NFS mount path (set via S3FILES_MOUNT_PATH env var)
_S3FILES_MOUNT = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")


# File preview configuration
_MIN_PREVIEW_SIZE = 50  # 50B minimum
_MAX_PREVIEW_CONTENT = 50_000  # 50KB cap
_SKIP_EXTENSIONS = {'.tmp', '.log', '.lock', '.bak', '.pyc'}
_TEXT_EXTENSIONS = {'.py', '.ts', '.js', '.json', '.yaml', '.yml', '.md', '.txt',
                    '.html', '.css', '.sh', '.sql', '.xml', '.toml', '.cfg', '.ini'}


def _should_show_preview(path: str, content_size: int) -> bool:
    """Decide whether to show inline preview for a workspace file."""
    if content_size < _MIN_PREVIEW_SIZE:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext not in _SKIP_EXTENSIONS and (ext in _TEXT_EXTENSIONS or not ext)


def _emit_file_preview(action: str, session_id: str, path: str, content: str):
    """Emit an asset_preview event with workspace_file type for inline preview."""
    try:
        from .streaming_callback import get_streaming_callback, get_message_index
        callback = get_streaming_callback()
        if not callback:
            return
        preview_content = content
        if len(content) > _MAX_PREVIEW_CONTENT:
            preview_content = content[:_MAX_PREVIEW_CONTENT] + f"\n\n... [{len(content)} chars total]"

        callback(
            asset_type="workspace_file",
            content=preview_content,
            operation_id=action,  # "write" | "patch" | "append"
            file_name=path,  # full relative path for extension detection
            is_complete=True,
            s3_key=None,
            message_index=get_message_index(),
            download_data=None,
        )
    except Exception as e:
        logger.debug(f"[workspace] file preview failed (non-critical): {e}")


def _emit_diff_preview(session_id: str, path: str, original: str, modified: str):
    """Emit an asset_preview event with unified diff content for inline diff display."""
    try:
        from .diff_utils import generate_unified_diff
        from .streaming_callback import get_streaming_callback, get_message_index

        diff_text = generate_unified_diff(original, modified, file_name=path)
        if not diff_text:
            return  # No changes

        callback = get_streaming_callback()
        if not callback:
            return

        callback(
            asset_type="workspace_file",
            content=diff_text,
            operation_id="diff",  # Special operation_id for diff previews
            file_name=path,
            is_complete=True,
            s3_key=None,
            message_index=get_message_index(),
            download_data=None,
        )
    except Exception as e:
        logger.debug(f"[workspace] diff preview failed (non-critical): {e}")


def _mirror_asset_to_s3(session_id: str, path: str, content: str) -> None:
    """
    If a workspace write lands under `assets/{type}[/{op}]/{file}`, mirror the
    content to S3 via save_asset_to_s3.

    This closes the gap where modifications written through workspace tools
    reach NFS but never propagate back to the assets bucket — causing the
    packaging/download flow and direct S3 reads to see stale first-generation
    versions. S3 Files write-through is best-effort; this explicit mirror
    guarantees parity.
    """
    try:
        parts = [p for p in path.split('/') if p]
        if len(parts) < 3 or parts[0] != "assets":
            return

        asset_type = parts[1]
        if len(parts) == 3:
            operation_id = None
            file_name = parts[2]
        else:
            operation_id = parts[2]
            file_name = "/".join(parts[3:]) if len(parts) > 4 else parts[3]

        from .s3_asset_storage import save_asset_to_s3
        save_asset_to_s3(
            session_id=session_id,
            asset_type=asset_type,
            file_name=file_name,
            content=content,
            operation_id=operation_id,
        )
    except Exception as e:
        logger.warning(f"[workspace] S3 mirror failed for {path} (non-fatal): {e}")


def _emit_workspace_event(action: str, session_id: str, path: str, size: int):
    """Emit workspace_update event to frontend via streaming callback."""
    try:
        from .streaming_callback import get_streaming_callback
        callback = get_streaming_callback()
        if callback:
            callback(
                asset_type="workspace_update",
                content=json.dumps({"action": action, "path": path, "size": size}),
                operation_id=None,
                file_name=path,
                is_complete=True,
                s3_key=None,
                message_index=None,
                download_data=None,
            )
    except Exception as e:
        logger.debug(f"[workspace] emit event failed (non-critical): {e}")


def _get_session_root(session_id: str) -> Path:
    """Get the root directory for a session, with sanitization."""
    # Sanitize session_id: reject path separators and traversal
    safe_id = re.sub(r'[/\\]', '', session_id)
    if '..' in safe_id or not safe_id:
        raise ValueError(f"Invalid session_id: {session_id}")
    return Path(_S3FILES_MOUNT) / "sessions" / safe_id


def _resolve_safe_path(session_id: str, relative_path: str) -> Path:
    """
    Resolve a relative path within a session directory safely.

    Raises ValueError if the path escapes the session directory.
    """
    # Reject absolute paths
    if relative_path.startswith('/') or relative_path.startswith('\\'):
        raise ValueError(f"Absolute paths not allowed: {relative_path}")

    # Reject obvious traversal
    if '..' in relative_path:
        raise ValueError(f"Path traversal not allowed: {relative_path}")

    session_root = _get_session_root(session_id)
    target = (session_root / relative_path).resolve()

    # Ensure resolved path is within session root
    try:
        target.relative_to(session_root.resolve())
    except ValueError:
        raise ValueError(f"Path escapes session directory: {relative_path}")

    return target


def _check_nfs_available() -> bool:
    """Check if NFS mount point is available."""
    mount_path = Path(_S3FILES_MOUNT)
    return mount_path.exists() and mount_path.is_dir()


@tool
def read_workspace_file(session_id: str, path: str) -> dict[str, Any]:
    """
    Read a file from the session workspace.

    Args:
        session_id: The session identifier
        path: Relative path within the session directory (e.g., "assets/lambda/op_id/index.py")

    Returns:
        Dict with success, content, and size fields
    """
    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        target = _resolve_safe_path(session_id, path)

        if not target.exists():
            return {"success": False, "error": f"File not found: {path}"}

        if not target.is_file():
            return {"success": False, "error": f"Not a file: {path}"}

        content = target.read_text(encoding="utf-8")
        byte_size = len(content.encode('utf-8'))
        return {
            "success": True,
            "content": content,
            "size": byte_size,
            "summary": f"Read {path} ({byte_size} bytes)",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except UnicodeDecodeError:
        # Binary file — return size info only
        try:
            size = target.stat().st_size
            return {
                "success": False,
                "error": f"Binary file (cannot read as text), size: {size} bytes",
            }
        except Exception:
            return {"success": False, "error": "Binary file (cannot read as text)"}
    except Exception as e:
        logger.error(f"[workspace] read_workspace_file failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def write_workspace_file(session_id: str, path: str, content: str) -> dict[str, Any]:
    """
    Write content to a file in the session workspace (atomic write).
    Creates parent directories automatically.

    Args:
        session_id: The session identifier
        path: Relative path within the session directory
        content: File content to write

    Returns:
        Dict with success, path, and size fields
    """
    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        target = _resolve_safe_path(session_id, path)

        # Create parent directories
        os.makedirs(target.parent, exist_ok=True)

        # Atomic write: write to temp file then rename
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            os.rename(tmp_path, target)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        _emit_workspace_event("write", session_id, path, len(content))
        if _should_show_preview(path, len(content)):
            _emit_file_preview("write", session_id, path, content)
        _mirror_asset_to_s3(session_id, path, content)
        return {
            "success": True,
            "path": path,
            "size": len(content),
            "summary": f"Wrote {len(content)} bytes to {path}",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[workspace] write_workspace_file failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def append_workspace_file(session_id: str, path: str, content: str) -> dict[str, Any]:
    """
    Append content to a file in the session workspace.
    Creates the file and parent directories if they don't exist.

    Args:
        session_id: The session identifier
        path: Relative path within the session directory
        content: Content to append

    Returns:
        Dict with success, path, and new_size fields
    """
    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        target = _resolve_safe_path(session_id, path)

        # Create parent directories
        os.makedirs(target.parent, exist_ok=True)

        with open(target, 'a', encoding='utf-8') as f:
            f.write(content)

        new_size = target.stat().st_size
        _emit_workspace_event("append", session_id, path, new_size)
        full_content_for_mirror = None
        if _should_show_preview(path, new_size):
            try:
                full_content_for_mirror = target.read_text(encoding="utf-8")
                _emit_file_preview("append", session_id, path, full_content_for_mirror)
            except Exception:
                pass
        if full_content_for_mirror is None:
            try:
                full_content_for_mirror = target.read_text(encoding="utf-8")
            except Exception:
                full_content_for_mirror = None
        if full_content_for_mirror is not None:
            _mirror_asset_to_s3(session_id, path, full_content_for_mirror)
        return {
            "success": True,
            "path": path,
            "new_size": new_size,
            "summary": f"Appended {len(content)} bytes to {path} (total: {new_size} bytes)",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[workspace] append_workspace_file failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def list_workspace_dir(session_id: str, path: str = "") -> dict[str, Any]:
    """
    List contents of a directory in the session workspace.

    Args:
        session_id: The session identifier
        path: Relative path within the session directory (empty string = session root)

    Returns:
        Dict with success and entries (list of {name, type, size}) fields
    """
    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        if path:
            target = _resolve_safe_path(session_id, path)
        else:
            target = _get_session_root(session_id)

        if not target.exists():
            return {"success": False, "error": f"Directory not found: {path or '(session root)'}"}

        if not target.is_dir():
            return {"success": False, "error": f"Not a directory: {path}"}

        entries = []
        with os.scandir(target) as it:
            for entry in sorted(it, key=lambda e: e.name):
                entry_info = {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                }
                if entry.is_file():
                    try:
                        entry_info["size"] = entry.stat().st_size
                    except OSError:
                        entry_info["size"] = -1
                entries.append(entry_info)

        dir_count = sum(1 for e in entries if e["type"] == "dir")
        file_count = sum(1 for e in entries if e["type"] == "file")
        return {
            "success": True,
            "entries": entries,
            "count": len(entries),
            "summary": f"Listed {path or '(root)'}: {file_count} file(s), {dir_count} dir(s)",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[workspace] list_workspace_dir failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def patch_workspace_file(
    session_id: str, path: str, search: str, replace: str
) -> dict[str, Any]:
    """
    Find and replace text in a workspace file. Replaces ALL occurrences.

    Args:
        session_id: The session identifier
        path: Relative path within the session directory
        search: Text to find (literal string match, not regex)
        replace: Text to replace with

    Returns:
        Dict with success, replacements_made, and new_size fields
    """
    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        target = _resolve_safe_path(session_id, path)

        if not target.exists():
            return {"success": False, "error": f"File not found: {path}"}

        if not target.is_file():
            return {"success": False, "error": f"Not a file: {path}"}

        original = target.read_text(encoding="utf-8")

        if search not in original:
            return {
                "success": False,
                "error": "Search text not found in file",
                "replacements_made": 0,
            }

        count = original.count(search)
        modified = original.replace(search, replace)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(modified)
            os.rename(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Collect line numbers where replacements occurred (scan for replace text)
        changed_lines = []
        for i, line in enumerate(modified.splitlines(), 1):
            if replace in line:
                changed_lines.append(i)
                if len(changed_lines) >= 10:
                    break

        _emit_workspace_event("patch", session_id, path, len(modified))
        if _should_show_preview(path, len(modified)):
            _emit_file_preview("patch", session_id, path, modified)
        # Emit unified diff preview for frontend diff rendering
        _emit_diff_preview(session_id, path, original, modified)
        _mirror_asset_to_s3(session_id, path, modified)
        return {
            "success": True,
            "replacements_made": count,
            "new_size": len(modified),
            "search": search[:200],
            "replace": replace[:200],
            "summary": f"Replaced '{search[:50]}' → '{replace[:50]}' ({count} occurrence(s))",
            "changed_lines": changed_lines,
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[workspace] patch_workspace_file failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def find_workspace_files(
    session_id: str, pattern: str, path: str = ""
) -> dict[str, Any]:
    """
    Recursively find files matching a glob pattern in the session workspace.

    Args:
        session_id: The session identifier
        pattern: Glob pattern to match (e.g., "*.py", "lambda/**/*.py", "*.yaml")
        path: Relative directory to search in (empty string = session root)

    Returns:
        Dict with success and matches (list of relative file paths) fields
    """
    MAX_RESULTS = 200

    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        if path:
            search_root = _resolve_safe_path(session_id, path)
        else:
            search_root = _get_session_root(session_id)

        if not search_root.exists():
            return {"success": False, "error": f"Directory not found: {path or '(session root)'}"}

        if not search_root.is_dir():
            return {"success": False, "error": f"Not a directory: {path}"}

        session_root = _get_session_root(session_id)
        matches = []

        for root, dirs, files in os.walk(search_root):
            # Skip hidden directories and temp files
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for filename in files:
                if fnmatch.fnmatch(filename, pattern):
                    full_path = Path(root) / filename
                    try:
                        rel_path = str(full_path.relative_to(session_root))
                        size = full_path.stat().st_size
                        matches.append({"path": rel_path, "size": size})
                    except (ValueError, OSError):
                        continue

                if len(matches) >= MAX_RESULTS:
                    break
            if len(matches) >= MAX_RESULTS:
                break

        matches.sort(key=lambda m: m["path"])

        return {
            "success": True,
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= MAX_RESULTS,
            "summary": f"Found {len(matches)} file(s) matching '{pattern}'",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[workspace] find_workspace_files failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def grep_workspace(
    session_id: str, pattern: str, path: str = "", file_pattern: str = "*"
) -> dict[str, Any]:
    """
    Search for text across files in the session workspace.

    Args:
        session_id: The session identifier
        pattern: Text or regex pattern to search for
        path: Relative directory to search in (empty string = session root)
        file_pattern: Glob pattern to filter files (e.g., "*.py", "*.yaml"). Default "*" matches all.

    Returns:
        Dict with success and results (list of {path, line_number, line, context}) fields
    """
    MAX_RESULTS = 100
    MAX_FILE_SIZE = 1024 * 1024  # 1MB limit per file

    try:
        if not _check_nfs_available():
            return {"success": False, "error": f"NFS mount not available at {_S3FILES_MOUNT}"}

        if path:
            search_root = _resolve_safe_path(session_id, path)
        else:
            search_root = _get_session_root(session_id)

        if not search_root.exists():
            return {"success": False, "error": f"Directory not found: {path or '(session root)'}"}

        if not search_root.is_dir():
            return {"success": False, "error": f"Not a directory: {path}"}

        try:
            compiled = re.compile(pattern)
        except re.error:
            # Fall back to literal match if regex is invalid
            compiled = re.compile(re.escape(pattern))

        session_root = _get_session_root(session_id)
        results = []

        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                full_path = Path(root) / filename

                # Skip large files and binary-looking files
                try:
                    file_size = full_path.stat().st_size
                    if file_size > MAX_FILE_SIZE or file_size == 0:
                        continue
                except OSError:
                    continue

                try:
                    content = full_path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue

                rel_path = str(full_path.relative_to(session_root))
                lines = content.splitlines()

                for line_num, line in enumerate(lines, 1):
                    if compiled.search(line):
                        results.append({
                            "path": rel_path,
                            "line_number": line_num,
                            "line": line[:500],  # Truncate very long lines
                        })

                        if len(results) >= MAX_RESULTS:
                            break

                if len(results) >= MAX_RESULTS:
                    break
            if len(results) >= MAX_RESULTS:
                break

        return {
            "success": True,
            "results": results,
            "count": len(results),
            "truncated": len(results) >= MAX_RESULTS,
            "summary": f"Found {len(results)} match(es) for '{pattern[:50]}'",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[workspace] grep_workspace failed: {e}")
        return {"success": False, "error": str(e)}


# ── Non-tool helpers for subagent modification workflows ──


def get_asset_workspace_path(
    session_id: str,
    asset_type: str,
    file_name: str,
    operation_id: str | None = None,
) -> str:
    """
    Build a workspace-relative path for an asset file.

    Flat structure: assets/{asset_type}/{operation_id}/{file_name}
    (e.g., assets/lambda/check_reservation/index.py).

    Args:
        session_id: The session identifier
        asset_type: Asset type (lambda, openapi, prompt, contact_flow, cloudformation)
        file_name: The file name (e.g., "index.py", "openapi.yaml")
        operation_id: Optional operation ID subfolder

    Returns:
        Workspace-relative path string usable with read_workspace_file / write_workspace_file
    """
    parts = ["assets", asset_type]
    if operation_id:
        parts.append(operation_id)
    parts.append(file_name)
    return "/".join(parts)


def write_with_diff(
    session_id: str,
    path: str,
    new_content: str,
) -> dict:
    """
    Write content to a workspace file and emit a unified diff preview.

    This is a non-@tool helper for subagent modification workflows.
    It reads the existing file, writes the new content, then emits
    both the full file preview and the diff preview.

    Args:
        session_id: The session identifier
        path: Workspace-relative path
        new_content: New file content to write

    Returns:
        Dict with success, path, size, and diff_emitted fields
    """
    # Read existing content for diff
    original = ""
    try:
        target = _resolve_safe_path(session_id, path)
        if target.exists() and target.is_file():
            original = target.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug(f"[workspace] write_with_diff: could not read original: {e}")

    # Write the new content using the existing tool function (unwrapped)
    result = write_workspace_file(
        session_id=session_id,
        path=path,
        content=new_content,
    )

    if not result.get("success"):
        return result

    # Emit diff preview if there was an original to compare against
    if original:
        _emit_diff_preview(session_id, path, original, new_content)
        result["diff_emitted"] = True
    else:
        result["diff_emitted"] = False

    return result
