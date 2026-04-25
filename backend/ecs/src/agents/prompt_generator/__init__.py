"""
Prompt Generator Sub-Agent

Generates Amazon Connect AI Agent prompts.
"""

from .agent import prompt_generator_agent, set_callback_handler
from .system_prompt import PROMPT_GENERATOR_SYSTEM_PROMPT

__all__ = [
    "prompt_generator_agent",
    "set_callback_handler",
    "PROMPT_GENERATOR_SYSTEM_PROMPT",
]
