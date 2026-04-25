"""
FAQ Generator Sub-Agent

This agent generates knowledge base documents from research results
and packages them into downloadable ZIP files for Amazon Bedrock
Knowledge Bases.

AsyncGenerator Streaming (Strands SDK v1.22.0+):
- Uses async def with yield for real-time event streaming
- Each yielded value becomes a tool_stream_event in the parent agent
- Final yield is the tool's return result

Context Management:
- Maintains per-session history of document generation
- Tracks all generated documents for packaging
- Returns ZIP download info to Orchestrator
"""

import json
import os
import io
import time
import zipfile
import base64
import logging
from datetime import datetime
from typing import Dict, List, Any, AsyncIterator
from strands import Agent, tool
from strands.models import BedrockModel
from botocore.config import Config as BotocoreConfig

# Heartbeat interval to keep WebSocket connection alive during long-running generation
HEARTBEAT_INTERVAL_SECONDS = 5

from .system_prompt import FAQ_GENERATOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Callback handler lives in a ContextVar so concurrent async users don't
# overwrite each other's handler.
from tools.session_context import current_callback_handler

# Session-scoped storage
_session_history: Dict[str, List[Dict[str, Any]]] = {}
_session_documents: Dict[str, List[Dict[str, Any]]] = {}
_session_errors: Dict[str, List[Dict[str, Any]]] = {}


def set_callback_handler(handler):
    """Set the callback handler from parent agent."""
    current_callback_handler.set(handler)


def get_callback_handler():
    """Get the current callback handler."""
    return current_callback_handler.get()


def get_session_history(session_id: str) -> List[Dict[str, Any]]:
    """Get document generation history for this session."""
    return _session_history.get(session_id, [])


def update_session_history(session_id: str, role: str, content: str):
    """Append a message to this session's history (max 10 turns = 20 messages)."""
    if session_id not in _session_history:
        _session_history[session_id] = []
    _session_history[session_id].append({"role": role, "content": content})
    # Keep last 10 turns (20 messages) - FAQ generation may need more context
    if len(_session_history[session_id]) > 20:
        _session_history[session_id] = _session_history[session_id][-20:]


def get_session_documents(session_id: str) -> List[Dict[str, Any]]:
    """Get all generated documents for this session."""
    return _session_documents.get(session_id, [])


def clear_session(session_id: str):
    """Clear session data (call on session end)."""
    _session_history.pop(session_id, None)
    _session_documents.pop(session_id, None)
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
    logger.info(f"[SUBAGENT_SETUP] faq_generator: handler={handler}, has_stream_asset_preview={hasattr(handler, 'stream_asset_preview') if handler else False}")
    if handler and hasattr(handler, 'stream_asset_preview'):
        try:
            from tools.streaming_callback import set_streaming_callback
            set_streaming_callback(handler.stream_asset_preview)
            logger.info(f"[SUBAGENT_SETUP] faq_generator: callback set successfully")
        except ImportError as e:
            logger.warning(f"[SUBAGENT_SETUP] faq_generator: ImportError - {e}")
    else:
        logger.warning(f"[SUBAGENT_SETUP] faq_generator: handler not available or missing stream_asset_preview")


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
# Internal Tools for FAQ Generator
# ========================================

@tool
def save_faq_document(
    filename: str,
    category: str,
    title: str,
    question: str,
    answer: str,
    related_info: list = None,
    keywords: list = None,
    session_id: str = "default"
) -> dict:
    """
    Save a single FAQ document to the knowledge base.

    Args:
        filename: Name of the file (e.g., "faq_membership.txt")
        category: Document category (e.g., "membership", "shipping")
        title: Document title
        question: The FAQ question
        answer: The FAQ answer
        related_info: List of related information items
        keywords: List of keywords for search
        session_id: Session identifier for tracking

    Returns:
        Confirmation of saved document
    """
    global _session_documents

    if session_id not in _session_documents:
        _session_documents[session_id] = []

    # Format document content
    related_items = related_info or []
    keyword_list = keywords or []

    content = f"""# {title}

## 질문 (Question)
{question}

## 답변 (Answer)
{answer}

## 관련 정보 (Related Information)
{chr(10).join(['- ' + item for item in related_items]) if related_items else '- 없음'}

## 메타데이터 (Metadata)
- 카테고리: {category}
- 키워드: {', '.join(keyword_list)}
- 최종 업데이트: {datetime.now().strftime('%Y-%m-%d')}
"""

    # Store document
    doc_data = {
        "filename": filename,
        "category": category,
        "title": title,
        "question": question,
        "answer": answer,
        "related_info": related_items,
        "keywords": keyword_list,
        "content": content,
        "created_at": datetime.now().isoformat()
    }

    _session_documents[session_id].append(doc_data)

    # Stream the document to frontend
    try:
        from tools.streaming_callback import stream_asset
        stream_asset("faq", filename, content, operation_id="knowledge_base", is_complete=True)
    except ImportError:
        pass

    return {
        "success": True,
        "filename": filename,
        "category": category,
        "title": title,
        "content_length": len(content),
        "message": f"FAQ document saved: {filename}"
    }


@tool
def list_generated_documents(session_id: str = "default") -> dict:
    """
    List all FAQ documents generated in this session.

    Args:
        session_id: Session identifier

    Returns:
        List of generated document summaries
    """
    documents = get_session_documents(session_id)

    doc_list = []
    for doc in documents:
        doc_list.append({
            "filename": doc.get("filename"),
            "category": doc.get("category"),
            "title": doc.get("title"),
            "content_length": len(doc.get("content", ""))
        })

    return {
        "success": True,
        "document_count": len(doc_list),
        "documents": doc_list
    }


@tool
def create_knowledge_base_package(
    package_name: str,
    include_readme: bool = True,
    output_format: str = "txt",
    session_id: str = "default"
) -> dict:
    """
    Package all generated FAQ documents into a downloadable ZIP file.

    Args:
        package_name: Name for the ZIP package
        include_readme: Whether to include a README file
        output_format: Output format (txt, md, json)
        session_id: Session identifier

    Returns:
        ZIP file info with base64 encoded content
    """
    documents = get_session_documents(session_id)

    if not documents:
        return {
            "success": False,
            "error": "No documents to package. Generate FAQ documents first.",
            "document_count": 0
        }

    # Create ZIP file in memory
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Group documents by category
        categories = {}
        for doc in documents:
            cat = doc.get("category", "general")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(doc)

        # Add README if requested
        if include_readme:
            readme_content = f"""# {package_name} Knowledge Base

이 패키지는 Amazon Bedrock Knowledge Base용 FAQ 문서를 포함하고 있습니다.

## 구성

- **전체 문서 수**: {len(documents)}개
- **카테고리**: {', '.join(categories.keys())}
- **생성일**: {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 파일 구조

```
{package_name}/
├── README.md
"""
            for cat, docs in categories.items():
                readme_content += f"├── {cat}/\n"
                for doc in docs:
                    readme_content += f"│   └── {doc.get('filename')}\n"

            readme_content += """```

## 사용 방법

1. Amazon Bedrock 콘솔에서 Knowledge Base 생성
2. S3 버킷에 이 폴더의 문서들 업로드
3. Knowledge Base의 데이터 소스로 S3 버킷 연결
4. Knowledge Base 동기화 실행

## 문서 형식

각 FAQ 문서는 다음 구조를 따릅니다:

- **질문**: 고객이 할 수 있는 질문
- **답변**: 해당 질문에 대한 답변
- **관련 정보**: 추가 참고 사항
- **메타데이터**: 카테고리, 키워드, 업데이트 날짜

## 주의사항

- 문서 내용이 정확한지 검토 후 사용하세요
- 필요시 답변을 수정하거나 보완할 수 있습니다
- 정기적으로 문서를 업데이트하여 최신 정보를 유지하세요
"""
            zip_file.writestr(f"{package_name}/README.md", readme_content)

        # Add documents organized by category
        for cat, docs in categories.items():
            for doc in docs:
                filename = doc.get("filename", "unknown.txt")
                content = doc.get("content", "")

                # Adjust extension based on format
                if output_format == "md":
                    filename = filename.replace(".txt", ".md")
                elif output_format == "json":
                    filename = filename.replace(".txt", ".json")
                    content = json.dumps({
                        "title": doc.get("title"),
                        "question": doc.get("question"),
                        "answer": doc.get("answer"),
                        "category": doc.get("category"),
                        "keywords": doc.get("keywords", []),
                        "related_info": doc.get("related_info", [])
                    }, ensure_ascii=False, indent=2)

                zip_file.writestr(f"{package_name}/{cat}/{filename}", content)

    # Get ZIP content
    zip_buffer.seek(0)
    zip_content = zip_buffer.getvalue()
    # Save ZIP to S3 for "Download All" functionality
    s3_key = None
    try:
        from tools.s3_asset_storage import save_binary_asset_to_s3
        s3_key = save_binary_asset_to_s3(
            session_id=session_id,
            asset_type="package",
            file_name=f"{package_name}.zip",
            content=zip_content,
            operation_id="knowledge_base",
            content_type="application/zip"
        )
        if s3_key:
            logger.info(f"[FAQ] Saved knowledge base package to S3: {s3_key}")
    except Exception as e:
        logger.warning(f"[FAQ] Failed to save package to S3: {e}")

    # Stream the package info to frontend with download data
    try:
        from tools.streaming_callback import stream_asset

        package_info = f"""# Knowledge Base Package: {package_name}

## Package Summary
- **Total Documents**: {len(documents)}
- **Categories**: {len(categories)}
- **Format**: {output_format}
- **File Size**: {len(zip_content):,} bytes

## Categories
{chr(10).join([f"- **{cat}**: {len(docs)} documents" for cat, docs in categories.items()])}

## Download
Use the download button to get the ZIP file containing all FAQ documents.
"""
        stream_asset(
            "package",
            f"{package_name}.zip",
            package_info,
            operation_id="knowledge_base",
            is_complete=True,
            s3_key=s3_key,  # Use the ZIP's S3 key — skip internal save to avoid overwriting
        )
        logger.info(f"[FAQ] Streamed package info (ZIP on S3: {s3_key})")
    except ImportError:
        pass

    return {
        "success": True,
        "package_name": f"{package_name}.zip",
        "document_count": len(documents),
        "category_count": len(categories),
        "file_size_bytes": len(zip_content),
        "format": output_format,
        "s3_key": s3_key,  # Frontend downloads from S3
        "message": f"Created {package_name}.zip with {len(documents)} FAQ documents"
    }


# ========================================
# Main FAQ Generator Agent Tool
# ========================================

@tool
async def faq_generator_agent(
    research_results: str = "",
    company_name: str = "",
    output_format: str = "txt",
    session_id: str = "default",
    orchestrator_context: str = "",
    auto_package: bool = True
) -> AsyncIterator:
    """
    Generate FAQ documents from research results for knowledge bases.

    This async tool creates structured FAQ documents optimized for
    RAG systems and Amazon Bedrock Knowledge Bases.

    Research data is loaded automatically from S3 ({session_id}/research/research.json).
    The research_results parameter is optional — only needed as fallback if S3 is unavailable.

    Yields:
        dict: Streaming events with progress and document previews

    Final yield:
        dict: Summary with document count and ZIP package info

    Args:
        research_results: (Optional) JSON string fallback. If empty, reads from S3 automatically.
        company_name: Name of the company (override)
        output_format: Document format (txt, md, json)
        session_id: Session identifier for continuity
        orchestrator_context: Context from parent agent
        auto_package: Automatically create ZIP package when done
    """
    _setup_streaming_for_subagent()

    # Yield: Starting
    _send_progress("faq_generator", "started", f"FAQ 문서 생성 시작: {company_name or 'Knowledge Base'}")
    yield {
        "type": "progress",
        "agent": "faq_generator",
        "status": "started",
        "content": f"FAQ 문서 생성 시작: {company_name or 'Knowledge Base'}"
    }

    # Load research: S3 first, then parameter fallback
    research = None

    # 1) Try S3
    if not research:
        try:
            from tools.s3_asset_storage import get_asset_from_s3, build_s3_key
            s3_key = build_s3_key(session_id, "research", "research.json")
            s3_content = get_asset_from_s3(s3_key)
            if s3_content:
                research = json.loads(s3_content)
                logger.info(f"[FAQ] Loaded research from S3: {s3_key}")
        except Exception as e:
            logger.warning(f"[FAQ] Failed to load research from S3: {e}")

    # 2) Fallback: parameter
    if not research and research_results:
        try:
            research = json.loads(research_results) if isinstance(research_results, str) else research_results
        except json.JSONDecodeError as e:
            yield {"type": "error", "agent": "faq_generator", "content": f"Invalid JSON: {e}"}
            yield {
                "success": False,
                "_completion_marker": "SUBAGENT_COMPLETE",
                "error": f"Invalid research results JSON: {e}",
                "summary": "Failed to parse research results"
            }
            return

    # 3) Fallback: shared context
    if not research:
        try:
            from tools.shared_context import get_research_context
            all_research = get_research_context(session_id)
            if all_research:
                # Use the first (most recent) research
                research = next(iter(all_research.values()))
                logger.info(f"[FAQ] Loaded research from shared context")
        except Exception:
            pass

    if not research:
        yield {"type": "error", "agent": "faq_generator", "content": "No research data found"}
        yield {
            "success": False,
            "_completion_marker": "SUBAGENT_COMPLETE",
            "error": "No research data available. Run research_agent first.",
            "summary": "No research data found"
        }
        return

    if not company_name:
        company_name = research.get("company_name", "Company")

    # Build generation prompt
    history = get_session_history(session_id)

    # Get shared context for additional customer/research info
    shared_context = ""
    try:
        from tools.shared_context import get_customer_context, get_research_context
        customer = get_customer_context(session_id)
        all_research = get_research_context(session_id)

        if customer or all_research:
            context_parts = []
            if customer:
                context_parts.append(f"## Customer Context\n- Company: {customer.get('company_name', 'N/A')}\n- Industry: {customer.get('industry', 'N/A')}")
            if all_research:
                context_parts.append(f"## Available Research\n{len(all_research)} research results available in context")
            shared_context = "\n\n".join(context_parts) + "\n\n---\n"
            logger.debug(f"[SharedContext] Loaded context for session {session_id}: {len(shared_context)} chars")
    except ImportError:
        logger.debug("[SharedContext] shared_context module not available (expected in some environments)")
    except Exception as e:
        logger.warning(f"[SharedContext] Failed to get shared context: {type(e).__name__}: {e}")

    pm_briefing = ""
    if orchestrator_context:
        pm_briefing = f"## PM Briefing\n{orchestrator_context}\n\n---\n"

    # Combine shared context with PM briefing
    full_context = shared_context + pm_briefing

    generation_prompt = f"""{full_context}## Generate FAQ Documents

Company: {company_name}

### Research Findings
{json.dumps(research, indent=2, ensure_ascii=False)}

### Instructions
Based on the research findings above, generate FAQ documents for a knowledge base.

**IMPORTANT: Call save_faq_document ONE AT A TIME — generate one document, save it, then move to the next. The user sees each document appear in real-time.**

1. Analyze the research and plan all FAQ documents
2. Call save_faq_document for each document sequentially (one tool call per turn)
3. Organize documents by category with metadata and keywords
4. Ensure answers are accurate based on research

Cover these topics:
- Company overview and general information
- Products/services offered
- Policies (return, shipping, etc.)
- Customer service information
- Any specific topics from the research

Use the same language as the research content.
"""

    documents_generated = []

    try:
        # Use Sonnet for FAQ generation (fast, sufficient quality for document writing)
        model_id = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
        region = os.environ.get("AWS_REGION", "ap-northeast-1")

        model = BedrockModel(
            model_id=model_id,
            region_name=region,
            temperature=0.5,
            max_tokens=64000,
            streaming=True,
            # cache_prompt removed - using cachePoint in system_prompt instead
            cache_tools="default",   # Cache tool definitions
            boto_client_config=BotocoreConfig(read_timeout=600),
        )

        # Convert history to Strands format
        recent_history = history[-10:] if len(history) > 10 else history
        strands_messages = [
            {"role": msg["role"], "content": [{"text": msg["content"]}]}
            for msg in recent_history
        ]

        # Create tracked tools with session_id
        @tool
        def save_faq_tracked(
            filename: str,
            category: str,
            title: str,
            question: str,
            answer: str,
            related_info: list = None,
            keywords: list = None
        ) -> dict:
            """Save FAQ document with session tracking."""
            documents_generated.append(filename)
            return save_faq_document(
                filename, category, title, question, answer,
                related_info, keywords, session_id
            )

        @tool
        def list_docs_tracked() -> dict:
            """List generated documents."""
            return list_generated_documents(session_id)

        @tool
        def create_package_tracked(
            package_name: str,
            include_readme: bool = True,
            format: str = "txt"
        ) -> dict:
            """Create ZIP package with session tracking."""
            return create_knowledge_base_package(
                package_name, include_readme, format, session_id
            )

        agent = Agent(
            model=model,
            system_prompt=[
                {"text": FAQ_GENERATOR_SYSTEM_PROMPT},
                {"cachePoint": {"type": "default"}},
            ],
            tools=[save_faq_tracked, list_docs_tracked, create_package_tracked],
            callback_handler=None,
            messages=strands_messages,
        )

        # Yield: Running
        yield {
            "type": "progress",
            "agent": "faq_generator",
            "status": "running",
            "content": "FAQ 문서 생성 중..."
        }

        # Stream the agent execution with periodic heartbeats
        # CRITICAL: Use explicit generator cleanup to prevent OpenTelemetry context errors
        from tools.heartbeat_utils import create_heartbeat_manager

        last_heartbeat = time.time()
        heartbeat = create_heartbeat_manager(
            callback_handler=get_callback_handler(),
            agent_name="faq_generator",
            project_name=company_name or "faq",
        )

        async with heartbeat:
            generator = agent.stream_async(generation_prompt)
            try:
                async for event in generator:
                    # Yield text chunks
                    if "data" in event:
                        yield {
                            "type": "text",
                            "agent": "faq_generator",
                            "content": event["data"]
                        }
                        heartbeat.update_progress(len(documents_generated))

                    # Track tool use
                    if "current_tool_use" in event:
                        tool_use = event["current_tool_use"]
                        tool_name = tool_use.get("name", "")

                        if tool_name:
                            yield {
                                "type": "tool_use",
                                "agent": "faq_generator",
                                "tool": tool_name,
                                "input": tool_use.get("input", {})
                            }

                    # Forward tool results (content already sent via stream_asset, keep result small)
                    if "tool_result" in event:
                        tool_result = event["tool_result"]
                        result_content = tool_result.get("content")
                        # Strip large content fields - actual content already streamed via stream_asset
                        if isinstance(result_content, dict):
                            result_content = {k: v for k, v in result_content.items()
                                              if k != "content" or not isinstance(v, str) or len(v) < 500}
                        elif isinstance(result_content, str) and len(result_content) > 8000:
                            result_content = result_content[:8000] + "... [truncated]"
                        yield {
                            "type": "tool_result",
                            "agent": "faq_generator",
                            "tool": tool_result.get("name", ""),
                            "result": result_content
                        }

                    if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        last_heartbeat = time.time()
                        _send_progress("faq_generator", "running",
                                       f"FAQ 문서 생성 중... ({len(documents_generated)} docs created)")
            finally:
                # Explicitly close the generator to ensure proper OpenTelemetry context cleanup
                try:
                    await generator.aclose()
                except Exception:
                    pass  # Ignore errors during cleanup

            # Auto-package INSIDE heartbeat block to prevent WebSocket idle during ZIP/S3
            package_result = None
            if auto_package and documents_generated:
                safe_name = company_name.replace(" ", "_").replace("/", "_")
                package_result = create_knowledge_base_package(
                    f"{safe_name}_knowledge_base",
                    include_readme=True,
                    output_format=output_format,
                    session_id=session_id
                )

                if package_result.get("success"):
                    logger.info(f"[FAQ] Package created: {package_result.get('package_name')}, {package_result.get('document_count')} docs")

        # Update history
        update_session_history(session_id, "user", f"Generate FAQs for {company_name}")
        update_session_history(
            session_id,
            "assistant",
            f"Generated {len(documents_generated)} FAQ documents"
        )

        # Yield: Completed
        _send_progress("faq_generator", "completed", f"FAQ 생성 완료: {len(documents_generated)}개 문서")
        yield {
            "type": "progress",
            "agent": "faq_generator",
            "status": "completed",
            "document_count": len(documents_generated),
            "content": f"FAQ 생성 완료: {len(documents_generated)}개 문서"
        }

        # Final yield: the tool's return value
        # CRITICAL: Include _completion_marker so Orchestrator knows the tool completed
        result = {
            "success": True,
            "_completion_marker": "SUBAGENT_COMPLETE",
            "documents_generated": documents_generated,
            "document_count": len(documents_generated),
            "company_name": company_name,
            "format": output_format,
            "summary": f"Generated {len(documents_generated)} FAQ documents for {company_name}"
        }

        if package_result and package_result.get("success"):
            result["package"] = {
                "name": package_result.get("package_name"),
                "size_bytes": package_result.get("file_size_bytes"),
                "s3_key": package_result.get("s3_key")
            }

        yield result

    except Exception as e:
        error_str = str(e)
        track_error(session_id, error_str, {
            "company_name": company_name,
            "documents_attempted": documents_generated
        })

        # Yield: Error
        _send_progress("faq_generator", "error", f"FAQ 생성 실패: {error_str[:100]}")
        yield {
            "type": "progress",
            "agent": "faq_generator",
            "status": "error",
            "error": error_str[:200],
            "content": f"FAQ 생성 실패: {error_str[:100]}"
        }

        # Final yield: error result
        # CRITICAL: Include _completion_marker so Orchestrator knows the tool completed
        yield {
            "success": False,
            "_completion_marker": "SUBAGENT_COMPLETE",
            "error": error_str,
            "documents_generated": documents_generated,
            "summary": f"FAQ generation failed: {error_str[:100]}"
        }
