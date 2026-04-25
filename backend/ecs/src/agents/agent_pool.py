"""
Agent Pool - Singleton Agent Instance Management

Pre-creates Agent instances at startup for reuse, eliminating the 200-500ms
overhead of creating new Agent instances per tool call. : 

Usage:
    from agents.agent_pool import get_agent, initialize_pool

    # At startup (called automatically on first get_agent)
    initialize_pool()

    # In sub-agent tool function
    agent = get_agent("lambda_generator")
    agent.messages = []  # Clear previous conversation
    result = agent(prompt)
"""

import os
import logging
from typing import Dict, Optional, Callable, Any
from strands import Agent
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

logger = logging.getLogger(__name__)

# Configuration per agent type
# Note: tools are loaded dynamically to avoid circular imports
AGENT_CONFIGS = {
    "lambda_generator": {
        "system_prompt_module": "agents.lambda_generator.system_prompt",
        "system_prompt_var": "LAMBDA_GENERATOR_SYSTEM_PROMPT",
        "temperature": 0.3,
        "max_tokens": 128000,
    },
    "openapi_generator": {
        "system_prompt_module": "agents.openapi_generator.system_prompt",
        "system_prompt_var": "OPENAPI_GENERATOR_SYSTEM_PROMPT",
        "temperature": 0.3,
        "max_tokens": 128000,
    },
    "prompt_generator": {
        "system_prompt_module": "agents.prompt_generator.system_prompt",
        "system_prompt_var": "PROMPT_GENERATOR_SYSTEM_PROMPT",
        "temperature": 0.5,
        "max_tokens": 128000,
    },
    "contact_flow_generator": {
        "system_prompt_module": "agents.contact_flow_generator.system_prompt",
        "system_prompt_var": "CONTACT_FLOW_GENERATOR_SYSTEM_PROMPT",
        "temperature": 0.3,
        "max_tokens": 128000,
    },
    # NOTE: the legacy `interviewer` sub-agent was removed.
    # All interviews now run through the phase-based prompt in
    # `prompts/interview_agent_prompt.py`, not a separate agent pool entry.
}

# Singleton pool storage
_agent_pool: Dict[str, Agent] = {}
_model_cache: Dict[str, BedrockModel] = {}
_initialized = False


def _get_model(temperature: float, max_tokens: int) -> BedrockModel:
    """
    Get or create a cached model instance.

    Models are cached by (temperature, max_tokens) to avoid recreating
    identical configurations.
    """
    cache_key = f"{temperature}:{max_tokens}"
    if cache_key not in _model_cache:
        # Use Opus for all sub-agents (consistent quality)
        model_id = "global.anthropic.claude-opus-4-6-v1"
        region = os.environ.get("AWS_REGION", "ap-northeast-1")

        logger.info(f"Creating model: {model_id} (temp={temperature}, max_tokens={max_tokens})")

        _model_cache[cache_key] = BedrockModel(
            model_id=model_id,
            region_name=region,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=True,
            # cache_prompt removed - using cachePoint in system_prompt instead
            cache_tools="default",
            boto_client_config=BotocoreConfig(read_timeout=600),
        )
    return _model_cache[cache_key]


def _load_module_attr(module_name: str, attr_name: str) -> Any:
    """Dynamically load an attribute from a module."""
    import importlib
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def initialize_pool(force: bool = False) -> None:
    """
    Initialize the agent pool.

    Creates Agent instances for all configured agent types.
    Called automatically on first get_agent() call, or can be called
    explicitly at startup for faster first request.

    Args:
        force: If True, reinitialize even if already initialized
    """
    global _initialized, _agent_pool

    if _initialized and not force:
        logger.debug("Agent pool already initialized")
        return

    logger.info("Initializing agent pool...")
    _agent_pool = {}  # Clear existing pool if force reinit

    for agent_name, config in AGENT_CONFIGS.items():
        try:
            # Load system prompt
            system_prompt = _load_module_attr(
                config["system_prompt_module"],
                config["system_prompt_var"]
            )

            # Get model (shared across agents with same config)
            model = _get_model(config["temperature"], config["max_tokens"])

            # Create agent WITHOUT tools (tools are added per-call)
            # This allows the pool to be initialized without circular imports
            _agent_pool[agent_name] = Agent(
                name=agent_name,
                model=model,
                system_prompt=[
                    {"text": system_prompt},
                    {"cachePoint": {"type": "default"}},
                ],
                tools=None,  # Tools added via get_agent_with_tools
            )

            logger.info(f"Agent pool: {agent_name} initialized")

        except Exception as e:
            logger.error(f"Failed to initialize {agent_name}: {e}")
            # Continue with other agents - partial pool is better than none

    _initialized = True
    logger.info(f"Agent pool initialized: {len(_agent_pool)}/{len(AGENT_CONFIGS)} agents")


def get_agent(agent_name: str) -> Agent:
    """
    Get an agent from the pool.

    IMPORTANT: Clear messages before use to reset conversation state!

    Args:
        agent_name: Name of the agent (e.g., "lambda_generator")

    Returns:
        Agent instance from the pool

    Raises:
        ValueError: If agent_name is not in the pool

    Usage:
        agent = get_agent("lambda_generator")
        agent.messages = []  # IMPORTANT: Clear previous conversation
        result = agent(prompt)
    """
    if not _initialized:
        initialize_pool()

    if agent_name not in _agent_pool:
        available = list(_agent_pool.keys())
        raise ValueError(f"Unknown agent: {agent_name}. Available: {available}")

    return _agent_pool[agent_name]


def get_agent_with_tools(
    agent_name: str,
    tools: list,
    callback_handler: Optional[Any] = None
) -> Agent:
    """
    Get an agent from the pool with specific tools and callback handler.

    This creates a new Agent instance that shares the model with the pool
    but has custom tools and callback handler for the current request.

    Args:
        agent_name: Name of the agent
        tools: List of tool functions for this request
        callback_handler: Optional callback handler for streaming

    Returns:
        New Agent instance with specified tools

    Usage:
        agent = get_agent_with_tools(
            "lambda_generator",
            tools=[save_generated_code, validate_python_syntax],
            callback_handler=streaming_handler
        )
        agent.messages = []
        result = agent(prompt)
    """
    if not _initialized:
        initialize_pool()

    if agent_name not in AGENT_CONFIGS:
        raise ValueError(f"Unknown agent: {agent_name}")

    config = AGENT_CONFIGS[agent_name]

    # Load system prompt
    system_prompt = _load_module_attr(
        config["system_prompt_module"],
        config["system_prompt_var"]
    )

    # Get shared model
    model = _get_model(config["temperature"], config["max_tokens"])

    # Create new agent with tools
    return Agent(
        name=agent_name,
        model=model,
        system_prompt=[
            {"text": system_prompt},
            {"cachePoint": {"type": "default"}},
        ],
        tools=tools,
        callback_handler=callback_handler,
    )


def get_pool_status() -> Dict[str, Any]:
    """
    Get pool status for monitoring and debugging.

    Returns:
        Dictionary with pool statistics
    """
    return {
        "initialized": _initialized,
        "agents_available": list(_agent_pool.keys()),
        "agents_configured": list(AGENT_CONFIGS.keys()),
        "models_cached": len(_model_cache),
        "pool_size": len(_agent_pool),
    }


def clear_pool() -> None:
    """
    Clear the agent pool.

    Useful for testing or when model configuration changes.
    """
    global _initialized, _agent_pool, _model_cache
    _agent_pool = {}
    _model_cache = {}
    _initialized = False
    logger.info("Agent pool cleared")
