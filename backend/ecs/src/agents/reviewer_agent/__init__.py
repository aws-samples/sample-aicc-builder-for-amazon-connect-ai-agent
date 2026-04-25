"""
Reviewer Agent Sub-Agent

Reviews generated assets for consistency, validates dependencies between assets,
and provides modification suggestions.

This agent is called after asset generation to ensure:
1. OpenAPI spec structure is valid
2. Lambda code matches OpenAPI definitions
3. Field names are consistent across Lambda/OpenAPI/Prompt
4. CloudFormation references are correct
5. Contact Flow transitions are valid
"""

from .agent import reviewer_agent, set_callback_handler
from .system_prompt import REVIEWER_AGENT_SYSTEM_PROMPT

__all__ = [
    "reviewer_agent",
    "set_callback_handler",
    "REVIEWER_AGENT_SYSTEM_PROMPT",
]
