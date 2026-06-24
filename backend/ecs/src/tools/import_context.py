"""
Cross-turn stash for an uploaded image's raw bytes, keyed by session.

When the user attaches a flow-diagram image, the bytes arrive only inside the
message's multimodal content blocks ({"image": {"source": {"bytes": ...}}}) — they
are never written to disk. The orchestrator can't pass raw bytes through a tool
call, so the WS handler stashes the image here when it arrives, and
`draft_flow_from_image_tool` reads it when the agent (after asking the user)
decides to transcribe the diagram into a Contact Flow.

IMPORTANT: the upload and the "yes, convert it" confirmation usually happen on
DIFFERENT turns (the agent acknowledges + asks first, then converts on a later
turn). So the stash must persist ACROSS turns — it is keyed by session id and
held until it is consumed by the conversion tool, replaced by a newer upload, or
the session is cleared. (A per-turn ContextVar would be gone by the time the user
confirms.) At most one image per session is retained; images are <= 3.75 MB.
"""
import logging
import threading
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# session_id -> (image_bytes, image_format). Guarded by a lock for the rare
# concurrent-session case. Bounded to one image per active session.
_pending_images: Dict[str, Tuple[bytes, str]] = {}
_lock = threading.Lock()


def set_pending_import_image(session_id: str, image_bytes: bytes, image_format: str = "png") -> None:
    """Stash the uploaded image bytes for this session (persists across turns)."""
    if not session_id or not image_bytes:
        return
    with _lock:
        _pending_images[session_id] = (image_bytes, (image_format or "png").lower())
    logger.info(f"[importImage] stashed {len(image_bytes)} bytes for {session_id} ({image_format})")


def get_pending_import_image(session_id: str) -> Optional[Tuple[bytes, str]]:
    """Return (image_bytes, image_format) stashed for this session, or None."""
    if not session_id:
        return None
    with _lock:
        return _pending_images.get(session_id)


def clear_pending_import_image(session_id: str) -> None:
    """Drop the stashed image for this session (after it's been consumed)."""
    if not session_id:
        return
    with _lock:
        _pending_images.pop(session_id, None)
