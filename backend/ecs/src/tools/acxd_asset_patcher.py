"""Patch-only modification path for ACXD assets (Task 15).

Mirrors the Classic ``modification_request`` philosophy (patch, never
regenerate) with a stronger guarantee: every patch is applied to the NFS
asset file, then the FULL deterministic validation stack runs (asset
schema + cross-asset consistency + determinism contract). If anything
fails, the patch is **rolled back** and the violations are returned —
a modification can never leave the bundle in an inconsistent state.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from strands import tool

from tools.acxd_bundle import load_acxd_bundle
from tools.acxd_generation_context import get_acxd_spec
from tools.session_context import current_session_id
from tools.validate_acxd_consistency import validate_acxd_consistency
from tools.validate_acxd_flow import SCHEMA_FILES, validate_acxd_asset

logger = logging.getLogger(__name__)

#: asset_type directory → contract schema kind (None = bundle-only contract)
ASSET_KIND = {
    "acxd_flow": "flow",
    "acxd_slot_type": "slot_type",
    "acxd_data_request": "data_request",
    "acxd_guardrail": "guardrail",
    "acxd_knowledge_base": "knowledge_base",
    "acxd_application": "application",
    "acxd_context_variable": None,
    "acxd_secret": None,
    "contact_flow": None,
}


def _assets_root(session_id: str) -> Optional[Path]:
    mount = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    if not os.path.isdir(mount):
        return None
    safe = session_id.replace("..", "_").replace("/", "_")
    return Path(mount) / "sessions" / safe / "assets"


def _find_asset_file(root: Path, asset_type: str, file_name: str) -> Optional[Path]:
    base = root / asset_type
    if not base.is_dir():
        return None
    direct = base / file_name
    if direct.is_file():
        return direct
    matches = sorted(base.rglob(file_name))
    return matches[0] if matches else None


@tool
def list_acxd_assets() -> dict:
    """List every generated ACXD asset file (for modification requests)."""
    session_id = current_session_id.get() or "default"
    root = _assets_root(session_id)
    if root is None:
        return {"status": "error", "problems": ["no NFS mount available"]}
    out: dict[str, list[str]] = {}
    for asset_type in ASSET_KIND:
        base = root / asset_type
        if base.is_dir():
            out[asset_type] = sorted(
                str(p.relative_to(base)) for p in base.rglob("*.*") if p.is_file()
            )
    return {"status": "ok", "assets": out}


@tool
def read_acxd_asset(asset_type: str, file_name: str,
                    contains: str = "") -> dict:
    """Read a generated ACXD asset before patching it.

    ``patch_acxd_asset`` needs an ``old_str`` that occurs EXACTLY once, and
    without this tool the only way to produce one was to guess. A live
    modification request failed three times on guessed Korean wording and the
    agent stopped — correctly, but it had no way to succeed. Classic sub-agents
    have had ``read_current_file`` for exactly this reason; ACXD now matches, so
    review-driven edits behave the same in both modes.

    Args:
        asset_type: one of acxd_flow, acxd_data_request, acxd_guardrail,
            acxd_knowledge_base, acxd_application, contact_flow
        file_name: the asset file name (e.g. 'deliveryLookup.json')
        contains: optional substring — when given, also returns every line that
            contains it with its line number, so a unique excerpt for the patch
            can be copied verbatim instead of reconstructed.
    """
    if asset_type not in ASSET_KIND:
        return {"status": "error",
                "problems": [f"unknown asset_type {asset_type!r}; expected one "
                             f"of {sorted(ASSET_KIND)}"]}
    session_id = current_session_id.get() or "default"
    root = _assets_root(session_id)
    if root is None:
        return {"status": "error", "problems": ["no NFS mount available"]}
    path = _find_asset_file(root, asset_type, file_name)
    if path is None:
        available = []
        base = root / asset_type
        if base.is_dir():
            available = sorted(str(p.relative_to(base)) for p in base.rglob("*.*")
                               if p.is_file())
        return {"status": "error",
                "problems": [f"no {asset_type} asset named {file_name!r}"],
                "available": available}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"status": "error", "problems": [f"could not read {file_name}: {e}"]}

    result = {
        "status": "ok",
        "asset_type": asset_type,
        "file_name": file_name,
        "content": content,
        "line_count": content.count("\n") + 1,
    }
    if contains:
        matches = [
            {"line": i, "text": line}
            for i, line in enumerate(content.splitlines(), start=1)
            if contains in line
        ]
        result["matches"] = matches
        result["match_count"] = len(matches)
        if not matches:
            result["hint"] = (
                f"{contains!r} does not appear in this file — read the content "
                "above and copy an excerpt from it verbatim."
            )
        elif len(matches) > 1:
            result["hint"] = (
                f"{contains!r} appears {len(matches)} times, so it is not unique. "
                "Extend the excerpt with neighbouring text before patching."
            )
    return result


@tool
def patch_acxd_asset(asset_type: str, file_name: str,
                     old_str: str, new_str: str) -> dict:
    """Apply a minimal patch to one generated ACXD asset (patch-only rule).

    Call ``read_acxd_asset`` FIRST and copy ``old_str`` out of its content —
    guessing the wording wastes attempts and cannot be relied on for
    non-English text.

    The patch is applied, then the full deterministic validation stack
    runs (asset schema + cross-asset consistency + the interview's
    confirmed determinism decisions). On ANY violation the file is
    rolled back and the problems are returned — fix the patch and retry.
    Full-file regeneration is not allowed for modification requests.

    Args:
        asset_type: one of acxd_flow, acxd_data_request, acxd_guardrail,
            acxd_knowledge_base, acxd_application, contact_flow
        file_name: the asset file name (e.g. 'RefundFlow.json')
        old_str: exact text to replace (must occur exactly once)
        new_str: replacement text
    """
    if asset_type not in ASSET_KIND:
        return {"status": "error",
                "problems": [f"unknown asset_type {asset_type!r}; expected one "
                             f"of {sorted(ASSET_KIND)}"]}
    session_id = current_session_id.get() or "default"
    root = _assets_root(session_id)
    if root is None:
        return {"status": "error", "problems": ["no NFS mount available"]}
    target = _find_asset_file(root, asset_type, file_name)
    if target is None:
        return {"status": "error",
                "problems": [f"asset file not found: {asset_type}/{file_name} "
                             "(use list_acxd_assets)"]}

    original = target.read_text(encoding="utf-8")
    occurrences = original.count(old_str)
    if occurrences == 0:
        # Hand back the content instead of only saying "not found". A live
        # session burned three attempts guessing Korean wording and then stopped,
        # because nothing in the failure told it what the file actually said.
        return {"status": "error",
                "problems": ["old_str not found in the asset — copy an exact "
                             "excerpt from `content` below (do not retype it)"],
                "content": original,
                "line_count": original.count("\n") + 1,
                "hint": "read_acxd_asset(asset_type, file_name, contains=...) "
                        "also locates candidate lines."}
    if occurrences > 1:
        lines = [
            {"line": i, "text": line}
            for i, line in enumerate(original.splitlines(), start=1)
            if old_str in line
        ]
        return {"status": "error",
                "problems": [f"old_str occurs {occurrences} times — include "
                             "more surrounding context to make it unique"],
                "matches": lines[:10]}

    patched = original.replace(old_str, new_str, 1)

    # --- validate the patched document ------------------------------------
    problems: list[str] = []
    doc = None
    if target.suffix == ".json":
        try:
            doc = json.loads(patched)
        except json.JSONDecodeError as e:
            problems.append(f"patched file is not valid JSON: {e}")
        else:
            kind = ASSET_KIND[asset_type]
            if kind in SCHEMA_FILES:
                problems += validate_acxd_asset(kind, doc)

    if not problems and target.suffix == ".json":
        # Cross-asset + determinism validation with the patch in place.
        target.write_text(patched, encoding="utf-8")
        try:
            spec = get_acxd_spec().model_dump()
            bundle = load_acxd_bundle(session_id)
            problems += [str(v) for v in validate_acxd_consistency(bundle, spec=spec)]
        finally:
            if problems:
                target.write_text(original, encoding="utf-8")  # rollback
    elif not problems:
        target.write_text(patched, encoding="utf-8")

    if problems:
        return {"status": "rolled_back", "problems": problems}

    # dual-write to S3 (best-effort; NFS is the source of truth locally)
    try:
        from tools.s3_asset_storage import save_asset_to_s3
        from tools.streaming_callback import stream_asset
        rel = target.relative_to(root / asset_type)
        operation_id = str(rel.parent) if str(rel.parent) != "." else None
        s3_key = save_asset_to_s3(session_id, asset_type, target.name, patched)
        stream_asset(
            asset_type,
            target.name,
            patched,
            operation_id=operation_id,
            is_complete=True,
            s3_key=s3_key,
            force_full=True,
        )
    except Exception:  # pragma: no cover
        pass

    return {"status": "patched", "asset": f"{asset_type}/{file_name}",
            "chars_changed": abs(len(new_str) - len(old_str)),
            "validation": "bundle consistent (0 violations)"}
