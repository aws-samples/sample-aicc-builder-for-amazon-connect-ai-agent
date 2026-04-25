"""
Fallback Asset Streaming Tool for Orchestrator

When a Sub-Agent fails to parse its output but returns raw_response,
the Orchestrator can use this tool to:
1. Attempt to parse the raw_response
2. Stream the parsed content to the Frontend

This allows the Orchestrator to recover from Sub-Agent parsing failures
and still provide the generated content to the user.
"""

import re
import logging
from strands import tool
from .streaming_callback import stream_asset

logger = logging.getLogger(__name__)


def _parse_python_code(text: str) -> tuple[str | None, str]:
    """Parse Python code from text with multiple patterns."""
    # Pattern 1: ```python
    pattern1 = r'```python\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_python"

    # Pattern 2: Generic code block
    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        code = match.group(1).strip()
        if 'def ' in code or 'import ' in code or 'class ' in code:
            return code, "markdown_generic"

    # Pattern 3: Raw code detection
    lines = text.strip().split('\n')
    if lines:
        first = lines[0].strip()
        if first.startswith(('"""', "'''", 'import ', 'from ', 'def ', 'class ', '# ')):
            code_lines = []
            for line in lines:
                if line.strip().startswith(('This code', 'The above', 'This ')):
                    break
                code_lines.append(line)
            if code_lines:
                return '\n'.join(code_lines).strip(), "raw_python"

    return None, "no_match"


def _parse_yaml_content(text: str) -> tuple[str | None, str]:
    """Parse YAML content from text with multiple patterns."""
    # Pattern 1: ```yaml or ```yml
    pattern1 = r'```(?:yaml|yml)\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_yaml"

    # Pattern 2: Generic code block with YAML markers
    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        # Check for OpenAPI or prompt YAML markers
        if any(marker in content for marker in ['openapi:', 'swagger:', 'paths:', 'agent_name:', 'persona:', 'system_prompt:']):
            return content, "markdown_generic"

    # Pattern 3: Raw YAML detection
    lines = text.strip().split('\n')
    if lines:
        first = lines[0].strip()
        if first.startswith(('openapi:', 'swagger:', 'agent_name:', 'persona:', '---')):
            yaml_lines = []
            for line in lines:
                if line.strip().startswith(('This ', 'The above')):
                    break
                yaml_lines.append(line)
            if yaml_lines:
                return '\n'.join(yaml_lines).strip(), "raw_yaml"

    return None, "no_match"


def _parse_json_content(text: str) -> tuple[str | None, str]:
    """Parse JSON content from text with multiple patterns."""
    # Pattern 1: ```json
    pattern1 = r'```json\s*\n(.*?)```'
    match = re.search(pattern1, text, re.DOTALL)
    if match:
        return match.group(1).strip(), "markdown_json"

    # Pattern 2: Generic code block
    pattern2 = r'```\s*\n(.*?)```'
    match = re.search(pattern2, text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        if content.startswith('{') and ('"Version"' in content or '"Actions"' in content):
            return content, "markdown_generic"

    # Pattern 3: Raw JSON detection
    lines = text.strip().split('\n')
    if lines and lines[0].strip().startswith('{'):
        brace_count = 0
        json_lines = []
        for line in lines:
            json_lines.append(line)
            brace_count += line.count('{') - line.count('}')
            if brace_count == 0 and json_lines:
                break
        if json_lines:
            candidate = '\n'.join(json_lines).strip()
            if '"Version"' in candidate or '"Actions"' in candidate:
                return candidate, "raw_json"

    return None, "no_match"


@tool
def stream_fallback_asset(
    asset_type: str,
    raw_response: str,
    operation_id: str = "",
    file_name: str = ""
) -> dict:
    """
    Parse and stream a fallback asset when Sub-Agent parsing failed.

    Use this tool when a Sub-Agent returns success=False with raw_response.
    This tool will attempt to parse the content and stream it to the frontend.

    Args:
        asset_type: Type of asset - "lambda", "openapi", "prompt", or "contact_flow"
        raw_response: The raw LLM response that failed to parse in the Sub-Agent
        operation_id: Operation identifier (e.g., "create_reservation")
        file_name: Output file name (e.g., "handler.py"). If empty, uses default for asset_type.

    Returns:
        dict with success status, parsed content (if any), and parse_method used
    """
    logger.info(f"Fallback parsing for {asset_type}, operation_id={operation_id}, response_len={len(raw_response)}")

    content = None
    parse_method = "no_match"

    # Parse based on asset type
    if asset_type == "lambda":
        content, parse_method = _parse_python_code(raw_response)
        if not file_name:
            file_name = "handler.py"
        language = "python"

    elif asset_type in ("openapi", "prompt"):
        content, parse_method = _parse_yaml_content(raw_response)
        if not file_name:
            file_name = "openapi.yaml" if asset_type == "openapi" else f"{operation_id}_prompt.yaml"
        language = "yaml"

    elif asset_type == "contact_flow":
        content, parse_method = _parse_json_content(raw_response)
        if not file_name:
            file_name = f"{operation_id}.json"
        language = "json"

    else:
        return {
            "success": False,
            "error": f"Unknown asset_type: {asset_type}",
            "parse_method": "unsupported_type"
        }

    if content:
        # Stream to frontend
        try:
            stream_asset(
                asset_type=asset_type,
                file_name=file_name,
                content=content,
                operation_id=operation_id,
                is_complete=True
            )
            logger.info(f"Fallback stream successful: {asset_type}/{file_name}, method={parse_method}")

            return {
                "success": True,
                "asset_type": asset_type,
                "file_name": file_name,
                "operation_id": operation_id,
                "parse_method": parse_method,
                "content_length": len(content),
                "summary": f"Successfully parsed and streamed {asset_type} using fallback ({parse_method})"
            }
        except Exception as e:
            logger.error(f"Fallback stream error: {e}")
            return {
                "success": False,
                "error": f"Stream failed: {str(e)}",
                "parse_method": parse_method
            }
    else:
        logger.warning(f"Fallback parsing failed for {asset_type}, no content extracted")
        return {
            "success": False,
            "asset_type": asset_type,
            "operation_id": operation_id,
            "error": "Could not parse content from raw_response",
            "parse_method": parse_method,
            "raw_response_preview": raw_response[:500] if raw_response else ""
        }
