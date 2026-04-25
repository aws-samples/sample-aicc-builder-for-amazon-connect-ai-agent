"""
Deterministic OpenAPI Fragment Merger

Merges base OpenAPI YAML with per-chunk YAML fragments.
No LLM involved - pure Python string manipulation.

Reads fragments from the openapi_generator_agent's internal registry,
so the orchestrator LLM doesn't need to pass huge YAML strings as tool params.

Usage by Orchestrator:
    merge_openapi_fragments(api_title="Hotel Reservation API")
"""

import logging
import re
from strands import tool

logger = logging.getLogger(__name__)

PATHS_ANCHOR = "# --- PATHS ANCHOR ---"
SCHEMAS_ANCHOR = "# --- SCHEMAS ANCHOR ---"
SCHEMAS_SEPARATOR = "# --- SCHEMAS SECTION ---"


def _clean_fragment(fragment: str) -> str:
    """Extract YAML content from a fragment, stripping markdown fences if present."""
    match = re.search(r'```(?:yaml|yml)?\s*\n(.*?)```', fragment, re.DOTALL)
    if match:
        return match.group(1).rstrip("\n")
    return fragment.strip()


def _normalize_indent(content: str, target: int, marker_re: str) -> str:
    """Normalize indentation so every top-level entry (matching marker_re) sits at *target* spaces.

    Handles chunks where different entries have different indentation
    (e.g. first path at 2-space, subsequent paths at 4-space).
    Each entry block is detected and shifted independently.
    """
    lines = content.split('\n')
    full_marker = re.compile(r'^(\s*)' + marker_re)

    # Find indices of all top-level entry lines
    entry_starts = []
    for i, line in enumerate(lines):
        m = full_marker.match(line)
        if m:
            entry_starts.append((i, len(m.group(1))))

    if not entry_starts:
        return content

    # Check if all entries already at target
    if all(indent == target for _, indent in entry_starts):
        return content

    # Process each entry block independently
    out = []
    for idx, (start, current_indent) in enumerate(entry_starts):
        # Lines before first entry (e.g. blank lines, comments)
        if idx == 0 and start > 0:
            out.extend(lines[:start])

        # Determine block end
        end = entry_starts[idx + 1][0] if idx + 1 < len(entry_starts) else len(lines)

        delta = target - current_indent
        if delta == 0:
            out.extend(lines[start:end])
        else:
            for line in lines[start:end]:
                if not line.strip():
                    out.append('')
                elif delta > 0:
                    out.append(' ' * delta + line)
                else:
                    cut = min(abs(delta), len(line) - len(line.lstrip()))
                    out.append(line[cut:])

    return '\n'.join(out)


def _dedup_paths(paths_content: str) -> str:
    """Remove duplicate path entries, keeping the first occurrence.
    
    Detects paths by operationId (most reliable) or by path key (fallback).
    """
    lines = paths_content.split('\n')
    seen_ops = set()
    out = []
    skip_until_next = False

    # Detect indent of path keys (lines starting with spaces + /)
    path_indent = None
    for line in lines:
        m = re.match(r'^(\s*)(/.+):\s*$', line)
        if m:
            path_indent = len(m.group(1))
            break
    if path_indent is None:
        return paths_content

    path_re = re.compile(r'^' + ' ' * path_indent + r'(/.+):\s*$')
    op_re = re.compile(r'^\s+operationId:\s*(\S+)')

    # Two-pass: first collect operationId per path block, then dedup
    blocks = []  # list of (start_line, op_id_or_path, lines)
    current_block = []
    current_key = None

    for line in lines:
        pm = path_re.match(line)
        if pm:
            if current_block and current_key:
                blocks.append((current_key, current_block))
            current_block = [line]
            current_key = pm.group(1)  # path as fallback key
        else:
            current_block.append(line)
            om = op_re.match(line)
            if om and current_key:
                current_key = om.group(1)  # upgrade to operationId
    if current_block and current_key:
        blocks.append((current_key, current_block))

    for key, block_lines in blocks:
        if key in seen_ops:
            logger.info(f"[MERGE_OPENAPI] Dedup: removing duplicate path '{key}'")
            continue
        seen_ops.add(key)
        out.extend(block_lines)

    return '\n'.join(out)


def _dedup_schemas(schemas_content: str) -> str:
    """Remove duplicate schema definitions, keeping the first occurrence."""
    lines = schemas_content.split('\n')
    seen = set()
    out = []
    skip_until_next = False
    # Detect indent of schema names (first non-empty line matching word + colon)
    schema_indent = None
    for line in lines:
        m = re.match(r'^(\s+)(\w+):\s*$', line)
        if m:
            schema_indent = len(m.group(1))
            break
    if schema_indent is None:
        return schemas_content

    for line in lines:
        m = re.match(r'^' + ' ' * schema_indent + r'(\w+):\s*$', line)
        if m:
            name = m.group(1)
            if name in seen:
                skip_until_next = True
                logger.info(f"[MERGE_OPENAPI] Dedup: removing duplicate schema '{name}'")
                continue
            seen.add(name)
            skip_until_next = False
        elif skip_until_next:
            # Still inside a duplicate schema block — skip lines that are more indented
            if line.strip() and not line.startswith(' ' * (schema_indent + 1)):
                skip_until_next = False  # reached next top-level item
            else:
                continue
        out.append(line)
    return '\n'.join(out)


def _split_chunk(chunk_content: str) -> tuple[str, str]:
    """Split a chunk into paths section and schemas section using the separator.

    Returns:
        (paths_content, schemas_content)
    """
    cleaned = _clean_fragment(chunk_content)

    if SCHEMAS_SEPARATOR in cleaned:
        parts = cleaned.split(SCHEMAS_SEPARATOR, 1)
        paths_part = parts[0].rstrip("\n")
        schemas_part = parts[1].lstrip("\n")
    else:
        # Fallback: try to detect schemas by indentation pattern (4-space indent + type: object)
        logger.warning("[MERGE_OPENAPI] No schemas separator found, attempting heuristic split")
        # Look for the first schema definition (4-space indented name ending with Request/Response:)
        schema_match = re.search(r'\n(    \w+(?:Request|Response):\s*\n)', cleaned)
        if schema_match:
            split_idx = schema_match.start()
            paths_part = cleaned[:split_idx].rstrip("\n")
            schemas_part = cleaned[split_idx:].lstrip("\n")
        else:
            # Can't split — treat everything as paths
            logger.warning("[MERGE_OPENAPI] Could not split chunk, treating all as paths")
            paths_part = cleaned
            schemas_part = ""

    # Remove the paths section header comment if present
    paths_part = re.sub(r'^# --- PATHS SECTION ---\s*\n?', '', paths_part).strip("\n")

    return paths_part, schemas_part


@tool
def merge_openapi_fragments(api_title: str) -> dict:
    """
    Merge base OpenAPI template with operation chunk fragments.

    This is a deterministic tool (no LLM). It reads the base template and
    chunk fragments from the openapi_generator_agent's internal registry
    (stored during mode="base" and mode="chunk" calls), merges them,
    and streams the final result to the frontend.

    Args:
        api_title: API title (must match the api_title used in
                   prior openapi_generator_agent calls)

    Returns:
        dict with success status and merge info
    """
    from agents.openapi_generator.agent import get_fragments, clear_fragments

    logger.info(f"[MERGE_OPENAPI] Starting merge for '{api_title}'")

    data = get_fragments(api_title)
    if not data or not data.get("base"):
        return {"success": False, "error": f"No base template found for '{api_title}'. Call openapi_generator_agent(mode='base') first."}

    base_yaml = data["base"]
    chunks = data.get("chunks", {})

    if not chunks:
        return {"success": False, "error": f"No chunk fragments found for '{api_title}'. Call openapi_generator_agent(mode='chunk') first."}

    logger.info(f"[MERGE_OPENAPI] Merging {len(chunks)} chunks into base ({len(base_yaml)} chars)")

    # Detect target indentation from anchor positions in base template
    paths_target = 2  # default
    schemas_target = 4  # default
    for line in base_yaml.split('\n'):
        if PATHS_ANCHOR in line:
            paths_target = len(line) - len(line.lstrip())
            break
    for line in base_yaml.split('\n'):
        if SCHEMAS_ANCHOR in line:
            schemas_target = len(line) - len(line.lstrip())
            break
    logger.info(f"[MERGE_OPENAPI] Detected indent targets: paths={paths_target}, schemas={schemas_target}")

    # Split each chunk into paths and schemas, normalizing indentation
    all_paths = []
    all_schemas = []
    for chunk_key, chunk_content in chunks.items():
        paths_part, schemas_part = _split_chunk(chunk_content)
        if paths_part:
            paths_part = _normalize_indent(paths_part, paths_target, r'/')
            all_paths.append(paths_part)
        if schemas_part:
            schemas_part = _normalize_indent(schemas_part, schemas_target, r'[A-Z]\w*:\s*$')
            all_schemas.append(schemas_part)
        logger.info(f"[MERGE_OPENAPI] Chunk '{chunk_key[:50]}': paths={len(paths_part)} chars, schemas={len(schemas_part)} chars")

    # Insert paths at PATHS_ANCHOR (replace entire anchor line to avoid prefix leak).
    # NOTE: pass replacement via a lambda so backslash sequences in fragment YAML
    # (e.g. regex patterns like `^\d{12}$`, `\.`, `\w+` in `pattern:` fields)
    # are treated as literals. Passing the string directly triggers
    # `re.error: bad escape \d at position ...` because `re.sub` interprets
    # the replacement as a template with backrefs like `\1`, `\g<name>`.
    paths_combined = _dedup_paths("\n\n".join(all_paths))
    if PATHS_ANCHOR in base_yaml:
        merged = re.sub(
            r'^[ \t]*' + re.escape(PATHS_ANCHOR) + r'[ \t]*$',
            lambda _m: paths_combined, base_yaml, count=1, flags=re.MULTILINE
        )
    else:
        logger.warning("[MERGE_OPENAPI] No paths anchor found, inserting after 'paths:'")
        merged = base_yaml.replace("paths:\n", f"paths:\n{paths_combined}\n", 1)

    # Insert schemas at SCHEMAS_ANCHOR (replace entire anchor line).
    # Same lambda trick — schemas commonly contain `pattern: '^\d+$'` etc.
    schemas_combined = _dedup_schemas("\n\n".join(all_schemas))
    if SCHEMAS_ANCHOR in merged:
        merged = re.sub(
            r'^[ \t]*' + re.escape(SCHEMAS_ANCHOR) + r'[ \t]*$',
            lambda _m: schemas_combined, merged, count=1, flags=re.MULTILINE
        )
    else:
        logger.warning("[MERGE_OPENAPI] No schemas anchor found, inserting after 'schemas:'")
        # Find the last 'schemas:' (under components)
        schemas_idx = merged.rfind("schemas:\n")
        if schemas_idx != -1:
            insert_at = schemas_idx + len("schemas:\n")
            merged = merged[:insert_at] + schemas_combined + "\n" + merged[insert_at:]

    final_yaml = merged

    # Final dedup pass: if base template contained paths (LLM ignoring instructions),
    # the merged result will have duplicates. Extract and dedup the entire paths section.
    paths_match = re.search(r'^(paths:\s*\n)(.*?)(?=^\S)', final_yaml, re.MULTILINE | re.DOTALL)
    if paths_match:
        paths_header = paths_match.group(1)
        paths_body = paths_match.group(2)
        deduped_body = _dedup_paths(paths_body)
        if deduped_body != paths_body:
            logger.info("[MERGE_OPENAPI] Final dedup pass removed duplicate paths from merged output")
            final_yaml = final_yaml[:paths_match.start(2)] + deduped_body + final_yaml[paths_match.end(2):]

    logger.info(f"[MERGE_OPENAPI] Final spec: {len(final_yaml)} chars")

    # Stream to frontend + save to S3
    op_id = api_title.replace(" ", "_").lower()
    try:
        from tools.streaming_callback import stream_asset, clear_asset_preview_cache, get_session_id
        from tools.s3_asset_storage import save_asset_to_s3

        clear_asset_preview_cache("openapi", "openapi.yaml", op_id)

        MAX_CHUNK = 15000
        for i in range(0, len(final_yaml), MAX_CHUNK):
            chunk_end = min(i + MAX_CHUNK, len(final_yaml))
            is_last = chunk_end >= len(final_yaml)
            stream_asset("openapi", "openapi.yaml", final_yaml[:chunk_end],
                         operation_id=op_id, is_complete=is_last)

        session_id = get_session_id()
        if session_id:
            s3_key = save_asset_to_s3(
                session_id=session_id, asset_type="openapi",
                file_name="openapi.yaml", content=final_yaml,
                operation_id=op_id,
            )
            logger.info(f"[MERGE_OPENAPI] Saved to S3: {s3_key}")
    except Exception as e:
        logger.error(f"[MERGE_OPENAPI] Streaming/S3 error: {e}")

    # Clean up registry
    clear_fragments(api_title)

    return {
        "success": True,
        "api_title": api_title,
        "chunk_count": len(chunks),
        "total_chars": len(final_yaml),
        "summary": f"Merged {len(chunks)} chunks into openapi.yaml ({len(final_yaml)} chars)",
    }
