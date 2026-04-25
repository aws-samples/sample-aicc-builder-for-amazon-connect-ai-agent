"""
OpenAPI Generator Sub-Agent

Generates OpenAPI 3.0 specifications with MCP Gateway extensions.
Supports full, base, and chunk modes for chunked generation.
"""

from .agent import openapi_generator_agent, set_callback_handler
from .system_prompt import OPENAPI_GENERATOR_SYSTEM_PROMPT

__all__ = [
    "openapi_generator_agent",
    "set_callback_handler",
    "OPENAPI_GENERATOR_SYSTEM_PROMPT",
]
