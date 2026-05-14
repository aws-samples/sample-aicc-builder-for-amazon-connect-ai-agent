"""
CLUES_FORMAT — Compressed response format for sub-agent results

Sub-agents return results in a compressed format that preserves
essential information while reducing token consumption.

Context Engineering Layer 1: Sub-agents compress results into
structured CLUES format for the orchestrator to consume efficiently.
"""

# Standard clues format template for sub-agent responses
CLUES_FORMAT = """
## Result Summary (CLUES Format)

**Status**: {status}
**Agent**: {agent_name}
**Operation**: {operation_id}

### Key Findings
{findings}

### Generated Artifacts
{artifacts}

### Issues
{issues}
"""


def format_clues(
    status: str,
    agent_name: str,
    operation_id: str = "",
    findings: str = "None",
    artifacts: str = "None",
    issues: str = "None",
) -> str:
    """Format a sub-agent result in CLUES format."""
    return CLUES_FORMAT.format(
        status=status,
        agent_name=agent_name,
        operation_id=operation_id,
        findings=findings,
        artifacts=artifacts,
        issues=issues,
    ).strip()


# Token budget annotation for sub-agent system prompts
TOKEN_BUDGET_ANNOTATION = """
<token_budget>
You have a token budget of {max_tokens} tokens for this response.
Prioritize:
1. Essential code/content generation
2. Brief explanation of key decisions
3. Skip verbose explanations of standard patterns
</token_budget>
"""


def get_token_budget(max_tokens: int = 8000) -> str:
    """Get token budget annotation for sub-agent system prompt."""
    return TOKEN_BUDGET_ANNOTATION.format(max_tokens=max_tokens).strip()


# ── Shared suffix for sub-agent system prompts ────────────────────────
CLUES_RESPONSE_INSTRUCTION = """

## Response Efficiency (Context Engineering)

<token_budget>
Prioritize generated artifacts over verbose explanations.
Brief rationale for key decisions only. Skip explanations of standard patterns.
</token_budget>

<result_summary_format>
After completing your primary task (code/YAML/JSON generation), append a brief
summary in the following CLUES format at the very end of your response:

## Result Summary
- **Status**: SUCCESS | PARTIAL | FAILED
- **Artifacts**: List of generated files/assets (name + line count)
- **Key Decisions**: 1-2 sentences on notable implementation choices
- **Issues**: Any warnings, missing info, or concerns (or "None")

This summary helps the orchestrator track progress efficiently without
re-parsing the full generated content.
</result_summary_format>
"""


def get_clues_suffix() -> str:
    """Get the CLUES response instruction suffix for sub-agent system prompts."""
    return CLUES_RESPONSE_INSTRUCTION
