"""
Operation Specification Manager

Manages the storage and retrieval of operation specifications
that the agent gathers through conversation.

This module uses flexible Pydantic models that accept various field naming
conventions from LLMs (e.g., "type" vs "field_type", "rule" vs "description").
"""

import os
import json as _json
import logging
from typing import Optional, Any, List
from pathlib import Path
from pydantic import BaseModel, Field, AliasChoices, ConfigDict, field_validator
from strands import tool

from tools.session_context import (
    current_session_id,
    operation_specs_bucket,
    get_infrastructure_spec_for,
    set_infrastructure_spec_for,
    get_session_flow_config_for,
    set_session_flow_config_for,
)

logger = logging.getLogger(__name__)


class FlexibleBaseModel(BaseModel):
    """
    Base model with flexible configuration for LLM compatibility.

    - extra="allow": Accept any additional fields LLM sends
    - populate_by_name=True: Allow both alias and original field names
    - validate_assignment=True: Validate on assignment
    """
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class FieldSpec(FlexibleBaseModel):
    """Specification for a single input/output field."""

    name: str = Field(description="Field name (camelCase)")

    # Accept "field_type" or "type"
    field_type: Optional[str] = Field(
        default=None,
        description="Data type: string, number, integer, boolean, date, datetime, email, phone, enum, array, object",
        validation_alias=AliasChoices("field_type", "type")
    )

    required: bool = Field(default=True, description="Whether this field is required")
    description: Optional[str] = Field(default=None, description="Human-readable description")

    # Validation rules - accept various naming conventions
    min_length: Optional[int] = Field(default=None, description="Minimum string length")
    max_length: Optional[int] = Field(default=None, description="Maximum string length")
    pattern: Optional[str] = Field(default=None, description="Regex pattern for validation")

    min_value: Optional[Any] = Field(
        default=None,
        description="Minimum numeric value",
        validation_alias=AliasChoices("min_value", "minimum", "min")
    )
    max_value: Optional[Any] = Field(
        default=None,
        description="Maximum numeric value",
        validation_alias=AliasChoices("max_value", "maximum", "max")
    )
    enum_values: Optional[list] = Field(
        default=None,
        description="Allowed values for enum type",
        validation_alias=AliasChoices("enum_values", "enum", "allowed_values", "options")
    )
    date_format: Optional[str] = Field(
        default=None,
        description="Expected date format",
        validation_alias=AliasChoices("date_format", "format", "dateFormat")
    )

    # Constraints
    allow_future_dates: Optional[bool] = Field(default=None)
    allow_past_dates: Optional[bool] = Field(default=None)
    max_days_in_future: Optional[int] = Field(default=None)
    max_days_in_past: Optional[int] = Field(default=None)
    validation: Optional[str] = Field(default=None, description="Additional validation rule")

    # Default and example - Any type for flexibility
    default_value: Optional[Any] = Field(
        default=None,
        validation_alias=AliasChoices("default_value", "default", "defaultValue")
    )
    example_value: Optional[Any] = Field(
        default=None,
        description="Example valid value",
        validation_alias=AliasChoices("example_value", "example", "exampleValue", "sample")
    )

    # Security
    is_pii: bool = Field(default=False)
    mask_in_logs: bool = Field(default=False)

    # Nested shape (recursive) — populated only when field_type is "array" or "object".
    # Forward reference resolved via FieldSpec.model_rebuild() below.
    # Without this, "machineStatus: list of {machineType, state, remainingSeconds}" or
    # similar nested customer requirements are flattened/collapsed through the generators.
    items: Optional["FieldSpec"] = Field(
        default=None,
        description=(
            "For field_type='array': schema of each element. "
            "If items.field_type='object', use items.properties for sub-fields. "
            "If items is a scalar/enum, use items.field_type + items.enum_values."
        ),
        validation_alias=AliasChoices("items", "item", "element", "elementType"),
    )
    properties: Optional[List["FieldSpec"]] = Field(
        default=None,
        description=(
            "For field_type='object': list of sub-field FieldSpec objects. "
            "For arrays of objects, set items.field_type='object' and items.properties."
        ),
        validation_alias=AliasChoices("properties", "sub_fields", "subFields", "fields"),
    )


# Resolve the forward reference to FieldSpec itself (Pydantic v2 recursive model).
FieldSpec.model_rebuild()


class BusinessRule(FlexibleBaseModel):
    """A single business rule for an operation."""

    rule_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("rule_id", "ruleId", "id")
    )
    description: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("description", "rule", "text", "desc")
    )
    condition: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("condition", "when", "if")
    )
    action: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("action", "then", "do")
    )
    error_message: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("error_message", "errorMessage", "message")
    )
    error_code: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("error_code", "errorCode", "code")
    )


class ErrorResponse(FlexibleBaseModel):
    """Specification for an error response."""

    status_code: Optional[int] = Field(
        default=None,
        description="HTTP status code",
        validation_alias=AliasChoices("status_code", "statusCode", "status", "code")
    )
    error_code: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("error_code", "errorCode")
    )
    message: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("message", "msg", "error", "errorMessage")
    )
    condition: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("condition", "scenario", "when", "case")
    )


class SideEffect(FlexibleBaseModel):
    """A side effect that occurs after the main operation."""

    effect_type: Optional[str] = Field(
        default=None,
        description="Type: email, sms, notification, webhook, audit_log",
        validation_alias=AliasChoices("effect_type", "type", "effectType")
    )
    description: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("description", "desc", "text")
    )
    condition: Optional[str] = Field(default=None)
    recipient_field: Optional[str] = Field(default=None)
    template_name: Optional[str] = Field(default=None)
    subject: Optional[str] = Field(default=None)
    webhook_url: Optional[str] = Field(default=None)
    audit_fields: Optional[list] = Field(default=None)


class ConversationStep(FlexibleBaseModel):
    """A single step in a structured conversation flow."""

    step_id: str = Field(
        description="Step identifier (e.g., '1', '2', 'A', 'B')",
        validation_alias=AliasChoices("step_id", "stepId", "id"),
    )
    label: str = Field(
        description="Step name (e.g., '통화 가능 여부 확인')",
        validation_alias=AliasChoices("label", "name", "title"),
    )
    message: Optional[str] = Field(
        default=None,
        description="Fixed message verbatim (None = AI decides)",
    )
    branches: list[dict] = Field(
        default=[],
        description='[{"condition": "네", "next_step": "3"}, ...]',
    )
    tool_call: Optional[str] = Field(
        default=None,
        description="tool_id to invoke at this step",
        validation_alias=AliasChoices("tool_call", "toolCall", "tool"),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Extra instructions for AI at this step",
    )


class DataSourceSpec(FlexibleBaseModel):
    """Specification for the data source."""

    db_type: Optional[str] = Field(
        default=None,
        description="Database type: dynamodb, rds_mysql, rds_postgresql",
        validation_alias=AliasChoices("db_type", "type", "dbType", "database_type")
    )
    table_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("table_name", "tableName", "table")
    )
    partition_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("partition_key", "partitionKey", "primary_key", "primaryKey", "pk")
    )
    sort_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("sort_key", "sortKey", "sk")
    )
    gsi_indexes: Optional[list] = Field(
        default=None,
        validation_alias=AliasChoices("gsi_indexes", "gsiIndexes", "indexes", "gsi", "secondaryIndexes")
    )
    connection_secret_arn: Optional[str] = Field(default=None)
    region: Optional[str] = Field(default=None)


class ToolSpec(FlexibleBaseModel):
    """Individual tool definition within an Operation. Each tool = 1 Lambda + 1 API path."""

    tool_id: str = Field(
        description="Tool identifier (e.g., 'notify_customs_clearance_status')",
        validation_alias=AliasChoices("tool_id", "toolId", "id"),
    )
    summary: str = Field(default="")
    role: str = Field(
        default="primary",
        description="'primary' | 'helper' | 'session'",
        validation_alias=AliasChoices("role", "type"),
    )

    # API definition
    http_method: Optional[str] = Field(default="POST")
    path: Optional[str] = Field(default=None, description="API path (auto: /tools/{tool_id})")

    # Input/Output
    input_fields: list[FieldSpec] = Field(default=[])
    output_fields: list[FieldSpec] = Field(default=[])

    # Data source (primary may share operation's data_source; helper can have its own)
    data_source: Optional[DataSourceSpec] = Field(default=None)

    # Behavior context
    trigger_context: Optional[str] = Field(
        default=None,
        description="When to use this tool (e.g., '이메일 미수신 시')",
        validation_alias=AliasChoices("trigger_context", "triggerContext", "trigger", "when"),
    )

    # Validation / error
    validation_rules: Optional[list[str]] = Field(default=None)
    error_handling: Optional[str] = Field(
        default=None,
        description="'retry_once' | 'retry_3' | 'escalate' | 'ignore'",
    )

    # Generation control
    generate_lambda: bool = Field(default=True)
    generate_openapi: bool = Field(default=True)


class CustomerInfoVariable(FlexibleBaseModel):
    """Customer information variable injected via Contact Flow."""

    name: str
    source: str = Field(
        default="",
        description="'phone_lookup' | 'contact_flow_attribute' | 'manual_input'",
    )
    description: str = ""


class NoResponsePolicy(FlexibleBaseModel):
    """Policy for handling no-response situations."""

    max_retries: int = Field(default=2)
    retry_message: str = Field(default="죄송합니다. 잘 들리지 않습니다. 다시 한 번 말씀해 주세요.")
    final_message: str = Field(default="응답이 없어 나중에 다시 연락드리겠습니다.")
    final_action: str = Field(
        default="complete",
        description="'complete' | 'escalate'",
    )


class SessionFlowConfig(FlexibleBaseModel):
    """Session-level flow configuration shared across all operations."""

    call_direction: str = Field(default="inbound")
    agent_persona: Optional[str] = Field(default=None)
    common_greeting: Optional[str] = Field(default=None)
    common_closing: Optional[str] = Field(default=None)
    customer_info_variables: list[CustomerInfoVariable] = Field(default=[])
    no_response_policy: Optional[NoResponsePolicy] = Field(default=None)
    shared_exceptions: list[dict] = Field(default=[])
    session_tools: list[ToolSpec] = Field(
        default=[],
        description="Session-wide tools (e.g., log_call_result, get_outbound_targets)",
    )


class RdsConfig(FlexibleBaseModel):
    """RDS connection configuration (only when db_type is rds_*)."""

    cluster_arn: str = Field(
        description="Aurora cluster ARN",
        validation_alias=AliasChoices("cluster_arn", "clusterArn"),
    )
    secret_arn: str = Field(
        description="Secrets Manager ARN for DB credentials",
        validation_alias=AliasChoices("secret_arn", "secretArn"),
    )
    database_name: str = Field(
        description="Database name to connect to",
        validation_alias=AliasChoices("database_name", "databaseName", "db_name"),
    )
    engine: str = Field(
        default="postgresql",
        description="Database engine: 'mysql' or 'postgresql'",
    )
    tables: list[dict] = Field(
        default=[],
        description="Known tables and their schemas [{'name': 'reservations', 'columns': [...]}]",
    )


class DynamoDbConfig(FlexibleBaseModel):
    """DynamoDB configuration (only when db_type is dynamodb)."""

    tables: list[dict] = Field(
        default=[],
        description="Table definitions [{'name': '...', 'partition_key': '...', 'sort_key': '...', 'gsi': [...]}]",
    )
    billing_mode: str = Field(
        default="PAY_PER_REQUEST",
        description="'PAY_PER_REQUEST' or 'PROVISIONED'",
        validation_alias=AliasChoices("billing_mode", "billingMode"),
    )
    include_sample_data: bool = Field(
        default=True,
        description="Whether to seed sample data on deployment",
        validation_alias=AliasChoices("include_sample_data", "includeSampleData"),
    )


class LambdaConfig(FlexibleBaseModel):
    """Lambda function defaults."""

    runtime: str = Field(default="python3.11")
    memory_mb: int = Field(
        default=256,
        validation_alias=AliasChoices("memory_mb", "memoryMb", "memory"),
    )
    timeout_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices("timeout_seconds", "timeoutSeconds", "timeout"),
    )
    architectures: list[str] = Field(
        default=["arm64"],
        description=(
            "CloudFormation AWS::Lambda::Function 'Architectures' property. "
            "MUST be a YAML list with exactly ONE string: ['arm64'] or ['x86_64']. "
            "Do NOT use the singular form 'Architecture' and do NOT use a bare string."
        ),
        validation_alias=AliasChoices("architectures", "architecture", "Architectures", "Architecture"),
    )

    @field_validator("architectures", mode="before")
    @classmethod
    def _coerce_architectures(cls, v):
        # Accept legacy/loose inputs: "arm64" -> ["arm64"], "arm64,x86_64" -> split, dicts rejected
        if v is None:
            return ["arm64"]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()] or ["arm64"]
        if isinstance(v, (list, tuple)):
            items = [str(s).strip() for s in v if str(s).strip()]
            return items or ["arm64"]
        raise ValueError(
            "architectures must be a list of strings (e.g. ['arm64']); got "
            f"{type(v).__name__}"
        )
    layers: list[str] = Field(
        default=[],
        description="Lambda layer ARNs to attach to all functions",
    )
    environment_variables: dict = Field(
        default={},
        description="Extra environment variables shared across all Lambda functions",
        validation_alias=AliasChoices("environment_variables", "environmentVariables", "env_vars"),
    )


class ApiGatewayConfig(FlexibleBaseModel):
    """API Gateway configuration."""

    stage_name: str = Field(
        default="prod",
        validation_alias=AliasChoices("stage_name", "stageName"),
    )
    api_key_required: bool = Field(
        default=False,
        description="Whether API key is required on methods",
        validation_alias=AliasChoices("api_key_required", "apiKeyRequired"),
    )
    cors_origins: str = Field(
        default="*",
        description="CORS allowed origins ('*' for all, or comma-separated domains)",
        validation_alias=AliasChoices("cors_origins", "corsOrigins"),
    )
    cors_methods: str = Field(
        default="*",
        description="CORS allowed methods",
        validation_alias=AliasChoices("cors_methods", "corsMethods"),
    )
    base_path: str = Field(
        default="/tools",
        description="Base path prefix for all tool endpoints (e.g., /tools → /tools/{tool_id})",
        validation_alias=AliasChoices("base_path", "basePath"),
    )
    custom_domain: Optional[str] = Field(
        default=None,
        description="Custom domain name if applicable",
        validation_alias=AliasChoices("custom_domain", "customDomain"),
    )


class VpcConfig(FlexibleBaseModel):
    """VPC configuration (only when vpc_required is True)."""

    vpc_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("vpc_id", "vpcId"),
    )
    subnet_ids: list[str] = Field(
        default=[],
        validation_alias=AliasChoices("subnet_ids", "subnetIds"),
    )
    security_group_ids: list[str] = Field(
        default=[],
        validation_alias=AliasChoices("security_group_ids", "securityGroupIds"),
    )


class InfrastructureSpec(FlexibleBaseModel):
    """Project-level infrastructure specification — the single source of truth for all generators.

    This spec defines the infrastructure architecture that all generators must follow:
    - Infrastructure Generator: uses this to produce CloudFormation
    - Lambda Generator: reads db_type, rds_config/dynamodb_config for data access patterns
    - OpenAPI Generator: reads api_gateway config for path structure
    - Reviewer: validates generated assets against this spec
    """

    # Project identity
    project_name: str = Field(
        description="Project identifier for resource naming (e.g., 'sunny-hotel')",
        validation_alias=AliasChoices("project_name", "projectName"),
    )
    region: str = Field(
        default="ap-northeast-2",
        description="AWS region for all resources",
    )

    # Database
    db_type: str = Field(
        description="Database type: 'dynamodb', 'rds_mysql', 'rds_postgresql'",
        validation_alias=AliasChoices("db_type", "dbType", "database_type"),
    )
    rds_config: Optional[RdsConfig] = Field(
        default=None,
        description="RDS connection details (required when db_type starts with 'rds_')",
        validation_alias=AliasChoices("rds_config", "rdsConfig"),
    )
    dynamodb_config: Optional[DynamoDbConfig] = Field(
        default=None,
        description="DynamoDB settings (required when db_type is 'dynamodb')",
        validation_alias=AliasChoices("dynamodb_config", "dynamodbConfig"),
    )

    # Compute
    lambda_config: Optional[LambdaConfig] = Field(
        default=None,
        description="Lambda function defaults",
        validation_alias=AliasChoices("lambda_config", "lambdaConfig"),
    )

    # API
    api_gateway_config: Optional[ApiGatewayConfig] = Field(
        default=None,
        description="API Gateway configuration",
        validation_alias=AliasChoices("api_gateway_config", "apiGatewayConfig"),
    )

    # Networking
    vpc_required: bool = Field(
        default=False,
        description="Whether Lambda functions need VPC access",
        validation_alias=AliasChoices("vpc_required", "vpcRequired"),
    )
    vpc_config: Optional[VpcConfig] = Field(
        default=None,
        description="VPC details (required when vpc_required is True)",
        validation_alias=AliasChoices("vpc_config", "vpcConfig"),
    )

    # Additional services
    include_s3_bucket: bool = Field(
        default=True,
        description="Whether to include S3 bucket (for FAQ/Knowledge Base uploads)",
        validation_alias=AliasChoices("include_s3_bucket", "includeS3Bucket"),
    )
    include_customer_phone_lookup: bool = Field(
        default=False,
        description="Whether to include CustomerLookup + UpdateQSession Lambda resources",
        validation_alias=AliasChoices("include_customer_phone_lookup", "includeCustomerPhoneLookup"),
    )

    # Tags
    tags: dict = Field(
        default={},
        description="AWS resource tags to apply to all resources",
    )

    # Notes
    notes: Optional[str] = Field(
        default=None,
        description="Additional infrastructure notes or constraints",
    )


# Infrastructure spec is stored per-session (see session_context); access via
# _get_infra_spec() / _set_infra_spec() helpers defined below.


class OperationSpec(FlexibleBaseModel):
    """Complete specification for a single API operation."""

    # Basic info
    operation_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("operation_id", "operationId", "id")
    )
    operation_type: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("operation_type", "operationType", "type")
    )
    http_method: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("http_method", "httpMethod", "method")
    )
    path: Optional[str] = Field(default=None)
    summary: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)

    # Input/Output
    input_fields: list[FieldSpec] = Field(default=[], description="All input fields")
    output_fields: list[FieldSpec] = Field(default=[], description="All output fields")

    # Data source
    data_source: Optional[DataSourceSpec] = Field(default=None)

    # Business logic
    business_rules: list[BusinessRule] = Field(default=[])
    cross_field_validations: Optional[list[str]] = Field(default=None)

    # Responses
    success_status_code: int = Field(default=200)
    success_message_template: Optional[str] = Field(default=None)
    error_responses: list[ErrorResponse] = Field(default=[])

    # Side effects
    side_effects: list[SideEffect] = Field(default=[])

    # Security
    requires_authentication: bool = Field(default=True)
    allowed_roles: Optional[list[str]] = Field(default=None)
    rate_limit_per_minute: Optional[int] = Field(default=None)

    # Conversation scenario (B1)
    conversation_script: Optional[str] = Field(
        default=None,
        description="Conversation scenario verbatim or S3 key reference",
        validation_alias=AliasChoices("conversation_script", "conversationScript", "script"),
    )
    greeting_message: Optional[str] = Field(
        default=None,
        description="Exact greeting message from customer requirements",
        validation_alias=AliasChoices("greeting_message", "greetingMessage", "greeting"),
    )
    closing_message: Optional[str] = Field(
        default=None,
        description="Exact closing message from customer requirements",
        validation_alias=AliasChoices("closing_message", "closingMessage", "closing"),
    )
    exception_scenarios: Optional[List[dict]] = Field(
        default=None,
        description="Exception/fallback scenarios",
        validation_alias=AliasChoices("exception_scenarios", "exceptionScenarios", "exceptions"),
    )
    call_direction: Optional[str] = Field(
        default=None,
        description="'inbound' or 'outbound'",
        validation_alias=AliasChoices("call_direction", "callDirection"),
    )
    scenario_step_count: Optional[int] = Field(
        default=None,
        description="Number of scenario steps (for verification)",
        validation_alias=AliasChoices("scenario_step_count", "scenarioStepCount", "stepCount"),
    )

    # === Multi-tool architecture (1 Operation = N Tools) ===
    tools: list[ToolSpec] = Field(
        default=[],
        description="Tools belonging to this operation (primary + helper)",
        validation_alias=AliasChoices("tools", "toolSpecs"),
    )
    conversation_steps: list[ConversationStep] = Field(
        default=[],
        description="Structured conversation steps (structured version of conversation_script)",
        validation_alias=AliasChoices("conversation_steps", "conversationSteps", "steps"),
    )
    flow_type: Optional[str] = Field(
        default=None,
        description="'scripted' | 'intent_driven' | 'hybrid'",
        validation_alias=AliasChoices("flow_type", "flowType"),
    )

    # Generation flags
    generate_lambda: bool = Field(default=True)
    generate_openapi: bool = Field(default=True)
    language: str = Field(default="python")


# Operation specs and session flow config are stored per-session (see
# session_context). Access via the helpers defined below.


def _specs_bucket() -> "dict[str, OperationSpec]":
    """Return the current session's op_id → OperationSpec dict.

    Falls back to a shared ``"__anon__"`` bucket when no session is bound
    (unit tests, one-off invocations). Production code paths always have
    ``current_session_id`` set by the WebSocket dispatch loop.
    """
    return operation_specs_bucket(current_session_id.get())


def _get_flow_cfg() -> Optional["SessionFlowConfig"]:
    return get_session_flow_config_for(current_session_id.get())


def _set_flow_cfg(cfg: Optional["SessionFlowConfig"]) -> None:
    sid = current_session_id.get()
    if sid is not None:
        set_session_flow_config_for(sid, cfg)


def _get_infra_spec() -> Optional["InfrastructureSpec"]:
    return get_infrastructure_spec_for(current_session_id.get())


def _set_infra_spec(spec: Optional["InfrastructureSpec"]) -> None:
    sid = current_session_id.get()
    if sid is not None:
        set_infrastructure_spec_for(sid, spec)

# ── NFS Direct Helpers ─────────────────────────────────────────────────
_S3FILES_MOUNT = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")


def _nfs_specs_dir(session_id: str) -> Optional[Path]:
    """Return NFS specs directory path, or None if NFS is unavailable."""
    if not os.path.isdir(_S3FILES_MOUNT):
        return None
    safe_sid = session_id.replace("..", "_").replace("/", "_").replace("\\", "_")
    return Path(_S3FILES_MOUNT) / "sessions" / safe_sid / "assets" / "specs"


def _nfs_state_dir(session_id: str) -> Optional[Path]:
    """Return NFS state directory path (for flow_config etc.), or None if NFS is unavailable."""
    if not os.path.isdir(_S3FILES_MOUNT):
        return None
    safe_sid = session_id.replace("..", "_").replace("/", "_").replace("\\", "_")
    return Path(_S3FILES_MOUNT) / "sessions" / safe_sid / "state"


def _nfs_persist_spec(session_id: str, op_id: str, spec_dict: dict) -> bool:
    """Persist a spec directly to NFS (fast-path, in addition to ProjectWorkspace)."""
    specs_dir = _nfs_specs_dir(session_id)
    if specs_dir is None:
        return False
    try:
        specs_dir.mkdir(parents=True, exist_ok=True)
        target = specs_dir / f"{op_id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(_json.dumps(spec_dict, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.rename(target)
        return True
    except Exception as e:
        logger.warning(f"[SpecManager] NFS persist failed for {op_id}: {e}")
        return False


def _nfs_restore_all_specs(session_id: str) -> dict[str, dict]:
    """Restore all specs directly from NFS."""
    specs_dir = _nfs_specs_dir(session_id)
    if specs_dir is None or not specs_dir.is_dir():
        return {}
    result = {}
    try:
        for entry in specs_dir.iterdir():
            if entry.is_file() and entry.suffix == ".json":
                op_id = entry.stem
                try:
                    data = _json.loads(entry.read_text(encoding="utf-8"))
                    result[op_id] = data
                except (_json.JSONDecodeError, OSError) as e:
                    logger.warning(f"[SpecManager] NFS spec read failed {entry.name}: {e}")
    except Exception as e:
        logger.warning(f"[SpecManager] NFS specs dir scan failed: {e}")
    return result


def _nfs_list_spec_ids(session_id: str) -> list[str]:
    """List spec IDs from NFS directory without loading content."""
    specs_dir = _nfs_specs_dir(session_id)
    if specs_dir is None or not specs_dir.is_dir():
        return []
    try:
        return [e.stem for e in specs_dir.iterdir() if e.is_file() and e.suffix == ".json"]
    except Exception:
        return []


def _get_current_session_id() -> Optional[str]:
    """Return the session_id bound to the current async context."""
    return current_session_id.get()


def _format_spec_as_markdown(op_id: str, spec: OperationSpec) -> str:
    """Format a single OperationSpec as a markdown summary for frontend preview."""
    lines = [
        f"## {op_id}",
        f"**{spec.http_method or '?'} {spec.path or '?'}** — {spec.summary or ''}",
        "",
        "### Input Fields",
    ]
    for f in spec.input_fields:
        req = "required" if getattr(f, "required", True) else "optional"
        lines.append(f"- `{f.name}` ({getattr(f, 'field_type', None) or 'string'}, {req})")

    lines += ["", "### Output Fields"]
    for f in spec.output_fields:
        lines.append(f"- `{f.name}` ({getattr(f, 'field_type', None) or 'string'})")

    # Data source
    ds = spec.data_source
    if ds:
        table = getattr(ds, 'table_name', None) or '?'
        pk = getattr(ds, 'partition_key', None) or '?'
        lines += ["", "### Data Source", f"- Table: `{table}` (PK: `{pk}`)"]
        gsi = getattr(ds, 'gsi_indexes', None) or []
        if gsi:
            gsi_names = [g.get('name', '?') if isinstance(g, dict) else str(g) for g in gsi]
            lines.append(f"- GSI: {', '.join(gsi_names)}")

    # Business rules
    if spec.business_rules:
        lines += ["", "### Business Rules"]
        for r in spec.business_rules:
            desc = getattr(r, 'description', None) or getattr(r, 'rule', None) or str(r)
            lines.append(f"- {desc}")

    # Tools
    if spec.tools:
        lines += ["", "### Tools"]
        for t in spec.tools:
            role_tag = f" [{t.role}]" if t.role != "primary" else ""
            trigger = f" — {t.trigger_context}" if t.trigger_context else ""
            lines.append(f"- `{t.tool_id}`{role_tag}: {t.summary or ''}{trigger}")
            if t.input_fields:
                in_names = ", ".join(f.name for f in t.input_fields)
                lines.append(f"  - In: {in_names}")
            if t.output_fields:
                out_names = ", ".join(f.name for f in t.output_fields)
                lines.append(f"  - Out: {out_names}")

    # Conversation steps
    if spec.conversation_steps:
        lines += ["", "### Conversation Steps"]
        for s in spec.conversation_steps:
            branch_info = ""
            if s.branches:
                branch_info = " → " + ", ".join(
                    f"{b.get('condition', '?')}→{b.get('next_step', '?')}" for b in s.branches
                )
            tool_info = f" [tool: {s.tool_call}]" if s.tool_call else ""
            lines.append(f"- {s.step_id}. {s.label}{tool_info}{branch_info}")

    # Scenario
    if spec.conversation_script:
        direction = spec.call_direction or 'not specified'
        steps = spec.scenario_step_count or '?'
        flow = spec.flow_type or 'not specified'
        lines += ["", "### Scenario", f"- Direction: {direction}", f"- Steps: {steps}", f"- Flow type: {flow}"]

    return "\n".join(lines)


def _safe_parse_model(model_class, data: dict):
    """
    Safely parse data into a Pydantic model.
    If validation fails, store the raw dict with extra="allow".
    """
    try:
        return model_class(**data)
    except Exception:
        # If model parsing fails, create instance with just the raw data
        # The extra="allow" config will accept all fields
        return model_class.model_construct(**data)


@tool
def save_operation_spec(
    operation_id: str,
    operation_type: str,
    http_method: str,
    path: str,
    summary: str,
    description: str,
    input_fields: list[dict],
    output_fields: list[dict],
    data_source: dict,
    business_rules: list[dict] = None,
    cross_field_validations: list[str] = None,
    success_status_code: int = 200,
    success_message_template: str = None,
    error_responses: list[dict] = None,
    side_effects: list[dict] = None,
    requires_authentication: bool = True,
    allowed_roles: list[str] = None,
    rate_limit_per_minute: int = None,
    language: str = "python",
    conversation_script: str = None,
    greeting_message: str = None,
    closing_message: str = None,
    exception_scenarios: list[dict] = None,
    call_direction: str = None,
    scenario_step_count: int = None,
    tools: list[dict] = None,
    conversation_steps: list[dict] = None,
    flow_type: str = None,
) -> dict:
    """
    Save a complete operation specification after gathering all details from the user.

    This tool should be called ONLY after you have gathered ALL required information
    about an operation through conversation with the user.

    Args:
        operation_id: Unique identifier like 'createReservation', 'getOrderStatus'
        operation_type: One of 'create', 'read', 'update', 'delete', 'list', 'search', 'custom'
        http_method: 'POST', 'GET', 'PUT', 'DELETE', or 'PATCH'
        path: API path like '/reservations' or '/orders/{orderId}'
        summary: Short one-line description
        description: Detailed description of what this operation does
        input_fields: List of field specifications with all validation rules
        output_fields: List of fields returned in success response
        data_source: Database connection details (type, table, keys)
        business_rules: List of business rules to enforce
        cross_field_validations: Validations involving multiple fields
        success_status_code: HTTP status code on success (200, 201, etc.)
        success_message_template: Template for success message
        error_responses: All possible error responses
        side_effects: Email, SMS, notifications, etc.
        requires_authentication: Whether auth is required
        allowed_roles: Roles allowed to access
        rate_limit_per_minute: Rate limiting
        language: 'python' or 'nodejs' for Lambda runtime
        conversation_script: Verbatim conversation scenario or S3 key
        greeting_message: Exact greeting phrase from customer
        closing_message: Exact closing phrase from customer
        exception_scenarios: Exception/fallback scenario list
        call_direction: 'inbound' or 'outbound'
        scenario_step_count: Number of scenario steps (for verification)
        tools: List of ToolSpec dicts for this operation. Each tool becomes 1 Lambda + 1 API path.
            role='primary' for main query/processing, role='helper' for auxiliary features.
        conversation_steps: Structured conversation steps (for scripted flows).
            Each step has step_id, label, message (verbatim), branches, tool_call.
        flow_type: 'scripted' | 'intent_driven' | 'hybrid'

    Returns:
        Confirmation with the saved specification summary
    """
    try:
        # Parse all nested models with flexible handling
        parsed_input_fields = [_safe_parse_model(FieldSpec, f) for f in input_fields]
        parsed_output_fields = [_safe_parse_model(FieldSpec, f) for f in output_fields]
        parsed_data_source = _safe_parse_model(DataSourceSpec, data_source)
        parsed_business_rules = [_safe_parse_model(BusinessRule, r) for r in (business_rules or [])]
        parsed_error_responses = [_safe_parse_model(ErrorResponse, e) for e in (error_responses or [])]
        parsed_side_effects = [_safe_parse_model(SideEffect, s) for s in (side_effects or [])]
        parsed_tools = [_safe_parse_model(ToolSpec, t) for t in (tools or [])]
        parsed_conversation_steps = [_safe_parse_model(ConversationStep, s) for s in (conversation_steps or [])]

        spec = OperationSpec(
            operation_id=operation_id,
            operation_type=operation_type,
            http_method=http_method,
            path=path,
            summary=summary,
            description=description,
            input_fields=parsed_input_fields,
            output_fields=parsed_output_fields,
            data_source=parsed_data_source,
            business_rules=parsed_business_rules,
            cross_field_validations=cross_field_validations,
            success_status_code=success_status_code,
            success_message_template=success_message_template,
            error_responses=parsed_error_responses,
            side_effects=parsed_side_effects,
            requires_authentication=requires_authentication,
            allowed_roles=allowed_roles,
            rate_limit_per_minute=rate_limit_per_minute,
            language=language,
            conversation_script=conversation_script,
            greeting_message=greeting_message,
            closing_message=closing_message,
            exception_scenarios=exception_scenarios,
            call_direction=call_direction,
            scenario_step_count=scenario_step_count,
            tools=parsed_tools,
            conversation_steps=parsed_conversation_steps,
            flow_type=flow_type,
        )

        _specs_bucket()[operation_id] = spec

        # Persist to NFS (fast-path) + S3 via ProjectWorkspace (A2)
        sid = _get_current_session_id()
        if sid:
            _nfs_persist_spec(sid, operation_id, spec.model_dump())
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                ws.save_spec(operation_id, spec.model_dump())
        except Exception as e:
            logger.warning(f"[SpecManager] S3 persist failed for {operation_id}: {e}")

        # Get db_type safely
        db_type = "unknown"
        table_name = "unknown"
        if parsed_data_source:
            db_type = getattr(parsed_data_source, 'db_type', None) or "unknown"
            table_name = getattr(parsed_data_source, 'table_name', None) or "unknown"

        # Stream to frontend preview panel
        try:
            from tools.streaming_callback import stream_asset
            md = _format_spec_as_markdown(operation_id, spec)
            stream_asset("operation_spec", f"{operation_id}.md", md, operation_id=operation_id, is_complete=True)
        except Exception:
            pass  # Preview failure must not block save

        # Backward phase transition: save_operation_spec during generation/review/post_generation
        # means the user is redefining requirements → transition back to interview
        try:
            from context.generation_progress import read_phase, update_phase
            sid = _get_current_session_id()
            if sid:
                current = read_phase(sid)
                if current in ("generation", "review", "post_generation"):
                    update_phase(sid, "interview", f"backward:save_operation_spec_during_{current}")
        except Exception:
            pass  # Phase tracking must never block core logic

        return {
            "success": True,
            "operation_id": operation_id,
            "message": f"Operation '{operation_id}' specification saved successfully.",
            "summary": {
                "input_field_count": len(spec.input_fields),
                "output_field_count": len(spec.output_fields),
                "business_rule_count": len(spec.business_rules),
                "error_response_count": len(spec.error_responses),
                "side_effect_count": len(spec.side_effects),
                "data_source": db_type,
                "table": table_name,
                "tool_count": len(spec.tools),
                "conversation_step_count": len(spec.conversation_steps),
                "flow_type": spec.flow_type,
            }
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Failed to save operation specification: {str(e)}"
        }


@tool
def get_operation_spec(operation_id: str) -> dict:
    """
    Retrieve a saved operation specification.

    Args:
        operation_id: The operation ID to retrieve

    Returns:
        The complete operation specification or error if not found
    """
    if operation_id not in _specs_bucket():
        # NFS fast-path fallback
        sid = _get_current_session_id()
        if sid:
            nfs_dir = _nfs_specs_dir(sid)
            if nfs_dir and nfs_dir.is_dir():
                spec_file = nfs_dir / f"{operation_id}.json"
                if spec_file.is_file():
                    try:
                        spec_dict = _json.loads(spec_file.read_text(encoding="utf-8"))
                        spec = _safe_parse_model(OperationSpec, spec_dict)
                        _specs_bucket()[operation_id] = spec
                        logger.info(f"[SpecManager] Restored {operation_id} from NFS")
                        return {"success": True, "specification": spec.model_dump()}
                    except Exception as e:
                        logger.warning(f"[SpecManager] NFS fallback failed for {operation_id}: {e}")

        # S3 fallback (A2)
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                spec_dict = ws.load_spec(operation_id)
                if spec_dict:
                    spec = _safe_parse_model(OperationSpec, spec_dict)
                    _specs_bucket()[operation_id] = spec
                    logger.info(f"[SpecManager] Restored {operation_id} from S3")
                    return {"success": True, "specification": spec.model_dump()}
        except Exception as e:
            logger.warning(f"[SpecManager] S3 fallback failed for {operation_id}: {e}")

        return {
            "success": False,
            "error": f"Operation '{operation_id}' not found",
            "available_operations": list(_specs_bucket().keys())
        }

    spec = _specs_bucket()[operation_id]
    return {
        "success": True,
        "specification": spec.model_dump()
    }


@tool
def list_operations() -> dict:
    """
    List all saved operation specifications.

    Returns:
        List of all operation IDs with their summaries
    """
    operations = []
    for op_id, spec in _specs_bucket().items():
        operations.append({
            "operation_id": op_id,
            "operation_type": spec.operation_type,
            "http_method": spec.http_method,
            "path": spec.path,
            "summary": spec.summary,
            "input_field_count": len(spec.input_fields),
            "has_side_effects": len(spec.side_effects) > 0
        })

    return {
        "success": True,
        "operation_count": len(operations),
        "operations": operations
    }


def get_all_specs() -> dict[str, OperationSpec]:
    """Get all operation specs (for internal use by other tools).

    Falls back to S3 if in-memory cache is empty.
    """
    if not _specs_bucket():
        # NFS fast-path restore
        sid = _get_current_session_id()
        if sid:
            nfs_dicts = _nfs_restore_all_specs(sid)
            for op_id, spec_dict in nfs_dicts.items():
                if op_id not in _specs_bucket():
                    _specs_bucket()[op_id] = _safe_parse_model(OperationSpec, spec_dict)
            if nfs_dicts:
                logger.info(f"[SpecManager] Restored {len(nfs_dicts)} specs from NFS")

        # S3 fallback if still empty
        if not _specs_bucket():
            try:
                from tools.project_workspace import get_workspace
                ws = get_workspace()
                if ws:
                    all_dicts = ws.load_all_specs()
                    for op_id, spec_dict in all_dicts.items():
                        if op_id not in _specs_bucket():
                            _specs_bucket()[op_id] = _safe_parse_model(OperationSpec, spec_dict)
                    if all_dicts:
                        logger.info(f"[SpecManager] Restored {len(all_dicts)} specs from S3")
            except Exception as e:
                logger.warning(f"[SpecManager] S3 bulk restore failed: {e}")
    return _specs_bucket().copy()


@tool
def get_all_operation_ids() -> dict:
    """
    Return all saved operation IDs with their basic info.

    Call this before Phase 2 (Lambda generation) to get the exact list of operations
    that need Lambda functions generated. This prevents missing or extra operations.

    Returns:
        {"operation_ids": ["op1", "op2", ...], "count": N, "details": {op_id: summary}}
    """
    specs = _specs_bucket().copy()
    details = {}
    for op_id, spec in specs.items():
        details[op_id] = {
            "summary": spec.summary or "",
            "input_count": len(spec.input_fields),
            "output_count": len(spec.output_fields),
        }
    return {
        "operation_ids": list(specs.keys()),
        "count": len(specs),
        "details": details,
    }


@tool
def get_all_tool_ids() -> dict:
    """
    Return all tool_ids that need Lambda functions, grouped by source.

    Call this before Phase 2 (Lambda generation) to get the exact list of tool_ids.
    With the multi-tool architecture, each tool_id = 1 Lambda function.
    The orchestrator should call lambda_generator_agent(operation_id=tool_id) for EACH tool_id.

    Returns:
        {
            "tool_ids": ["get_shipment_status", "resend_email", "log_call_result", ...],
            "count": N,
            "by_operation": {"op_id": [{"tool_id": "...", "role": "primary|helper", "summary": "..."}]},
            "session_tools": [{"tool_id": "...", "role": "session", "summary": "..."}]
        }
    """
    all_specs = _specs_bucket().copy()
    all_tool_ids = []
    by_operation = {}

    for op_id, spec in all_specs.items():
        op_tools = []
        if spec.tools:
            for t in spec.tools:
                if t.generate_lambda:
                    all_tool_ids.append(t.tool_id)
                    op_tools.append({
                        "tool_id": t.tool_id,
                        "role": t.role,
                        "summary": t.summary or "",
                        "parent_operation_id": op_id,
                    })
        else:
            # Backward compatibility: operation itself is a single tool
            all_tool_ids.append(op_id)
            op_tools.append({
                "tool_id": op_id,
                "role": "primary",
                "summary": spec.summary or "",
                "parent_operation_id": op_id,
            })
        if op_tools:
            by_operation[op_id] = op_tools

    # Session tools
    session_tool_list = []
    _flow_cfg = _get_flow_cfg()
    if _flow_cfg and _flow_cfg.session_tools:
        for t in _flow_cfg.session_tools:
            if t.generate_lambda:
                all_tool_ids.append(t.tool_id)
                session_tool_list.append({
                    "tool_id": t.tool_id,
                    "role": "session",
                    "summary": t.summary or "",
                })

    return {
        "tool_ids": all_tool_ids,
        "count": len(all_tool_ids),
        "by_operation": by_operation,
        "session_tools": session_tool_list,
    }


def get_tool_with_parent_spec(tool_id: str) -> tuple:
    """Find a ToolSpec and its parent OperationSpec by tool_id.

    Returns:
        (tool_spec: ToolSpec or None, parent_spec: OperationSpec or None)
    """
    all_specs = _specs_bucket().copy()

    for op_id, spec in all_specs.items():
        if spec.tools:
            for t in spec.tools:
                if t.tool_id == tool_id:
                    return t, spec
        elif op_id == tool_id:
            # Backward compatibility: operation itself is the tool
            return None, spec

    # Check session tools
    _flow_cfg = _get_flow_cfg()
    if _flow_cfg and _flow_cfg.session_tools:
        for t in _flow_cfg.session_tools:
            if t.tool_id == tool_id:
                return t, None

    return None, None


def restore_specs_from_workspace():
    """Explicitly restore all specs from NFS/S3 workspace into in-memory cache.

    Called during session restore to warm the cache.
    NFS is attempted first for speed, then S3 fills any gaps.
    """
    restored = 0

    # NFS fast-path
    sid = _get_current_session_id()
    if sid:
        nfs_dicts = _nfs_restore_all_specs(sid)
        for op_id, spec_dict in nfs_dicts.items():
            if op_id not in _specs_bucket():
                _specs_bucket()[op_id] = _safe_parse_model(OperationSpec, spec_dict)
                restored += 1
        if nfs_dicts:
            logger.info(f"[SpecManager] Restored {restored} specs from NFS")

    # S3 fallback for any missing specs
    try:
        from tools.project_workspace import get_workspace
        ws = get_workspace()
        if ws:
            all_dicts = ws.load_all_specs()
            s3_restored = 0
            for op_id, spec_dict in all_dicts.items():
                if op_id not in _specs_bucket():
                    _specs_bucket()[op_id] = _safe_parse_model(OperationSpec, spec_dict)
                    s3_restored += 1
                    restored += 1
            if s3_restored:
                logger.info(f"[SpecManager] Restored {s3_restored} additional specs from S3")
    except Exception as e:
        logger.warning(f"[SpecManager] Workspace restore failed: {e}")

    logger.info(f"[SpecManager] Total restored: {restored} (total in cache: {len(_specs_bucket())})")
    return restored


@tool
def update_operation_spec(
    operation_id: str,
    updates: dict,
) -> dict:
    """
    Update specific fields of an existing operation specification.
    Only fields included in `updates` are modified; others remain unchanged.

    Use this when you need to fix or adjust a single field (e.g., input_fields,
    business_rules, conversation_script) without re-saving the entire spec.

    Args:
        operation_id: The operation to update
        updates: Dict of field_name → new_value. Only provided fields are updated.
                 Supported fields: any OperationSpec field (input_fields, output_fields,
                 business_rules, data_source, conversation_script, greeting_message,
                 closing_message, exception_scenarios, call_direction, etc.)

    Returns:
        Confirmation with updated field list, or error if operation not found.
    """
    # Load existing spec (memory first, then S3 fallback)
    if operation_id not in _specs_bucket():
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                spec_dict = ws.load_spec(operation_id)
                if spec_dict:
                    _specs_bucket()[operation_id] = _safe_parse_model(OperationSpec, spec_dict)
                    logger.info(f"[SpecManager] Restored {operation_id} from S3 for update")
        except Exception as e:
            logger.warning(f"[SpecManager] S3 fallback failed for {operation_id}: {e}")

    if operation_id not in _specs_bucket():
        return {
            "success": False,
            "error": f"Operation '{operation_id}' not found. Save it first with save_operation_spec.",
            "available_operations": list(_specs_bucket().keys()),
        }

    try:
        spec = _specs_bucket()[operation_id]
        current_data = spec.model_dump()

        # Parse nested models for known list/dict fields
        updated_fields = []
        for field_name, new_value in updates.items():
            if field_name in ("input_fields", "output_fields") and isinstance(new_value, list):
                new_value = [_safe_parse_model(FieldSpec, f) if isinstance(f, dict) else f for f in new_value]
            elif field_name == "business_rules" and isinstance(new_value, list):
                new_value = [_safe_parse_model(BusinessRule, r) if isinstance(r, dict) else r for r in new_value]
            elif field_name == "error_responses" and isinstance(new_value, list):
                new_value = [_safe_parse_model(ErrorResponse, e) if isinstance(e, dict) else e for e in new_value]
            elif field_name == "side_effects" and isinstance(new_value, list):
                new_value = [_safe_parse_model(SideEffect, s) if isinstance(s, dict) else s for s in new_value]
            elif field_name == "data_source" and isinstance(new_value, dict):
                new_value = _safe_parse_model(DataSourceSpec, new_value)
            elif field_name == "tools" and isinstance(new_value, list):
                new_value = [_safe_parse_model(ToolSpec, t) if isinstance(t, dict) else t for t in new_value]
            elif field_name == "conversation_steps" and isinstance(new_value, list):
                new_value = [_safe_parse_model(ConversationStep, s) if isinstance(s, dict) else s for s in new_value]

            current_data[field_name] = new_value if not isinstance(new_value, list) else new_value
            updated_fields.append(field_name)

        # Re-create spec from merged data (serialize nested models first)
        serialized = {}
        for k, v in current_data.items():
            if hasattr(v, "model_dump"):
                serialized[k] = v.model_dump()
            elif isinstance(v, list) and v and hasattr(v[0], "model_dump"):
                serialized[k] = [item.model_dump() if hasattr(item, "model_dump") else item for item in v]
            else:
                serialized[k] = v

        updated_spec = _safe_parse_model(OperationSpec, serialized)
        _specs_bucket()[operation_id] = updated_spec

        # Persist to NFS (fast-path) + S3
        sid = _get_current_session_id()
        if sid:
            _nfs_persist_spec(sid, operation_id, updated_spec.model_dump())
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                ws.save_spec(operation_id, updated_spec.model_dump())
        except Exception as e:
            logger.warning(f"[SpecManager] S3 persist failed for {operation_id}: {e}")

        # Stream updated spec to frontend preview panel
        try:
            from tools.streaming_callback import stream_asset
            md = _format_spec_as_markdown(operation_id, updated_spec)
            stream_asset("operation_spec", f"{operation_id}.md", md, operation_id=operation_id, is_complete=True)
        except Exception:
            pass  # Preview failure must not block update

        return {
            "success": True,
            "operation_id": operation_id,
            "updated_fields": updated_fields,
            "message": f"Operation '{operation_id}' updated: {', '.join(updated_fields)}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Failed to update operation specification: {str(e)}",
        }


@tool
def format_operation_summary() -> dict:
    """
    Format all saved operations as a structured summary for user confirmation.
    Call this BEFORE starting generation (Phase B) to show the user a clear
    overview of all defined operations. The user can then request changes
    via update_operation_spec before generation begins.

    Returns:
        A human-readable table-style summary of each operation with
        key details (fields, rules, data source, scenario status).
    """
    # Ensure specs are loaded from S3 if memory is empty
    if not _specs_bucket():
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                all_dicts = ws.load_all_specs()
                for op_id, spec_dict in all_dicts.items():
                    if op_id not in _specs_bucket():
                        _specs_bucket()[op_id] = _safe_parse_model(OperationSpec, spec_dict)
        except Exception:
            pass

    if not _specs_bucket():
        return {
            "success": False,
            "operation_count": 0,
            "message": "No operations defined yet. Complete the interview first.",
        }

    summaries = []
    for idx, (op_id, spec) in enumerate(_specs_bucket().items(), 1):
        # Input fields summary
        input_summary = []
        for f in spec.input_fields:
            req = "(required)" if getattr(f, "required", True) else "(optional)"
            input_summary.append(f"{f.name}{req}")

        # Output fields summary
        output_names = [f.name for f in spec.output_fields]

        # Data source summary
        ds = spec.data_source
        ds_info = "Not specified"
        if ds:
            table = getattr(ds, "table_name", None) or "?"
            pk = getattr(ds, "partition_key", None) or "?"
            gsi_list = getattr(ds, "gsi_indexes", None) or []
            gsi_names = []
            for g in gsi_list:
                if isinstance(g, dict):
                    gsi_names.append(g.get("name", "?"))
                elif hasattr(g, "name"):
                    gsi_names.append(getattr(g, "name", "?"))
                else:
                    gsi_names.append(str(g))
            gsi_str = ", ".join(gsi_names) if gsi_names else "none"
            ds_info = f"{table} (PK: {pk}, GSI: {gsi_str})"

        # Scenario status
        script = spec.conversation_script
        step_count = spec.scenario_step_count
        direction = spec.call_direction or "not specified"
        flow = spec.flow_type or "not specified"
        if script:
            scenario_status = f"saved ({step_count or '?'} steps, {direction})"
        else:
            scenario_status = "not provided"

        # Tools summary
        tools_summary = "none (legacy single-tool)"
        if spec.tools:
            tool_parts = []
            for t in spec.tools:
                role_tag = f"[{t.role}]" if t.role != "primary" else ""
                tool_parts.append(f"{t.tool_id}{role_tag}")
            tools_summary = ", ".join(tool_parts)

        # Conversation steps summary
        steps_summary = ""
        if spec.conversation_steps:
            steps_summary = f"{len(spec.conversation_steps)} steps"

        summaries.append({
            "index": idx,
            "operation_id": op_id,
            "type": f"{spec.http_method or '?'} {spec.path or '?'}",
            "summary": spec.summary or spec.description or "",
            "input_fields": ", ".join(input_summary) if input_summary else "none",
            "output_fields": ", ".join(output_names) if output_names else "none",
            "data_source": ds_info,
            "business_rules_count": len(spec.business_rules),
            "error_responses_count": len(spec.error_responses),
            "side_effects_count": len(spec.side_effects),
            "scenario_status": scenario_status,
            "call_direction": direction,
            "flow_type": flow,
            "tools": tools_summary,
            "conversation_steps": steps_summary,
        })

    # Build text summary (markdown format for frontend preview)
    lines = [f"# Defined Operations Summary ({len(summaries)})", ""]
    for s in summaries:
        lines.append(f"## {s['index']}. {s['operation_id']}")
        lines.append(f"**{s['type']}** — {s['summary']}")
        lines.append("")
        lines.append(f"| Field | Value |")
        lines.append(f"|-------|-------|")
        lines.append(f"| Input | {s['input_fields']} |")
        lines.append(f"| Output | {s['output_fields']} |")
        lines.append(f"| DB | {s['data_source']} |")
        lines.append(f"| Business Rules | {s['business_rules_count']} |")
        lines.append(f"| Scenario | {s['scenario_status']} |")
        lines.append(f"| Tools | {s['tools']} |")
        if s['conversation_steps']:
            lines.append(f"| Conv Steps | {s['conversation_steps']} |")
        if s['flow_type'] != 'not specified':
            lines.append(f"| Flow Type | {s['flow_type']} |")
        lines.append("")

    text_summary = "\n".join(lines)

    # Stream summary to frontend preview panel
    try:
        from tools.streaming_callback import stream_asset
        stream_asset("operation_spec", "summary.md", text_summary, is_complete=True)
    except Exception:
        pass  # Preview failure must not block summary

    return {
        "success": True,
        "operation_count": len(summaries),
        "text_summary": text_summary,
        "operations": summaries,
    }


def get_all_tools() -> list[ToolSpec]:
    """Get all tools from all operations + session_tools, merged.

    For operations with empty tools[], converts the operation itself into
    a single primary ToolSpec for backward compatibility.
    """
    all_specs = get_all_specs()
    result: list[ToolSpec] = []

    for op_id, spec in all_specs.items():
        if spec.tools:
            result.extend(spec.tools)
        else:
            # Backward compatibility: treat entire operation as a single primary tool
            result.append(ToolSpec(
                tool_id=op_id,
                summary=spec.summary or "",
                role="primary",
                http_method=spec.http_method or "POST",
                path=spec.path,
                input_fields=spec.input_fields,
                output_fields=spec.output_fields,
                data_source=spec.data_source,
            ))

    # Add session tools
    _flow_cfg = _get_flow_cfg()
    if _flow_cfg and _flow_cfg.session_tools:
        result.extend(_flow_cfg.session_tools)

    return result


def get_session_flow_config() -> Optional[SessionFlowConfig]:
    """Get session flow config (for internal use by generators)."""
    cfg = _get_flow_cfg()
    if cfg is None:
        # NFS fast-path fallback
        sid = _get_current_session_id()
        if sid:
            state_dir = _nfs_state_dir(sid)
            if state_dir is not None:
                flow_file = state_dir / "flow_config.json"
                if flow_file.is_file():
                    try:
                        data = _json.loads(flow_file.read_text(encoding="utf-8"))
                        cfg = _safe_parse_model(SessionFlowConfig, data)
                        _set_flow_cfg(cfg)
                        logger.info("[SpecManager] Restored session flow config from NFS")
                    except Exception as e:
                        logger.warning(f"[SpecManager] NFS flow config restore failed: {e}")

        # S3 fallback
        if cfg is None:
            try:
                from tools.project_workspace import get_workspace
                ws = get_workspace()
                if ws:
                    data = ws.load_flow_config()
                    if data:
                        cfg = _safe_parse_model(SessionFlowConfig, data)
                        _set_flow_cfg(cfg)
                        logger.info("[SpecManager] Restored session flow config from S3")
            except Exception as e:
                logger.warning(f"[SpecManager] S3 flow config restore failed: {e}")
    return cfg


@tool
def save_session_flow_config(
    call_direction: str = "inbound",
    agent_persona: str = None,
    common_greeting: str = None,
    common_closing: str = None,
    customer_info_variables: list[dict] = None,
    no_response_policy: dict = None,
    shared_exceptions: list[dict] = None,
    session_tools: list[dict] = None,
) -> dict:
    """
    Save session-level flow configuration. Covers common settings shared across all operations:
    greeting/closing phrases, no-response policy, customer info variables, session-wide tools.

    Call this AFTER all operation specs are saved and BEFORE format_operation_summary.

    Args:
        call_direction: 'inbound' or 'outbound'
        agent_persona: AI agent persona description
        common_greeting: Shared greeting message
        common_closing: Shared closing message
        customer_info_variables: List of customer info variables injected from Contact Flow.
            Each: {"name": "customerId", "source": "phone_lookup", "description": "고객 ID"}
        no_response_policy: No-response handling policy.
            {"max_retries": 2, "retry_message": "...", "final_message": "...", "final_action": "complete|escalate"}
        shared_exceptions: Exception scenarios shared across all operations.
            [{"condition": "고객이 화냄", "action": "escalate", "message": "..."}]
        session_tools: Session-wide tools (e.g., log_call_result, get_outbound_targets).
            Each: {"tool_id": "...", "role": "session", "summary": "...", ...}

    Returns:
        Confirmation with saved configuration summary
    """
    try:
        parsed_customer_vars = [
            _safe_parse_model(CustomerInfoVariable, v) for v in (customer_info_variables or [])
        ]
        parsed_no_response = (
            _safe_parse_model(NoResponsePolicy, no_response_policy)
            if no_response_policy else None
        )
        parsed_session_tools = [
            _safe_parse_model(ToolSpec, t) for t in (session_tools or [])
        ]

        config = SessionFlowConfig(
            call_direction=call_direction,
            agent_persona=agent_persona,
            common_greeting=common_greeting,
            common_closing=common_closing,
            customer_info_variables=parsed_customer_vars,
            no_response_policy=parsed_no_response,
            shared_exceptions=shared_exceptions or [],
            session_tools=parsed_session_tools,
        )
        _set_flow_cfg(config)

        # Persist to NFS (fast-path) + S3
        sid = _get_current_session_id()
        if sid:
            state_dir = _nfs_state_dir(sid)
            if state_dir is not None:
                try:
                    state_dir.mkdir(parents=True, exist_ok=True)
                    target = state_dir / "flow_config.json"
                    tmp = target.with_suffix(".tmp")
                    tmp.write_text(_json.dumps(config.model_dump(), ensure_ascii=False, default=str), encoding="utf-8")
                    tmp.rename(target)
                except Exception as e:
                    logger.warning(f"[SpecManager] NFS persist failed for flow config: {e}")
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                ws.save_flow_config(config.model_dump())
        except Exception as e:
            logger.warning(f"[SpecManager] S3 persist failed for flow config: {e}")

        return {
            "success": True,
            "message": "Session flow configuration saved.",
            "summary": {
                "call_direction": call_direction,
                "has_persona": agent_persona is not None,
                "has_greeting": common_greeting is not None,
                "has_closing": common_closing is not None,
                "customer_info_count": len(parsed_customer_vars),
                "has_no_response_policy": parsed_no_response is not None,
                "shared_exception_count": len(shared_exceptions or []),
                "session_tool_count": len(parsed_session_tools),
            },
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Failed to save session flow config: {str(e)}",
        }


@tool
def get_session_flow_config_tool() -> dict:
    """
    Retrieve the saved session flow configuration.

    Returns:
        The session flow config or error if not yet saved.
    """
    config = get_session_flow_config()
    if config:
        return {"success": True, "config": config.model_dump()}
    return {"success": False, "error": "Session flow config not saved yet."}


@tool
def save_infrastructure_spec(
    project_name: str,
    db_type: str,
    region: str = "ap-northeast-2",
    rds_config: dict = None,
    dynamodb_config: dict = None,
    lambda_config: dict = None,
    api_gateway_config: dict = None,
    vpc_required: bool = False,
    vpc_config: dict = None,
    include_s3_bucket: bool = True,
    include_customer_phone_lookup: bool = False,
    tags: dict = None,
    notes: str = None,
) -> dict:
    """
    Save the project-level infrastructure specification. This is the SINGLE SOURCE OF TRUTH
    for all infrastructure decisions. All generators (Infrastructure, Lambda, OpenAPI, Prompt,
    Reviewer) reference this spec.

    Call this ONCE during interview Phase 2, after determining the database type and
    infrastructure requirements.

    Args:
        project_name: Project identifier for resource naming (e.g., "sunny-hotel")
        db_type: Database type — 'dynamodb', 'rds_mysql', or 'rds_postgresql'
        region: AWS region (default: ap-northeast-2)
        rds_config: RDS connection details (required when db_type starts with 'rds_').
            {"cluster_arn": "arn:...", "secret_arn": "arn:...", "database_name": "mydb",
             "engine": "postgresql", "tables": [{"name": "reservations", "columns": [...]}]}
        dynamodb_config: DynamoDB settings (required when db_type is 'dynamodb').
            {"tables": [{"name": "...", "partition_key": "pk", "sort_key": "sk", "gsi": [...]}],
             "billing_mode": "PAY_PER_REQUEST", "include_sample_data": true}
        lambda_config: Lambda function defaults.
            {"runtime": "python3.11", "memory_mb": 256, "timeout_seconds": 30,
             "architectures": ["arm64"], "layers": [], "environment_variables": {}}
        api_gateway_config: API Gateway configuration.
            {"stage_name": "prod", "api_key_required": false, "cors_origins": "*",
             "cors_methods": "*", "base_path": "/tools", "custom_domain": null}
        vpc_required: Whether Lambda functions need VPC access (default: false)
        vpc_config: VPC details (required when vpc_required is true).
            {"vpc_id": "vpc-...", "subnet_ids": [...], "security_group_ids": [...]}
        include_s3_bucket: Whether to include S3 bucket for FAQ uploads (default: true)
        include_customer_phone_lookup: Whether to include phone lookup Lambda resources
        tags: AWS resource tags to apply to all resources
        notes: Additional infrastructure notes or constraints

    Returns:
        Confirmation with saved configuration summary
    """
    try:
        parsed_rds = _safe_parse_model(RdsConfig, rds_config) if rds_config else None
        parsed_dynamodb = _safe_parse_model(DynamoDbConfig, dynamodb_config) if dynamodb_config else None
        parsed_lambda = _safe_parse_model(LambdaConfig, lambda_config) if lambda_config else None
        parsed_api_gw = _safe_parse_model(ApiGatewayConfig, api_gateway_config) if api_gateway_config else None
        parsed_vpc = _safe_parse_model(VpcConfig, vpc_config) if vpc_config else None

        spec = InfrastructureSpec(
            project_name=project_name,
            region=region,
            db_type=db_type,
            rds_config=parsed_rds,
            dynamodb_config=parsed_dynamodb,
            lambda_config=parsed_lambda,
            api_gateway_config=parsed_api_gw,
            vpc_required=vpc_required,
            vpc_config=parsed_vpc,
            include_s3_bucket=include_s3_bucket,
            include_customer_phone_lookup=include_customer_phone_lookup,
            tags=tags or {},
            notes=notes,
        )
        _set_infra_spec(spec)

        # Persist to NFS
        sid = _get_current_session_id()
        spec_dict = spec.model_dump()
        if sid:
            state_dir = _nfs_state_dir(sid)
            if state_dir is not None:
                try:
                    state_dir.mkdir(parents=True, exist_ok=True)
                    target = state_dir / "infrastructure_spec.json"
                    tmp = target.with_suffix(".tmp")
                    tmp.write_text(
                        _json.dumps(spec_dict, ensure_ascii=False, default=str),
                        encoding="utf-8",
                    )
                    tmp.rename(target)
                except Exception as e:
                    logger.warning(f"[SpecManager] NFS persist failed for infra spec: {e}")

        # Persist to S3
        try:
            from tools.project_workspace import get_workspace
            ws = get_workspace()
            if ws:
                ws.save_infrastructure_spec(spec_dict)
        except Exception as e:
            logger.warning(f"[SpecManager] S3 persist failed for infra spec: {e}")

        return {
            "success": True,
            "message": "Infrastructure specification saved. All generators will reference this spec.",
            "summary": {
                "project_name": project_name,
                "region": region,
                "db_type": db_type,
                "has_rds_config": parsed_rds is not None,
                "has_dynamodb_config": parsed_dynamodb is not None,
                "lambda_runtime": (parsed_lambda.runtime if parsed_lambda else "python3.11"),
                "lambda_memory_mb": (parsed_lambda.memory_mb if parsed_lambda else 256),
                "api_base_path": (parsed_api_gw.base_path if parsed_api_gw else "/tools"),
                "vpc_required": vpc_required,
                "include_s3_bucket": include_s3_bucket,
                "include_customer_phone_lookup": include_customer_phone_lookup,
            },
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Failed to save infrastructure spec: {str(e)}",
        }


def get_infrastructure_spec() -> Optional[InfrastructureSpec]:
    """Get infrastructure spec (for internal use by generators). Returns None if not saved yet."""
    spec = _get_infra_spec()
    if spec is None:
        # NFS fast-path
        sid = _get_current_session_id()
        if sid:
            state_dir = _nfs_state_dir(sid)
            if state_dir is not None:
                infra_file = state_dir / "infrastructure_spec.json"
                if infra_file.is_file():
                    try:
                        data = _json.loads(infra_file.read_text(encoding="utf-8"))
                        spec = _safe_parse_model(InfrastructureSpec, data)
                        _set_infra_spec(spec)
                        logger.info("[SpecManager] Restored infrastructure spec from NFS")
                    except Exception as e:
                        logger.warning(f"[SpecManager] NFS infra spec restore failed: {e}")

        # S3 fallback
        if spec is None:
            try:
                from tools.project_workspace import get_workspace
                ws = get_workspace()
                if ws:
                    data = ws.load_infrastructure_spec()
                    if data:
                        spec = _safe_parse_model(InfrastructureSpec, data)
                        _set_infra_spec(spec)
                        logger.info("[SpecManager] Restored infrastructure spec from S3")
            except Exception as e:
                logger.warning(f"[SpecManager] S3 infra spec restore failed: {e}")
    return spec


@tool
def get_infrastructure_spec_tool() -> dict:
    """
    Retrieve the saved infrastructure specification — the source of truth for all
    infrastructure decisions (DB type, Lambda config, API Gateway config, VPC, etc.).

    All generators should call this to understand the infrastructure context before generating.

    Returns:
        The infrastructure spec or error if not yet saved.
    """
    spec = get_infrastructure_spec()
    if spec:
        return {"success": True, "spec": spec.model_dump()}
    return {"success": False, "error": "Infrastructure spec not saved yet. Interview agent must call save_infrastructure_spec first."}


@tool
def infer_missing_tools() -> dict:
    """
    Analyze all operation conversation_steps and exception_scenarios to identify
    tools that are referenced but not defined in any operation's tools[] list.

    Call this BEFORE starting generation to catch missing tool definitions.

    Returns:
        {
            "success": True,
            "missing_tools": [{"operation_id": "...", "step_id": "...", "tool_id": "..."}],
            "all_defined_tools": ["tool1", "tool2", ...],
            "summary": "Found N missing tool references"
        }
    """
    all_specs = get_all_specs()
    if not all_specs:
        return {"success": True, "missing_tools": [], "all_defined_tools": [], "summary": "No specs found"}

    # Collect all defined tool_ids
    defined_ids: set[str] = set()
    for spec in all_specs.values():
        for t in spec.tools:
            defined_ids.add(t.tool_id)
    _flow_cfg = _get_flow_cfg()
    if _flow_cfg:
        for t in _flow_cfg.session_tools:
            defined_ids.add(t.tool_id)

    # Scan conversation_steps for tool_call references
    missing: list[dict] = []
    for op_id, spec in all_specs.items():
        for step in spec.conversation_steps:
            if step.tool_call and step.tool_call not in defined_ids:
                missing.append({
                    "operation_id": op_id,
                    "step_id": step.step_id,
                    "tool_id": step.tool_call,
                    "context": f"Step '{step.label}' references undefined tool",
                })

    summary = f"Found {len(missing)} missing tool reference(s)" if missing else "All tool references are defined"

    return {
        "success": len(missing) == 0,
        "missing_tools": missing,
        "all_defined_tools": sorted(defined_ids),
        "summary": summary,
    }


def restore_flow_config_from_workspace():
    """Restore session flow config from NFS/S3 workspace into memory.

    Called during session restore. NFS is attempted first for speed.
    """
    # NFS fast-path
    sid = _get_current_session_id()
    if sid:
        state_dir = _nfs_state_dir(sid)
        if state_dir is not None:
            flow_file = state_dir / "flow_config.json"
            if flow_file.is_file():
                try:
                    data = _json.loads(flow_file.read_text(encoding="utf-8"))
                    _set_flow_cfg(_safe_parse_model(SessionFlowConfig, data))
                    logger.info("[SpecManager] Restored session flow config from NFS")
                    return True
                except Exception as e:
                    logger.warning(f"[SpecManager] NFS flow config restore failed: {e}")

    # S3 fallback
    try:
        from tools.project_workspace import get_workspace
        ws = get_workspace()
        if ws:
            data = ws.load_flow_config()
            if data:
                _set_flow_cfg(_safe_parse_model(SessionFlowConfig, data))
                logger.info("[SpecManager] Restored session flow config from S3 workspace")
                return True
    except Exception as e:
        logger.warning(f"[SpecManager] Flow config workspace restore failed: {e}")
    return False


def clear_all_specs():
    """Clear all specs for the current session (for testing / session reset)."""
    from tools.session_context import cleanup_session
    _specs_bucket().clear()
    _set_flow_cfg(None)
    _set_infra_spec(None)
    sid = current_session_id.get()
    if sid is not None:
        cleanup_session(sid)
