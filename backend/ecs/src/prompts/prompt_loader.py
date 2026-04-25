"""
System Prompt Hot-Reload from S3 Files NFS

Reads system prompts from /mnt/s3/prompts/{name}.py and reloads
when the file changes (MD5 hash check per call).

Falls back to the compiled prompts/system_prompt.py if NFS is unavailable.
"""

import os
import hashlib
import logging
import importlib.util
from typing import Optional

logger = logging.getLogger(__name__)

# Cache
_prompt_cache: dict = {}  # name → {"hash": "...", "content": "..."}


def _nfs_prompt_path(name: str = "system_prompt") -> str:
    mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    return os.path.join(mount_path, "prompts", f"{name}.py")


def _file_md5(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None


def _load_prompt_from_file(path: str) -> Optional[str]:
    """Load SYSTEM_PROMPT from a Python file."""
    try:
        spec = importlib.util.spec_from_file_location("nfs_prompt", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "SYSTEM_PROMPT", None)
    except Exception as e:
        logger.warning(f"Failed to load prompt from {path}: {e}")
        return None


def get_system_prompt(name: str = "system_prompt") -> Optional[str]:
    """
    Get system prompt with hot-reload support.

    Checks NFS file hash; re-reads only on change.
    Falls back to compiled module if NFS unavailable.
    """
    nfs_path = _nfs_prompt_path(name)

    if not os.path.exists(nfs_path):
        # NFS prompt not available, return None to use compiled default
        return None

    current_hash = _file_md5(nfs_path)
    if current_hash is None:
        return None

    cached = _prompt_cache.get(name)
    if cached and cached["hash"] == current_hash:
        return cached["content"]

    # Hash changed or first load — reload
    content = _load_prompt_from_file(nfs_path)
    if content:
        _prompt_cache[name] = {"hash": current_hash, "content": content}
        logger.info(f"[PromptLoader] Reloaded {name} from NFS (hash: {current_hash[:8]})")
        return content

    return None


def get_model_config(name: str = "model_config") -> Optional[dict]:
    """Load model config from NFS /mnt/s3/config/{name}.json."""
    import json
    mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    config_path = os.path.join(mount_path, "config", f"{name}.json")

    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load model config from {config_path}: {e}")
        return None
