"""
Parameter Consistency Validator

Validates that field names in generated assets (Lambda, OpenAPI, Prompt)
match the operation spec's input_fields/output_fields exactly.

Called by Orchestrator after Phase 3b (Prompt) and by Reviewer Agent.
"""

import ast
import json
import re
import logging
from typing import List, Dict, Any, Optional

import yaml
from strands import tool

from .spec_manager import get_all_specs, get_all_tools
from .s3_asset_storage import list_session_assets, get_asset_from_s3
from .acxd_bundle import load_acxd_bundle
from .acxd_flow_spec import get_acxd_flow_spec, is_acxd_target

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lambda IAM permission check (D2)
#
# Root cause this guards against (live workshop QA): a generated Lambda calls
# an AWS API at runtime (e.g. update_q_session calling connect:DescribeContact +
# wisdom:UpdateSessionData) but the CFN role for that function only carries
# AWSLambdaBasicExecutionRole → AccessDeniedException mid-call. The LLM
# recurrently omits inline policies even when instructed. This deterministic
# check derives required IAM actions from the SDK calls in each handler and
# cross-checks them against the merged infrastructure template.
# ─────────────────────────────────────────────────────────────────────────────

# boto3/aws-sdk service name → IAM action prefix (only where they differ)
_IAM_PREFIX_OVERRIDES = {
    "qconnect": "wisdom",   # qconnect: is NOT a real IAM namespace
    "wisdom": "wisdom",
    "sesv2": "ses",
    "ses": "ses",
}

# JS v3 client class name → IAM service prefix
_JS_CLIENT_PREFIXES = {
    "QConnectClient": "wisdom",
    "ConnectClient": "connect",
    "DynamoDBClient": "dynamodb",
    "DynamoDBDocumentClient": "dynamodb",
    "S3Client": "s3",
    "LambdaClient": "lambda",
    "SNSClient": "sns",
    "SQSClient": "sqs",
    "SESv2Client": "ses",
    "SESClient": "ses",
    "SecretsManagerClient": "secretsmanager",
    "RDSDataClient": "rds-data",
    "BedrockRuntimeClient": "bedrock",
    "PinpointClient": "mobiletargeting",
}

# Actions implicitly granted (basic execution role) — never reported
_IMPLICITLY_GRANTED = {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}


def _snake_to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in name.split("_"))


def _extract_required_iam_actions(code: str) -> set:
    """Derive IAM actions a Lambda handler needs from its AWS SDK calls.

    Handles Python boto3 (client/resource method calls) and JS SDK v3
    (``new XClient`` + ``XCommand``). Best-effort static analysis: unknown
    services fall back to using the SDK service name as the IAM prefix.
    """
    required: set = set()

    # ---- Python: boto3.client("svc") / boto3.resource("svc") ----
    var_to_svc: Dict[str, str] = {}
    for m in re.finditer(
            r'(\w+)\s*=\s*boto3\.(?:client|resource)\(\s*[\'"]([\w-]+)[\'"]', code):
        var_to_svc[m.group(1)] = m.group(2)
    for var, svc in var_to_svc.items():
        prefix = _IAM_PREFIX_OVERRIDES.get(svc, svc)
        for cm in re.finditer(rf'\b{re.escape(var)}\.([a-z_]+)\(', code):
            method = cm.group(1)
            if method in ("close", "get_paginator", "get_waiter", "Table", "meta"):
                continue
            required.add(f"{prefix}:{_snake_to_pascal(method)}")
    # boto3 dynamodb resource Table(...) usage
    if re.search(r'boto3\.resource\(\s*[\'"]dynamodb[\'"]', code):
        for cm in re.finditer(r'\btable\.([a-z_]+)\(', code, re.IGNORECASE):
            method = cm.group(1)
            if method in ("close",):
                continue
            required.add(f"dynamodb:{_snake_to_pascal(method)}")

    # ---- JS SDK v3: new XClient(...) + new YCommand(...) via .send() ----
    js_prefixes = {
        _JS_CLIENT_PREFIXES[c] for c in _JS_CLIENT_PREFIXES if c in code
    }
    if js_prefixes:
        commands = set(re.findall(r'new\s+(\w+)Command\(', code))
        for cmd in commands:
            # A command belongs to whichever imported client package declares it;
            # with a single client the mapping is unambiguous, with several we
            # attribute conservatively to all imported prefixes that Amazon
            # scopes that action under (best-effort: first import wins).
            for m in re.finditer(
                    r'require\([\'"]@aws-sdk/client-([\w-]+)[\'"]\)|from\s+[\'"]@aws-sdk/client-([\w-]+)[\'"]',
                    code):
                pkg = (m.group(1) or m.group(2) or "").replace("-", "")
                # match command to package by import statement co-occurrence
            if len(js_prefixes) == 1:
                required.add(f"{next(iter(js_prefixes))}:{cmd}")
            else:
                # map per import block: find the import line that mentions the command
                for m in re.finditer(
                        r'(?:const|import)\s*\{([^}]*)\}\s*(?:=\s*require\([\'"]@aws-sdk/client-([\w-]+)[\'"]\)|from\s+[\'"]@aws-sdk/client-([\w-]+)[\'"])',
                        code):
                    names, pkg = m.group(1), (m.group(2) or m.group(3) or "")
                    if f"{cmd}Command" in names:
                        svc = pkg.replace("qconnect", "wisdom")
                        prefix = _IAM_PREFIX_OVERRIDES.get(svc, svc)
                        required.add(f"{prefix}:{cmd}")
                        break

    return {a for a in required if a not in _IMPLICITLY_GRANTED}


def _cfn_gsi_names(infra_yaml: Optional[str]) -> Dict[str, set]:
    """{table logical id or TableName: {GSI IndexName, ...}} read from the
    CloudFormation template — the artifact that actually deploys. Tolerant of
    short-form intrinsics; empty on any parse problem (never raises)."""
    if not infra_yaml or not isinstance(infra_yaml, str):
        return {}
    try:
        sanitized = re.sub(r'!(Sub|Ref|GetAtt|Join|If|ImportValue|Select|FindInMap)\b', '', infra_yaml)
        doc = yaml.safe_load(sanitized) or {}
    except Exception as e:
        logger.debug(f"[VALIDATE] template not parseable for GSI names: {e}")
        return {}
    found: Dict[str, set] = {}
    for logical_id, resource in ((doc.get("Resources") or {}) or {}).items():
        if not isinstance(resource, dict) or resource.get("Type") != "AWS::DynamoDB::Table":
            continue
        props = resource.get("Properties") or {}
        names = {
            str(gsi.get("IndexName"))
            for gsi in (props.get("GlobalSecondaryIndexes") or [])
            if isinstance(gsi, dict) and isinstance(gsi.get("IndexName"), str)
        }
        if not names:
            continue
        table_name = props.get("TableName")
        found[str(table_name) if isinstance(table_name, str) else str(logical_id)] = names
    return found


def _extract_role_actions_from_template(infra_yaml: str) -> Dict[str, set]:
    """Map each Lambda function logical/FunctionName to the IAM actions its role grants.

    Best-effort YAML parse tolerant of CFN intrinsics (!Sub/!Ref/!GetAtt).
    Returns {function_key: {actions...}} where '*'-style wildcards are expanded
    conceptually via prefix matching in the caller.
    """
    try:
        # neutralize CFN short-form intrinsics for safe_load
        sanitized = re.sub(r'!(Sub|Ref|GetAtt|Join|If|ImportValue|Select|FindInMap)\b', '', infra_yaml)
        doc = yaml.safe_load(sanitized) or {}
    except Exception as e:
        logger.warning(f"[VALIDATE] Could not parse infrastructure template for IAM check: {e}")
        return {}

    resources = doc.get("Resources", {}) or {}

    # role logical id -> set of actions
    role_actions: Dict[str, set] = {}
    for rid, res in resources.items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::IAM::Role":
            continue
        actions: set = set()
        props = res.get("Properties", {}) or {}
        for pol in (props.get("Policies") or []):
            stmts = ((pol or {}).get("PolicyDocument") or {}).get("Statement") or []
            for st in stmts:
                if not isinstance(st, dict) or st.get("Effect") != "Allow":
                    continue
                acts = st.get("Action")
                acts = acts if isinstance(acts, list) else [acts]
                actions.update(a for a in acts if isinstance(a, str))
        for mp in (props.get("ManagedPolicyArns") or []):
            if isinstance(mp, str) and "AWSLambdaBasicExecutionRole" in mp:
                actions.update(_IMPLICITLY_GRANTED)
        role_actions[rid] = actions

    # function -> role
    fn_actions: Dict[str, set] = {}
    for rid, res in resources.items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::Lambda::Function":
            continue
        props = res.get("Properties", {}) or {}
        role_ref = props.get("Role")
        role_id = ""
        if isinstance(role_ref, str):
            # sanitized "!GetAtt X.Arn" became " X.Arn"
            role_id = role_ref.strip().split(".")[0]
        elif isinstance(role_ref, dict):
            ga = role_ref.get("Fn::GetAtt")
            if isinstance(ga, list) and ga:
                role_id = ga[0]
            elif isinstance(ga, str):
                role_id = ga.split(".")[0]
        fn_actions[rid] = role_actions.get(role_id, set())
    return fn_actions


def _action_granted(action: str, granted: set) -> bool:
    """True if *action* is covered by *granted*, honoring '*' wildcards."""
    if action in granted:
        return True
    svc, _, op = action.partition(":")
    for g in granted:
        if g == "*":
            return True
        gsvc, _, gop = g.partition(":")
        if gsvc != svc:
            continue
        if gop == "*" or (gop.endswith("*") and op.startswith(gop[:-1])):
            return True
    return False


def _extract_lambda_fields(code: str) -> set:
    """Extract field names accessed via body.get/event.get/body[...] in Lambda code."""
    patterns = [
        r'''(?:body|event|params|data)\.get\(\s*['\"](\w+)['\"]''',
        r'''(?:body|event|params|data)\[['\"](\w+)['\"]\]''',
    ]
    fields = set()
    for p in patterns:
        fields.update(re.findall(p, code))
    return fields


def lambda_field_gaps(operation_id: str, code: str) -> dict:
    """Save-time spec↔handler check for ONE Lambda (the D1 rule, per asset).

    Run by the Lambda generator right after it writes the handler, so a spec
    field the handler never reads, or a response field (spec output + the
    shared envelope) it never writes, is fixed while that handler is the
    thing being generated — not found weeks later by the reviewer.
    Returns {"missing_inputs": [...], "missing_outputs": [...], "checked": bool}.
    """
    try:
        from tools.spec_manager import get_all_specs, get_all_tools
        from tools.response_contract import ENVELOPE_FIELD_NAMES
    except Exception:
        return {"missing_inputs": [], "missing_outputs": [], "checked": False}
    specs = get_all_specs() or {}
    spec = specs.get(operation_id)
    inputs: set[str] = set()
    outputs: set[str] = set()

    def _name(f: Any) -> str:
        return (f.get("name") if isinstance(f, dict) else getattr(f, "name", "")) or ""

    if spec is not None:
        inputs |= {_name(f) for f in (getattr(spec, "input_fields", None) or []) if _name(f)}
        outputs |= {_name(f) for f in (getattr(spec, "output_fields", None) or []) if _name(f)}
    try:
        for tool in get_all_tools() or []:
            t_id = tool.get("tool_id") if isinstance(tool, dict) else getattr(tool, "tool_id", "")
            if t_id == operation_id:
                t_in = tool.get("input_fields") if isinstance(tool, dict) else getattr(tool, "input_fields", None)
                t_out = tool.get("output_fields") if isinstance(tool, dict) else getattr(tool, "output_fields", None)
                inputs |= {_name(f) for f in (t_in or []) if _name(f)}
                outputs |= {_name(f) for f in (t_out or []) if _name(f)}
    except Exception:
        pass
    if not inputs and not outputs:
        return {"missing_inputs": [], "missing_outputs": [], "checked": False}
    read_fields = _extract_lambda_fields(code)
    written_fields = set(re.findall(r'''['\"](\w+)['\"]\s*:''', code))
    expected_outputs = outputs | set(ENVELOPE_FIELD_NAMES)
    return {
        "missing_inputs": sorted(f for f in inputs if f not in read_fields),
        "missing_outputs": sorted(f for f in expected_outputs if f not in written_fields),
        "checked": True,
    }


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a $ref pointer like '#/components/schemas/Foo' to its schema dict."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = spec
    for p in parts:
        if isinstance(node, dict):
            node = node.get(p, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _get_schema_props(spec: dict, schema: dict) -> set:
    """Extract property names from a schema, resolving $ref if present."""
    if "$ref" in schema:
        resolved = _resolve_ref(spec, schema["$ref"])
        return set(resolved.get("properties", {}).keys())
    return set(schema.get("properties", {}).keys())


def _extract_openapi_fields(yaml_content: str) -> Dict[str, Dict[str, set]]:
    """Extract request/response property names per operationId from OpenAPI YAML.
    
    Returns: {operationId: {"input": set, "output": set}}
    """
    try:
        spec = yaml.safe_load(yaml_content)
    except Exception:
        return {}

    result: Dict[str, Dict[str, set]] = {}
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method.startswith("x-") or not isinstance(details, dict):
                continue
            op_id = details.get("operationId", path)
            input_fields = set()
            output_fields = set()
            # Request body properties (resolve $ref)
            rb = details.get("requestBody", {})
            if isinstance(rb, dict):
                for ct, sw in rb.get("content", {}).items():
                    if isinstance(sw, dict) and "schema" in sw:
                        input_fields.update(_get_schema_props(spec, sw["schema"]))
            # Response properties (resolve $ref)
            for code in ("200", "201", 200, 201):
                resp = details.get("responses", {}).get(code, {})
                if isinstance(resp, dict):
                    for ct, sw in resp.get("content", {}).items():
                        if isinstance(sw, dict) and "schema" in sw:
                            output_fields.update(_get_schema_props(spec, sw["schema"]))
            result[op_id] = {"input": input_fields, "output": output_fields}
    return result


# ─────────────────────────────────────────────────────────────────────────────
# SQL identifier check (D4)
#
# Root cause this guards against (found in live E2E against a scanned Aurora
# PostgreSQL schema): even with a correct introspection result in context, the
# generator writes a column onto the WRONG table — e.g. `oi.product_name` when
# product_name lives on `products`, or `pv.serial_number` when serial_number
# lives on `warranty_claims`. The Lambda then fails at runtime with
# "column ... does not exist" (SQLState 42703) on the very first call.
#
# The check is deliberately conservative: it only reports a column when the
# schema knows that column on a DIFFERENT table. Columns the schema summary
# simply omitted are ignored, so it cannot false-positive on an incomplete
# column list.
# ─────────────────────────────────────────────────────────────────────────────
_SQL_KEYWORD_RE = re.compile(r'^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b', re.I)
# alias declarations:  FROM orders o  /  JOIN order_items AS oi
_SQL_ALIAS_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)\s+(?:AS\s+)?([A-Za-z_][\w]*)\b', re.I)
_SQL_QUALIFIED_RE = re.compile(r'\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b')
_SQL_RESERVED = {
    "select", "insert", "update", "delete", "with", "from", "join", "inner",
    "left", "right", "outer", "on", "where", "and", "or", "not", "null", "as",
    "order", "by", "group", "having", "limit", "offset", "into", "values", "set",
    "asc", "desc", "distinct", "case", "when", "then", "else", "end", "returning",
    "union", "all", "exists", "in", "is", "like", "ilike", "between", "interval",
    "coalesce", "count", "sum", "max", "min", "avg", "now", "cast",
}


def _extract_sql_statements(code: str) -> list:
    """Pull SQL statements out of a generated Python handler.

    Uses the AST rather than a regex so that implicit string concatenation
    (``"SELECT a " "FROM t " "WHERE ..."`` split across lines — which is what
    the generators actually emit) is seen as ONE statement. A regex sees each
    fragment separately and never observes the FROM clause, so alias resolution
    silently finds nothing.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    consts: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value

    def as_text(node) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):   # f-string
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue) and \
                        isinstance(value.value, ast.Name) and value.value.id in consts:
                    parts.append(consts[value.value.id])
                else:
                    parts.append(" ")
            return "".join(parts)
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = as_text(node.left), as_text(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in list(node.args) + [k.value for k in node.keywords if k.arg == "sql"]:
                text = as_text(arg)
                if text and _SQL_KEYWORD_RE.match(text):
                    found.append(text)
    for text in consts.values():
        if _SQL_KEYWORD_RE.match(text) and text not in found:
            found.append(text)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Data API parameter type-binding check (D5)
#
# Root cause this guards against (found in live E2E): the handler bound a
# BIGINT key as a string —
#     parameters=[{"name": "orderId", "value": {"stringValue": str(order_id)}}]
# — and PostgreSQL rejected the query with
#     operator does not exist: bigint = text  (SQLState 42883)
# on the first invocation. MySQL silently coerces instead, which hides the bug
# but throws away index usage on the compared column.
#
# Only unambiguous mismatches are reported. A string bound to a date/timestamp/
# uuid/json/enum/numeric column is legitimate for the Data API and is ignored.
# ─────────────────────────────────────────────────────────────────────────────
_INT_TYPE_RE = re.compile(
    r'^(small|big|tiny|medium)?(int|integer|serial)\d*(\s+unsigned)?$', re.I)
_BOOL_TYPE_RE = re.compile(r'^(bool|boolean|bit|tinyint\(1\))$', re.I)
_TEXT_TYPE_RE = re.compile(
    r'^n?(var)?char(acter)?(\s+varying)?(\(\s*(\d+|max)\s*\))?$|'
    r'^n?(tiny|medium|long)?text$', re.I)
# columns where a stringValue binding is the correct Data API representation
_STRING_OK_TYPE_RE = re.compile(
    r'date|time|timestamp|uuid|json|xml|enum|set\(|numeric|decimal|money|'
    r'interval|inet|cidr|bytea|blob|user-defined', re.I)

_PARAM_COMPARISON_RE = re.compile(
    r'\b(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*(?:=|<>|!=|>=|<=|>|<)\s*:(\w+)')
_PARAM_INSERT_RE = re.compile(
    r'INSERT\s+INTO\s+"?`?([A-Za-z_]\w*)`?"?\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)',
    re.I | re.S)
_VALUE_KEYS = ("stringValue", "longValue", "doubleValue", "booleanValue",
               "blobValue", "isNull", "arrayValue")


def _extract_sql_with_bindings(code: str) -> list:
    """Return [(sql_text, {param_name: dataApiValueKey})] per SQL call site."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    consts: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value

    def as_text(node) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue) and \
                        isinstance(value.value, ast.Name) and value.value.id in consts:
                    parts.append(consts[value.value.id])
                else:
                    parts.append(" ")
            return "".join(parts)
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = as_text(node.left), as_text(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    def bindings(node) -> Dict[str, str]:
        """Parse a parameters=[{"name": ..., "value": {"longValue": ...}}] list."""
        out: Dict[str, str] = {}
        if not isinstance(node, (ast.List, ast.Tuple)):
            return out
        for element in node.elts:
            if not isinstance(element, ast.Dict):
                continue
            name = None
            value_key = None
            for k, v in zip(element.keys, element.values):
                key = k.value if isinstance(k, ast.Constant) else None
                if key == "name" and isinstance(v, ast.Constant):
                    name = v.value
                elif key == "value" and isinstance(v, ast.Dict):
                    for vk in v.keys:
                        if isinstance(vk, ast.Constant) and vk.value in _VALUE_KEYS:
                            value_key = vk.value
                            break
            if name and value_key:
                out[name] = value_key
        return out

    pairs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        sql = None
        for arg in list(node.args) + [k.value for k in node.keywords if k.arg == "sql"]:
            text = as_text(arg)
            if text and _SQL_KEYWORD_RE.match(text):
                sql = text
                break
        if not sql:
            continue
        binds: Dict[str, str] = {}
        for kw in node.keywords:
            if kw.arg in ("parameters", "params"):
                binds = bindings(kw.value)
        if not binds:
            for arg in node.args:
                if isinstance(arg, (ast.List, ast.Tuple)):
                    binds = bindings(arg)
                    if binds:
                        break
        pairs.append((sql, binds))
    return pairs


def _column_type_index(schema: dict) -> Dict[tuple, str]:
    """(table, column) -> declared sql type, plus (None, column) when unambiguous."""
    per_table: Dict[tuple, str] = {}
    by_column: Dict[str, set] = {}
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    if isinstance(tables, dict):
        tables = list(tables.values())
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        table = t.get("name") or t.get("table_name")
        for c in t.get("columns", []) or []:
            if not isinstance(c, dict):
                continue
            col = c.get("name") or c.get("column_name")
            sql_type = c.get("sql_type") or c.get("full_type") or c.get("type")
            if not col or not sql_type:
                continue
            per_table[(table, col)] = str(sql_type)
            by_column.setdefault(col, set()).add(str(sql_type))
    for col, types in by_column.items():
        if len(types) == 1:
            per_table[(None, col)] = next(iter(types))
    return per_table


def _expected_value_key(sql_type: str) -> Optional[str]:
    """The Data API value key a column of this type must be bound with."""
    t = str(sql_type).strip()
    if _STRING_OK_TYPE_RE.search(t):
        return None          # a stringValue binding is legitimate here
    if _BOOL_TYPE_RE.match(t):
        return "booleanValue"
    if _INT_TYPE_RE.match(t):
        return "longValue"
    if _TEXT_TYPE_RE.match(t):
        return "stringValue"
    return None


def _check_sql_param_types(op_id: str, code: str, type_index: Dict[tuple, str]) -> list:
    """Report Data API parameters bound with a type the column cannot accept."""
    issues = []
    if not type_index:
        return issues

    for sql, binds in _extract_sql_with_bindings(code):
        if not binds:
            continue
        aliases = {}
        for table, alias in _SQL_ALIAS_RE.findall(sql):
            if alias.lower() not in _SQL_RESERVED:
                aliases[alias] = table
                aliases[table] = table

        mapping = {}   # param -> (table, column)
        for qualifier, column, param in _PARAM_COMPARISON_RE.findall(sql):
            table = aliases.get(qualifier) if qualifier else None
            mapping[param] = (table, column)
        for table, col_list, val_list in _PARAM_INSERT_RE.findall(sql):
            cols = [c.strip().strip('"`') for c in col_list.split(",") if c.strip()]
            vals = [v.strip() for v in val_list.split(",")]
            for col, val in zip(cols, vals):
                m = re.match(r'^:(\w+)', val)
                if m:
                    mapping.setdefault(m.group(1), (table, col))

        for param, (table, column) in mapping.items():
            bound = binds.get(param)
            if not bound or bound in ("isNull", "arrayValue", "blobValue"):
                continue
            sql_type = type_index.get((table, column)) or type_index.get((None, column))
            if not sql_type:
                continue
            expected = _expected_value_key(sql_type)
            if not expected or expected == bound:
                continue
            issues.append({
                "operation_id": op_id, "field": f":{param}",
                "asset_type": "sql_param_type_mismatch",
                "issue": f"Lambda '{op_id}' binds `:{param}` as `{bound}` but "
                         f"`{(table + '.') if table else ''}{column}` is "
                         f"`{sql_type}`, which needs `{expected}`. PostgreSQL "
                         f"rejects this outright (e.g. 'operator does not exist: "
                         f"bigint = text', SQLState 42883); MySQL silently coerces "
                         f"and stops using the index. Bind it as {expected}.",
            })
    return issues


def _schema_column_index(schema: dict) -> Dict[str, set]:
    """table_name -> set(column names) from the infrastructure schema."""
    index: Dict[str, set] = {}
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    if isinstance(tables, dict):
        tables = list(tables.values())
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("table_name") or t.get("tableName")
        if not name:
            continue
        cols = set()
        for c in t.get("columns", []) or []:
            if isinstance(c, str):
                cols.add(c)
            elif isinstance(c, dict):
                cn = c.get("name") or c.get("column_name")
                if cn:
                    cols.add(cn)
        pk = t.get("primary_key")
        if isinstance(pk, str):
            for part in re.split(r'[+,]', pk):
                part = part.strip()
                if part:
                    cols.add(part)
        elif isinstance(pk, list):
            cols.update(str(p) for p in pk)
        if cols:
            index[name] = cols
    return index


_SQL_SELECT_LIST_RE = re.compile(r'\bSELECT\b(?P<list>.*?)\bFROM\b', re.I | re.S)
_SQL_FROM_TARGETS_RE = re.compile(r'\b(?:FROM|JOIN)\s+"?`?\[?([A-Za-z_]\w*)\]?`?"?', re.I)
# Left-hand side of a predicate: after WHERE/AND/OR, an unqualified identifier
# followed by a comparison operator. The negative lookahead on '(' keeps
# function calls out; requiring the operator keeps bare keywords out.
_SQL_PREDICATE_LHS_RE = re.compile(
    r'\b(?:WHERE|AND|OR)\s+"?`?\[?([A-Za-z_]\w*)\]?`?"?\s*'
    r'(?:=|<>|!=|>=|<=|>|<|\bIS\b|\bIN\b|\bLIKE\b|\bBETWEEN\b)(?!\s*\()',
    re.I)


def _unqualified_columns(sql: str) -> list:
    """Column identifiers used without a table qualifier.

    Read from the SELECT list, the INSERT column list, and the left-hand side of
    WHERE/AND/OR comparisons — never the whole statement, so SQL keywords,
    function names and literals are not mistaken for columns.
    """
    names = []
    m = _SQL_SELECT_LIST_RE.search(sql)
    if m:
        for item in m.group("list").split(","):
            item = re.split(r'\s+AS\s+|\s+', item.strip(), flags=re.I)[0].strip()
            if "." in item or "(" in item or "*" in item:
                continue          # qualified, a function call, or SELECT *
            ident = item.strip('"`[]')
            if re.fullmatch(r'[A-Za-z_]\w*', ident) and ident.lower() not in _SQL_RESERVED:
                names.append(ident)
    for _, col_list, _ in _PARAM_INSERT_RE.findall(sql):
        for c in col_list.split(","):
            ident = c.strip().strip('"`[]')
            if re.fullmatch(r'[A-Za-z_]\w*', ident) and ident.lower() not in _SQL_RESERVED:
                names.append(ident)
    # A predicate's left-hand side: `WHERE order_number = :x`, `AND status <> 'X'`.
    # Requires a comparison operator so bare keywords cannot slip through, and
    # skips anything qualified or followed by '(' (a function call).
    for ident in _SQL_PREDICATE_LHS_RE.findall(sql):
        ident = ident.strip('"`[]')
        if re.fullmatch(r'[A-Za-z_]\w*', ident) and ident.lower() not in _SQL_RESERVED:
            names.append(ident)
    return names


def _check_sql_identifiers(op_id: str, code: str, col_index: Dict[str, set]) -> list:
    """Report SQL columns that the table they are used against does not have.

    Covers both `alias.column` references and unqualified columns — the latter
    only when the statement touches exactly one known table, which makes the
    owning table unambiguous. Observed live: a handler selected
    `product_name` straight out of `order_items` (it lives on `products`) and
    inserted `created_at` into `returns` (that column is `requested_at`); with
    no alias in the SQL, a qualified-only check saw neither.

    Once the table is resolved, a column missing from that table's column list
    is wrong whether or not it exists elsewhere: the scan returns the complete
    column list for every table it read, so `shipments` having no `created_at`
    is a fact, not a guess. Naming the table that does own the column is extra
    help when there is one, not a precondition for reporting.
    """
    issues = []
    if not col_index:
        return issues
    owner: Dict[str, set] = {}
    for tbl, cols in col_index.items():
        for c in cols:
            owner.setdefault(c, set()).add(tbl)

    def report(qualifier, column, table):
        if column in col_index.get(table, set()):
            return
        shown = f"{qualifier}.{column}" if qualifier else column
        real_owners = sorted(owner.get(column, set()) - {table})
        if real_owners:
            where = (f"but `{column}` belongs to {real_owners} — not to `{table}`. "
                     f"Join through to {real_owners[0]} or use the correct column "
                     f"from `{table}`.")
        else:
            where = (f"but `{table}` has no column `{column}`, and no scanned table "
                     f"does. Use one of the columns the scan reported for "
                     f"`{table}`: {sorted(col_index.get(table, set()))}.")
        issues.append({
            "operation_id": op_id, "field": shown,
            "asset_type": "sql_schema_mismatch",
            "issue": f"Lambda '{op_id}' SQL references `{shown}` against `{table}`, "
                     f"{where} This fails at runtime with 'column does not exist' "
                     f"(SQLState 42703).",
        })

    for sql in _extract_sql_statements(code):
        aliases = {}
        for tbl, alias in _SQL_ALIAS_RE.findall(sql):
            if alias.lower() in _SQL_RESERVED:
                continue
            if tbl in col_index:
                aliases[alias] = tbl
                aliases[tbl] = tbl

        for qualifier, column in _SQL_QUALIFIED_RE.findall(sql):
            table = aliases.get(qualifier)
            if table and column.lower() not in _SQL_RESERVED:
                report(qualifier, column, table)

        targets = {t for t in _SQL_FROM_TARGETS_RE.findall(sql) if t in col_index}
        targets |= {t for t, _, _ in _PARAM_INSERT_RE.findall(sql) if t in col_index}
        if len(targets) == 1:
            only = next(iter(targets))
            for column in _unqualified_columns(sql):
                report(None, column, only)

    return issues


_PG_BUILTIN_TYPES = {
    "text", "varchar", "char", "bpchar", "citext", "name",
    "int", "int2", "int4", "int8", "smallint", "integer", "bigint",
    "numeric", "decimal", "real", "float4", "float8", "double precision", "money",
    "bool", "boolean", "bytea", "uuid", "json", "jsonb", "xml",
    "date", "time", "timetz", "timestamp", "timestamptz", "interval",
    "inet", "cidr", "macaddr", "tsvector", "tsquery", "oid", "regclass",
    "array", "character", "user-defined", "record", "void", "anyelement",
}
_SQL_CAST_RE = re.compile(r'::\s*"?([A-Za-z_][\w ]*?)"?\s*(?=[\s,)\]]|$)')


def _check_sql_casts(op_id: str, code: str, enum_types: set) -> list:
    """Report ``::type`` casts to a type the schema does not define.

    Observed live: the generator correctly cast a real PostgreSQL enum
    (``:reason::return_reason``) and then invented the same pattern for two
    plain VARCHAR columns (``:approvalStatus::approval_status``,
    ``:claimStatus::claim_status``). Postgres rejects those with
    ``type "approval_status" does not exist`` on the first write.
    """
    issues = []
    for sql in _extract_sql_statements(code):
        for raw in _SQL_CAST_RE.findall(sql):
            type_name = raw.strip().lower().rstrip("[]")
            base = type_name.replace("[]", "").strip()
            if not base or base in _PG_BUILTIN_TYPES or base in enum_types:
                continue
            if re.match(r'^(varchar|char|numeric|decimal|timestamp|time)\s*\(', base):
                continue
            issues.append({
                "operation_id": op_id, "field": f"::{raw.strip()}",
                "asset_type": "sql_type_mismatch",
                "issue": f"Lambda '{op_id}' SQL casts to type `{raw.strip()}`, which is "
                         f"neither a built-in type nor one of the schema's enum types "
                         f"({sorted(enum_types) if enum_types else 'none'}). Postgres "
                         f"fails with 'type \"{raw.strip()}\" does not exist' on the first "
                         f"call. If that column is a plain VARCHAR, drop the cast.",
            })
    return issues


def _schema_enum_types(schema: dict) -> set:
    """Collect enum type names the schema declares."""
    names = set()
    if not isinstance(schema, dict):
        return names
    enum_types = schema.get("enum_types") or {}
    if isinstance(enum_types, dict):
        names.update(k.lower() for k in enum_types)
    elif isinstance(enum_types, list):
        for e in enum_types:
            if isinstance(e, str):
                names.add(e.lower())
            elif isinstance(e, dict) and e.get("name"):
                names.add(str(e["name"]).lower())
    tables = schema.get("tables", [])
    if isinstance(tables, dict):
        tables = list(tables.values())
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        enums = t.get("enums")
        if isinstance(enums, dict):
            # {"status": [...]} — the column name is not the type name, but the
            # generator commonly casts to it, so accept it rather than false-flag.
            names.update(k.lower() for k in enums)
        for c in t.get("columns", []) or []:
            if isinstance(c, dict):
                st = (c.get("sql_type") or c.get("full_type") or "")
                m = re.match(r'^([a-z_][\w]*)$', str(st).strip().lower())
                if m and m.group(1) not in _PG_BUILTIN_TYPES:
                    names.add(m.group(1))
    return names


_SQL_INSERT_RE = re.compile(
    r'INSERT\s+INTO\s+"?([A-Za-z_][\w]*)"?\s*\(([^)]*)\)', re.I | re.S)


def _check_sql_insert_required_columns(op_id: str, code: str, schema: dict) -> list:
    """Report INSERTs that omit a NOT NULL column having no default.

    Observed live: the generated `create_return` INSERT listed
    (return_number, order_id, line_number, reason, refund_amount,
     requires_manager_approval, approval_status) but `returns.quantity` is
    NOT NULL with no default, so the first write fails with
    'null value in column "quantity" violates not-null constraint'.
    """
    issues = []
    required: Dict[str, set] = {}
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    if isinstance(tables, dict):
        tables = list(tables.values())
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("table_name")
        cols = t.get("columns") or []
        if not name or not cols or not isinstance(cols[0], dict):
            continue  # need per-column metadata to judge
        needed = set()
        for c in cols:
            nullable = c.get("nullable")
            if nullable in (True, "YES", "yes"):
                continue
            if nullable is None:
                continue  # unknown → don't guess
            if c.get("default") not in (None, "", "None"):
                continue
            if c.get("generated") or c.get("auto_increment"):
                continue
            cname = c.get("name") or c.get("column_name")
            if cname:
                needed.add(cname)
        if needed:
            required[name] = needed

    if not required:
        return issues

    for sql in _extract_sql_statements(code):
        for table, col_list in _SQL_INSERT_RE.findall(sql):
            if table not in required:
                continue
            provided = {c.strip().strip('"') for c in col_list.split(",") if c.strip()}
            missing = sorted(required[table] - provided)
            # a single-column PK that looks generated is usually a serial
            missing = [m for m in missing if not m.endswith("_id") or m in provided]
            if missing:
                issues.append({
                    "operation_id": op_id, "field": ", ".join(missing),
                    "asset_type": "sql_missing_required_column",
                    "issue": f"Lambda '{op_id}' INSERTs into `{table}` without the NOT NULL "
                             f"column(s) {missing}, which have no default. The write fails "
                             f"with 'null value in column ... violates not-null "
                             f"constraint'. Add them to the INSERT column list and "
                             f"parameters.",
                })
    return issues


@tool
def validate_parameter_consistency(session_id: str) -> dict:
    """
    Validate that field names across Lambda, OpenAPI, and Prompt match the operation spec.

    Loads assets from S3 and compares field names against saved operation specs.
    Returns a report of mismatches that the Orchestrator can use to trigger re-generation.

    Args:
        session_id: Session ID to validate assets for

    Returns:
        {
            "success": True,
            "mismatches": [...],
            "summary": "Found N mismatches across M operations",
            "operations_checked": N
        }
    """
    # Fault-tolerance: this gate must NEVER die with a raw exception. In two
    # live sessions an AttributeError propagated as a tool error, the whole
    # deterministic validation silently disappeared, and the reviewer had to
    # fall back to manual cross-checks. A structured failure keeps the
    # orchestrator informed and tells it what to do instead.
    try:
        return _validate_parameter_consistency_impl(session_id)
    except Exception as e:
        logger.exception(f"[VALIDATE] validate_parameter_consistency crashed: {e}")
        return {
            "success": False,
            "mismatches": [],
            "summary": (
                f"Validator internal error ({type(e).__name__}: {e}). "
                "Deterministic checks could not run — manually cross-check field "
                "names between spec, Lambda, OpenAPI and prompt before proceeding."
            ),
            "operations_checked": 0,
            "internal_error": True,
        }


def _validate_parameter_consistency_impl(session_id: str) -> dict:
    specs = get_all_specs()
    if not specs:
        return {"success": True, "mismatches": [], "summary": "No operation specs found", "operations_checked": 0}

    def _fname(f) -> str:
        """Field name accessor tolerant of raw dicts.

        Specs restored via the model_construct fallback (or any legacy
        persisted shape) can carry dict field entries; a bare ``f.name``
        crashed this whole gate with AttributeError in two live sessions,
        forcing the reviewer to fall back to manual cross-checks.
        """
        if isinstance(f, dict):
            return f.get("name") or f.get("field_name") or ""
        return getattr(f, "name", "") or ""

    # Build expected field sets per operation (backward-compatible)
    expected: Dict[str, Dict[str, set]] = {}
    for op_id, spec in specs.items():
        inp = {_fname(f) for f in (spec.input_fields or []) if _fname(f)}
        out = {_fname(f) for f in (spec.output_fields or []) if _fname(f)}
        expected[op_id] = {"input": inp, "output": out, "all": inp | out}

    # Build expected field sets per tool (multi-tool architecture)
    all_tools = get_all_tools()
    tool_expected: Dict[str, Dict[str, set]] = {}
    for tool in all_tools:
        if isinstance(tool, dict):
            t_id = tool.get("tool_id") or tool.get("toolId") or ""
            t_in_list = tool.get("input_fields") or []
            t_out_list = tool.get("output_fields") or []
        else:
            t_id = getattr(tool, "tool_id", "") or ""
            t_in_list = getattr(tool, "input_fields", None) or []
            t_out_list = getattr(tool, "output_fields", None) or []
        if not t_id:
            continue
        t_inp = {_fname(f) for f in t_in_list if _fname(f)}
        t_out = {_fname(f) for f in t_out_list if _fname(f)}
        tool_expected[t_id] = {"input": t_inp, "output": t_out, "all": t_inp | t_out}

    # Load assets from S3
    asset_keys = list_session_assets(session_id) if session_id else []
    lambda_code: Dict[str, str] = {}  # op_id -> code
    lambda_all_code: Dict[str, str] = {}  # op_id -> code (any handler file, for IAM check)
    openapi_yaml: Optional[str] = None
    infra_yaml: Optional[str] = None
    infra_schema: Optional[str] = None
    contact_flow_json: Optional[str] = None
    prompt_text: Optional[str] = None

    for key in asset_keys:
        parts = key.split("/")
        if len(parts) < 3:
            continue
        asset_type = parts[2]
        if asset_type == "lambda" and key.endswith("handler.py"):
            op_id = parts[3] if len(parts) > 4 else "default"
            content = get_asset_from_s3(key)
            if content:
                lambda_code[op_id] = content
                lambda_all_code[op_id] = content
        elif asset_type == "lambda" and (key.endswith("index.py") or key.endswith("index.js")) \
                and len(parts) > 4 and _is_spec_operation_folder(parts[3], specs):
            # Live (SELC, 2026-09-12): the generator wrote index.py for the four
            # business operations (matching the template's Handler: index.lambda_handler),
            # and the field checks silently skipped them as "supporting" Lambdas.
            # A folder named after a spec operation is a business Lambda whatever
            # the file is called.
            op_id = parts[3]
            content = get_asset_from_s3(key)
            if content:
                lambda_code[op_id] = content
                lambda_all_code[op_id] = content
        elif asset_type == "lambda" and (key.endswith("index.py") or key.endswith("index.js")):
            # supporting lambdas (update_q_session, customer_lookup, ...) — IAM check only
            op_id = parts[3] if len(parts) > 4 else "default"
            content = get_asset_from_s3(key)
            if content:
                lambda_all_code[op_id] = content
        elif asset_type == "openapi" and (key.endswith(".yaml") or key.endswith(".yml")):
            content = get_asset_from_s3(key)
            if content:
                openapi_yaml = content
        elif asset_type in ("infrastructure", "cloudformation", "cdk") and (key.endswith(".yaml") or key.endswith(".yml")):
            # The generator stores the template under cloudformation/<project>/…;
            # matching only "infrastructure" left infra_yaml empty for every
            # session, silently skipping the IAM, GSI and env-var checks (live).
            content = get_asset_from_s3(key)
            if content:
                infra_yaml = content
        elif asset_type == "contact_flow" and key.endswith(".json"):
            content = get_asset_from_s3(key)
            if content:
                contact_flow_json = content
        elif asset_type == "prompt" and (key.endswith(".yaml") or key.endswith(".yml") or key.endswith(".md") or key.endswith(".txt")):
            content = get_asset_from_s3(key)
            if content:
                prompt_text = content

    # Auto-load infrastructure schema from registry
    try:
        from agents.infrastructure_generator.agent import get_infrastructure_schema
        infra_schema = get_infrastructure_schema()
    except Exception:
        pass

    mismatches: List[Dict[str, Any]] = []
    openapi_fields: Dict[str, Dict[str, set]] = {}  # pre-declare for D1 checks below

    # Check Lambda vs spec (input + output)
    for op_id, spec_fields in expected.items():
        code = lambda_code.get(op_id)
        if not code:
            continue
        lambda_fields = _extract_lambda_fields(code)
        for field in spec_fields["input"]:
            if field not in lambda_fields:
                mismatches.append({
                    "operation_id": op_id, "field": field,
                    "asset_type": "lambda",
                    "issue": f"Spec input field '{field}' not found in Lambda handler",
                })
        # Check output fields appear in Lambda response building
        lambda_response_fields = set(re.findall(r'''['\"](\w+)['\"]\s*:''', code))
        for field in spec_fields["output"]:
            if field not in lambda_response_fields:
                mismatches.append({
                    "operation_id": op_id, "field": field,
                    "asset_type": "lambda",
                    "issue": f"Spec output field '{field}' not found in Lambda response",
                })

    # Check OpenAPI vs spec (input + output, with $ref resolution)
    if openapi_yaml:
        openapi_fields = _extract_openapi_fields(openapi_yaml)
        for op_id, spec_fields in expected.items():
            matched = openapi_fields.get(op_id)
            if not matched:
                kebab = "/" + op_id.replace("_", "-")
                matched = openapi_fields.get(kebab)
            if not matched:
                continue
            for field in spec_fields["input"]:
                if field not in matched["input"]:
                    mismatches.append({
                        "operation_id": op_id, "field": field,
                        "asset_type": "openapi",
                        "issue": f"Spec input field '{field}' not found in OpenAPI requestBody schema",
                    })
            for field in spec_fields["output"]:
                if field not in matched["output"]:
                    mismatches.append({
                        "operation_id": op_id, "field": field,
                        "asset_type": "openapi",
                        "issue": f"Spec output field '{field}' not found in OpenAPI response schema",
                    })

    # Check Infrastructure schema vs spec data_source keys
    infra_gsi_names: Dict[str, set] = {}  # table_name -> {gsi_name, ...}
    infra_env_vars: Dict[str, str] = {}  # env_var_name -> table_name
    if infra_schema:
        try:
            import json
            schema = json.loads(infra_schema) if isinstance(infra_schema, str) else infra_schema
            tables = schema.get("tables", schema.get("dynamodb_tables", {}))
            if isinstance(tables, list):
                tables = {t.get("table_name", t.get("tableName", "")): t for t in tables}
            for tbl_name, tbl_def in tables.items():
                if not isinstance(tbl_def, dict):
                    continue
                # Collect GSI names for cross-validation
                gsi_names = set()
                for gsi in tbl_def.get("gsi", tbl_def.get("global_secondary_indexes", tbl_def.get("gsi_indexes", []))):
                    if isinstance(gsi, dict):
                        gsi_name = gsi.get("index_name", gsi.get("indexName", gsi.get("name", "")))
                        if gsi_name:
                            gsi_names.add(gsi_name)
                infra_gsi_names[tbl_name] = gsi_names
                # Collect env var mappings
                env_name = tbl_def.get("env_var_name", tbl_def.get("envVarName", ""))
                if env_name:
                    infra_env_vars[env_name] = tbl_name

            for op_id, spec in specs.items():
                ds = spec.data_source
                if not ds:
                    continue
                table_name = getattr(ds, "table_name", None) or getattr(ds, "tableName", None)
                if not table_name or table_name not in tables:
                    continue
                table_def = tables[table_name]
                # Collect all key/attribute names from infra schema
                infra_keys = set()
                for k in table_def.get("keys", table_def.get("key_schema", [])):
                    if isinstance(k, dict):
                        infra_keys.add(k.get("attribute_name", k.get("attributeName", k.get("name", ""))))
                    elif isinstance(k, str):
                        infra_keys.add(k)
                for gsi in table_def.get("gsi", table_def.get("global_secondary_indexes", table_def.get("gsi_indexes", []))):
                    if isinstance(gsi, dict):
                        for k in gsi.get("keys", gsi.get("key_schema", [])):
                            if isinstance(k, dict):
                                infra_keys.add(k.get("attribute_name", k.get("attributeName", k.get("name", ""))))
                # Check spec fields exist in infra
                spec_all = expected[op_id]["all"]
                pk = getattr(ds, "primary_key", None) or getattr(ds, "primaryKey", None)
                if pk and pk not in infra_keys and infra_keys:
                    mismatches.append({
                        "operation_id": op_id, "field": pk,
                        "asset_type": "infrastructure",
                        "issue": f"Spec data_source primary_key '{pk}' not found in infra table '{table_name}' keys: {infra_keys}",
                    })
        except Exception as e:
            logger.warning(f"[VALIDATE] Failed to check infra schema: {e}")

    # D1-1: Lambda IndexName= vs infrastructure GSI name matching
    # The schema registry is a draft; the CloudFormation template is what deploys.
    # Live (Hanbit): PatientsTable carried 'phone-birth-index' in the template
    # but not in the registry, and the gate blocked a correct Lambda. Union the
    # template's GlobalSecondaryIndexes into the known set.
    for tbl_name, gsis in _cfn_gsi_names(infra_yaml).items():
        infra_gsi_names.setdefault(tbl_name, set()).update(gsis)
    for op_id, code in lambda_code.items():
        index_names_in_code = set(re.findall(r"IndexName\s*[=:]\s*['\"](\w[\w-]*)['\"]", code))
        for idx_name in index_names_in_code:
            found_in_any_table = False
            for tbl_gsis in infra_gsi_names.values():
                if idx_name in tbl_gsis:
                    found_in_any_table = True
                    break
            if not found_in_any_table and infra_gsi_names:
                all_gsis = set()
                for g in infra_gsi_names.values():
                    all_gsis.update(g)
                mismatches.append({
                    "operation_id": op_id, "field": idx_name,
                    "asset_type": "lambda_gsi",
                    "issue": f"Lambda uses IndexName='{idx_name}' but not found in infra GSIs: {all_gsis}",
                })

    # D1-2: Lambda os.environ["X_TABLE_NAME"] vs infrastructure env_var_name matching
    for op_id, code in lambda_code.items():
        env_refs = set(re.findall(r'os\.environ\s*\[\s*[\'"](\w+_TABLE_NAME)[\'"]\s*\]', code))
        env_refs.update(re.findall(r'os\.environ\.get\s*\(\s*[\'"](\w+_TABLE_NAME)[\'"]', code))
        for env_name in env_refs:
            if infra_env_vars and env_name not in infra_env_vars:
                mismatches.append({
                    "operation_id": op_id, "field": env_name,
                    "asset_type": "lambda_env",
                    "issue": f"Lambda references env var '{env_name}' but not found in infra env vars: {set(infra_env_vars.keys())}",
                })

    # D1-3: Lambda response structure (data wrapper) vs OpenAPI response schema
    if openapi_yaml:
        for op_id, code in lambda_code.items():
            lambda_has_data_wrapper = bool(re.search(r'["\']data["\']\s*:', code))
            openapi_op = openapi_fields.get(op_id) if openapi_fields else None
            if not openapi_op:
                kebab = "/" + op_id.replace("_", "-")
                openapi_op = openapi_fields.get(kebab) if openapi_fields else None
            if openapi_op:
                openapi_has_data = "data" in openapi_op.get("output", set())
                if lambda_has_data_wrapper != openapi_has_data:
                    mismatches.append({
                        "operation_id": op_id, "field": "data",
                        "asset_type": "response_structure",
                        "issue": f"Response structure mismatch: Lambda {'uses' if lambda_has_data_wrapper else 'omits'} data wrapper, "
                                 f"OpenAPI {'has' if openapi_has_data else 'omits'} data property",
                    })

    # D1-4: Operation/tool count verification
    # When tools are defined, check tool-level counts; otherwise fall back to operation-level
    tool_count = len(tool_expected)
    spec_count = max(len(expected), tool_count)  # Use tool count if multi-tool
    lambda_count = len(lambda_code)
    openapi_count = len(openapi_fields) if openapi_fields else 0

    if tool_count > len(expected):
        # Multi-tool mode: check tool-level counts
        if lambda_count > 0 and lambda_count < tool_count:
            missing = set(tool_expected.keys()) - set(lambda_code.keys())
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"Lambda count ({lambda_count}) < tool count ({tool_count}). Missing: {missing}",
            })
        if openapi_count > 0 and openapi_count < tool_count:
            missing_openapi = set(tool_expected.keys()) - set(openapi_fields.keys()) if openapi_fields else set()
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"OpenAPI path count ({openapi_count}) < tool count ({tool_count}). Missing: {missing_openapi}",
            })
    else:
        # Legacy mode: operation-level counts
        if lambda_count > 0 and lambda_count < len(expected):
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"Lambda count ({lambda_count}) < spec count ({len(expected)}). Missing: {set(expected.keys()) - set(lambda_code.keys())}",
            })
        if openapi_count > 0 and openapi_count < len(expected):
            missing_openapi = set(expected.keys()) - set(openapi_fields.keys()) if openapi_fields else set()
            mismatches.append({
                "operation_id": "__all__", "field": "",
                "asset_type": "count",
                "issue": f"OpenAPI path count ({openapi_count}) < spec count ({len(expected)}). Missing: {missing_openapi}",
            })

    # Additional: validate tool-level Lambda field consistency
    for tool_id, tool_fields in tool_expected.items():
        code = lambda_code.get(tool_id)
        if not code:
            continue
        lf = _extract_lambda_fields(code)
        for field in tool_fields["input"]:
            if field not in lf:
                mismatches.append({
                    "operation_id": tool_id, "field": field,
                    "asset_type": "lambda_tool",
                    "issue": f"ToolSpec input field '{field}' not found in Lambda handler for tool '{tool_id}'",
                })

    # D2: Lambda runtime AWS calls vs IAM role grants in the infrastructure template
    #     (catches AccessDeniedException-at-runtime before deployment; e.g. the
    #     update_q_session connect:DescribeContact omission found in workshop QA)
    if infra_yaml and lambda_all_code:
        fn_actions = _extract_role_actions_from_template(infra_yaml)
        # index CFN function grants by normalized name for fuzzy matching
        norm_grants = {re.sub(r'(function|lambda)$', '', k.lower()): v
                       for k, v in fn_actions.items()}
        for op_id, code in lambda_all_code.items():
            required = _extract_required_iam_actions(code)
            if not required:
                continue
            norm_op = op_id.replace("_", "").replace("-", "").lower()
            granted = None
            for nk, v in norm_grants.items():
                if norm_op in nk or nk in norm_op:
                    granted = v
                    break
            if granted is None:
                continue  # function not in template (deployed elsewhere) — skip
            missing = sorted(a for a in required if not _action_granted(a, granted))
            if missing:
                mismatches.append({
                    "operation_id": op_id, "field": "",
                    "asset_type": "iam_permissions",
                    "issue": f"Lambda '{op_id}' calls AWS APIs requiring {missing} but its "
                             f"IAM role in infrastructure.yaml does not grant them — this "
                             f"fails with AccessDeniedException at runtime. Add an inline "
                             f"policy with these actions to the function's role.",
                })

    # D3: RDS Data API contract (only when the Lambdas actually use rds-data)
    #     Catches, deterministically:
    #       a) env var name drift — Lambda reads DB_CLUSTER_ARN while the
    #          CloudFormation template exports RDS_CLUSTER_ARN → KeyError at
    #          import time on every invocation
    #       b) execute_statement without includeResultMetadata=True → the
    #          response carries no columnMetadata, so rows can only be read
    #          positionally and any schema change silently corrupts results
    #       c) positional row access (record[0]) instead of by column name
    #       d) SQL built by string concatenation/f-string → SQL injection
    _RDS_ENV_CANON = ("DB_CLUSTER_ARN", "DB_SECRET_ARN", "DB_NAME")
    _RDS_ENV_WRONG = {
        "RDS_CLUSTER_ARN": "DB_CLUSTER_ARN",
        "RDS_SECRET_ARN": "DB_SECRET_ARN",
        "RDS_DATABASE_NAME": "DB_NAME",
        "RDS_DB_NAME": "DB_NAME",
        "CLUSTER_ARN": "DB_CLUSTER_ARN",
        "SECRET_ARN": "DB_SECRET_ARN",
    }
    for op_id, code in lambda_all_code.items():
        if "rds-data" not in code and "rds_data" not in code:
            continue

        env_reads = set(re.findall(r'os\.environ(?:\.get)?[\[\(]\s*["\']([A-Z0-9_]+)["\']', code))

        for wrong, right in _RDS_ENV_WRONG.items():
            if wrong in env_reads and right not in env_reads:
                mismatches.append({
                    "operation_id": op_id, "field": wrong,
                    "asset_type": "rds_env_contract",
                    "issue": f"Lambda '{op_id}' reads os.environ['{wrong}'] but the RDS "
                             f"env var contract is '{right}'. Rename it in the handler and "
                             f"make sure infrastructure.yaml sets the same name, otherwise "
                             f"the function raises KeyError on cold start.",
                })

        if infra_yaml:
            for env_name in sorted(env_reads & set(_RDS_ENV_CANON)):
                if env_name not in infra_yaml:
                    mismatches.append({
                        "operation_id": op_id, "field": env_name,
                        "asset_type": "rds_env_contract",
                        "issue": f"Lambda '{op_id}' reads os.environ['{env_name}'] but "
                                 f"infrastructure.yaml never defines it — the function "
                                 f"fails with KeyError at runtime. Add it to the function's "
                                 f"Environment.Variables.",
                    })

        if "execute_statement" in code and "includeResultMetadata" not in code:
            mismatches.append({
                "operation_id": op_id, "field": "includeResultMetadata",
                "asset_type": "rds_data_api",
                "issue": f"Lambda '{op_id}' calls rds-data execute_statement without "
                         f"includeResultMetadata=True. The response then has no "
                         f"columnMetadata, so columns can only be read by position and "
                         f"results break on any schema change. Pass "
                         f"includeResultMetadata=True and map rows to dicts by column name.",
            })

        positional = (
            re.search(r'\[\s*["\']records["\']\s*\]\s*\[\s*\d+\s*\]', code)
            or re.search(r'\brecords\s*\[\s*\d+\s*\]\s*\[\s*\d+\s*\]', code)
            or re.search(r'\brecord\s*\[\s*\d+\s*\]', code)
            or re.search(r'\.get\(\s*["\']records["\']\s*[^)]*\)\s*\[\s*\d+\s*\]\s*\[\s*\d+\s*\]', code)
        )
        if positional:
            mismatches.append({
                "operation_id": op_id, "field": "",
                "asset_type": "rds_data_api",
                "issue": f"Lambda '{op_id}' reads Data API rows by position "
                         f"(record[0], records[0][1], …). Read columns by name using "
                         f"columnMetadata instead — positional access silently returns the "
                         f"wrong column when the table changes.",
            })

        # SQL text built with an f-string / concatenation / % / .format() instead
        # of Data API named parameters
        injection = (
            re.search(r'\bsql\s*=\s*f["\']', code)
            or re.search(r'\bsql\s*=\s*["\'][^"\']*["\']\s*(?:\+|%|\.format\()', code)
            or re.search(r'(?:execute_sql|execute_statement)\s*\(\s*f["\']', code)
            or re.search(r'(?:execute_sql|execute_statement)\s*\(\s*["\'][^"\']*["\']\s*(?:\+|%|\.format\()', code)
        )
        if injection:
            mismatches.append({
                "operation_id": op_id, "field": "",
                "asset_type": "rds_data_api",
                "issue": f"Lambda '{op_id}' builds SQL by string interpolation/concatenation "
                         f"— SQL injection risk. Use Data API named parameters "
                         f"(:param) with the parameters=[...] argument.",
            })

    # D4: SQL identifiers in generated handlers vs the scanned/provided schema
    if infra_schema and lambda_all_code:
        try:
            _schema = json.loads(infra_schema) if isinstance(infra_schema, str) else infra_schema
        except Exception:
            _schema = None
        if isinstance(_schema, dict):
            _col_index = _schema_column_index(_schema)
            _enum_types = _schema_enum_types(_schema)
            _type_index = _column_type_index(_schema)
            for op_id, code in lambda_all_code.items():
                mismatches.extend(_check_sql_identifiers(op_id, code, _col_index))
                mismatches.extend(_check_sql_casts(op_id, code, _enum_types))
                mismatches.extend(
                    _check_sql_insert_required_columns(op_id, code, _schema))
                mismatches.extend(
                    _check_sql_param_types(op_id, code, _type_index))

    # D5: Contact Flow → Lambda invoke permissions (Principal connect.amazonaws.com)
    #     Every Lambda the flow calls directly via InvokeLambdaFunction needs an
    #     AWS::Lambda::Permission with Principal connect.amazonaws.com — an
    #     apigateway.amazonaws.com permission does NOT cover it. Missing grants
    #     were found for different functions in two consecutive live sessions
    #     (log_call_result / get_monitoring_target / record_monitoring_result):
    #     the call fails with AccessDeniedException and the flow takes its error
    #     branch, silently dropping call logging / personalization.
    if contact_flow_json and infra_yaml:
        flow_lambda_refs = set(re.findall(
            r'\{\{\s*([A-Z0-9_]+?)_LAMBDA_ARN\s*\}\}', contact_flow_json))
        if flow_lambda_refs:
            # Map: normalized function token -> has connect permission
            connect_permitted: set = set()
            for pm in re.finditer(
                    r'^  (\w+):\n((?:^    .*\n?)+)', infra_yaml, re.M):
                body = pm.group(2)
                if 'AWS::Lambda::Permission' not in body:
                    continue
                if 'connect.amazonaws.com' not in body:
                    continue
                fn = re.search(
                    r'FunctionName:.*?(?:!GetAtt\s+(\w+)\.Arn|!Ref\s+(\w+)|"(\w+)")',
                    body)
                if fn:
                    name = fn.group(1) or fn.group(2) or fn.group(3) or ""
                    connect_permitted.add(
                        re.sub(r'(function|lambda)$', '', name.lower().replace("_", "").replace("-", "")))
            # All function logical ids present in the template (to skip
            # placeholders resolved outside this stack, e.g. deploy.sh-created)
            template_functions = {
                re.sub(r'(function|lambda)$', '', fm.group(1).lower().replace("_", "").replace("-", ""))
                for fm in re.finditer(
                    r'^  (\w+):\n(?:^    .*\n?)*?^    Type:\s*AWS::Lambda::Function',
                    infra_yaml, re.M)
            }
            for ref in sorted(flow_lambda_refs):
                norm = ref.lower().replace("_", "")
                if norm not in template_functions:
                    continue  # function not defined in this template — skip
                if norm not in connect_permitted:
                    mismatches.append({
                        "operation_id": ref.lower(), "field": "",
                        "asset_type": "connect_invoke_permission",
                        "issue": f"Contact Flow invokes Lambda '{{{{{ref}_LAMBDA_ARN}}}}' directly, "
                                 f"but infrastructure.yaml has no AWS::Lambda::Permission with "
                                 f"Principal connect.amazonaws.com for that function. The flow's "
                                 f"invoke fails with AccessDeniedException at call time (an "
                                 f"apigateway.amazonaws.com permission does not cover Connect). "
                                 f"Add a Permission resource with Principal connect.amazonaws.com "
                                 f"and SourceAccount !Ref AWS::AccountId.",
                    })

    # D6: Contact Flow ↔ Prompt session-attribute name contract
    #     The flow reads $.Lex.SessionAttributes.<name> that only the AI agent
    #     (per its prompt) can set. A name the prompt never mentions is a
    #     guaranteed empty value at runtime — found live as conversationSummary
    #     (flow) vs escalationSummary (prompt): agent-screen context always blank.
    if contact_flow_json and prompt_text:
        flow_session_attrs = set(re.findall(
            r'\$\.Lex\.SessionAttributes\.(\w+)', contact_flow_json))
        # 'Tool' is the canonical bot-result contract (validated separately by
        # the contact flow linter); skip it here.
        flow_session_attrs.discard("Tool")
        for attr in sorted(flow_session_attrs):
            if attr not in prompt_text:
                mismatches.append({
                    "operation_id": "__flow__", "field": attr,
                    "asset_type": "session_attribute_contract",
                    "issue": f"Contact Flow reads $.Lex.SessionAttributes.{attr} but the AI "
                             f"prompt never mentions '{attr}' — the bot will never set it, so "
                             f"the flow always reads an empty value. Either instruct the bot "
                             f"to set '{attr}' in the prompt, or rename the flow attribute to "
                             f"one the prompt already defines.",
                })

    # D6b: AI prompt import safety. A prompt patched after generation (the
    #     generator lints its own output, a workspace patch is not re-linted)
    #     can carry a variable CreateAIPrompt rejects — live: `{{$.channel}}`,
    #     "Prompt contains unknown variable", deploy finished with no AI agent.
    if prompt_text:
        try:
            from tools.asset_linters import _AI_PROMPT_KNOWN_VARIABLES as _KNOWN_PROMPT_VARIABLES
            from tools.asset_linters import lint_ai_prompt as _lint_prompt
            _prompt_lint = _lint_prompt(prompt_text)
            for _var in _prompt_lint.get("unknown_variables") or []:
                mismatches.append({
                    "operation_id": "__prompt__", "field": _var,
                    "asset_type": "prompt_variable",
                    "issue": f"AI prompt uses {{{{$.{_var}}}}}, which the Amazon Connect AI prompt API does not "
                             f"know (CreateAIPrompt rejects the whole prompt: 'Prompt contains unknown "
                             f"variable'). Known variables: {', '.join(sorted(_KNOWN_PROMPT_VARIABLES))}, plus "
                             f"$.Custom.<attribute> for a contact attribute the Contact Flow sets. Rewrite it "
                             f"(e.g. {{{{$.Custom.{_var}}}}} and set the attribute in the flow) or state the "
                             f"condition in words.",
                })
        except Exception as _exc:  # pragma: no cover - defensive
            logger.debug(f"[VALIDATE] prompt variable check skipped: {_exc}")

    # D7: update_q_session env-var contract
    #     The static handler throws on cold start without CONNECT_INSTANCE_ID /
    #     AI_ASSISTANT_ID env vars. deploy.sh backfills the VALUES, but the CFN
    #     template must declare the KEYS — omitted by the LLM in two consecutive
    #     live sessions (merge now injects them; this is the belt-and-braces check).
    if infra_yaml and 'UpdateQSessionFunction' in infra_yaml:
        qf = re.search(r'(^  UpdateQSessionFunction:\n(?:^(?:    |\n).*\n?)*)', infra_yaml, re.M)
        if qf:
            qblock = qf.group(1)
            for env_key in ("CONNECT_INSTANCE_ID", "AI_ASSISTANT_ID"):
                if env_key not in qblock:
                    mismatches.append({
                        "operation_id": "update_q_session", "field": env_key,
                        "asset_type": "lambda_env",
                        "issue": f"UpdateQSessionFunction is missing the '{env_key}' environment "
                                 f"variable — the handler throws on every invocation without it, "
                                 f"so customer data is never injected into the AI session. Add "
                                 f"'{env_key}: \"\"' under Environment.Variables (deploy.sh fills "
                                 f"the value after the Connect instance exists).",
                    })

    # D8: OpenAPI success response keyed under spec.success_status_code
    #     The openapi_generator has no deterministic builder (it free-writes
    #     YAML), and its prompt used to say nothing about success_status_code
    #     at all — it always assumed '200'. A create operation with
    #     success_status_code=201 (Lambda actually returns 201) then had no
    #     '201' response declared, so the gateway treated every successful
    #     call as an undeclared-status tool failure (found live).
    if openapi_yaml:
        try:
            _openapi_doc = yaml.safe_load(openapi_yaml)
        except Exception:
            _openapi_doc = None
        if isinstance(_openapi_doc, dict):
            _paths = _openapi_doc.get("paths", {}) or {}
            # op_id -> declared response status codes (as strings)
            _declared_codes: Dict[str, set] = {}
            for _path, _methods in _paths.items():
                if not isinstance(_methods, dict):
                    continue
                for _method, _details in _methods.items():
                    if _method.startswith("x-") or not isinstance(_details, dict):
                        continue
                    _op_id = _details.get("operationId", _path)
                    _codes = {str(c) for c in (_details.get("responses") or {}).keys()}
                    _declared_codes[_op_id] = _codes

            def _check_success_code(op_id: str, expected_code) -> None:
                codes = _declared_codes.get(op_id)
                if codes is None:
                    kebab = op_id.replace("_", "-")
                    codes = _declared_codes.get(kebab)
                if codes is None:
                    return  # operation not in this OpenAPI doc — other checks cover that
                if str(expected_code) not in codes:
                    mismatches.append({
                        "operation_id": op_id, "field": "success_status_code",
                        "asset_type": "openapi_status_code",
                        "issue": f"Spec declares success_status_code={expected_code} for '{op_id}' "
                                 f"but OpenAPI only declares response(s) {sorted(codes)}. A Lambda "
                                 f"returning HTTP {expected_code} on success has no matching schema, "
                                 f"so the gateway treats every successful call as an undeclared-"
                                 f"status tool failure. Add a '{expected_code}' response to "
                                 f"/tools/{op_id} in openapi.yaml.",
                    })

            for _op_id, _spec in specs.items():
                _expected = getattr(_spec, "success_status_code", None)
                if _expected is not None:
                    _check_success_code(_op_id, _expected)
            for _tool in all_tools:
                if isinstance(_tool, dict):
                    _t_id = _tool.get("tool_id") or ""
                    _t_code = _tool.get("success_status_code")
                else:
                    _t_id = getattr(_tool, "tool_id", "") or ""
                    _t_code = getattr(_tool, "success_status_code", None)
                if _t_id and _t_code is not None:
                    _check_success_code(_t_id, _t_code)

    # SAMPLE_DATA: rows the customer supplied must be seeded verbatim. The
    # requirements' test dialogs are written against these values (live: the
    # seeder "improved" a birth date and every identity check in the PoC
    # failed). Each scalar value of each supplied row must appear literally
    # in the merged template; the seeder is inline code, so a text search is
    # exact enough and independent of how the rows are encoded.
    if infra_yaml:
        try:
            from tools.spec_manager import get_infrastructure_spec as _get_infra_spec
            _ispec = _get_infra_spec()
            _ddb = getattr(_ispec, "dynamodb_config", None) if _ispec else None
            _sample_rows = getattr(_ddb, "sample_rows", None) if _ddb else None
        except Exception:
            _sample_rows = None
        if isinstance(_sample_rows, dict):
            for _table, _rows in _sample_rows.items():
                for _idx, _row in enumerate(_rows or []):
                    if not isinstance(_row, dict):
                        continue
                    _missing = [
                        f"{k}={v!r}" for k, v in _row.items()
                        if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                        and str(v).strip() and str(v) not in infra_yaml
                    ]
                    if _missing:
                        mismatches.append({
                            "operation_id": _table, "field": f"sample_rows[{_idx}]",
                            "asset_type": "infrastructure",
                            "issue": f"Customer-supplied sample row {_idx + 1} of table '{_table}' is not "
                                     f"seeded verbatim: {', '.join(_missing[:6])} not found in the "
                                     f"CloudFormation template. The requirements' test dialogs use these "
                                     f"exact values — seed the row as given instead of an invented one.",
                        })

    summary = f"Found {len(mismatches)} mismatches across {len(expected)} operations"
    if mismatches:
        summary += ". Fix by using patch_workspace_file for simple renames, or re-calling the affected generator with modification_request for structural changes."

    return {
        "success": len(mismatches) == 0,
        "mismatches": mismatches,
        "summary": summary,
        "operations_checked": len(expected),
    }


# ---------------------------------------------------------------------------
# D9 — ACXD runtime-target consistency gate
# ---------------------------------------------------------------------------
# D1–D8 intentionally stay fail-soft. D9 is the ACXD packaging gate: every
# returned entry uses a stable design-document id and an explicit error
# severity so packagers and reviewers can relay it without interpretation.
_D1_D8_IMPLEMENTATION = _validate_parameter_consistency_impl


def _d9_issue(check_id: str, message: str, **details: Any) -> dict:
    issue = {"id": check_id, "severity": "error", "message": message}
    issue.update({key: value for key, value in details.items() if value is not None})
    return issue


def _d9_violation_issue(check_id: str, violation: Any) -> dict:
    return _d9_issue(
        check_id,
        str(getattr(violation, "message", violation)),
        code=getattr(violation, "code", None),
        path=getattr(violation, "path", None),
        asset_type="acxd",
    )


def _d9_as_mismatch(issue: dict) -> dict:
    """Keep D9 in the established D1–D8 `mismatches` list shape."""
    return {
        "id": issue["id"],
        "severity": issue["severity"],
        "message": issue["message"],
        "operation_id": issue.get("operation_id", "__acxd__"),
        "field": issue.get("field", ""),
        "asset_type": issue.get("asset_type", "acxd"),
        "issue": issue["message"],
        **{
            key: value for key, value in issue.items()
            if key not in {"id", "severity", "message", "operation_id", "field", "asset_type"}
        },
    }


def _d9_doc(value: Any) -> Optional[dict]:
    """Parse a JSON/YAML mapping while accepting already-decoded fixtures."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        try:
            loaded = yaml.safe_load(value)
        except yaml.YAMLError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _load_d9_openapi_documents(session_id: str, bundle: dict) -> list[dict]:
    """Read OpenAPI documents from injected bundle data or the asset store."""
    candidates = []
    for key in ("openapi_documents", "openapi_docs", "openapi"):
        value = bundle.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, (dict, str)):
            candidates.append(value)
    documents = [doc for candidate in candidates if (doc := _d9_doc(candidate)) and doc.get("paths")]
    if documents:
        return documents

    for key in list_session_assets(session_id) if session_id else []:
        parts = key.split("/")
        if "openapi" not in parts or not key.lower().endswith((".yaml", ".yml", ".json")):
            continue
        content = get_asset_from_s3(key)
        doc = _d9_doc(content)
        if doc and doc.get("paths"):
            documents.append(doc)
    return documents


def _load_d9_faq_documents(session_id: str, bundle: dict) -> list[Any]:
    """Read ordinary Classic FAQ assets; ACXD KB articles are their rendering."""
    for key in ("faq_documents", "faq"):
        value = bundle.get(key)
        if isinstance(value, list):
            return value
        if value is not None:
            return [value]

    documents: list[Any] = []
    for asset_key in list_session_assets(session_id) if session_id else []:
        parts = asset_key.split("/")
        if "faq" not in parts:
            continue
        content = get_asset_from_s3(asset_key)
        if content:
            documents.append(content)
    return documents


def _d9_schema_properties(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    return set((schema.get("properties") or {}).keys())


def _d9_openapi_operations(documents: list[dict]) -> dict[str, dict]:
    """Index OpenAPI paths, resolving components and the Classic data wrapper."""
    operations: dict[str, dict] = {}

    def resolved(document: dict, schema: Any) -> dict:
        if not isinstance(schema, dict):
            return {}
        return _resolve_ref(document, schema["$ref"]) if "$ref" in schema else schema

    def request_properties(document: dict, schema: Any) -> set[str]:
        return _get_schema_props(document, schema) if isinstance(schema, dict) else set()

    def response_properties(document: dict, schema: Any) -> set[str]:
        root = resolved(document, schema)
        properties = root.get("properties") or {}
        # The Classic OpenAPI generator may wrap normal tool output under
        # `data`; ACXDGenerationContext intentionally unwraps the same shape.
        if isinstance(properties.get("data"), dict):
            return _get_schema_props(document, properties["data"])
        return set(properties)

    for doc in documents:
        for path, methods in (doc.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if not isinstance(operation, dict):
                    continue
                request_fields: set[str] = set()
                request_body = operation.get("requestBody") or {}
                for media in (request_body.get("content") or {}).values() if isinstance(request_body, dict) else []:
                    if isinstance(media, dict):
                        request_fields.update(request_properties(doc, media.get("schema")))
                response_fields: set[str] = set()
                for status, response in (operation.get("responses") or {}).items():
                    if not str(status).startswith("2") or not isinstance(response, dict):
                        continue
                    for media in (response.get("content") or {}).values():
                        if isinstance(media, dict):
                            response_fields.update(response_properties(doc, media.get("schema")))
                entry = {
                    "request": request_fields,
                    "response": response_fields,
                    "operation_id": operation.get("operationId"),
                }
                # Register every spelling a Data Request may legitimately use for
                # this operation. Live (4 sessions): the OpenAPI generator put the
                # /tools prefix in servers[0].url and left the path key bare
                # ("/check_balance"), or used kebab-case, while the Data Request
                # builder targets {WEBHOOK_URL}/tools/<operation_id>. Same
                # endpoint, so it must not be a D9-3 finding.
                for alias in _d9_path_aliases(doc, path):
                    operations.setdefault(alias, entry)
    return operations


def _d9_path_aliases(document: dict, path: str) -> list[str]:
    """Canonical spellings of an OpenAPI path: as written, with the servers[0].url
    path prefix folded in, and snake_case/kebab-case variants of the last segment."""
    aliases = [path]
    servers = document.get("servers") or []
    server_url = str((servers[0] or {}).get("url") or "") if servers and isinstance(servers[0], dict) else ""
    prefix = re.sub(r"^https?://[^/]+", "", server_url).rstrip("/")
    prefix = re.sub(r"\{[^}]*\}", "", prefix).rstrip("/")     # drop {stage}-style variables
    if prefix and prefix != "/" and not path.startswith(prefix + "/"):
        aliases.append(f"{prefix}{path}")
    if "/tools/" not in path and not path.startswith("/tools"):
        aliases.append(f"/tools{path}")
    for candidate in list(aliases):
        head, _, tail = candidate.rpartition("/")
        for variant in (tail.replace("-", "_"), re.sub(r"(?<!^)([A-Z])", r"_\1", tail).lower()):
            if variant and variant != tail:
                aliases.append(f"{head}/{variant}")
    return list(dict.fromkeys(aliases))


def _d9_get(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _d9_name_variants(name: Any) -> set[str]:
    raw = str(name or "")
    snake = re.sub(r"(?<!^)([A-Z])", r"_\1", raw).lower()
    camel = re.sub(r"_+([a-zA-Z0-9])", lambda m: m.group(1).upper(), raw)
    return {raw, raw.lower(), snake, camel, (camel[:1].lower() + camel[1:]) if camel else camel}


def _d9_field_index(operation: Any) -> dict[str, Any]:
    fields = list(_d9_get(operation, "input_fields", "inputFields", default=[]) or [])
    fields.extend(_d9_get(operation, "output_fields", "outputFields", default=[]) or [])
    index: dict[str, Any] = {}
    for field in fields:
        name = _d9_get(field, "name", "field_name", "fieldName")
        if not name:
            continue
        index.setdefault(name, field)
        # A flow slot `phonePin` for the FieldSpec `phone_pin` (or vice versa)
        # is the same field — live runs failed D9-4 on the spelling alone.
        for variant in _d9_name_variants(name):
            index.setdefault(variant, field)
    return index


def _is_spec_operation_folder(folder: str, operation_specs: Any) -> bool:
    """True when a Lambda folder name is one of the spec operations in any spelling."""
    try:
        names = set((operation_specs or {}).keys()) if hasattr(operation_specs, "keys") else set()
    except Exception:
        return False
    folder_l = str(folder or "").lower()
    for name in names:
        raw = str(name)
        snake = re.sub(r"(?<!^)([A-Z])", r"_\1", raw).lower()
        camel = re.sub(r"_+([a-zA-Z0-9])", lambda m: m.group(1).upper(), raw)
        if folder_l in {raw.lower(), snake, camel.lower(), snake.replace("_", "")}:
            return True
    return False


def _d9_regex_canonical(pattern: Any) -> str:
    """Canonical form for comparing two regexes that mean the same thing:
    `^\\d{8}$` (FieldSpec) vs `^[0-9]{8}$` (what the flow generator wrote), and
    `^010\\-\\d{4}$` vs `^010-[0-9]{4}$` — an escaped hyphen outside a character
    class is the literal hyphen (a live spec carried both spellings and D9-4
    blocked the bundle on the backslash alone)."""
    text = str(pattern or "").strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("\\\\d", "\\d").replace("\\d", "[0-9]")
    text = text.replace("[[:digit:]]", "[0-9]")
    text = _d9_unescape_literals(text)
    if text and not text.startswith("^"):
        text = "^" + text
    if text and not text.endswith("$"):
        text = text + "$"
    return text


# Punctuation that means the same escaped or not when it stands OUTSIDE a
# character class. `.`, `*`, `+`, `?`, `(`, `)`, `[`, `{`, `|`, `^`, `$`, `\\`
# change meaning and are deliberately absent.
_D9_LITERAL_PUNCT = set("-/:,;_@#%&=<>!~'\"` ")


def _d9_unescape_literals(text: str) -> str:
    out: list[str] = []
    in_class = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if not in_class and nxt in _D9_LITERAL_PUNCT:
                out.append(nxt)
            else:
                out.append(ch + nxt)
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        out.append(ch)
        i += 1
    return "".join(out)


def _d9_constraint_metadata(slot_type: dict) -> dict:
    metadata = slot_type.get("metadata") or {}
    constraints = metadata.get("constraints") if isinstance(metadata, dict) else None
    return constraints if isinstance(constraints, dict) else metadata if isinstance(metadata, dict) else {}


def _d9_article_has_question_and_answer(article: Any) -> bool:
    if not isinstance(article, dict):
        return False
    question = article.get("question") or article.get("title")
    if isinstance(question, dict):
        question = question.get("text")
    responses = article.get("responses") or []
    answer = article.get("answer")
    if not answer and isinstance(responses, list):
        answer = next((response.get("body") for response in responses if isinstance(response, dict)), None)
    return bool(str(question or "").strip() and str(answer or "").strip())


def _d9_faq_has_question_and_answer(document: Any) -> bool:
    if isinstance(document, dict):
        return bool(str(document.get("question") or "").strip() and str(document.get("answer") or "").strip())
    text = str(document or "")
    # Same parser the ACXD generation context uses to turn FAQ documents into
    # KB articles, so D9-5 and the generator agree on what a valid FAQ is
    # (the FAQ generator writes headings like "## 질문 (Question)").
    try:
        from .acxd_generation_context import _article_from_content
        return _article_from_content(text) is not None
    except Exception:
        question = re.search(r"(?:^|\n)##?\s*(?:질문|Question)[^\n]*\n+([^\n#]+)", text, re.IGNORECASE)
        answer = re.search(r"(?:^|\n)##?\s*(?:답변|Answer)[^\n]*\n+([^\n#]+)", text, re.IGNORECASE)
        return bool(question and answer and question.group(1).strip() and answer.group(1).strip())


def _d9_contact_flow_parts(document: Any) -> tuple[dict, Optional[dict], list[dict]]:
    outer = _d9_doc(document) or {}
    content = outer.get("content")
    if isinstance(content, str):
        content = _d9_doc(content)
    flow = content if isinstance(content, dict) else outer
    metadata = flow.get("Metadata") or flow.get("metadata") or {}
    binding = metadata.get("acxdBinding") if isinstance(metadata, dict) else None
    if not isinstance(binding, dict):
        binding = outer.get("acxdBinding")
    actions = [action for action in (flow.get("Actions") or flow.get("actions") or []) if isinstance(action, dict)]
    return flow, binding if isinstance(binding, dict) else None, actions


def _d9_binding_target(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("actionId") or value.get("identifier") or value.get("Identifier") or value.get("target")
    return None


def _d9_context_variable_names(bundle: dict, flow_spec: dict) -> set[str]:
    names: set[str] = set()
    application = flow_spec.get("application") or {}
    for value in application.get("context_variables") or application.get("contextVariables") or []:
        name = _d9_get(value, "name", "key")
        if name:
            names.add(str(name))
    for value in bundle.get("context_variables") or []:
        name = _d9_get(value, "name", "key", "variableName")
        if name:
            names.add(str(name))
    app_doc = bundle.get("application") or {}
    for value in app_doc.get("contextVariables") or app_doc.get("context_variables") or []:
        name = _d9_get(value, "name", "key", "variableName")
        if name:
            names.add(str(name))
    return names


def _d9_is_full_locale(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}", value):
        return False
    try:
        from tools.acxd_contract import LANGUAGE_CODES
        return value in LANGUAGE_CODES
    except Exception:
        return True


def _d9_non_ascii_metadata(document: Any, path: str = "") -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if isinstance(document, list):
        for index, value in enumerate(document):
            findings.extend(_d9_non_ascii_metadata(value, f"{path}[{index}]"))
        return findings
    if not isinstance(document, dict):
        return findings
    for key, value in document.items():
        item_path = f"{path}.{key}" if path else key
        if key in {"description", "aiDescription"} and isinstance(value, str):
            if any(ord(character) > 0x7F for character in value):
                findings.append((item_path, key))
        findings.extend(_d9_non_ascii_metadata(value, item_path))
    return findings


def _d9_structural_checks(bundle: dict, flow_spec: Optional[dict]) -> list[dict]:
    from tools.validate_acxd_consistency import validate_acxd_consistency

    issues: list[dict] = []
    flows = bundle.get("flows") or []
    if not flows:
        issues.append(_d9_issue("D9-1", "No ACXD flow assets were generated", asset_type="flow"))
    for violation in validate_acxd_consistency(bundle, spec=flow_spec):
        code = getattr(violation, "code", "")
        message = str(getattr(violation, "message", violation))
        lowered = message.lower()
        if code.startswith("DETERMINISM_"):
            check_id = "D9-2"
        elif code.startswith("CONTACT_FLOW"):
            check_id = "D9-6"
        elif code.startswith("DATA_REQUEST"):
            check_id = "D9-3"
        elif code.startswith("SLOT_"):
            check_id = "D9-4"
        elif code.startswith("KB_"):
            check_id = "D9-5"
        elif code == "ASCII_METADATA" or (
            code == "SCHEMA" and any(token in lowered for token in (
                "flowid", "nodeid", "description", "aidescription", "languagecode",
            ))
        ):
            # D9-7 performs the field-specific checks below; avoid reporting
            # its schema manifestation as a misleading D9-1 failure too.
            continue
        else:
            check_id = "D9-1"
        issues.append(_d9_violation_issue(check_id, violation))
    return issues


def _d9_data_request_checks(bundle: dict, session_id: str) -> list[dict]:
    issues: list[dict] = []
    documents = _load_d9_openapi_documents(session_id, bundle)
    operations = _d9_openapi_operations(documents)
    for data_request in bundle.get("data_requests") or []:
        if not isinstance(data_request, dict):
            continue
        webhook = data_request.get("webhook") or {}
        if webhook.get("implementation") != "external":
            continue
        request_id = data_request.get("dataRequestId") or "<unknown>"
        url = str(webhook.get("url") or "")
        match = re.search(r"/tools/([^/?#]+)", url)
        if not match:
            issues.append(_d9_issue(
                "D9-3", f"Data Request {request_id!r} external URL must contain /tools/<operation>",
                asset_type="data_request", field="webhook.url",
            ))
            continue
        path = f"/tools/{match.group(1)}"
        operation = operations.get(path)
        if operation is None:
            issues.append(_d9_issue(
                "D9-3", f"Data Request {request_id!r} targets {path}, but OpenAPI has no such path",
                asset_type="data_request", field="webhook.url", operation_id=match.group(1),
            ))
            continue
        expected_request = _d9_schema_properties(data_request.get("requestSchema"))
        expected_response = _d9_schema_properties(data_request.get("responseSchema"))
        if expected_request != operation["request"]:
            issues.append(_d9_issue(
                "D9-3", f"Data Request {request_id!r} request fields {sorted(expected_request)} do not match "
                f"OpenAPI {path} fields {sorted(operation['request'])}",
                asset_type="data_request", field="requestSchema", operation_id=match.group(1),
            ))
        if expected_response != operation["response"]:
            issues.append(_d9_issue(
                "D9-3", f"Data Request {request_id!r} response fields {sorted(expected_response)} do not match "
                f"OpenAPI {path} fields {sorted(operation['response'])}",
                asset_type="data_request", field="responseSchema", operation_id=match.group(1),
            ))
    return issues


_D9_SELF_VALIDATING_BUILTINS = frozenset({"NLX.Date", "NLX.Time", "NLX.Email", "NLX.Url", "NLX.Number"})
_D9_TIME_SHAPE = re.compile(r"^\^?(?:\\d|\[0-9\])\{1,2\}:(?:\\d|\[0-9\])\{2\}\$?$|^\^?(?:\\d|\[0-9\])\{2\}:(?:\\d|\[0-9\])\{2\}\$?$")


def _d9_open_value_slot_issues(bundle: dict, flow_plan: dict, slot: dict, field_name: str,
                               operation_id: Optional[str], expected_regex, expected_min,
                               expected_max) -> list[dict]:
    """D9-4 for a regex/length-only field: the generated flow must attach the slot
    as an NLX built-in that carries the constraint. A custom slot type here is
    the one-item menu the runtime auto-selects (live defect); a built-in with no
    regex accepts anything."""
    flow_id = flow_plan.get("flow_id") or flow_plan.get("flowId")
    generated = next((f for f in (bundle.get("flows") or [])
                      if isinstance(f, dict) and f.get("flowId") == flow_id), None)
    if generated is None:
        return []  # not generated yet — D9-1 reports missing flows
    slot_name = str(slot.get("name") or "")
    attached = next((s for s in (generated.get("slotTypes") or [])
                     if isinstance(s, dict) and s.get("name") == slot_name), None)
    if attached is None:
        for variant in _d9_name_variants(slot_name):
            attached = next((s for s in (generated.get("slotTypes") or [])
                             if isinstance(s, dict) and str(s.get("name")) in _d9_name_variants(variant)), None)
            if attached:
                break
    if attached is None:
        return [_d9_issue(
            "D9-4", f"Flow {flow_id!r} does not attach slot {slot_name!r} for constrained field "
            f"{field_name!r}", asset_type="flow", field=field_name, operation_id=operation_id)]
    slot_type = str(attached.get("type") or "")
    if not slot_type.startswith("NLX."):
        return [_d9_issue(
            "D9-4", f"Flow {flow_id!r} slot {slot_name!r} attaches custom slot type {slot_type!r} for an "
            f"open value; a custom slot type is a value set and a single sample deploys as a one-item "
            f"menu the runtime auto-selects. Attach an NLX built-in with regex instead",
            asset_type="flow", field=field_name, operation_id=operation_id)]
    if slot_type == "NLX.PhoneNumber":
        return []  # the built-in validates the shape itself
    if slot_type in _D9_SELF_VALIDATING_BUILTINS:
        # NLX.Date / NLX.Time / NLX.Email / NLX.Url / NLX.Number recognise the
        # value's shape themselves, and built-in values arrive without
        # separators, so a YYYY-MM-DD or HH:MM regex on them could never match
        # (live: '2026-09-18' rejected). The regex is the spec's transport
        # format, restored by the Lambda adapter, not a capture constraint.
        return []
    if expected_regex:
        if attached.get("regex") == "^[0-9]{3,4}$" and _D9_TIME_SHAPE.match(str(expected_regex).strip()):
            return []  # S9: a typed HH:MM reaches the slot as compact digits; the adapter restores it
        if _d9_regex_canonical(attached.get("regex")) != _d9_regex_canonical(expected_regex):
            return [_d9_issue(
                "D9-4", f"Flow {flow_id!r} slot {slot_name!r} regex {attached.get('regex')!r} does not match "
                f"OperationSpec {field_name!r} regex {expected_regex!r}", asset_type="flow",
                field=field_name, operation_id=operation_id)]
        return []
    if (expected_min is not None or expected_max is not None) and not attached.get("regex"):
        return [_d9_issue(
            "D9-4", f"Flow {flow_id!r} slot {slot_name!r} has no regex although OperationSpec "
            f"{field_name!r} constrains its length ({expected_min}-{expected_max})",
            asset_type="flow", field=field_name, operation_id=operation_id)]
    return []


def _d9_slot_type_checks(bundle: dict, flow_spec: Optional[dict]) -> list[dict]:
    if not flow_spec:
        return [_d9_issue("D9-4", "ACXDFlowSpec is missing; slot constraints cannot be verified", asset_type="slot_type")]
    slot_types = {
        slot_type.get("slotTypeId"): slot_type
        for slot_type in bundle.get("slot_types") or []
        if isinstance(slot_type, dict) and slot_type.get("slotTypeId")
    }
    operation_specs = get_all_specs() or {}
    issues: list[dict] = []
    for flow in flow_spec.get("flows") or []:
        if not isinstance(flow, dict) or flow.get("role", "operation") != "operation":
            continue
        operation_id = flow.get("operation_id")
        fields = _d9_field_index(operation_specs.get(operation_id))
        for slot in flow.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            field_name = slot.get("field_name") or slot.get("fieldName") or slot.get("name")
            field = fields.get(field_name)
            if field is None:
                for variant in _d9_name_variants(field_name) | _d9_name_variants(slot.get("name")):
                    if variant in fields:
                        field = fields[variant]
                        break
            if field is None:
                issues.append(_d9_issue(
                    "D9-4", f"Flow {flow.get('flow_id')!r} slot {slot.get('name')!r} maps to unknown "
                    f"OperationSpec field {field_name!r}", asset_type="slot_type", field=field_name,
                    operation_id=operation_id,
                ))
                continue
            expected_enum = list(_d9_get(field, "enum_values", "enum", "allowed_values", "options", default=[]) or [])
            expected_regex = _d9_get(field, "pattern", "regex")
            expected_min = _d9_get(field, "min_length", "minLength")
            expected_max = _d9_get(field, "max_length", "maxLength")
            if not any(value is not None and value != [] for value in (expected_enum, expected_regex, expected_min, expected_max)):
                continue
            if not expected_enum:
                # An OPEN value (regex / length only) is NOT a custom slot type: a
                # slot type is a value set, and one built from a single sample
                # deploys as a one-item menu the runtime auto-selects without
                # asking (live). The contract is an NLX built-in on the flow's
                # attached slot carrying the field's regex — check that instead.
                issues.extend(_d9_open_value_slot_issues(
                    bundle, flow, slot, field_name, operation_id,
                    expected_regex, expected_min, expected_max))
                continue
            # Task B derives a custom SlotType for every ENUM slot: named
            # after the slot when the plan only declared a built-in or generic
            # type ('text', 'enum', …), after the declared type otherwise. Resolve
            # the id with the builder's own rule so D9 validates the emitted
            # asset, not the plan's spelling.
            from .acxd_generation_context import slot_type_id_for
            type_id = slot_type_id_for(str(slot.get("name") or ""), str(slot.get("type") or ""))
            slot_type = slot_types.get(type_id)
            if slot_type is None:
                issues.append(_d9_issue(
                    "D9-4", f"Enumerated field {field_name!r} requires a generated custom slot type; "
                    f"{type_id or '<missing>'!r} is not available", asset_type="slot_type", field=field_name,
                    operation_id=operation_id,
                ))
                continue
            actual_enum = [entry.get("value") for entry in slot_type.get("values") or [] if isinstance(entry, dict)]
            metadata = _d9_constraint_metadata(slot_type)
            # The shipped asset is what deploys — read its constraint first and fall
            # back to the plan's slot spelling only when the asset carries none.
            actual_regex = metadata.get("regex") or metadata.get("pattern") or slot.get("regex")
            actual_min = metadata.get("minLength", metadata.get("min_length"))
            actual_max = metadata.get("maxLength", metadata.get("max_length"))
            if expected_enum and actual_enum != expected_enum:
                issues.append(_d9_issue(
                    "D9-4", f"Slot type {type_id!r} enum {actual_enum!r} does not match "
                    f"OperationSpec {field_name!r} enum {expected_enum!r}", asset_type="slot_type",
                    field=field_name, operation_id=operation_id,
                ))
            if expected_regex and _d9_regex_canonical(actual_regex) != _d9_regex_canonical(expected_regex):
                issues.append(_d9_issue(
                    "D9-4", f"Slot type {type_id!r} regex {actual_regex!r} does not match "
                    f"OperationSpec {field_name!r} regex {expected_regex!r}", asset_type="slot_type",
                    field=field_name, operation_id=operation_id,
                ))
            if expected_min is not None and actual_min != expected_min:
                issues.append(_d9_issue(
                    "D9-4", f"Slot type {type_id!r} minLength {actual_min!r} does not match "
                    f"OperationSpec {field_name!r} min_length {expected_min!r}", asset_type="slot_type",
                    field=field_name, operation_id=operation_id,
                ))
            if expected_max is not None and actual_max != expected_max:
                issues.append(_d9_issue(
                    "D9-4", f"Slot type {type_id!r} maxLength {actual_max!r} does not match "
                    f"OperationSpec {field_name!r} max_length {expected_max!r}", asset_type="slot_type",
                    field=field_name, operation_id=operation_id,
                ))
    return issues


def _d9_knowledge_base_checks(bundle: dict, flow_spec: Optional[dict], session_id: str) -> list[dict]:
    issues: list[dict] = []
    knowledge_bases = [kb for kb in bundle.get("knowledge_bases") or [] if isinstance(kb, dict)]
    spec_kb = (flow_spec or {}).get("knowledge_base") or {}
    planned = bool(spec_kb.get("name") or spec_kb.get("topics")) or any(
        bool(flow.get("uses_knowledge_base"))
        for flow in (flow_spec or {}).get("flows") or [] if isinstance(flow, dict)
    )
    if planned and not knowledge_bases:
        return [_d9_issue("D9-5", "ACXDFlowSpec requires a knowledge base but no KB asset was generated", asset_type="knowledge_base")]
    if not knowledge_bases:
        return issues
    for kb in knowledge_bases:
        articles = kb.get("articles") or []
        if not articles:
            issues.append(_d9_issue("D9-5", f"Knowledge base {kb.get('name')!r} has no articles", asset_type="knowledge_base"))
            continue
        for index, article in enumerate(articles):
            if not _d9_article_has_question_and_answer(article):
                issues.append(_d9_issue(
                    "D9-5", f"Knowledge base {kb.get('name')!r} article {index} needs a non-empty question and answer",
                    asset_type="knowledge_base",
                ))
    faq_documents = _load_d9_faq_documents(session_id, bundle)
    if not faq_documents:
        issues.append(_d9_issue("D9-5", "Knowledge-base articles require generated FAQ source documents", asset_type="faq"))
    else:
        for index, document in enumerate(faq_documents):
            if not _d9_faq_has_question_and_answer(document):
                issues.append(_d9_issue(
                    "D9-5", f"FAQ source document {index} needs a non-empty question and answer", asset_type="faq",
                ))
    return issues


def _d9_contact_flow_checks(bundle: dict, flow_spec: Optional[dict]) -> list[dict]:
    contact_flows = bundle.get("contact_flows") or []
    if not contact_flows:
        return [_d9_issue("D9-6", "ACXD runtime target requires a Contact Flow with an AgenticCX binding", asset_type="contact_flow")]
    issues: list[dict] = []
    context_names = _d9_context_variable_names(bundle, flow_spec or {})
    required_branches = ("Default", "Escalation", "Error", "IdleChatTimeout")
    for index, document in enumerate(contact_flows):
        flow, binding, actions = _d9_contact_flow_parts(document)
        action_ids = {action.get("Identifier") or action.get("identifier") for action in actions}
        action_ids.discard(None)
        agentic_actions = [action for action in actions if str(action.get("Identifier") or action.get("identifier") or "").startswith("AgenticCX")]
        if not agentic_actions:
            issues.append(_d9_issue("D9-6", "Contact Flow has no AgenticCX action", asset_type="contact_flow", path=f"contact_flows[{index}]"))
        if binding is None:
            issues.append(_d9_issue("D9-6", "Contact Flow has no Metadata.acxdBinding", asset_type="contact_flow", path=f"contact_flows[{index}]"))
            continue
        context_variables = binding.get("contextVariables") or []
        if len(context_variables) > 10:
            issues.append(_d9_issue("D9-6", f"Agentic CX binding has {len(context_variables)} context variables; maximum is 10", asset_type="contact_flow", path=f"contact_flows[{index}].Metadata.acxdBinding.contextVariables"))
        branches = binding.get("branches") or {}
        for branch in required_branches:
            target = _d9_binding_target(branches.get(branch))
            if not target:
                issues.append(_d9_issue("D9-6", f"Agentic CX binding is missing the {branch!r} branch", asset_type="contact_flow", path=f"contact_flows[{index}].Metadata.acxdBinding.branches"))
            elif target not in action_ids:
                issues.append(_d9_issue("D9-6", f"Agentic CX {branch!r} branch targets {target!r}, which is not an action Identifier", asset_type="contact_flow", path=f"contact_flows[{index}].Metadata.acxdBinding.branches.{branch}"))
        serialized = json.dumps(flow, ensure_ascii=False)
        for variable_name in sorted(set(re.findall(r"\$\.AgenticCX\.ContextVariables\.([A-Za-z_][A-Za-z0-9_]*)", serialized))):
            if variable_name not in context_names:
                issues.append(_d9_issue("D9-6", f"Contact Flow references $.AgenticCX.ContextVariables.{variable_name}, but application context variables do not define it", asset_type="contact_flow", field=variable_name))
        # $.AgenticCX.* is populated when the Agentic CX block RETURNS. An action
        # that can run before the caller reaches the block reads an empty value
        # (live: a Compare on ...isKnownCustomer and a greeting with
        # ...customerName sat before the block — the lookup attributes
        # $.Attributes.* are what those actions must read).
        for action_id, refs in _d9_agenticcx_refs_before_block(flow, actions).items():
            issues.append(_d9_issue(
                "D9-6", f"Action {action_id!r} runs before the Agentic CX block but reads "
                f"{', '.join(refs)} — undefined at that point; read the contact attribute "
                "($.Attributes.<name>) set before the block instead",
                asset_type="contact_flow", field=action_id,
            ))
        # Speech ownership: the application greets, converses and says goodbye.
        # The Contact Flow speaks only for telephony states (outside hours, queue
        # full, transfer error, app unavailable) and a recording/legal notice.
        for action_id, reason in _d9_owned_speech(flow, actions).items():
            issues.append(_d9_issue(
                "D9-6", f"Action {action_id!r} {reason} — the ACXD application owns that speech; "
                "remove the action (a recording/legal notice before the block is the one exception)",
                asset_type="contact_flow", field=action_id,
            ))
    return issues


_D9_NOTICE_HINTS = ("record", "notice", "consent", "legal", "녹음", "고지", "recorded", "録音")


def _d9_owned_speech(flow: dict, actions: list) -> dict[str, str]:
    """{identifier: reason} for speech the Contact Flow must not carry in the ACXD
    target: any MessageParticipant / GetParticipantInput reachable before the
    block (except a recording notice), and any MessageParticipant on the Default
    path (conversation finished → the app already closed)."""
    by_id: dict[str, dict] = {}
    for action in actions:
        if isinstance(action, dict):
            ident = action.get("Identifier") or action.get("identifier")
            if ident:
                by_id[str(ident)] = action
    start = str((flow or {}).get("StartAction") or "")
    if start not in by_id:
        return {}

    def _is_block(action: dict) -> bool:
        ident = str(action.get("Identifier") or action.get("identifier") or "")
        return ident.startswith("AgenticCX") or "AgentConfiguration" in (action.get("Parameters") or {})

    def _next(action: dict) -> list[str]:
        transitions = action.get("Transitions") or {}
        out = [transitions.get("NextAction")]
        out += [c.get("NextAction") for c in transitions.get("Conditions") or [] if isinstance(c, dict)]
        out += [e.get("NextAction") for e in transitions.get("Errors") or [] if isinstance(e, dict)]
        return [str(n) for n in out if n]

    out: dict[str, str] = {}
    seen: set[str] = set()
    queue = [start]
    block: Optional[dict] = None
    while queue:
        ident = queue.pop(0)
        if ident in seen or ident not in by_id:
            continue
        seen.add(ident)
        action = by_id[ident]
        if _is_block(action):
            block = action
            continue
        a_type = str(action.get("Type") or "")
        text = str((action.get("Parameters") or {}).get("Text") or "")
        if a_type == "GetParticipantInput":
            out[ident] = "collects input before the Agentic CX block"
        elif a_type == "MessageParticipant" and not (
                any(h in ident.lower() for h in _D9_NOTICE_HINTS) or any(h in text for h in _D9_NOTICE_HINTS)):
            out[ident] = "plays a message before the Agentic CX block"
        queue.extend(_next(action))
    if block is not None:
        ident = str((block.get("Transitions") or {}).get("NextAction") or "")
        path: set[str] = set()
        while ident in by_id and ident not in path:
            path.add(ident)
            action = by_id[ident]
            if str(action.get("Type") or "") == "MessageParticipant" and not ident.startswith("AgenticCX"):
                out[ident] = "plays a message on the Default (conversation finished) branch"
            ident = str((action.get("Transitions") or {}).get("NextAction") or "")
    return out


def _d9_agenticcx_refs_before_block(flow: dict, actions: list) -> dict[str, list[str]]:
    """Actions reachable from StartAction without passing the Agentic CX block
    that reference `$.AgenticCX.*`, as {identifier: [refs]}."""
    by_id: dict[str, dict] = {}
    for action in actions:
        if not isinstance(action, dict):
            continue
        ident = action.get("Identifier") or action.get("identifier")
        if ident:
            by_id[str(ident)] = action
    start = str((flow or {}).get("StartAction") or (flow or {}).get("startAction") or "")
    if start not in by_id:
        return {}

    def _is_block(action: dict) -> bool:
        ident = str(action.get("Identifier") or action.get("identifier") or "")
        return ident.startswith("AgenticCX") or "AgentConfiguration" in (action.get("Parameters") or {})

    def _next(action: dict) -> list[str]:
        transitions = action.get("Transitions") or {}
        out = [transitions.get("NextAction")]
        out += [c.get("NextAction") for c in transitions.get("Conditions") or [] if isinstance(c, dict)]
        out += [e.get("NextAction") for e in transitions.get("Errors") or [] if isinstance(e, dict)]
        return [str(n) for n in out if n]

    before: dict[str, list[str]] = {}
    seen: set[str] = set()
    queue = [start]
    while queue:
        ident = queue.pop(0)
        if ident in seen or ident not in by_id:
            continue
        seen.add(ident)
        action = by_id[ident]
        if _is_block(action):
            continue
        refs = sorted(set(re.findall(r"\$\.AgenticCX\.[A-Za-z_][A-Za-z0-9_.]*", json.dumps(action, ensure_ascii=False))))
        if refs:
            before[ident] = refs
        queue.extend(_next(action))
    return before


def _d9_identity_metadata_checks(bundle: dict, flow_spec: Optional[dict]) -> list[dict]:
    issues: list[dict] = []
    flow_id_pattern = re.compile(r"^[A-Za-z]{3,64}$")
    uuid_pattern = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    for flow_index, flow in enumerate(bundle.get("flows") or []):
        if not isinstance(flow, dict):
            continue
        flow_id = flow.get("flowId")
        if not isinstance(flow_id, str) or not flow_id_pattern.fullmatch(flow_id):
            issues.append(_d9_issue("D9-7", f"flowId {flow_id!r} must contain letters only and be 3–64 characters", asset_type="flow", path=f"flows[{flow_index}].flowId"))
        for node_id, node in (flow.get("nodes") or {}).items():
            if not isinstance(node_id, str) or not uuid_pattern.fullmatch(node_id):
                issues.append(_d9_issue("D9-7", f"Flow node id {node_id!r} must be a UUID", asset_type="flow", path=f"flows[{flow_index}].nodes"))
            if isinstance(node, dict) and node.get("nodeId") != node_id:
                issues.append(_d9_issue("D9-7", f"Flow nodeId {node.get('nodeId')!r} must match UUID map key {node_id!r}", asset_type="flow", path=f"flows[{flow_index}].nodes[{node_id!r}].nodeId"))
        flow_locales = []
        for key in ("mainLanguageCode", "languageCode"):
            if flow.get(key) is not None:
                flow_locales.append(flow[key])
        flow_locales.extend(flow.get("languageCodes") or [])
        if not flow_locales:
            issues.append(_d9_issue("D9-7", f"Flow {flow_id!r} must declare full locale codes", asset_type="flow", path=f"flows[{flow_index}]"))
        for locale in flow_locales:
            if not _d9_is_full_locale(locale):
                issues.append(_d9_issue("D9-7", f"Locale {locale!r} must be a supported full locale code such as 'en-US'", asset_type="flow", path=f"flows[{flow_index}]"))
    for family in ("flows", "slot_types", "data_requests", "guardrails", "knowledge_bases"):
        for path, key in _d9_non_ascii_metadata(bundle.get(family) or [], family):
            issues.append(_d9_issue("D9-7", f"{key} must be ASCII-only metadata", asset_type=family, path=path))
    if bundle.get("application") is None:
        issues.append(_d9_issue("D9-7", "ACXD application asset is missing; full locale metadata cannot be verified", asset_type="application"))
    else:
        for path, key in _d9_non_ascii_metadata(bundle["application"], "application"):
            issues.append(_d9_issue("D9-7", f"{key} must be ASCII-only metadata", asset_type="application", path=path))
        settings = bundle["application"].get("settings") or {}
        app_locales = []
        for key in ("languageCode",):
            if settings.get(key) is not None:
                app_locales.append(settings[key])
        app_locales.extend(settings.get("languageCodes") or [])
        app_locales.extend(entry.get("languageCode") for entry in settings.get("languageSettings") or [] if isinstance(entry, dict))
        if not app_locales:
            issues.append(_d9_issue("D9-7", "ACXD application must declare full locale codes", asset_type="application", path="application.settings"))
        for locale in app_locales:
            if not _d9_is_full_locale(locale):
                issues.append(_d9_issue("D9-7", f"Locale {locale!r} must be a supported full locale code such as 'en-US'", asset_type="application", path="application.settings"))
    application_spec = (flow_spec or {}).get("application") or {}
    for locale in list(application_spec.get("locales") or []) + ([application_spec["primary_locale"]] if application_spec.get("primary_locale") else []):
        if not _d9_is_full_locale(locale):
            issues.append(_d9_issue("D9-7", f"ACXDFlowSpec locale {locale!r} must be a supported full locale code", asset_type="application"))
    return issues


def _d9_external_backend_checks(bundle: dict, classic_mismatches: list[dict]) -> list[dict]:
    if not any(
        isinstance(data_request, dict)
        and (data_request.get("webhook") or {}).get("implementation") == "external"
        for data_request in bundle.get("data_requests") or []
    ):
        return []
    category_by_asset_type = {
        "iam_permissions": "D2",
        "rds_env_contract": "D3",
        "rds_data_api": "D3",
        "sql_param_type_mismatch": "D4",
        "sql_schema_mismatch": "D4",
        "sql_type_mismatch": "D4",
        "sql_missing_required_column": "D4",
        "connect_invoke_permission": "D5",
    }
    issues: list[dict] = []
    for mismatch in classic_mismatches:
        source_id = category_by_asset_type.get(mismatch.get("asset_type"))
        if source_id:
            issues.append(_d9_issue(
                "D9-8", f"{source_id} failure required by an external Data Request: {mismatch.get('issue', '')}",
                asset_type=mismatch.get("asset_type"), operation_id=mismatch.get("operation_id"),
                field=mismatch.get("field"), source_id=source_id,
            ))
    return issues


def _dedupe_d9_issues(issues: list[dict]) -> list[dict]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict] = []
    for issue in issues:
        key = (issue.get("id"), issue.get("message"), issue.get("path"), issue.get("field"))
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def run_d9_checks(session_id: str, *, classic_mismatches: Optional[list[dict]] = None) -> list[dict]:
    """Run D9-1…D9-8 for an ACXD runtime target.

    Returns only stable machine-readable findings:
    ``[{"id": "D9-1", "severity": "error", "message": "...", ...}]``.
    It is intentionally callable by the packager without invoking the reviewer.
    """
    if not is_acxd_target(session_id):
        return []
    try:
        bundle = load_acxd_bundle(session_id)
    except Exception as exc:
        return [_d9_issue("D9-1", f"Could not load ACXD asset bundle: {exc}", asset_type="bundle")]
    flow_spec_model = get_acxd_flow_spec(session_id)
    flow_spec = None
    if hasattr(flow_spec_model, "model_dump"):
        flow_spec = flow_spec_model.model_dump()
    elif isinstance(flow_spec_model, dict):
        flow_spec = flow_spec_model
    if flow_spec is None:
        return [_d9_issue("D9-1", "ACXDFlowSpec is missing for this ACXD session", asset_type="flow_spec")]

    if classic_mismatches is None:
        # Use the pre-D9 implementation to fold D2–D5 without recursion.
        try:
            legacy = _D1_D8_IMPLEMENTATION(session_id)
            classic_mismatches = list(legacy.get("mismatches") or [])
        except Exception:
            classic_mismatches = []

    issues: list[dict] = []
    issues.extend(_d9_structural_checks(bundle, flow_spec))       # D9-1 / D9-2 + retained cross-refs
    issues.extend(_d9_data_request_checks(bundle, session_id))    # D9-3
    issues.extend(_d9_slot_type_checks(bundle, flow_spec))        # D9-4
    issues.extend(_d9_knowledge_base_checks(bundle, flow_spec, session_id))  # D9-5
    issues.extend(_d9_contact_flow_checks(bundle, flow_spec))     # D9-6
    issues.extend(_d9_identity_metadata_checks(bundle, flow_spec))  # D9-7
    issues.extend(_d9_external_backend_checks(bundle, classic_mismatches))  # D9-8
    issues.extend(_d9_backend_auth_checks(bundle, session_id))    # D9-8 (auth)
    return _dedupe_d9_issues(issues)


def _d9_backend_auth_checks(bundle: dict, session_id: str) -> list[dict]:
    """The ACXD application calls the generated API Gateway directly — no
    AgentCore Gateway in front — so the API must require its key and every Data
    Request that calls it must send that key from a Secret. Live (2026-09-11):
    ten methods with ApiKeyRequired: false / AuthorizationType: NONE and Data
    Requests with `headers: []` — an anonymous public backend."""
    issues: list[dict] = []
    calls_backend = False
    for data_request in bundle.get("data_requests") or []:
        if not isinstance(data_request, dict):
            continue
        webhook = data_request.get("webhook") or {}
        if webhook.get("implementation") != "external":
            continue
        url = str(webhook.get("url") or "")
        if "{WEBHOOK_URL}" not in url:
            continue                      # a customer-supplied endpoint: its auth is theirs
        calls_backend = True
        headers = webhook.get("headers") or []
        # Two spellings reference a Secret: the live contract
        # `{Name:NLX.Secret}` (D1, 2026-09-13) and the legacy `{{secrets.Name}}`
        # an older session may still carry (repaired on bundle load).
        has_secret_header = any(
            isinstance(h, dict) and h.get("key")
            and re.search(r"\{[A-Za-z0-9_]+:NLX\.Secret\}|\{\{secrets\.", str(h.get("value") or ""))
            for h in headers)
        if not has_secret_header:
            issues.append(_d9_issue(
                "D9-8", f"Data Request {data_request.get('dataRequestId')!r} calls the generated API without an "
                "auth header — add x-api-key from the BackendApiKey secret (rebuild_acxd_slot_types_tool "
                "rebuilds the Data Requests)", asset_type="data_request",
                operation_id=data_request.get("dataRequestId"),
            ))
    if not calls_backend:
        return issues
    template = _d9_load_infrastructure_yaml(session_id)
    if not template:
        return issues
    for logical_id, http_method, required in _d9_api_methods(template):
        if http_method == "OPTIONS":
            continue
        if required != "true":
            issues.append(_d9_issue(
                "D9-8", f"API Gateway method {logical_id!r} ({http_method}) does not require the API key "
                "(ApiKeyRequired: true) — the ACXD Data Requests call it directly, so it must "
                "(re-run merge_infrastructure_fragments or patch the template)", asset_type="infrastructure",
                field=logical_id,
            ))
    return issues


def _d9_load_infrastructure_yaml(session_id: str) -> Optional[str]:
    try:
        from tools.s3_asset_storage import list_session_assets, get_asset_from_s3
        for key in list_session_assets(session_id) or []:
            parts = [p for p in str(key).split("/") if p]
            if len(parts) >= 4 and parts[2] in ("cloudformation", "infrastructure", "cdk") \
                    and parts[-1].endswith((".yaml", ".yml")):
                return get_asset_from_s3(key)
    except Exception as exc:  # pragma: no cover
        logger.debug("[D9-8] infrastructure template not readable: %s", exc)
    return None


def _d9_api_methods(template: str) -> list[tuple[str, str, str]]:
    """(logical id, HttpMethod, ApiKeyRequired as text) for each API Gateway method."""
    out: list[tuple[str, str, str]] = []
    lines = template.split("\n")
    for index, line in enumerate(lines):
        if not re.match(r"^\s+Type:\s*AWS::ApiGateway::Method\s*$", line):
            continue
        indent = len(line) - len(line.lstrip())
        logical_id = ""
        for back in range(index - 1, -1, -1):
            candidate = lines[back]
            if candidate.strip() and (len(candidate) - len(candidate.lstrip())) == indent - 2:
                logical_id = candidate.strip().rstrip(":")
                break
        http_method, required = "", "false"
        for forward in range(index + 1, len(lines)):
            nxt = lines[forward]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent - 2:
                break
            m = re.match(r"^\s+HttpMethod:\s*[\"']?(\w+)", nxt)
            if m:
                http_method = m.group(1).upper()
            k = re.match(r"^\s+ApiKeyRequired:\s*(\w+)", nxt)
            if k:
                required = k.group(1).lower()
        out.append((logical_id, http_method, required))
    return out


def _validate_parameter_consistency_impl(session_id: str) -> dict:
    """Preserve D1–D8 behavior and append D9 only for ACXD sessions."""
    result = _D1_D8_IMPLEMENTATION(session_id)
    try:
        acxd_target = is_acxd_target(session_id)
    except Exception:
        acxd_target = False
    mismatches = list(result.get("mismatches") or [])
    if acxd_target:
        mismatches.extend(_d9_as_mismatch(issue) for issue in run_d9_checks(
            session_id, classic_mismatches=mismatches,
        ))
    # An additive severity field preserves existing D1–D8 issue payloads while
    # letting packagers use one uniform severity filter for all D-series gates.
    for mismatch in mismatches:
        mismatch.setdefault("severity", "error")
    result["mismatches"] = mismatches
    result["success"] = not mismatches
    if acxd_target:
        result["summary"] = f"{result.get('summary', '')}; ACXD D9 found {sum(1 for issue in mismatches if issue.get('id', '').startswith('D9-'))} violation(s)"
    return result
