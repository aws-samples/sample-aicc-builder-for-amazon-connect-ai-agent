"""
Unified Diff Utility

Generates unified diff text from original and modified content.
Used by workspace_file_tools to emit diff previews to the frontend.
"""

import difflib

_MAX_DIFF_SIZE = 50_000  # 50KB cap


def generate_unified_diff(
    original: str,
    modified: str,
    file_name: str = "file",
    context_lines: int = 3,
) -> str | None:
    """
    Generate a unified diff between original and modified content.

    Args:
        original: The original file content
        modified: The modified file content
        file_name: File name for the diff header
        context_lines: Number of context lines around changes

    Returns:
        Unified diff string, or None if there are no changes
    """
    if original == modified:
        return None

    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile=f"a/{file_name}",
        tofile=f"b/{file_name}",
        n=context_lines,
    ))

    if not diff_lines:
        return None

    diff_text = "".join(diff_lines)

    # Apply size cap
    if len(diff_text) > _MAX_DIFF_SIZE:
        diff_text = diff_text[:_MAX_DIFF_SIZE] + f"\n\n... [diff truncated, {len(diff_text)} chars total]"

    return diff_text
