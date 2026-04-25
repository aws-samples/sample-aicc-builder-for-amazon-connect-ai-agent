"""
Research Agent Sub-Agent

This agent performs web research to gather information about companies,
their services, and any relevant business data that can be used to
generate FAQ documents, Lambda functions, and other assets.

Key capabilities:
- Web search using Brave Search API
- Website content fetching and analysis
- Information extraction and summarization
- Research result structuring for downstream agents
"""

from .agent import research_agent
from .system_prompt import (
    RESEARCH_AGENT_SYSTEM_PROMPT,
    RESEARCH_AGENT_STATIC_PROMPT,
)

__all__ = [
    "research_agent",
    "RESEARCH_AGENT_SYSTEM_PROMPT",
    "RESEARCH_AGENT_STATIC_PROMPT",
]
