"""Path segments built from request data.

Session ids, asset types and file names arrive from the client and end up as
directory names under the NFS mount. Replacing ``..`` and ``/`` is not enough
(``.\\.`` on some filesystems, control characters, a lone ``.``), so a segment
is accepted only when it matches a strict allowlist, and every path built from
one is normalised and checked to still lie under its base directory.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

#: A single directory or file name: letters, digits, dot, underscore, hyphen.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def safe_segment(value: object) -> Optional[str]:
    """``value`` as a single path segment, or None when it is not one."""
    text = str(value or "").strip()
    if not text or text in (".", "..") or not _SEGMENT.fullmatch(text):
        return None
    return text


def path_under(base: Path, *segments: object) -> Optional[Path]:
    """``base/seg1/seg2/…`` when every segment is safe and the normalised result
    is still inside ``base``; None otherwise."""
    parts = [safe_segment(s) for s in segments]
    if not segments or any(p is None for p in parts):
        return None
    root = os.path.normpath(str(base))
    candidate = os.path.normpath(os.path.join(root, *[p for p in parts if p]))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return Path(candidate)
