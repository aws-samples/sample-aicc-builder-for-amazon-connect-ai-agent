"""
Research Agent Sub-Agent

This agent performs web research using Brave Search API to gather
information about companies, services, and business data.

AsyncGenerator Streaming (Strands SDK v1.22.0+):
- Uses async def with yield for real-time event streaming
- Each yielded value becomes a tool_stream_event in the parent agent
- Final yield is the tool's return result

Context Management:
- Maintains per-session history of research activities
- Returns structured findings to Orchestrator
- Research results can be used by FAQ Generator and other agents
"""

import json
import os
import time
import logging
import requests
from typing import Dict, List, Any, AsyncIterator
from strands import Agent, tool
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

from .system_prompt import RESEARCH_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Heartbeat interval to keep WebSocket connection alive during long-running operations
HEARTBEAT_INTERVAL_SECONDS = 5

# Callback handler lives in a ContextVar (session_context) so concurrent
# async users don't overwrite each other's handler mid-request.
from tools.session_context import current_callback_handler

# Session-scoped history for Sub-Agent continuity
_session_history: Dict[str, List[Dict[str, Any]]] = {}
_session_research_results: Dict[str, Dict[str, Any]] = {}
_session_errors: Dict[str, List[Dict[str, Any]]] = {}


def set_callback_handler(handler):
    """Set the callback handler from parent agent."""
    current_callback_handler.set(handler)


def get_callback_handler():
    """Get the current callback handler."""
    return current_callback_handler.get()


def get_session_history(session_id: str) -> List[Dict[str, Any]]:
    """Get research history for this session."""
    return _session_history.get(session_id, [])


def update_session_history(session_id: str, role: str, content: str):
    """Append a message to this session's history (max 10 turns = 20 messages)."""
    if session_id not in _session_history:
        _session_history[session_id] = []
    _session_history[session_id].append({"role": role, "content": content})
    # Keep last 10 turns (20 messages) - Research may involve multiple searches
    if len(_session_history[session_id]) > 20:
        _session_history[session_id] = _session_history[session_id][-20:]


def get_research_results(session_id: str) -> Dict[str, Any]:
    """Get accumulated research results for this session."""
    return _session_research_results.get(session_id, {})


def clear_session(session_id: str):
    """Clear session data (call on session end)."""
    _session_history.pop(session_id, None)
    _session_research_results.pop(session_id, None)
    _session_errors.pop(session_id, None)


def track_error(session_id: str, error: str, context: dict = None):
    """Track errors for debugging."""
    if session_id not in _session_errors:
        _session_errors[session_id] = []
    _session_errors[session_id].append({
        "error": error,
        "context": context or {}
    })


def _setup_streaming_for_subagent():
    """Set up streaming callback for Sub-Agent execution."""
    handler = get_callback_handler()
    logger.info(f"[SUBAGENT_SETUP] research_agent: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback
            set_streaming_callback(handler.stream_asset_preview)
            logger.info(f"[SUBAGENT_SETUP] research_agent: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] research_agent: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] research_agent: handler not available or missing stream_asset_preview")


def _send_progress(agent_name: str, status: str, message: str = ""):
    """Send progress event via callback handler."""
    handler = get_callback_handler()
    if handler and hasattr(handler, 'add_ws_event'):
        event = {"type": "subagent_progress", "subagent": agent_name, "status": status}
        if message:
            event["message"] = message
        try:
            handler.add_ws_event(event)
        except Exception:
            pass


# ========================================
# Internal Tools for Research Agent
# ========================================

@tool
def brave_web_search(
    query: str,
    count: int = 10,
    session_id: str = "default"
) -> dict:
    """
    Search the web using Brave Search API.

    Args:
        query: Search query string
        count: Number of results to return (max 20)
        session_id: Session identifier for tracking

    Returns:
        Search results with titles, URLs, and descriptions
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")

    if not api_key:
        return {
            "success": False,
            "error": "BRAVE_API_KEY not configured. Please set the API key in environment variables.",
            "results": []
        }

    try:
        headers = {
            "X-Subscription-Token": api_key,
            "Accept": "application/json"
        }

        params = {
            "q": query,
            "count": min(count, 20),  # Max 20 results
        }

        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params,
            timeout=30
        )
        response.raise_for_status()

        data = response.json()

        # Extract web results
        results = []
        if "web" in data and "results" in data["web"]:
            for item in data["web"]["results"]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", ""),
                })

        return {
            "success": True,
            "query": query,
            "result_count": len(results),
            "results": results
        }

    except requests.exceptions.HTTPError as e:
        error_msg = f"Brave Search API error: {e.response.status_code}"
        if e.response.status_code == 401:
            error_msg = "Invalid Brave Search API key"
        elif e.response.status_code == 429:
            error_msg = "Brave Search API rate limit exceeded"
        return {
            "success": False,
            "error": error_msg,
            "results": []
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Search failed: {str(e)}",
            "results": []
        }


@tool
def fetch_webpage(
    url: str,
    max_length: int = 10000
) -> dict:
    """
    Fetch and extract text content from a webpage.

    Args:
        url: URL to fetch
        max_length: Maximum content length to return

    Returns:
        Extracted text content from the webpage
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AIResearchBot/1.0)"
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        # Try to parse HTML and extract text
        content = response.text

        # Simple HTML to text conversion (basic)
        import re

        # Remove script and style elements
        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML tags
        content = re.sub(r'<[^>]+>', ' ', content)

        # Decode HTML entities
        import html
        content = html.unescape(content)

        # Clean up whitespace
        content = re.sub(r'\s+', ' ', content).strip()

        # Truncate if too long
        if len(content) > max_length:
            content = content[:max_length] + "... [truncated]"

        return {
            "success": True,
            "url": url,
            "content_length": len(content),
            "content": content
        }

    except requests.exceptions.HTTPError as e:
        return {
            "success": False,
            "url": url,
            "error": f"HTTP error: {e.response.status_code}",
            "content": ""
        }
    except Exception as e:
        return {
            "success": False,
            "url": url,
            "error": f"Fetch failed: {str(e)}",
            "content": ""
        }


@tool
def save_research_result(
    research_id: str,
    company_name: str,
    industry: str,
    findings: dict,
    sources: list = None,
    session_id: str = "default"
) -> dict:
    """
    Save structured research findings incrementally.

    IMPORTANT: Call this after EACH research phase, not just at the end.
    Each call merges new findings into the existing research on S3.
    This ensures partial results are preserved even if the session times out.

    Args:
        research_id: Unique identifier for this research
        company_name: Name of the company researched
        industry: Industry/sector of the company
        findings: Structured findings dictionary containing any of:
            - overview: Company overview text
            - services: List of services/products
            - policies: Dict of policies (return, cancellation, etc.)
            - faq_topics: List of potential FAQ Q&A pairs
            - contact_info: Contact information dict
        sources: List of source URLs used
        session_id: Session identifier

    Returns:
        Confirmation of saved research
    """
    global _session_research_results

    if session_id not in _session_research_results:
        _session_research_results[session_id] = {}

    # Merge with existing findings (incremental update)
    existing = _session_research_results[session_id].get(research_id, {})
    existing_findings = existing.get("findings", {})
    existing_sources = existing.get("sources", [])

    # Merge findings: lists append, dicts deep-merge, strings overwrite
    merged_findings = {**existing_findings}
    for key, value in findings.items():
        if isinstance(value, list) and isinstance(merged_findings.get(key), list):
            # Deduplicate list items (for faq_topics, services, etc.)
            seen = {json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
                    for item in merged_findings[key]}
            for item in value:
                item_key = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
                if item_key not in seen:
                    merged_findings[key].append(item)
                    seen.add(item_key)
        elif isinstance(value, dict) and isinstance(merged_findings.get(key), dict):
            merged_findings[key] = {**merged_findings[key], **value}
        else:
            merged_findings[key] = value

    # Merge sources
    merged_sources = list(dict.fromkeys(existing_sources + (sources or [])))

    research_data = {
        "research_id": research_id,
        "company_name": company_name,
        "industry": industry,
        "findings": merged_findings,
        "sources": merged_sources,
    }

    _session_research_results[session_id][research_id] = research_data

    # Save to shared context store for cross-agent access
    try:
        from tools.shared_context import save_research_context
        save_research_context(
            session_id=session_id,
            research_id=research_id,
            company_name=company_name,
            industry=industry,
            findings=merged_findings,
            sources=merged_sources
        )
    except ImportError:
        logger.warning("shared_context module not available")

    # Persist to S3 as JSON (incremental — always overwrites with merged state)
    try:
        from tools.s3_asset_storage import save_asset_to_s3
        save_asset_to_s3(
            session_id=session_id,
            asset_type="research",
            file_name="research.json",
            content=json.dumps(research_data, ensure_ascii=False, indent=2),
            content_type="application/json"
        )
    except Exception as e:
        logger.warning(f"Failed to save research to S3: {e}")

    # Stream research.json to frontend as asset preview (incremental updates)
    try:
        from tools.streaming_callback import stream_asset
        json_content = json.dumps(research_data, ensure_ascii=False, indent=2)
        # Cap content to prevent oversized WS messages (full data is on S3)
        if len(json_content) > 24000:
            # Trim faq_topics answers to keep under limit
            trimmed = {**research_data}
            if "findings" in trimmed and "faq_topics" in trimmed["findings"]:
                trimmed["findings"] = {**trimmed["findings"]}
                trimmed["findings"]["faq_topics"] = [
                    {**t, "answer": t.get("answer", "")[:200] + "..." if len(t.get("answer", "")) > 200 else t.get("answer", "")}
                    for t in trimmed["findings"]["faq_topics"]
                ]
            json_content = json.dumps(trimmed, ensure_ascii=False, indent=2)
        stream_asset(
            "research", "research.json", json_content,
            operation_id=research_id, is_complete=False, force_full=True
        )
    except ImportError:
        pass

    return {
        "success": True,
        "research_id": research_id,
        "company_name": company_name,
        "faq_topic_count": len(merged_findings.get("faq_topics", [])),
        "message": f"Research saved (incremental): {company_name} ({len(merged_findings.get('faq_topics', []))} FAQ topics)"
    }


# ========================================
# Main Research Agent Tool
# ========================================

@tool
async def research_agent(
    research_request: str,
    company_name: str = "",
    company_url: str = "",
    session_id: str = "default",
    orchestrator_context: str = "",
    research_depth: str = "standard"
) -> AsyncIterator:
    """
    Research companies, APIs, and topics using web search to gather information.

    This async tool performs web research using Brave Search API and
    extracts relevant information for generating FAQ documents and
    other business assets.

    Yields:
        dict: Streaming events with progress and findings

    Final yield:
        dict: Summary of research with findings structure

    Args:
        research_request: Description of what to research
        company_name: Name of the company (if known)
        company_url: Company website URL (if known)
        session_id: Session identifier for continuity
        orchestrator_context: Context from parent agent
        research_depth: "light" (1-5 FAQs, ~2 min), "standard" (5-10 FAQs, ~5 min), "deep" (all available info, ~10 min)
    """
    _setup_streaming_for_subagent()

    # Yield: Starting
    yield {
        "type": "progress",
        "agent": "research_agent",
        "status": "started",
        "content": f"리서치 시작: {company_name or research_request[:50]}"
    }

    # Build research prompt
    history = get_session_history(session_id)

    pm_briefing = ""
    if orchestrator_context:
        pm_briefing = f"## PM Briefing\n{orchestrator_context}\n\n---\n"

    depth_instruction = {
        "light": "⚠️ LIGHT MODE: You have a HARD LIMIT of 2 searches and 1 page fetch. Do ONE broad search, ONE targeted search, fetch at most 1 page, then call save_research_result and STOP. Do NOT do any more research after saving.",
        "standard": "STANDARD MODE: Limit to 6 searches and 3 page fetches. Cover Phases 1-3 only. Call save_research_result after each phase.",
        "deep": "DEEP MODE: Be thorough. Cover all phases. Call save_research_result after each phase.",
    }.get(research_depth, "STANDARD MODE: Limit to 6 searches and 3 page fetches.")

    research_prompt = f"""{pm_briefing}## Research Request
{research_request}

## {depth_instruction}
"""

    if company_name:
        research_prompt += f"Company Name: {company_name}\n"

    if company_url:
        research_prompt += f"Company URL: {company_url}\n"

    if research_depth == "light":
        research_prompt += """
Steps: Search → (optionally) fetch 1 page → save_research_result → DONE.
"""
    else:
        research_prompt += """
Please research and save structured results using save_research_result.
Focus on: services, policies, FAQ-worthy info, contact information.
"""

    # Track research activities
    searches_performed = []
    pages_fetched = []

    try:
        # Use Opus for Research (consistent quality)
        model_id = "global.anthropic.claude-opus-4-6-v1"
        region = os.environ.get("AWS_REGION", "ap-northeast-1")

        model = BedrockModel(
            model_id=model_id,
            region_name=region,
            temperature=0.5,
            max_tokens=128000,
            streaming=True,
            # cache_prompt removed - using cachePoint in system_prompt instead
            cache_tools="default",   # Cache tool definitions
            boto_client_config=BotocoreConfig(
                read_timeout=600,
                retries={"max_attempts": 2, "mode": "adaptive"},
            ),
        )

        # Convert history to Strands format
        recent_history = history[-10:] if len(history) > 10 else history
        strands_messages = [
            {"role": msg["role"], "content": [{"text": msg["content"]}]}
            for msg in recent_history
        ]

        # Create internal agent with research tools
        # Pass session_id to internal tools
        @tool
        def brave_search_tracked(query: str, count: int = 10) -> dict:
            """Search web with tracking."""
            searches_performed.append(query)
            return brave_web_search(query, count, session_id)

        @tool
        def fetch_page_tracked(url: str, max_length: int = 10000) -> dict:
            """Fetch webpage with tracking."""
            pages_fetched.append(url)
            return fetch_webpage(url, max_length)

        @tool
        def save_result_tracked(
            research_id: str,
            company_name: str,
            industry: str,
            findings: dict,
            sources: list = None
        ) -> dict:
            """Save research result with session tracking."""
            return save_research_result(
                research_id, company_name, industry, findings, sources, session_id
            )

        agent = Agent(
            model=model,
            system_prompt=[
                {"text": RESEARCH_AGENT_SYSTEM_PROMPT},
                {"cachePoint": {"type": "default"}},
            ],
            tools=[brave_search_tracked, fetch_page_tracked, save_result_tracked],
            callback_handler=None,
            messages=strands_messages,
        )

        # Yield: Running
        yield {
            "type": "progress",
            "agent": "research_agent",
            "status": "running",
            "content": "웹 검색 및 정보 수집 중..."
        }

        # Track heartbeat for long-running operations
        from tools.heartbeat_utils import create_heartbeat_manager

        last_heartbeat = time.time()
        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="research_agent",
            project_name=company_name or "research",
        )

        # Stream the agent execution
        # CRITICAL: Use explicit generator cleanup to prevent OpenTelemetry context errors
        async with heartbeat:
            generator = agent.stream_async(research_prompt)
            try:
                async for event in generator:
                    # Yield text chunks
                    if "data" in event:
                        yield {
                            "type": "text",
                            "agent": "research_agent",
                            "content": event["data"]
                        }
                        heartbeat.update_progress(len(searches_performed))

                    # Track tool use
                    if "current_tool_use" in event:
                        tool_use = event["current_tool_use"]
                        tool_name = tool_use.get("name", "")

                        if tool_name:
                            yield {
                                "type": "tool_use",
                                "agent": "research_agent",
                                "tool": tool_name,
                                "input": tool_use.get("input", {})
                            }

                    # Forward tool results (strip large content to stay under 32KB WS limit)
                    if "tool_result" in event:
                        tool_result = event["tool_result"]
                        result_content = tool_result.get("content")
                        if isinstance(result_content, str) and len(result_content) > 8000:
                            result_content = result_content[:8000] + "... [truncated]"
                        elif isinstance(result_content, list):
                            # Strands SDK wraps tool results as [{"text": "..."}]
                            truncated = []
                            for item in result_content:
                                if isinstance(item, dict) and "text" in item:
                                    text = item["text"]
                                    if isinstance(text, str) and len(text) > 8000:
                                        truncated.append({"text": text[:8000] + "... [truncated]"})
                                    else:
                                        truncated.append(item)
                                else:
                                    truncated.append(item)
                            result_content = truncated
                        yield {
                            "type": "tool_result",
                            "agent": "research_agent",
                            "tool": tool_result.get("name", ""),
                            "result": result_content
                        }

                    if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = time.time()
                        _send_progress("research_agent", "running",
                                       f"리서치 진행 중... (검색: {len(searches_performed)}, 페이지: {len(pages_fetched)})")
            finally:
                # Explicitly close the generator to ensure proper OpenTelemetry context cleanup
                try:
                    await generator.aclose()
                except Exception:
                    pass  # Ignore errors during cleanup

            # Finalize research.json INSIDE heartbeat block to prevent WS idle during S3/stream
            results = get_research_results(session_id)
            try:
                from tools.streaming_callback import stream_asset
                if results:
                    first_research = next(iter(results.values()), {})
                    json_content = json.dumps(first_research, ensure_ascii=False, indent=2)
                    # Apply same size guard as save_research_result
                    if len(json_content) > 24000:
                        trimmed = {**first_research}
                        if "findings" in trimmed and "faq_topics" in trimmed.get("findings", {}):
                            trimmed["findings"] = {**trimmed["findings"]}
                            trimmed["findings"]["faq_topics"] = [
                                {**t, "answer": t.get("answer", "")[:200] + "..." if len(t.get("answer", "")) > 200 else t.get("answer", "")}
                                for t in trimmed["findings"]["faq_topics"]
                            ]
                        json_content = json.dumps(trimmed, ensure_ascii=False, indent=2)
                    rid = first_research.get("research_id", "research")
                    stream_asset(
                        "research", "research.json", json_content,
                        operation_id=rid, is_complete=True, force_full=True
                    )
            except Exception:
                pass

        # Update history
        update_session_history(session_id, "user", f"Research: {research_request}")
        update_session_history(
            session_id,
            "assistant",
            f"Completed research with {len(searches_performed)} searches, {len(pages_fetched)} pages"
        )

        # Yield: Completed
        yield {
            "type": "progress",
            "agent": "research_agent",
            "status": "completed",
            "searches": len(searches_performed),
            "pages_fetched": len(pages_fetched),
            "content": f"리서치 완료: {len(searches_performed)}개 검색, {len(pages_fetched)}개 페이지 분석"
        }

        # Final yield: the tool's return value
        # CRITICAL: Include _completion_marker so Orchestrator knows the tool completed
        yield {
            "success": True,
            "_completion_marker": "SUBAGENT_COMPLETE",
            "searches_performed": len(searches_performed),
            "pages_fetched": len(pages_fetched),
            "summary": f"Research completed with {len(searches_performed)} searches and {len(pages_fetched)} pages analyzed. Results saved to S3."
        }

    except Exception as e:
        error_str = str(e)
        is_timeout = "timed out" in error_str.lower() or "ReadTimeoutError" in error_str
        track_error(session_id, error_str, {
            "research_request": research_request,
            "company_name": company_name,
            "is_timeout": is_timeout,
        })

        # Timeout with partial results → return what we have
        if is_timeout:
            results = get_research_results(session_id)
            if results:
                # Finalize research.json asset preview with partial data
                try:
                    from tools.streaming_callback import stream_asset
                    first_research = next(iter(results.values()), {})
                    json_content = json.dumps(first_research, ensure_ascii=False, indent=2)
                    rid = first_research.get("research_id", "research")
                    stream_asset(
                        "research", "research.json", json_content,
                        operation_id=rid, is_complete=True, force_full=True
                    )
                except Exception:
                    pass

                yield {
                    "type": "progress",
                    "agent": "research_agent",
                    "status": "completed",
                    "content": f"리서치 부분 완료 (timeout, 수집된 데이터 반환)"
                }
                yield {
                    "success": True,
                    "_completion_marker": "SUBAGENT_COMPLETE",
                    "searches_performed": len(searches_performed),
                    "pages_fetched": len(pages_fetched),
                    "summary": f"Research partially completed (timeout after {len(searches_performed)} searches). Partial results saved to S3."
                }
                return

        # Yield: Error
        yield {
            "type": "progress",
            "agent": "research_agent",
            "status": "error",
            "error": error_str[:200],
            "content": f"리서치 실패: {error_str[:100]}"
        }

        # Final yield: error result
        # CRITICAL: Include _completion_marker so Orchestrator knows the tool completed
        yield {
            "success": False,
            "_completion_marker": "SUBAGENT_COMPLETE",
            "error": error_str,
            "summary": f"Research failed: {error_str[:100]}"
        }
