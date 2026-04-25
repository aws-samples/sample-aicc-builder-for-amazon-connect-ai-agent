"""
Retrieve Tool for Contact Flow Knowledge Base

Uses Amazon Bedrock Knowledge Base to retrieve curated Amazon Connect
Contact Flow documentation for RAG-enhanced generation.
"""

import os
import logging
import boto3
from strands import tool

logger = logging.getLogger(__name__)


@tool
def retrieve_contact_flow_knowledge(
    query: str,
    max_results: int = 5
) -> dict:
    """
    Retrieve Amazon Connect Contact Flow documentation from Knowledge Base.

    Use this tool FIRST for ANY questions about:
    - Contact Flow block parameters and JSON syntax
    - Error types and required transitions for blocks
    - Design patterns (callback, queue overflow, hours check)
    - Best practices for Contact Flow design

    Only fall back to web search if Knowledge Base returns no results (score < 0.5)
    or if you need information about preview/beta features.

    Args:
        query: The search query describing what you need to know
               Good examples:
               - "TransferContactToQueue error types and transitions"
               - "UpdateContactTargetQueue parameters JSON format"
               - "callback pattern with UpdateContactCallbackNumber"
               - "Check hours of operation branching"
        max_results: Maximum number of results to return (default 5, max 10)

    Returns:
        dict with:
        - success: bool indicating if retrieval worked
        - results: list of retrieved documents with content, score, and source
        - source: "knowledge_base" to indicate RAG source
        - error: error message if retrieval failed
    """
    kb_id = os.environ.get("CONTACT_FLOW_KB_ID", "")

    if not kb_id:
        logger.warning("[retrieve_contact_flow_knowledge] CONTACT_FLOW_KB_ID not configured")
        return {
            "success": False,
            "error": "Knowledge Base not configured. Use web search as fallback.",
            "results": [],
            "source": "knowledge_base"
        }

    try:
        # Use bedrock-agent-runtime for retrieval
        client = boto3.client(
            "bedrock-agent-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1")
        )

        logger.info(f"[retrieve_contact_flow_knowledge] Query: {query}, KB: {kb_id}")

        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": min(max_results, 10)
                }
            }
        )

        results = []
        for item in response.get("retrievalResults", []):
            content = item.get("content", {}).get("text", "")
            score = item.get("score", 0.0)

            # Extract source URI from location
            location = item.get("location", {})
            source_uri = ""
            if "s3Location" in location:
                source_uri = location["s3Location"].get("uri", "")

            results.append({
                "content": content,
                "score": score,
                "source_uri": source_uri
            })

        logger.info(
            f"[retrieve_contact_flow_knowledge] Retrieved {len(results)} results, "
            f"top score: {results[0]['score'] if results else 'N/A'}"
        )

        return {
            "success": True,
            "results": results,
            "source": "knowledge_base",
            "query": query,
            "count": len(results)
        }

    except client.exceptions.ResourceNotFoundException:
        logger.error(f"[retrieve_contact_flow_knowledge] Knowledge Base not found: {kb_id}")
        return {
            "success": False,
            "error": f"Knowledge Base {kb_id} not found",
            "results": [],
            "source": "knowledge_base"
        }
    except client.exceptions.ValidationException as e:
        logger.error(f"[retrieve_contact_flow_knowledge] Validation error: {e}")
        return {
            "success": False,
            "error": f"Invalid request: {str(e)}",
            "results": [],
            "source": "knowledge_base"
        }
    except Exception as e:
        logger.error(f"[retrieve_contact_flow_knowledge] Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "results": [],
            "source": "knowledge_base"
        }
