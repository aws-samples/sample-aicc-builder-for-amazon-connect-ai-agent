"""
Shared Context Store for Cross-Agent Communication

Simple context sharing between Sub-Agents with minimal access control.

Context Access:
- FAQ Generator: Full research access (needs all details for knowledge base)
- Other Agents: Use parameters from Orchestrator, no shared context access

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                     Orchestrator Agent                          │
    │        (passes operation_spec, orchestrator_context to agents)  │
    │                           │                                     │
    │        ┌──────────────────┼──────────────────┐                  │
    │        ▼                  ▼                  ▼                  │
    │  Research Agent    Lambda Generator    FAQ Generator            │
    │   (writes)         (params only)       (FULL research)         │
    │        │                                     │                  │
    │        ▼                                     ▼                  │
    │  ┌──────────────────────────────────────────────┐              │
    │  │          Shared Context Store                 │              │
    │  │  research: {...}  ──► FAQ Generator only     │              │
    │  │  customer: {...}  ──► FAQ Generator only     │              │
    │  └──────────────────────────────────────────────┘              │
    └─────────────────────────────────────────────────────────────────┘
"""

from typing import Dict, Any, Optional, List
import threading
import logging

logger = logging.getLogger(__name__)

# Thread-safe session context store
_context_lock = threading.RLock()
_session_context: Dict[str, Dict[str, Any]] = {}


def _ensure_session(session_id: str) -> Dict[str, Any]:
    """Ensure session exists in context store."""
    if session_id not in _session_context:
        _session_context[session_id] = {
            "research": {},
            "customer": {},
            "operations": [],
            "faq_documents": [],
            "metadata": {}
        }
    return _session_context[session_id]


# ==============================================
# Research Context (from Research Agent)
# ==============================================

def save_research_context(
    session_id: str,
    research_id: str,
    company_name: str,
    industry: str,
    findings: Dict[str, Any],
    sources: List[str] = None
) -> bool:
    """
    Save research findings to shared context store.

    Called by Research Agent after completing web research.
    Other agents can access this via get_research_context().

    Args:
        session_id: Session identifier
        research_id: Unique research identifier
        company_name: Name of researched company
        industry: Industry/sector
        findings: Structured findings (overview, services, policies, faq_topics)
        sources: List of source URLs

    Returns:
        True if saved successfully
    """
    with _context_lock:
        ctx = _ensure_session(session_id)

        ctx["research"][research_id] = {
            "research_id": research_id,
            "company_name": company_name,
            "industry": industry,
            "findings": findings,
            "sources": sources or []
        }

        # Also update customer context if company info is new
        if company_name and not ctx["customer"].get("company_name"):
            ctx["customer"]["company_name"] = company_name
            ctx["customer"]["industry"] = industry

        logger.info(f"[SharedContext] Saved research '{research_id}' for {company_name} ({industry})")
        logger.debug(f"[SharedContext] Research findings keys: {list(findings.keys()) if findings else 'None'}")
        return True


def get_research_context(
    session_id: str,
    research_id: str = None
) -> Dict[str, Any]:
    """
    Get research findings from shared context.

    Can be called by Lambda Generator, Prompt Generator, etc.
    to incorporate research findings into generated assets.

    Args:
        session_id: Session identifier
        research_id: Specific research ID (optional, returns all if None)

    Returns:
        Research findings dict
    """
    with _context_lock:
        ctx = _ensure_session(session_id)

        if research_id:
            result = ctx["research"].get(research_id, {})
            logger.debug(f"[SharedContext] Retrieved research '{research_id}': {'found' if result else 'not found'}")
            return result

        research_count = len(ctx["research"])
        logger.debug(f"[SharedContext] Retrieved all research for session {session_id}: {research_count} items")
        return ctx["research"]


def get_research_summary(session_id: str) -> str:
    """
    Get a formatted summary of all research for LLM context injection.

    Returns a concise text summary suitable for inclusion in prompts.
    """
    with _context_lock:
        ctx = _ensure_session(session_id)
        research = ctx.get("research", {})

        if not research:
            return ""

        summaries = []
        for rid, data in research.items():
            company = data.get("company_name", "Unknown")
            industry = data.get("industry", "Unknown")
            findings = data.get("findings", {})

            summary_parts = [f"## {company} ({industry})"]

            if findings.get("overview"):
                summary_parts.append(f"Overview: {findings['overview'][:200]}...")

            services = findings.get("services", [])
            if services:
                summary_parts.append(f"Services: {', '.join(services[:5])}")

            policies = findings.get("policies", {})
            if policies:
                policy_names = list(policies.keys())[:5]
                summary_parts.append(f"Policies: {', '.join(policy_names)}")

            faq_topics = findings.get("faq_topics", [])
            if faq_topics:
                summary_parts.append(f"FAQ Topics: {len(faq_topics)} items identified")

            summaries.append("\n".join(summary_parts))

        return "\n\n---\n\n".join(summaries)


# ==============================================
# Customer Context (from Questionnaire/Interview)
# ==============================================

def save_customer_context(
    session_id: str,
    company_name: str = None,
    industry: str = None,
    language: str = None,
    services: List[str] = None,
    custom_fields: Dict[str, Any] = None
) -> bool:
    """
    Save customer information to shared context.

    Called during interview/questionnaire processing.

    Args:
        session_id: Session identifier
        company_name: Customer's company name
        industry: Industry/sector
        language: Preferred language (ko-KR, en-US, etc.)
        services: List of services offered
        custom_fields: Any additional customer-specific fields

    Returns:
        True if saved successfully
    """
    with _context_lock:
        ctx = _ensure_session(session_id)

        if company_name:
            ctx["customer"]["company_name"] = company_name
        if industry:
            ctx["customer"]["industry"] = industry
        if language:
            ctx["customer"]["language"] = language
        if services:
            ctx["customer"]["services"] = services
        if custom_fields:
            ctx["customer"].update(custom_fields)

        return True


def get_customer_context(session_id: str) -> Dict[str, Any]:
    """
    Get customer information from shared context.

    Returns:
        Customer context dict with company_name, industry, etc.
    """
    with _context_lock:
        ctx = _ensure_session(session_id)
        return ctx.get("customer", {})


# ==============================================
# Operations Context (from Operation Specs)
# ==============================================

def save_operation_context(
    session_id: str,
    operation_id: str,
    operation_type: str,
    description: str,
    input_fields: List[Dict[str, Any]] = None,
    metadata: Dict[str, Any] = None
) -> bool:
    """
    Save operation specification to shared context.

    Called when operations are defined/saved.
    """
    with _context_lock:
        ctx = _ensure_session(session_id)

        # Check if operation already exists
        existing = next(
            (op for op in ctx["operations"] if op["operation_id"] == operation_id),
            None
        )

        op_data = {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "description": description,
            "input_fields": input_fields or [],
            "metadata": metadata or {}
        }

        if existing:
            # Update existing
            idx = ctx["operations"].index(existing)
            ctx["operations"][idx] = op_data
        else:
            ctx["operations"].append(op_data)

        return True


def get_operations_context(session_id: str) -> List[Dict[str, Any]]:
    """Get all operations for this session."""
    with _context_lock:
        ctx = _ensure_session(session_id)
        return ctx.get("operations", [])


# ==============================================
# FAQ Documents (from FAQ Generator)
# ==============================================

def save_faq_document(
    session_id: str,
    category: str,
    title: str,
    content: str
) -> bool:
    """Save generated FAQ document to shared context."""
    with _context_lock:
        ctx = _ensure_session(session_id)

        ctx["faq_documents"].append({
            "category": category,
            "title": title,
            "content": content
        })

        return True


def get_faq_documents(session_id: str, category: str = None) -> List[Dict[str, Any]]:
    """Get FAQ documents, optionally filtered by category."""
    with _context_lock:
        ctx = _ensure_session(session_id)
        docs = ctx.get("faq_documents", [])

        if category:
            return [d for d in docs if d.get("category") == category]
        return docs


# ==============================================
# Full Context (for LLM prompts)
# ==============================================

def get_full_context(session_id: str) -> Dict[str, Any]:
    """
    Get complete context for this session.

    Useful for passing to Orchestrator or Sub-Agents that need
    full visibility into accumulated session state.
    """
    with _context_lock:
        ctx = _ensure_session(session_id)
        return {
            "customer": ctx.get("customer", {}),
            "research": ctx.get("research", {}),
            "operations": ctx.get("operations", []),
            "faq_documents": ctx.get("faq_documents", []),
            "metadata": ctx.get("metadata", {})
        }


def get_context_for_generation(session_id: str) -> str:
    """
    DEPRECATED: Other agents should use Orchestrator parameters instead.

    This function now returns empty string.
    FAQ Generator should use get_full_research_context() directly.
    """
    logger.warning(
        f"[SharedContext] DEPRECATED: get_context_for_generation() called for session {session_id}. "
        "Use Orchestrator parameters instead."
    )
    return ""


def get_full_research_context(session_id: str) -> str:
    """
    Get FULL research context for FAQ Generator.

    This is the ONLY function that provides full research access.
    Other agents should use parameters passed from Orchestrator.

    Returns:
        Formatted context string with all research details
    """
    with _context_lock:
        ctx = _ensure_session(session_id)

        parts = []

        # Customer info
        customer = ctx.get("customer", {})
        if customer:
            company = customer.get("company_name", "Unknown Company")
            industry = customer.get("industry", "Unknown Industry")
            parts.append(f"## Customer: {company} ({industry})")

            services = customer.get("services", [])
            if services:
                parts.append(f"Services: {', '.join(services)}")

            language = customer.get("language")
            if language:
                parts.append(f"Language: {language}")

        # Full research findings
        research = ctx.get("research", {})
        if research:
            parts.append("\n## Research Findings")
            for rid, data in research.items():
                findings = data.get("findings", {})

                if findings.get("overview"):
                    parts.append(f"\n### Overview\n{findings['overview']}")

                services = findings.get("services", [])
                if services:
                    parts.append(f"\n### Services\n- " + "\n- ".join(services))

                policies = findings.get("policies", {})
                if policies:
                    parts.append("\n### Policies")
                    for policy_name, policy_detail in policies.items():
                        parts.append(f"- {policy_name}: {policy_detail}")

                faq_topics = findings.get("faq_topics", [])
                if faq_topics:
                    parts.append(f"\n### FAQ Topics ({len(faq_topics)} items)")
                    for topic in faq_topics[:30]:  # Limit to 30 topics
                        if isinstance(topic, dict):
                            parts.append(f"- {topic.get('question', topic)}")
                        else:
                            parts.append(f"- {topic}")

                sources = data.get("sources", [])
                if sources:
                    parts.append(f"\n### Sources\n- " + "\n- ".join(sources[:10]))

        return "\n".join(parts) if parts else ""


# ==============================================
# Session Management
# ==============================================

def clear_session_context(session_id: str) -> bool:
    """
    Clear all context for a session.

    Should be called when session ends.
    """
    with _context_lock:
        if session_id in _session_context:
            # Log what's being cleared for debugging
            ctx = _session_context[session_id]
            logger.info(
                f"[SharedContext] Clearing session {session_id}: "
                f"research={len(ctx.get('research', {}))}, "
                f"operations={len(ctx.get('operations', []))}, "
                f"faq_docs={len(ctx.get('faq_documents', []))}"
            )
            del _session_context[session_id]
            return True
        logger.debug(f"[SharedContext] Session {session_id} not found (nothing to clear)")
        return False


def get_session_metadata(session_id: str) -> Dict[str, Any]:
    """Get session metadata."""
    with _context_lock:
        ctx = _ensure_session(session_id)
        return ctx.get("metadata", {})


def set_session_metadata(session_id: str, key: str, value: Any) -> bool:
    """Set a metadata value for the session."""
    with _context_lock:
        ctx = _ensure_session(session_id)
        ctx["metadata"][key] = value
        return True
