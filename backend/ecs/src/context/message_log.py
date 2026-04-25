"""
NFS-backed Message Log for WebSocket event replay.

Each agent invocation writes all WebSocket events (stream_start, text, tool_start,
tool_end, asset_preview, stream_end, etc.) to an append-only log file on NFS.

When a client reconnects mid-invocation, it reads events it missed via the
REST endpoint GET /api/message-log/{session_id}?after_seq=N.

NFS Layout:
    /mnt/s3/sessions/{session_id}/message_log/current.jsonl

Each line is a JSON object with a monotonically increasing "seq" field.
"""

import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Module-level cache: mount_path+session_id → MessageLog instance
_logs: Dict[str, "MessageLog"] = {}
_logs_lock = threading.Lock()


class MessageLog:
    """Append-only log of WebSocket events for a single agent invocation."""

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._log_file = log_dir / "current.jsonl"
        self._seq = 0
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Reset the log for a new agent invocation."""
        with self._lock:
            self._seq = 0
            try:
                os.makedirs(self._log_dir, exist_ok=True)
                # Truncate the file
                with open(self._log_file, "w", encoding="utf-8") as f:
                    pass  # empty file
            except Exception as e:
                logger.warning(f"[message_log] clear failed: {e}")

    def append(self, data: Dict[str, Any]) -> int:
        """Append an event to the log. Returns the assigned sequence number.

        Storage format matches the frontend MessageLogEntry interface:
            { seq: number, ts: number, event: Record<string, unknown> }
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
        entry = {"seq": seq, "ts": time.time(), "event": data}
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"[message_log] append failed (seq={seq}): {e}")
        return seq

    def read_after(self, after_seq: int = 0) -> List[Dict[str, Any]]:
        """Read all entries with seq > after_seq."""
        entries: List[Dict[str, Any]] = []
        try:
            if not self._log_file.exists():
                return entries
            with open(self._log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("seq", 0) > after_seq:
                            entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"[message_log] read_after failed: {e}")
        return entries


def get_message_log(mount_path: str, session_id: str) -> MessageLog:
    """Get or create a MessageLog for the given session."""
    safe_id = session_id.replace("..", "_").replace("/", "_")
    cache_key = f"{mount_path}:{safe_id}"

    with _logs_lock:
        if cache_key not in _logs:
            log_dir = Path(mount_path) / "sessions" / safe_id / "message_log"
            _logs[cache_key] = MessageLog(log_dir)
        return _logs[cache_key]
