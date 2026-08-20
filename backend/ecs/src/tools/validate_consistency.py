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
        elif asset_type == "infrastructure" and (key.endswith(".yaml") or key.endswith(".yml")):
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

    summary = f"Found {len(mismatches)} mismatches across {len(expected)} operations"
    if mismatches:
        summary += ". Fix by using patch_workspace_file for simple renames, or re-calling the affected generator with modification_request for structural changes."

    return {
        "success": len(mismatches) == 0,
        "mismatches": mismatches,
        "summary": summary,
        "operations_checked": len(expected),
    }
