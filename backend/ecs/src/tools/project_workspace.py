"""
Project Workspace — S3-backed project state management.

Persists all structured state (OperationSpecs, infrastructure_schema,
progress, requirements) to S3 so that sessions survive WebSocket
disconnects and MicroVM restarts.

S3 layout:
    assets/{session_id}/
    ├── state/
    │   ├── project.json
    │   ├── progress.json
    │   ├── requirements/{doc_type}.txt
    │   └── schemas/infrastructure.json
    └── specs/{op_id}.json          ← specs are assets, not state
"""

import json
import logging
from typing import Optional

from strands import tool

from tools.s3_asset_storage import (
    get_asset_from_s3,
    get_s3_client,
    get_bucket_name,
)
from tools.session_context import (
    current_session_id,
    get_workspace_for,
    set_workspace_for,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-session workspace accessors
#
# Previously, a single module-level ``_workspace`` singleton was mutated on
# every request — which meant concurrent users on the same ECS task shared
# one workspace and leaked each other's state. Workspaces are now stored
# per-session in ``session_context`` and selected via the ``current_session_id``
# ContextVar, which is async-task-local.
# ---------------------------------------------------------------------------

def set_workspace_session_id(session_id: str) -> "ProjectWorkspace":
    """Bind *session_id* to the current async context and ensure its workspace exists."""
    current_session_id.set(session_id)
    ws = get_workspace_for(session_id)
    if ws is None:
        ws = ProjectWorkspace(session_id)
        set_workspace_for(session_id, ws)
        logger.info(f"[Workspace] Initialised for session {session_id}")
    return ws


def get_workspace() -> Optional["ProjectWorkspace"]:
    """Return the workspace for the current async context's session_id."""
    return get_workspace_for(current_session_id.get())


# ---------------------------------------------------------------------------
# ProjectWorkspace
# ---------------------------------------------------------------------------
class ProjectWorkspace:
    """S3-backed project state that survives session restarts.

    Builds S3 keys directly to preserve nested path structure
    (``build_s3_key`` sanitises slashes which breaks sub-directories).
    """

    def __init__(self, session_id: str):
        self.session_id = session_id.replace("..", "_")
        # In-memory caches (hot path)
        self._specs_cache: dict[str, dict] = {}
        self._schema_cache: Optional[dict] = None
        self._progress_cache: Optional[dict] = None

    # ── helpers ─────────────────────────────────────────────────────────
    def _s3_key(self, *parts: str) -> str:
        """Build ``assets/{session_id}/state/<parts…>`` preserving ``/``."""
        return f"assets/{self.session_id}/state/{'/'.join(parts)}"

    def _specs_s3_key(self, *parts: str) -> str:
        """Build ``assets/{session_id}/specs/<parts…>`` — specs are assets, not state."""
        if parts:
            return f"assets/{self.session_id}/specs/{'/'.join(parts)}"
        return f"assets/{self.session_id}/specs"

    def _nfs_path(self, *parts: str) -> Optional[str]:
        """Build NFS path for state files if S3FILES_MOUNT_PATH is set."""
        import os
        mount = os.environ.get("S3FILES_MOUNT_PATH", "")
        if not mount or not os.path.isdir(mount):
            return None
        from pathlib import Path
        return str(Path(mount) / "sessions" / self.session_id / "state" / "/".join(parts))

    def _specs_nfs_path(self, *parts: str) -> Optional[str]:
        """Build NFS path for specs under assets/specs/ if S3FILES_MOUNT_PATH is set."""
        import os
        mount = os.environ.get("S3FILES_MOUNT_PATH", "")
        if not mount or not os.path.isdir(mount):
            return None
        from pathlib import Path
        base = Path(mount) / "sessions" / self.session_id / "assets" / "specs"
        if parts:
            base = base / "/".join(parts)
        return str(base)

    def _put(self, path_parts: list[str], body: str, content_type: str = "application/json") -> bool:
        """Write to NFS (fast-path) and S3 (durable backup)."""
        nfs_ok = False
        nfs_path = self._nfs_path(*path_parts)
        if nfs_path:
            try:
                import os
                os.makedirs(os.path.dirname(nfs_path), exist_ok=True)
                tmp = nfs_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(body)
                os.rename(tmp, nfs_path)
                nfs_ok = True
                logger.debug(f"[Workspace] NFS PUT {nfs_path} ({len(body)} bytes)")
            except Exception as e:
                logger.warning(f"[Workspace] NFS PUT failed {nfs_path}: {e}")

        s3_ok = self._put_s3(self._s3_key(*path_parts), body, content_type)
        return nfs_ok or s3_ok

    def _get(self, path_parts: list[str]) -> Optional[str]:
        """Read from NFS (fast-path), fall back to S3."""
        nfs_path = self._nfs_path(*path_parts)
        if nfs_path:
            try:
                import os
                if os.path.exists(nfs_path):
                    with open(nfs_path, "r", encoding="utf-8") as f:
                        return f.read()
            except Exception as e:
                logger.warning(f"[Workspace] NFS GET failed {nfs_path}: {e}")
        return self._get_s3(self._s3_key(*path_parts))

    def _put_s3(self, s3_key: str, body: str, content_type: str = "application/json") -> bool:
        """Low-level S3 put that preserves the key as-is."""
        bucket = get_bucket_name()
        if not bucket:
            return False
        try:
            s3 = get_s3_client()
            s3.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=body.encode("utf-8"),
                ContentType=content_type,
                Metadata={"session-id": self.session_id},
            )
            logger.debug(f"[Workspace] PUT s3://{bucket}/{s3_key} ({len(body)} bytes)")
            return True
        except Exception as e:
            logger.error(f"[Workspace] PUT failed {s3_key}: {e}")
            return False

    def _get_s3(self, s3_key: str) -> Optional[str]:
        """Low-level S3 get — delegates to the shared helper."""
        return get_asset_from_s3(s3_key)

    def _save_json(self, path_parts: list[str], data: dict) -> bool:
        return self._put(path_parts, json.dumps(data, ensure_ascii=False, default=str))

    def _load_json(self, path_parts: list[str]) -> Optional[dict]:
        raw = self._get(path_parts)
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"[Workspace] Invalid JSON at {path_parts}")
        return None

    # ── Operation Specs (stored under assets/specs/, not state/) ────────
    def save_spec(self, op_id: str, spec_dict: dict):
        """Persist a single OperationSpec (dict form) to cache + NFS + S3."""
        self._specs_cache[op_id] = spec_dict
        body = json.dumps(spec_dict, ensure_ascii=False, default=str)

        # NFS fast-path
        nfs_path = self._specs_nfs_path(f"{op_id}.json")
        if nfs_path:
            try:
                import os
                os.makedirs(os.path.dirname(nfs_path), exist_ok=True)
                tmp = nfs_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(body)
                os.rename(tmp, nfs_path)
                logger.debug(f"[Workspace] NFS PUT spec {nfs_path}")
            except Exception as e:
                logger.warning(f"[Workspace] NFS PUT spec failed {nfs_path}: {e}")

        # S3 durable backup
        s3_key = self._specs_s3_key(f"{op_id}.json")
        self._put_s3(s3_key, body)
        logger.info(f"[Workspace] Saved spec: {op_id}")

    def load_spec(self, op_id: str) -> Optional[dict]:
        """Load a spec: memory cache → NFS → S3 fallback."""
        if op_id in self._specs_cache:
            return self._specs_cache[op_id]

        # NFS fast-path
        nfs_path = self._specs_nfs_path(f"{op_id}.json")
        if nfs_path:
            try:
                import os
                if os.path.exists(nfs_path):
                    with open(nfs_path, "r", encoding="utf-8") as f:
                        data = json.loads(f.read())
                    self._specs_cache[op_id] = data
                    return data
            except Exception as e:
                logger.warning(f"[Workspace] NFS GET spec failed {nfs_path}: {e}")

        # S3 fallback
        s3_key = self._specs_s3_key(f"{op_id}.json")
        raw = self._get_s3(s3_key)
        if raw:
            try:
                data = json.loads(raw)
                self._specs_cache[op_id] = data
                return data
            except json.JSONDecodeError:
                logger.warning(f"[Workspace] Invalid JSON for spec {op_id}")
        return None

    def load_all_specs(self) -> dict[str, dict]:
        """Load every spec from NFS (fast-path) then S3 fallback (session restore)."""
        # NFS fast-path: scan assets/specs/ directory directly
        nfs_dir = self._specs_nfs_path()
        if nfs_dir:
            import os
            try:
                if os.path.isdir(nfs_dir):
                    for entry in os.scandir(nfs_dir):
                        if entry.is_file() and entry.name.endswith(".json"):
                            op_id = entry.name.replace(".json", "")
                            if op_id not in self._specs_cache:
                                try:
                                    with open(entry.path, "r", encoding="utf-8") as f:
                                        self._specs_cache[op_id] = json.loads(f.read())
                                except (json.JSONDecodeError, OSError) as e:
                                    logger.warning(f"[Workspace] NFS spec load failed {entry.name}: {e}")
                    if self._specs_cache:
                        logger.info(f"[Workspace] Loaded {len(self._specs_cache)} specs from NFS")
                        return self._specs_cache.copy()
            except Exception as e:
                logger.warning(f"[Workspace] NFS specs dir scan failed: {e}")

        # S3 fallback
        bucket = get_bucket_name()
        if not bucket:
            return self._specs_cache.copy()
        prefix = self._specs_s3_key() + "/"
        try:
            s3 = get_s3_client()
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".json"):
                        continue
                    op_id = key.split("/")[-1].replace(".json", "")
                    if op_id not in self._specs_cache:
                        raw = self._get_s3(key)
                        if raw:
                            try:
                                self._specs_cache[op_id] = json.loads(raw)
                            except json.JSONDecodeError:
                                pass
        except Exception as e:
            logger.error(f"[Workspace] Failed to list specs from S3: {e}")
        logger.info(f"[Workspace] Loaded {len(self._specs_cache)} specs total")
        return self._specs_cache.copy()

    # ── Session Flow Config ──────────────────────────────────────────────
    def save_flow_config(self, data: dict):
        """Persist SessionFlowConfig dict to S3."""
        self._save_json(["flow_config.json"], data)
        logger.info("[Workspace] Saved session flow config")

    def load_flow_config(self) -> Optional[dict]:
        """Load SessionFlowConfig from S3."""
        return self._load_json(["flow_config.json"])

    # ── Requirements (large text) ───────────────────────────────────────
    def save_requirement(self, doc_type: str, content: str, op_id: Optional[str] = None) -> bool:
        """Save a requirement document to NFS + S3. Returns True on success."""
        filename = f"{op_id}_{doc_type}.txt" if op_id else f"{doc_type}.txt"
        ok = self._put(["requirements", filename], content, content_type="text/plain; charset=utf-8")
        if ok:
            logger.info(f"[Workspace] Saved requirement: {filename} ({len(content)} chars)")
        else:
            logger.error(f"[Workspace] FAILED to save requirement: {filename} ({len(content)} chars)")
        return ok

    def load_requirement(self, doc_type: str, op_id: Optional[str] = None) -> Optional[str]:
        """Load a requirement document from NFS/S3."""
        filename = f"{op_id}_{doc_type}.txt" if op_id else f"{doc_type}.txt"
        return self._get(["requirements", filename])

    # ── Infrastructure Schema ───────────────────────────────────────────
    def save_schema(self, schema: dict):
        self._schema_cache = schema
        self._save_json(["schemas", "infrastructure.json"], schema)
        logger.info("[Workspace] Saved infrastructure schema")

    def load_schema(self) -> Optional[dict]:
        if self._schema_cache:
            return self._schema_cache
        data = self._load_json(["schemas", "infrastructure.json"])
        if data:
            self._schema_cache = data
        return data

    # ── Infrastructure Spec ────────────────────────────────────────────
    def save_infrastructure_spec(self, data: dict):
        """Persist InfrastructureSpec dict to S3."""
        self._save_json(["specs", "infrastructure_spec.json"], data)
        logger.info("[Workspace] Saved infrastructure spec")

    def load_infrastructure_spec(self) -> Optional[dict]:
        """Load InfrastructureSpec from S3."""
        return self._load_json(["specs", "infrastructure_spec.json"])

    # ── Progress Tracking ───────────────────────────────────────────────
    def save_progress(self, progress: dict):
        self._progress_cache = progress
        self._save_json(["progress.json"], progress)

    def load_progress(self) -> Optional[dict]:
        if self._progress_cache:
            return self._progress_cache
        data = self._load_json(["progress.json"])
        if data:
            self._progress_cache = data
        return data

    # ── Project Metadata ────────────────────────────────────────────────
    def save_project(self, meta: dict):
        self._save_json(["project.json"], meta)

    def load_project(self) -> Optional[dict]:
        return self._load_json(["project.json"])

    # ── Full Restore (session recovery) ─────────────────────────────────
    def restore_all(self) -> dict:
        """Restore entire project state from S3. Returns summary for orchestrator."""
        specs = self.load_all_specs()
        schema = self.load_schema()
        progress = self.load_progress() or {}
        project = self.load_project() or {}
        flow_config = self.load_flow_config()

        summary_lines = []
        if project:
            company = project.get("company_name", "")
            industry = project.get("industry", "")
            if company or industry:
                summary_lines.append(f"Company: {company} | Industry: {industry}")

        if specs:
            op_summaries = []
            for op_id, spec in specs.items():
                s = spec.get("summary", spec.get("description", ""))
                op_summaries.append(f"{op_id} ({s[:60]})" if s else op_id)
            summary_lines.append(f"Operations ({len(specs)}): " + ", ".join(op_summaries))

        if schema:
            tables = schema.get("tables", [])
            summary_lines.append(
                f"Infrastructure Schema: {len(tables)} table(s)"
            )

        if progress:
            completed = [k for k, v in progress.items() if v.get("status") == "completed"]
            in_progress = [k for k, v in progress.items() if v.get("status") == "in_progress"]
            if completed:
                summary_lines.append(f"Completed phases: {', '.join(completed)}")
            if in_progress:
                summary_lines.append(f"In-progress phases: {', '.join(in_progress)}")

        if flow_config:
            direction = flow_config.get("call_direction", "")
            session_tools = flow_config.get("session_tools", [])
            if direction or session_tools:
                summary_lines.append(
                    f"Flow Config: direction={direction}, session_tools={len(session_tools)}"
                )

        return {
            "specs": specs,
            "schema": schema,
            "progress": progress,
            "project": project,
            "flow_config": flow_config,
            "summary": "\n".join(summary_lines) if summary_lines else "No prior state found",
            "has_state": bool(specs or schema or progress or flow_config),
        }


# ---------------------------------------------------------------------------
# @tool wrappers — callable by orchestrator / sub-agents
# ---------------------------------------------------------------------------

@tool
def save_requirement_document(
    doc_type: str,
    content: str,
    operation_id: str = None,
) -> dict:
    """
    Save a large requirement document to S3 project workspace.

    Use this when the user provides a requirement text longer than 500
    characters. The original text is stored in S3 and only a short
    summary should remain in the conversation context.

    Args:
        doc_type: Document type — one of 'raw_input', 'script',
                  'questionnaire', 'scenario', 'custom'
        content: The full requirement text to persist
        operation_id: Optional operation_id if the document is specific
                      to one operation

    Returns:
        Confirmation with S3 key and a character count
    """
    ws = get_workspace()
    if not ws:
        return {"success": False, "error": "Workspace not initialised (no session)"}
    ok = ws.save_requirement(doc_type, content, operation_id)
    if not ok:
        return {
            "success": False,
            "error": f"Failed to write {doc_type} document to S3. Check bucket config and permissions.",
            "doc_type": doc_type,
            "operation_id": operation_id,
            "char_count": len(content),
        }

    # Stream as asset preview so customer can see and verify parsed requirements
    try:
        from tools.streaming_callback import stream_asset
        filename = f"{operation_id}_{doc_type}.txt" if operation_id else f"{doc_type}.txt"
        stream_asset(
            asset_type="requirement",
            file_name=filename,
            content=content,
            operation_id=operation_id,
            is_complete=True,
        )
    except Exception as e:
        logger.warning(f"[Workspace] Failed to stream requirement preview: {e}")

    return {
        "success": True,
        "doc_type": doc_type,
        "operation_id": operation_id,
        "char_count": len(content),
        "message": f"Saved {doc_type} document ({len(content)} chars) to S3 workspace.",
    }


@tool
def load_requirement_document(
    doc_type: str,
    operation_id: str = None,
) -> dict:
    """
    Load a previously saved requirement document from S3 workspace.

    Args:
        doc_type: Document type — 'raw_input', 'script', 'questionnaire', etc.
        operation_id: Optional operation_id for operation-specific docs

    Returns:
        The document content or an error if not found
    """
    ws = get_workspace()
    if not ws:
        return {"success": False, "error": "Workspace not initialised (no session)"}
    text = ws.load_requirement(doc_type, operation_id)
    if text:
        return {"success": True, "content": text, "char_count": len(text)}
    return {"success": False, "error": f"Document '{doc_type}' not found in workspace"}
