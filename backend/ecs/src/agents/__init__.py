"""
Sub-Agents for AICC Builder

Each sub-agent is a specialized expert that can be called as a tool by the orchestrator.

Sub-Agent Types:
1. Interviewer Agent - Multi-turn conversation to gather requirements
2. Generator Agents - Single-turn artifact generation (Lambda, OpenAPI, Prompt, Contact Flow, Infrastructure)
3. Research Agent - Web search and information gathering
4. FAQ Generator Agent - Knowledge base document generation

Agent Pool:
- Pre-creates Agent instances for reuse (reduces 200-500ms latency per call)
- Use get_agent() or get_agent_with_tools() to get pooled agents
"""

from .lambda_generator import lambda_generator_agent
from .openapi_generator import openapi_generator_agent
from .prompt_generator import prompt_generator_agent
from .contact_flow_generator import contact_flow_generator_agent
from .infrastructure_generator import infrastructure_generator_agent
from .research_agent import research_agent
from .faq_generator import faq_generator_agent
from .reviewer_agent import reviewer_agent

# Agent Pool for warm instance reuse
from .agent_pool import (
    get_agent,
    get_agent_with_tools,
    initialize_pool,
    get_pool_status,
    clear_pool,
)

__all__ = [
    # Sub-Agents (as tools)
    "research_agent",  # Research: Web search and information gathering
    "faq_generator_agent",  # Generator: Knowledge base FAQ documents
    "lambda_generator_agent",  # Single-turn: Lambda code generation
    "openapi_generator_agent",  # Single-turn: OpenAPI spec generation
    "prompt_generator_agent",  # Single-turn: AI prompt generation
    "contact_flow_generator_agent",  # Single-turn: Contact Flow generation
    "infrastructure_generator_agent",  # Single-turn: CDK infrastructure generation
    "reviewer_agent",  # Review: Asset consistency and validation
    # Agent Pool
    "get_agent",
    "get_agent_with_tools",
    "initialize_pool",
    "get_pool_status",
    "clear_pool",
]
