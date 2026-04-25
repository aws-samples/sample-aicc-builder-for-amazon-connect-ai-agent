"""
FAQ Generator Sub-Agent

This agent generates knowledge base documents from research results
for use with Amazon Bedrock Knowledge Bases and RAG systems.

Key capabilities:
- Generate FAQ documents from research findings
- Create structured knowledge base files
- Package files into downloadable ZIP archives
- Support multiple output formats (txt, md, json)
"""

from .agent import faq_generator_agent
from .system_prompt import (
    FAQ_GENERATOR_SYSTEM_PROMPT,
    FAQ_GENERATOR_STATIC_PROMPT,
)

__all__ = [
    "faq_generator_agent",
    "FAQ_GENERATOR_SYSTEM_PROMPT",
    "FAQ_GENERATOR_STATIC_PROMPT",
]
