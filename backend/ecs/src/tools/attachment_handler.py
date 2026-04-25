"""
Attachment handling utilities for converting uploaded files to Strands SDK multimodal format.

This module handles:
- Image files (PNG, JPEG, GIF, WebP) → Claude Vision direct processing
- Document files (PDF, TXT, MD, DOCX, CSV, XLSX) → Claude Document processing

Usage:
    from tools.attachment_handler import convert_attachment_to_strands_format, validate_attachment

    # Validate before processing
    is_valid, error = validate_attachment(attachment_data)
    if not is_valid:
        return {"error": error}

    # Convert to Strands format
    content_block = convert_attachment_to_strands_format(attachment_data)
"""

import base64
import logging
import re
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)

# Supported image formats for direct Claude Vision processing
IMAGE_FORMATS: Dict[str, str] = {
    'image/png': 'png',
    'image/jpeg': 'jpeg',
    'image/jpg': 'jpeg',  # Handle both jpeg and jpg
    'image/gif': 'gif',
    'image/webp': 'webp',
}

# Supported document formats for Claude Document processing
DOCUMENT_FORMATS: Dict[str, str] = {
    'application/pdf': 'pdf',
    'text/plain': 'txt',
    'text/markdown': 'md',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'text/csv': 'csv',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.ms-excel': 'xls',
}

# Size limits for WebSocket-based uploads (smaller due to WebSocket frame limits)
MAX_IMAGE_SIZE: int = int(3.75 * 1024 * 1024)  # 3.75 MB
MAX_DOCUMENT_SIZE: int = int(4.5 * 1024 * 1024)  # 4.5 MB
MAX_ATTACHMENTS_PER_MESSAGE: int = 5  # Bedrock limit

# Size limits for S3-based uploads (larger - up to AgentCore Runtime limit)
MAX_S3_IMAGE_SIZE: int = int(20 * 1024 * 1024)  # 20 MB (Bedrock Vision limit)
MAX_S3_DOCUMENT_SIZE: int = int(100 * 1024 * 1024)  # 100 MB (AgentCore Runtime limit)


def get_supported_formats() -> Dict[str, List[str]]:
    """
    Get list of supported file formats.

    Returns:
        Dictionary with 'images' and 'documents' keys containing format lists
    """
    return {
        'images': list(set(IMAGE_FORMATS.values())),  # ['png', 'jpeg', 'gif', 'webp']
        'documents': list(set(DOCUMENT_FORMATS.values())),  # ['pdf', 'txt', 'md', 'docx', 'csv', 'xlsx', 'xls']
    }


def get_format_from_mime(mime_type: str) -> Optional[str]:
    """
    Get Strands/Bedrock format string from MIME type.

    Args:
        mime_type: MIME type string (e.g., 'image/png', 'application/pdf')

    Returns:
        Format string (e.g., 'png', 'pdf') or None if unsupported
    """
    mime_type = mime_type.lower()

    if mime_type in IMAGE_FORMATS:
        return IMAGE_FORMATS[mime_type]

    if mime_type in DOCUMENT_FORMATS:
        return DOCUMENT_FORMATS[mime_type]

    return None


def is_image(mime_type: str) -> bool:
    """Check if MIME type is a supported image format."""
    return mime_type.lower() in IMAGE_FORMATS


def is_document(mime_type: str) -> bool:
    """Check if MIME type is a supported document format."""
    return mime_type.lower() in DOCUMENT_FORMATS


def validate_attachment(attachment: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Validate an attachment before processing.

    Args:
        attachment: Dict with keys:
            - name: File name
            - mimeType: MIME type
            - size: File size in bytes
            - data: Base64 encoded content (optional for validation)

    Returns:
        Tuple of (is_valid: bool, error_message: str)
        If valid, error_message is empty string
    """
    name = attachment.get('name', 'unknown')
    mime_type = attachment.get('mimeType', '').lower()
    size = attachment.get('size', 0)

    # Check if format is supported
    if mime_type in IMAGE_FORMATS:
        if size > MAX_IMAGE_SIZE:
            return False, f"Image '{name}' too large ({size / 1024 / 1024:.2f} MB). Maximum: 3.75 MB"
        return True, ""

    if mime_type in DOCUMENT_FORMATS:
        if size > MAX_DOCUMENT_SIZE:
            return False, f"Document '{name}' too large ({size / 1024 / 1024:.2f} MB). Maximum: 4.5 MB"
        return True, ""

    # Unsupported format
    supported_exts = list(set(IMAGE_FORMATS.values())) + list(set(DOCUMENT_FORMATS.values()))
    return False, f"Unsupported file type: {mime_type}. Supported formats: {', '.join(supported_exts)}"


def validate_attachments(attachments: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Validate a list of attachments.

    Args:
        attachments: List of attachment dicts

    Returns:
        Tuple of (all_valid: bool, first_error_message: str)
    """
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        return False, f"Too many attachments ({len(attachments)}). Maximum: {MAX_ATTACHMENTS_PER_MESSAGE}"

    for att in attachments:
        is_valid, error = validate_attachment(att)
        if not is_valid:
            return False, error

    return True, ""


def convert_attachment_to_strands_format(attachment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert an uploaded attachment to Strands SDK multimodal content block format.

    Args:
        attachment: Dict with keys:
            - name: File name
            - mimeType: MIME type (e.g., 'image/png', 'application/pdf')
            - data: Base64 encoded content
            - size: (optional) File size for validation

    Returns:
        Strands content block dict in one of these formats:

        For images:
        {
            "image": {
                "source": {"bytes": <bytes>},
                "format": "png"
            }
        }

        For documents:
        {
            "document": {
                "format": "pdf",
                "name": "Document Name",
                "source": {"bytes": <bytes>}
            }
        }

        Returns None if:
        - Format is unsupported
        - Base64 decoding fails
        - File exceeds size limit
    """
    mime_type = attachment.get('mimeType', '').lower()
    data_b64 = attachment.get('data', '')
    name = attachment.get('name', 'file')

    # CRITICAL: Check for empty data early
    if not data_b64:
        logger.error(f"[MULTIMODAL] Attachment '{name}' has EMPTY data field!")
        return None

    # Log data size before decode
    logger.info(f"[MULTIMODAL] Processing '{name}': mimeType={mime_type}, data_len={len(data_b64)}")

    # Decode base64 to bytes
    try:
        # Handle data URLs (e.g., "data:image/png;base64,...")
        if data_b64.startswith('data:'):
            logger.info(f"[MULTIMODAL] Data URL detected, stripping prefix")
            if ',' in data_b64:
                data_b64 = data_b64.split(',', 1)[1]
            else:
                logger.error(f"[MULTIMODAL] Invalid data URL format for '{name}'")
                return None
        elif ',' in data_b64:
            # Handle case where prefix exists but doesn't start with 'data:'
            data_b64 = data_b64.split(',', 1)[1]

        file_bytes = base64.b64decode(data_b64)
        logger.info(f"[MULTIMODAL] Decoded '{name}': {len(file_bytes)} bytes")

    except Exception as e:
        logger.error(f"[MULTIMODAL] Failed to decode base64 for '{name}': {e}")
        return None

    # Validate decoded size
    if len(file_bytes) == 0:
        logger.error(f"[MULTIMODAL] Decoded bytes are empty for '{name}'!")
        return None

    # Handle images → Strands image format
    if mime_type in IMAGE_FORMATS:
        if len(file_bytes) > MAX_IMAGE_SIZE:
            logger.warning(f"[MULTIMODAL] Image '{name}' exceeds size limit: {len(file_bytes)} > {MAX_IMAGE_SIZE}")
            return None

        result = {
            "image": {
                "source": {"bytes": file_bytes},
                "format": IMAGE_FORMATS[mime_type]
            }
        }
        logger.info(f"[MULTIMODAL] Created image block for '{name}': format={IMAGE_FORMATS[mime_type]}, bytes={len(file_bytes)}")
        return result

    # Handle documents → Strands document format
    if mime_type in DOCUMENT_FORMATS:
        if len(file_bytes) > MAX_DOCUMENT_SIZE:
            logger.warning(f"[MULTIMODAL] Document '{name}' exceeds size limit: {len(file_bytes)} > {MAX_DOCUMENT_SIZE}")
            return None

        # Extract name without extension for Bedrock
        doc_name = name.rsplit('.', 1)[0] if '.' in name else name
        # Bedrock Converse API requires document name to match [a-zA-Z0-9_.-]+
        doc_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', doc_name) or 'document'

        result = {
            "document": {
                "format": DOCUMENT_FORMATS[mime_type],
                "name": doc_name,
                "source": {"bytes": file_bytes}
            }
        }
        logger.info(f"[MULTIMODAL] Created document block for '{name}': format={DOCUMENT_FORMATS[mime_type]}, bytes={len(file_bytes)}")
        return result

    logger.warning(f"[MULTIMODAL] Unsupported MIME type for '{name}': {mime_type}")
    return None


def convert_attachments_to_content_blocks(
    attachments: List[Dict[str, Any]],
    user_message: str = ""
) -> List[Dict[str, Any]]:
    """
    Convert a list of attachments and optional text message to Strands content blocks.

    Args:
        attachments: List of attachment dicts
        user_message: Optional text message to include

    Returns:
        List of content blocks in Strands format:
        [
            {"image": {...}},   # or {"document": {...}}
            {"text": "user message"}
        ]

    Note: Attachments come before text in the content blocks
    """
    content_blocks: List[Dict[str, Any]] = []

    logger.info(f"[MULTIMODAL] Converting {len(attachments)} attachments")

    # Process attachments first
    for i, att in enumerate(attachments):
        logger.info(f"[MULTIMODAL] Processing attachment {i+1}/{len(attachments)}: {att.get('name')}")
        block = convert_attachment_to_strands_format(att)
        if block:
            content_blocks.append(block)
            logger.info(f"[MULTIMODAL] Successfully converted attachment {i+1}")
        else:
            logger.error(f"[MULTIMODAL] FAILED to convert attachment {i+1}: {att.get('name')}")

    # Add text message - REQUIRED by Bedrock when documents are present
    # If no user message provided, add a default prompt for document analysis
    has_documents = any("document" in block for block in content_blocks)

    if user_message:
        content_blocks.append({"text": user_message})
        logger.info(f"[MULTIMODAL] Added text block: {len(user_message)} chars")
    elif has_documents:
        # Bedrock Converse API requires a text block when using documents
        default_prompt = "Please analyze this document and help me with any questions I have about it."
        content_blocks.append({"text": default_prompt})
        logger.info(f"[MULTIMODAL] Added default text block for document (Bedrock requirement)")

    logger.info(f"[MULTIMODAL] Total content blocks: {len(content_blocks)}")

    # CRITICAL: Log if content is empty
    if not content_blocks:
        logger.error("[MULTIMODAL] WARNING: content_blocks is EMPTY!")

    return content_blocks


def format_attachment_for_history(attachment: Dict[str, Any]) -> str:
    """
    Format an attachment reference for conversation history.

    Since we don't store actual file content in history (to save context),
    we store a marker indicating what was attached.

    Args:
        attachment: Attachment dict with name and mimeType

    Returns:
        Formatted string like "[Attached: requirements.pdf (PDF)]"
    """
    name = attachment.get('name', 'unknown')
    mime_type = attachment.get('mimeType', '')

    file_type = get_format_from_mime(mime_type)
    if file_type:
        return f"[Attached: {name} ({file_type.upper()})]"

    return f"[Attached: {name}]"


def format_attachments_for_history(attachments: List[Dict[str, Any]]) -> str:
    """
    Format multiple attachments for conversation history.

    Args:
        attachments: List of attachment dicts

    Returns:
        Formatted string with all attachments listed
    """
    if not attachments:
        return ""

    markers = [format_attachment_for_history(att) for att in attachments]
    return " ".join(markers)


# ============================================================================
# S3 File Reading Functions (for large file support via presigned URL upload)
# ============================================================================

import os
import boto3
from botocore.exceptions import ClientError

# S3 client (initialized lazily)
_s3_client = None

def get_s3_client():
    """Get or create S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client('s3')
    return _s3_client


def get_assets_bucket() -> Optional[str]:
    """Get the assets bucket name from environment variable."""
    bucket = os.environ.get('ASSETS_BUCKET_NAME')
    if not bucket:
        logger.warning("[S3] ASSETS_BUCKET_NAME environment variable not set")
    return bucket


def read_file_from_s3(s3_key: str, bucket: Optional[str] = None) -> Optional[bytes]:
    """
    Read a file from S3 and return its bytes.

    Args:
        s3_key: The S3 object key (e.g., 'uploads/session123/image.png')
        bucket: Optional bucket name (defaults to ASSETS_BUCKET_NAME env var)

    Returns:
        File bytes or None if failed
    """
    bucket = bucket or get_assets_bucket()
    if not bucket:
        logger.error("[S3] No bucket specified and ASSETS_BUCKET_NAME not set")
        return None

    try:
        s3 = get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=s3_key)
        file_bytes = response['Body'].read()

        logger.info(f"[S3] Successfully read {len(file_bytes)} bytes from s3://{bucket}/{s3_key}")
        return file_bytes

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        logger.error(f"[S3] Failed to read s3://{bucket}/{s3_key}: {error_code} - {e}")
        return None
    except Exception as e:
        logger.error(f"[S3] Unexpected error reading s3://{bucket}/{s3_key}: {e}")
        return None


def get_mime_type_from_s3_key(s3_key: str) -> str:
    """
    Infer MIME type from S3 key (filename extension).

    Args:
        s3_key: S3 object key

    Returns:
        MIME type string
    """
    extension = s3_key.lower().split('.')[-1] if '.' in s3_key else ''

    # Map extensions to MIME types
    extension_to_mime = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'gif': 'image/gif',
        'webp': 'image/webp',
        'pdf': 'application/pdf',
        'txt': 'text/plain',
        'md': 'text/markdown',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'csv': 'text/csv',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xls': 'application/vnd.ms-excel',
    }

    return extension_to_mime.get(extension, 'application/octet-stream')


def convert_s3_reference_to_content_block(
    s3_key: str,
    content_type: Optional[str] = None,
    filename: Optional[str] = None,
    bucket: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Read a file from S3 and convert it to a Strands SDK multimodal content block.

    Args:
        s3_key: S3 object key
        content_type: Optional MIME type (inferred from key if not provided)
        filename: Optional display name (extracted from key if not provided)
        bucket: Optional bucket name (defaults to ASSETS_BUCKET_NAME)

    Returns:
        Content block dict ({"image": {...}} or {"document": {...}}) or None
    """
    # Read file from S3
    file_bytes = read_file_from_s3(s3_key, bucket)
    if not file_bytes:
        logger.error(f"[S3] Failed to read file from S3: {s3_key}")
        return None

    # Determine MIME type
    mime_type = content_type or get_mime_type_from_s3_key(s3_key)

    # Determine filename
    if not filename:
        filename = s3_key.split('/')[-1] if '/' in s3_key else s3_key
        # Remove timestamp prefix if present (format: {timestamp}_{uuid}_{filename})
        parts = filename.split('_', 2)
        if len(parts) >= 3 and parts[0].isdigit():
            filename = parts[2]

    logger.info(f"[S3] Converting S3 file to content block: {s3_key}, mime={mime_type}, size={len(file_bytes)}")

    # Check size limits (use S3 limits - larger than WebSocket limits)
    if is_image(mime_type):
        if len(file_bytes) > MAX_S3_IMAGE_SIZE:
            logger.error(f"[S3] Image exceeds size limit: {len(file_bytes)} > {MAX_S3_IMAGE_SIZE}")
            return None

        format_str = IMAGE_FORMATS.get(mime_type.lower())
        if not format_str:
            logger.error(f"[S3] Unsupported image format: {mime_type}")
            return None

        result = {
            "image": {
                "format": format_str,
                "source": {"bytes": file_bytes}
            }
        }
        logger.info(f"[S3] Created image block: format={format_str}, bytes={len(file_bytes)}")
        return result

    elif is_document(mime_type):
        if len(file_bytes) > MAX_S3_DOCUMENT_SIZE:
            logger.error(f"[S3] Document exceeds size limit: {len(file_bytes)} > {MAX_S3_DOCUMENT_SIZE}")
            return None

        format_str = DOCUMENT_FORMATS.get(mime_type.lower())
        if not format_str:
            logger.error(f"[S3] Unsupported document format: {mime_type}")
            return None

        # Clean filename for document name
        doc_name = filename.rsplit('.', 1)[0] if '.' in filename else filename
        doc_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', doc_name) or 'document'
        doc_name = doc_name[:100]  # Limit length

        result = {
            "document": {
                "format": format_str,
                "name": doc_name,
                "source": {"bytes": file_bytes}
            }
        }
        logger.info(f"[S3] Created document block: format={format_str}, name={doc_name}, bytes={len(file_bytes)}")
        return result

    else:
        logger.warning(f"[S3] Unsupported MIME type: {mime_type}")
        return None


def convert_s3_attachments_to_content_blocks(
    s3_attachments: List[Dict[str, Any]],
    user_message: str = ""
) -> List[Dict[str, Any]]:
    """
    Convert a list of S3 attachment references to Strands content blocks.

    Args:
        s3_attachments: List of dicts with 's3Key', optional 'contentType', 'filename'
            Example: [{"s3Key": "uploads/session/file.png", "contentType": "image/png"}]
        user_message: Optional text message to include

    Returns:
        List of content blocks in Strands format
    """
    content_blocks: List[Dict[str, Any]] = []

    logger.info(f"[S3] Converting {len(s3_attachments)} S3 attachments to content blocks")

    for i, att in enumerate(s3_attachments):
        s3_key = att.get('s3Key')
        if not s3_key:
            logger.warning(f"[S3] Attachment {i+1} missing s3Key, skipping")
            continue

        content_type = att.get('contentType') or att.get('mimeType')
        filename = att.get('filename') or att.get('name')

        logger.info(f"[S3] Processing attachment {i+1}: {s3_key}")

        block = convert_s3_reference_to_content_block(
            s3_key=s3_key,
            content_type=content_type,
            filename=filename
        )

        if block:
            content_blocks.append(block)
            logger.info(f"[S3] Successfully converted attachment {i+1}")
        else:
            logger.error(f"[S3] FAILED to convert attachment {i+1}: {s3_key}")

    # Add text message - REQUIRED by Bedrock when documents are present
    has_documents = any("document" in block for block in content_blocks)

    if user_message:
        content_blocks.append({"text": user_message})
        logger.info(f"[S3] Added text block: {len(user_message)} chars")
    elif has_documents:
        # Bedrock Converse API requires a text block when using documents
        default_prompt = "Please analyze this document and help me with any questions I have about it."
        content_blocks.append({"text": default_prompt})
        logger.info(f"[S3] Added default text block for document (Bedrock requirement)")

    logger.info(f"[S3] Total content blocks: {len(content_blocks)}")

    return content_blocks
