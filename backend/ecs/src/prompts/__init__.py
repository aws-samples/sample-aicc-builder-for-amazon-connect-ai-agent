"""Prompt templates for AICC Builder Agent."""

from .system_prompt import SYSTEM_PROMPT

# Base prompt fragments for modular prompt building
from .base import (
    # Execution rules
    EXECUTION_RULES_SINGLE_TURN,
    EXECUTION_RULES_MULTI_TURN,
    # Response formatting
    RESPONSE_FORMAT_RULES,
    VOICE_FRIENDLY_GUIDE,
    VOICE_FRIENDLY_GUIDE_KO,
    # Tool usage
    TOOL_USAGE_RULES,
    SECURITY_RULES,
    # Q in Connect integration
    RETRIEVE_TOOL_GUIDE,
    RETRIEVE_TOOL_GUIDE_KO,
    ESCALATE_TOOL_GUIDE,
    ESCALATE_TOOL_GUIDE_KO,
    # City conversion
    CITY_NAME_CONVERSION_KO,
    CITY_NAME_CONVERSION_JA,
    # Message formatting
    MESSAGE_TAG_FORMAT,
    # Code patterns
    LAMBDA_RESPONSE_PATTERN,
    LAMBDA_LOGGING_PATTERN,
    DYNAMODB_QUERY_PATTERN,
    # System variables
    SYSTEM_VARIABLES_TEMPLATE,
    # Never do rules
    NEVER_DO_RULES,
    NEVER_DO_RULES_EN,
    # Builder functions
    build_prompt,
    get_native_tools_guide,
    get_city_conversion_guide,
)

__all__ = [
    # Main prompt
    "SYSTEM_PROMPT",
    # Execution rules
    "EXECUTION_RULES_SINGLE_TURN",
    "EXECUTION_RULES_MULTI_TURN",
    # Response formatting
    "RESPONSE_FORMAT_RULES",
    "VOICE_FRIENDLY_GUIDE",
    "VOICE_FRIENDLY_GUIDE_KO",
    # Tool usage
    "TOOL_USAGE_RULES",
    "SECURITY_RULES",
    # Q in Connect
    "RETRIEVE_TOOL_GUIDE",
    "RETRIEVE_TOOL_GUIDE_KO",
    "ESCALATE_TOOL_GUIDE",
    "ESCALATE_TOOL_GUIDE_KO",
    # City conversion
    "CITY_NAME_CONVERSION_KO",
    "CITY_NAME_CONVERSION_JA",
    # Message formatting
    "MESSAGE_TAG_FORMAT",
    # Code patterns
    "LAMBDA_RESPONSE_PATTERN",
    "LAMBDA_LOGGING_PATTERN",
    "DYNAMODB_QUERY_PATTERN",
    # System variables
    "SYSTEM_VARIABLES_TEMPLATE",
    # Never do rules
    "NEVER_DO_RULES",
    "NEVER_DO_RULES_EN",
    # Builder functions
    "build_prompt",
    "get_native_tools_guide",
    "get_city_conversion_guide",
]
