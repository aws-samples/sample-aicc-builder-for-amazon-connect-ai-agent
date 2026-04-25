"""
Lambda Generator Sub-Agent

Generates production-ready AWS Lambda functions.
"""

from .agent import lambda_generator_agent, set_callback_handler
from .system_prompt import LAMBDA_GENERATOR_SYSTEM_PROMPT

__all__ = [
    "lambda_generator_agent",
    "set_callback_handler",
    "LAMBDA_GENERATOR_SYSTEM_PROMPT",
]
