"""
OpenAPI Specification Generator Tool

Generates OpenAPI 3.0 specifications for MCP Gateway integration
based on saved operation specifications.

Features:
- AgentCore Gateway compatible extension fields (x-amazon-connect-tool-*)
- AI-friendly descriptions optimized for LLM understanding
- Structured response schemas for easy AI parsing
"""

import json
import yaml
from strands import tool
from .spec_manager import get_all_specs, OperationSpec, FieldSpec
from .streaming_callback import stream_asset, complete_asset


@tool
def generate_openapi_spec(
    api_title: str,
    api_description: str,
    api_version: str = "1.0.0",
    server_url: str = "${API_GATEWAY_URL}",
    include_operations: list[str] = None,
    include_mcp_extensions: bool = True
) -> dict:
    """
    Generate an OpenAPI 3.0 specification from saved operation specifications.

    This tool generates a complete OpenAPI spec that can be used with:
    - Amazon API Gateway
    - Bedrock AgentCore MCP Gateway
    - API documentation tools

    Args:
        api_title: Title of the API (e.g., "Customer Reservation API")
        api_description: Description of what this API does
        api_version: API version string (default: "1.0.0")
        server_url: Base URL for the API (use ${API_GATEWAY_URL} for variable substitution)
        include_operations: List of operation IDs to include (None = all)
        include_mcp_extensions: Include x-amazon-connect-tool-* extension fields for MCP Gateway

    Returns:
        Generated OpenAPI specification in YAML format
    """
    specs = get_all_specs()

    if not specs:
        return {
            "success": False,
            "error": "No operation specifications found. Please save at least one operation using save_operation_spec first."
        }

    # Filter operations if specified
    if include_operations:
        specs = {k: v for k, v in specs.items() if k in include_operations}

    if not specs:
        return {
            "success": False,
            "error": f"None of the specified operations found. Available: {list(get_all_specs().keys())}"
        }

    try:
        openapi_spec = _build_openapi_spec(
            title=api_title,
            description=api_description,
            version=api_version,
            server_url=server_url,
            operations=specs,
            include_mcp_extensions=include_mcp_extensions
        )

        # Convert to YAML
        yaml_content = yaml.dump(openapi_spec, default_flow_style=False, allow_unicode=True, sort_keys=False)
        json_content = json.dumps(openapi_spec, indent=2, ensure_ascii=False)

        # Stream the generated content (use api_title as operation_id for uniqueness)
        op_id = api_title.replace(" ", "_").lower()
        stream_asset("openapi", "openapi.yaml", yaml_content, operation_id=op_id, is_complete=True)
        stream_asset("openapi", "openapi.json", json_content, operation_id=op_id, is_complete=True)
        complete_asset("openapi", operation_id=op_id)

        return {
            "success": True,
            "operation_count": len(specs),
            "operations_included": list(specs.keys()),
            "openapi_yaml": yaml_content,
            "openapi_json": json_content
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to generate OpenAPI spec: {str(e)}"
        }


def _build_openapi_spec(
    title: str,
    description: str,
    version: str,
    server_url: str,
    operations: dict[str, OperationSpec],
    include_mcp_extensions: bool = True
) -> dict:
    """Build the complete OpenAPI specification."""

    spec = {
        "openapi": "3.0.1",
        "info": {
            "title": title,
            "description": description,
            "version": version,
            "contact": {
                "name": "AI Contact Center Builder"
            }
        },
        "servers": [
            {
                "url": server_url,
                "description": "API Gateway endpoint"
            }
        ],
        "paths": {},
        "components": {
            "schemas": {},
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key"
                }
            }
        },
        "security": [
            {"ApiKeyAuth": []}
        ],
        "tags": []
    }

    # Add MCP Gateway extension at spec level
    if include_mcp_extensions:
        spec["x-amazon-connect-mcp-gateway"] = {
            "version": "1.0",
            "toolProvider": "AgentCore Gateway",
            "description": "MCP tools for Amazon Connect Q in Connect AI self-service"
        }

    # Collect unique tags
    tags_set = set()

    # Generate paths for each operation
    for op_id, op_spec in operations.items():
        path = op_spec.path
        method = op_spec.http_method.lower()

        if path not in spec["paths"]:
            spec["paths"][path] = {}

        # Determine tag from operation type
        tag = _get_tag_from_operation(op_spec)
        tags_set.add(tag)

        # Build operation object with AI-friendly descriptions
        ai_friendly_desc = _build_ai_friendly_description(op_spec)

        operation_obj = {
            "operationId": op_id,
            "summary": op_spec.summary,
            "description": ai_friendly_desc,
            "tags": [tag],
        }

        # Add MCP Gateway extension fields for each operation
        if include_mcp_extensions:
            operation_obj["x-amazon-connect-tool-name"] = op_id
            operation_obj["x-amazon-connect-tool-description"] = op_spec.summary
            operation_obj["x-amazon-connect-tool-category"] = _get_tool_category(op_spec)
            operation_obj["x-amazon-connect-tool-confirmation-required"] = _requires_confirmation(op_spec)

            # Add usage hints for AI
            operation_obj["x-amazon-connect-tool-usage-hints"] = _generate_usage_hints(op_spec)

        # Add security if required
        if op_spec.requires_authentication:
            operation_obj["security"] = [{"ApiKeyAuth": []}]

        # Add request body for POST/PUT/PATCH
        if method in ("post", "put", "patch"):
            request_schema = _build_request_schema(op_spec)
            schema_name = f"{op_id}Request"
            spec["components"]["schemas"][schema_name] = request_schema

            operation_obj["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema_name}"}
                    }
                }
            }

        # Add path parameters for GET/DELETE with path variables
        path_params = _extract_path_params(path)
        if path_params:
            operation_obj["parameters"] = [
                {
                    "name": param,
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": f"The {param} to operate on"
                }
                for param in path_params
            ]

        # Add responses
        response_schema = _build_response_schema(op_spec)
        success_schema_name = f"{op_id}Response"
        spec["components"]["schemas"][success_schema_name] = response_schema

        operation_obj["responses"] = _build_responses(op_spec, success_schema_name)

        spec["paths"][path][method] = operation_obj

    # Add tags definitions
    spec["tags"] = [
        {"name": tag, "description": _get_tag_description(tag)}
        for tag in sorted(tags_set)
    ]

    return spec


def _build_request_schema(op_spec: OperationSpec) -> dict:
    """Build JSON Schema for request body."""
    properties = {}
    required = []

    for field in op_spec.input_fields:
        field_schema = _field_to_schema(field)
        properties[field.name] = field_schema

        if field.required:
            required.append(field.name)

    schema = {
        "type": "object",
        "properties": properties
    }

    if required:
        schema["required"] = required

    return schema


def _build_response_schema(op_spec: OperationSpec) -> dict:
    """Build JSON Schema for success response."""
    properties = {
        "success": {
            "type": "boolean",
            "description": "Whether the operation was successful",
            "example": True
        },
        "message": {
            "type": "string",
            "description": "Success or status message",
            "example": op_spec.success_message_template or f"{op_spec.operation_id} completed successfully"
        },
        "data": {
            "type": "object",
            "description": "Response data",
            "properties": {}
        }
    }

    # Add output fields to data
    for field in op_spec.output_fields:
        field_schema = _field_to_schema(field)
        properties["data"]["properties"][field.name] = field_schema

    return {
        "type": "object",
        "properties": properties
    }


def _field_to_schema(field: FieldSpec) -> dict:
    """Convert a FieldSpec to JSON Schema."""
    schema = {
        "description": field.description
    }

    # Map field types to JSON Schema types
    type_mapping = {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
        "date": "string",
        "datetime": "string",
        "email": "string",
        "phone": "string",
        "enum": "string",
        "array": "array",
        "object": "object"
    }

    schema["type"] = type_mapping.get(field.field_type, "string")

    # Add format for special types
    if field.field_type == "date":
        schema["format"] = "date"
    elif field.field_type == "datetime":
        schema["format"] = "date-time"
    elif field.field_type == "email":
        schema["format"] = "email"

    # Add validation constraints
    if field.min_length is not None:
        schema["minLength"] = field.min_length
    if field.max_length is not None:
        schema["maxLength"] = field.max_length
    if field.pattern is not None:
        schema["pattern"] = field.pattern
    if field.min_value is not None:
        schema["minimum"] = field.min_value
    if field.max_value is not None:
        schema["maximum"] = field.max_value
    if field.enum_values:
        schema["enum"] = field.enum_values

    # Add example
    if field.example_value:
        schema["example"] = field.example_value

    return schema


def _build_responses(op_spec: OperationSpec, success_schema_name: str) -> dict:
    """Build responses section."""
    responses = {
        str(op_spec.success_status_code): {
            "description": "Successful operation",
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{success_schema_name}"}
                }
            }
        }
    }

    # Add error responses
    error_schema = {
        "type": "object",
        "properties": {
            "error": {
                "type": "string",
                "description": "Error code"
            },
            "message": {
                "type": "string",
                "description": "Error message"
            },
            "details": {
                "type": "object",
                "description": "Additional error details"
            }
        },
        "required": ["error", "message"]
    }

    # Group error responses by status code
    error_by_status = {}
    for error in op_spec.error_responses:
        status = str(error.status_code)
        if status not in error_by_status:
            error_by_status[status] = []
        error_by_status[status].append(error)

    for status, errors in error_by_status.items():
        descriptions = [f"{e.error_code}: {e.message}" for e in errors]
        responses[status] = {
            "description": " | ".join(descriptions),
            "content": {
                "application/json": {
                    "schema": error_schema
                }
            }
        }

    # Add standard error responses if not already present
    if "400" not in responses:
        responses["400"] = {
            "description": "Bad request - validation error",
            "content": {
                "application/json": {
                    "schema": error_schema
                }
            }
        }

    if "401" not in responses and op_spec.requires_authentication:
        responses["401"] = {
            "description": "Unauthorized - API key required"
        }

    if "500" not in responses:
        responses["500"] = {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "schema": error_schema
                }
            }
        }

    return responses


def _extract_path_params(path: str) -> list[str]:
    """Extract path parameters from a path template."""
    import re
    return re.findall(r'\{(\w+)\}', path)


def _get_tag_from_operation(op_spec: OperationSpec) -> str:
    """Determine tag from operation type."""
    type_to_tag = {
        "create": "Resource Management",
        "read": "Resource Management",
        "update": "Resource Management",
        "delete": "Resource Management",
        "list": "Query Operations",
        "search": "Query Operations",
        "custom": "Business Operations"
    }
    return type_to_tag.get(op_spec.operation_type, "General")


def _get_tag_description(tag: str) -> str:
    """Get description for a tag."""
    descriptions = {
        "Resource Management": "Operations for creating, reading, updating, and deleting resources",
        "Query Operations": "Operations for listing and searching resources",
        "Business Operations": "Custom business logic operations",
        "General": "General API operations"
    }
    return descriptions.get(tag, "API operations")


def _build_ai_friendly_description(op_spec: OperationSpec) -> str:
    """Build an AI-friendly description optimized for LLM understanding."""
    parts = [op_spec.description]

    # Add input requirements
    required_fields = [f for f in op_spec.input_fields if f.required]
    optional_fields = [f for f in op_spec.input_fields if not f.required]

    if required_fields:
        req_list = ", ".join([f"`{f.name}` ({f.field_type})" for f in required_fields])
        parts.append(f"\n\n**Required inputs:** {req_list}")

    if optional_fields:
        opt_list = ", ".join([f"`{f.name}` ({f.field_type})" for f in optional_fields])
        parts.append(f"\n\n**Optional inputs:** {opt_list}")

    # Add expected outcomes
    if op_spec.success_message_template:
        parts.append(f"\n\n**Success response:** {op_spec.success_message_template}")

    # Add side effects warning
    if op_spec.side_effects:
        effects = [f"- {e.effect_type}: {e.description}" for e in op_spec.side_effects]
        parts.append(f"\n\n**Side effects:**\n" + "\n".join(effects))

    # Add error scenarios
    if op_spec.error_responses:
        error_hints = [f"- {e.error_code}: {e.message}" for e in op_spec.error_responses[:3]]
        parts.append(f"\n\n**Possible errors:**\n" + "\n".join(error_hints))

    return "".join(parts)


def _get_tool_category(op_spec: OperationSpec) -> str:
    """Determine tool category for MCP Gateway."""
    type_to_category = {
        "create": "data_modification",
        "update": "data_modification",
        "delete": "data_modification",
        "read": "data_retrieval",
        "list": "data_retrieval",
        "search": "data_retrieval",
        "custom": "business_logic"
    }
    return type_to_category.get(op_spec.operation_type, "general")


def _requires_confirmation(op_spec: OperationSpec) -> bool:
    """Determine if operation requires user confirmation before execution."""
    # Operations that modify or delete data should require confirmation
    confirmation_types = {"create", "update", "delete"}
    if op_spec.operation_type in confirmation_types:
        return True

    # Check for side effects
    if op_spec.side_effects:
        for effect in op_spec.side_effects:
            if effect.effect_type in ("delete", "update", "payment", "notification"):
                return True

    return False


def _generate_usage_hints(op_spec: OperationSpec) -> list[str]:
    """Generate usage hints for AI to better understand how to use the tool."""
    hints = []

    # Add hints based on operation type
    op_type = op_spec.operation_type
    if op_type == "create":
        hints.append("Collect all required information from the customer before calling this tool")
        hints.append("Confirm the details with the customer before creating")
    elif op_type == "search" or op_type == "list":
        hints.append("Use this to find information based on customer criteria")
        hints.append("Present results in a conversational, easy-to-understand format")
    elif op_type == "update":
        hints.append("Verify what the customer wants to change before calling")
        hints.append("Confirm the changes with the customer before proceeding")
    elif op_type == "delete":
        hints.append("Always confirm with the customer before deleting")
        hints.append("Explain any consequences of deletion")
    elif op_type == "read":
        hints.append("Use this to retrieve specific information for the customer")

    # Add hints for required fields
    required_fields = [f for f in op_spec.input_fields if f.required]
    if required_fields:
        field_names = ", ".join([f.name for f in required_fields])
        hints.append(f"Ensure you have gathered: {field_names}")

    # Add hints for error handling
    if op_spec.error_responses:
        hints.append("If the operation fails, apologize and offer alternatives or agent transfer")

    return hints
