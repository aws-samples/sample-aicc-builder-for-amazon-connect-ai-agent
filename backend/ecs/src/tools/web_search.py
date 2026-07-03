"""
Web search via Amazon Bedrock AgentCore Gateway.

Web search runs through an Amazon Bedrock AgentCore Gateway whose target is the
managed **Web Search** connector. The gateway is reached over MCP and
authenticated with the ECS task role's SigV4 credentials via `mcp-proxy-for-aws`
— there is no API key to manage, and queries never leave the AWS environment
("zero data egress").

Region: Web Search on Amazon Bedrock AgentCore is only available in **us-east-1**
(GA 2026-06-17), so the gateway is region-pinned there regardless of the app's
own deployment region.

Configuration (env):
- ``AGENTCORE_GATEWAY_URL``    full MCP endpoint, e.g.
  ``https://<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp``
- ``AGENTCORE_GATEWAY_REGION`` optional, defaults to ``us-east-1``

When ``AGENTCORE_GATEWAY_URL`` is unset the search functions degrade gracefully
(``success: False`` with an explanatory error), so the agents keep working off
built-in knowledge.

Reference:
https://builder.aws.com/content/3FHnWgGt3ebBdMQWA2k4bVOnMbB/web-search-in-claude-code-on-amazon-bedrock
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Web Search on Amazon Bedrock AgentCore is only available in us-east-1.
DEFAULT_GATEWAY_REGION = "us-east-1"
# AWS service id used for SigV4 signing of MCP requests to the gateway.
GATEWAY_SERVICE = "bedrock-agentcore"
# AgentCore Web Search caps results at 25 per query.
MAX_RESULTS_CAP = 25

# Substrings used to pick the web-search tool out of the gateway's tool list.
# Connector tool names can carry a target prefix (e.g. "web_search___search"),
# so we match loosely rather than on an exact name.
_SEARCH_TOOL_HINTS = ("search", "web")

# Cache the resolved tool name per gateway URL so we skip list_tools on every
# call (a fresh MCP connection is opened per search).
_TOOL_NAME_CACHE: Dict[str, str] = {}


def get_gateway_url() -> str:
    """The configured AgentCore Gateway MCP endpoint (empty if unset)."""
    return os.environ.get("AGENTCORE_GATEWAY_URL", "").strip()


def get_gateway_region() -> str:
    """Region for SigV4 signing — pinned to us-east-1 unless overridden."""
    region = os.environ.get("AGENTCORE_GATEWAY_REGION", "").strip()
    return region or DEFAULT_GATEWAY_REGION


def is_configured() -> bool:
    """True when the gateway endpoint is configured."""
    return bool(get_gateway_url())


def _coerce_results(obj: Any) -> List[Dict[str, Any]]:
    """Pull a list of result dicts out of an arbitrary parsed MCP payload."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ("results", "observations", "webResults", "items", "documents", "data"):
            value = obj.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        # A single result object (has at least one recognizable field).
        if any(k in obj for k in ("title", "url", "text", "snippet", "description")):
            return [obj]
    return []


def _to_result(d: Dict[str, Any]) -> Dict[str, str]:
    """Normalize one AgentCore web-search observation to {title, url, description}."""
    description = (
        d.get("description")
        or d.get("text")
        or d.get("snippet")
        or d.get("content")
        or ""
    )
    return {
        "title": str(d.get("title") or d.get("name") or ""),
        "url": str(d.get("url") or d.get("link") or d.get("source") or ""),
        "description": str(description),
    }


def _normalize_results(result: Any, count: int) -> List[Dict[str, str]]:
    """Extract normalized results from an MCP CallToolResult.

    Web Search returns raw observations (title/url/text). It may arrive as
    structured content or as JSON inside text blocks — handle both.
    """
    collected: List[Dict[str, Any]] = []

    structured = getattr(result, "structuredContent", None)
    if structured:
        collected = _coerce_results(structured)

    if not collected:
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                # Plain-text block — keep it as a single descriptive result.
                collected.append({"description": text})
                continue
            collected.extend(_coerce_results(parsed))

    normalized = [_to_result(d) for d in collected if isinstance(d, dict)]
    normalized = [r for r in normalized if r["title"] or r["url"] or r["description"]]
    return normalized[:count]


async def _resolve_search_tool(session: Any, gateway_url: str) -> Optional[str]:
    """Find the web-search tool exposed by the gateway (cached per URL)."""
    cached = _TOOL_NAME_CACHE.get(gateway_url)
    if cached:
        return cached

    listing = await session.list_tools()
    names = [t.name for t in getattr(listing, "tools", []) if getattr(t, "name", None)]
    if not names:
        return None

    chosen = next(
        (n for n in names if any(h in n.lower() for h in _SEARCH_TOOL_HINTS)),
        names[0],  # documented setup exposes exactly one tool
    )
    _TOOL_NAME_CACHE[gateway_url] = chosen
    return chosen


async def web_search(query: str, count: int = 10, site: Optional[str] = None) -> Dict[str, Any]:
    """Run a web search through the AgentCore Gateway.

    Args:
        query: Search query string.
        count: Desired number of results (clamped to 1..25).
        site: Optional domain to restrict the search to (folded in as ``site:``).

    Returns:
        ``{"success": bool, "query": str, "result_count": int,
           "results": [{"title", "url", "description"}, ...]}``
        On failure, ``{"success": False, "error": str, "results": []}``.
    """
    gateway_url = get_gateway_url()
    if not gateway_url:
        return {
            "success": False,
            "error": (
                "AGENTCORE_GATEWAY_URL not configured. Web search is unavailable; "
                "relying on built-in knowledge."
            ),
            "results": [],
        }

    full_query = f"site:{site} {query}" if site else query
    max_results = max(1, min(int(count or 10), MAX_RESULTS_CAP))

    try:
        from mcp import ClientSession
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    except ImportError as exc:  # pragma: no cover - deps shipped in container
        return {
            "success": False,
            "error": f"Web search dependencies not installed: {exc}",
            "results": [],
        }

    region = get_gateway_region()
    try:
        async with aws_iam_streamablehttp_client(
            endpoint=gateway_url,
            aws_region=region,
            aws_service=GATEWAY_SERVICE,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tool_name = await _resolve_search_tool(session, gateway_url)
                if not tool_name:
                    return {
                        "success": False,
                        "error": "AgentCore Gateway exposes no web-search tool.",
                        "results": [],
                    }

                result = await session.call_tool(
                    tool_name,
                    arguments={"query": full_query, "maxResults": max_results},
                )

                if getattr(result, "isError", False):
                    return {
                        "success": False,
                        "error": f"Gateway tool '{tool_name}' returned an error.",
                        "results": [],
                    }

                results = _normalize_results(result, max_results)
                logger.info(
                    "[web_search] query=%r tool=%s results=%d",
                    full_query, tool_name, len(results),
                )
                return {
                    "success": True,
                    "query": full_query,
                    "result_count": len(results),
                    "results": results,
                }
    except Exception as exc:  # noqa: BLE001 - surface any transport/auth error gracefully
        logger.warning("[web_search] failed: %s", exc)
        return {
            "success": False,
            "error": f"Web search failed: {exc}",
            "results": [],
        }
