#!/usr/bin/env python3
"""Cross-asset consistency validator for AICC Builder skill output.

Ported from backend/ecs/src/tools/validate_consistency.py to operate on a LOCAL
output directory instead of S3, and to load specs from JSON files on disk
instead of the in-memory spec_manager bucket. The checks — and the failure modes
they were each written for — are the same ones the webapp runs after every
generation phase.

Directory layout expected (matches what the skill instructs Claude to write):

    <output_dir>/
      state/
        specs/
          <operation_id>.json    # one file per OperationSpec
        infrastructure_schema.json
        session_flow_config.json
      assets/v1/                 # plain assets/ is also accepted
        lambda/<tool_id>/index.py        # (handler.py also accepted)
        openapi/openapi.yaml
        infrastructure/template.yaml

Checks (check id → what it catches):

  spec ↔ generated asset
    lambda_input                 spec input field never read by the handler
    lambda_output                spec output field absent from the response body
    lambda_tool                  ToolSpec input field never read by its handler
    openapi_input/openapi_output spec field missing from the OpenAPI schema
    infra_pk                     spec primary_key is not a key on the infra table
    lambda_gsi                   IndexName= that is not a GSI in the schema
    lambda_env                   *_TABLE_NAME env var the schema never defines
    response_structure           `data` wrapper present on one side only
    count_lambda/count_openapi   fewer handlers/paths than specs (or tools)

  IAM (D2)
    iam_permissions              handler calls an AWS API its CFN role never grants

  RDS Data API contract (D3)
    rds_env_contract             DB_CLUSTER_ARN/DB_SECRET_ARN/DB_NAME drift
    rds_data_api                 missing includeResultMetadata, positional row
                                 access, SQL built by interpolation

  SQL vs the database schema (D4/D5)
    sql_schema_mismatch          column that the resolved table does not have
    sql_type_mismatch            ::cast to a type the schema never defines
    sql_missing_required_column  INSERT omits a NOT NULL column with no default
    sql_param_type_mismatch      :param bound with the wrong Data API value key

Usage:
    python validate_consistency.py <output_dir>
    python validate_consistency.py <output_dir> --json

Exits 0 if consistent, 1 if mismatches found. Prints a human-readable report.
Requires: PyYAML (pip install pyyaml).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Spec loading
# --------------------------------------------------------------------------

def _field_name(f: Any) -> Optional[str]:
    if isinstance(f, dict):
        return f.get("name")
    return None


def _field_set(fields: Any) -> Set[str]:
    if not isinstance(fields, list):
        return set()
    return {n for n in (_field_name(f) for f in fields) if n}


def load_specs(state_dir: Path) -> Dict[str, dict]:
    """Load all operation specs from state/specs/*.json."""
    specs_dir = state_dir / "specs"
    if not specs_dir.is_dir():
        return {}
    out = {}
    for p in specs_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(f"WARN: failed to parse {p}: {e}", file=sys.stderr)
            continue
        op_id = data.get("operation_id") or p.stem
        out[op_id] = data
    return out


def load_infra_schema(state_dir: Path) -> Optional[dict]:
    p = state_dir / "infrastructure_schema.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        print(f"WARN: infra schema parse failed: {e}", file=sys.stderr)
        return None


def collect_tools(specs: Dict[str, dict]) -> Dict[str, dict]:
    """Flatten ToolSpec entries across operations keyed by tool_id."""
    out = {}
    for op_id, spec in specs.items():
        tools = spec.get("tools") or []
        for t in tools:
            if isinstance(t, dict) and t.get("tool_id"):
                out[t["tool_id"]] = t
    return out


# --------------------------------------------------------------------------
# Asset extraction
# --------------------------------------------------------------------------

def assets_root(output_dir: Path) -> Path:
    """Resolve the asset root, preferring the newest ``assets/vN`` directory.

    The skill writes ``assets/v1/`` (and ``assets/v2/`` on regeneration) to match
    the webapp; a flat ``assets/`` tree is still accepted.
    """
    base = output_dir / "assets"
    versioned = sorted(
        (d for d in base.glob("v*") if d.is_dir() and re.fullmatch(r"v\d+", d.name)),
        key=lambda d: int(d.name[1:]),
    )
    return versioned[-1] if versioned else base


def _extract_lambda_fields(code: str) -> Set[str]:
    patterns = [
        r"""(?:body|event|params|data)\.get\(\s*['\"](\w+)['\"]""",
        r"""(?:body|event|params|data)\[['\"](\w+)['\"]\]""",
    ]
    fields: Set[str] = set()
    for p in patterns:
        fields.update(re.findall(p, code))
    return fields


# Lambdas that are bundled/flow-invoked rather than spec-driven API tools. They
# still take part in the IAM and SQL checks (the webapp's lambda_all_code), just
# not in the spec field comparison.
SUPPORTING_LAMBDAS = {"update_q_session", "customer_lookup"}


def load_lambda_code(assets_dir: Path) -> Dict[str, Dict[str, str]]:
    """Return {"spec": {tool_id: code}, "all": {tool_id: code}}.

    Accepts both the backend-canonical ``lambda/<tool_id>/index.py`` and the
    legacy ``lambda/<tool_id>/handler.py`` layout, plus ``index.js`` for the
    fixed Node.js Lambdas (IAM check only).
    """
    lam_dir = assets_dir / "lambda"
    if not lam_dir.is_dir():
        return {"spec": {}, "all": {}}
    spec_code: Dict[str, str] = {}
    all_code: Dict[str, str] = {}
    for fname in ("index.py", "handler.py", "index.js"):
        for handler in sorted(lam_dir.rglob(fname)):
            rel = handler.relative_to(lam_dir)
            op_id = rel.parts[0] if rel.parts else handler.stem
            try:
                content = handler.read_text()
            except Exception as e:
                print(f"WARN: failed to read {handler}: {e}", file=sys.stderr)
                continue
            all_code.setdefault(op_id, content)
            if op_id not in SUPPORTING_LAMBDAS and fname != "index.js":
                # index.py wins if both exist for the same tool.
                spec_code.setdefault(op_id, content)
    return {"spec": spec_code, "all": all_code}


def load_infra_template(assets_dir: Path) -> Optional[str]:
    """Raw text of the CloudFormation template (needed for the IAM/env checks)."""
    infra_dir = assets_dir / "infrastructure"
    if not infra_dir.is_dir():
        return None
    for p in sorted(infra_dir.glob("*.y*ml")):
        try:
            return p.read_text()
        except Exception as e:
            print(f"WARN: failed to read {p}: {e}", file=sys.stderr)
    return None


def _resolve_ref(spec: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if isinstance(node, dict):
            node = node.get(part, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _schema_props(spec: dict, schema: dict) -> Set[str]:
    if "$ref" in schema:
        return set(_resolve_ref(spec, schema["$ref"]).get("properties", {}).keys())
    return set(schema.get("properties", {}).keys())


def load_openapi(assets_dir: Path) -> Dict[str, Dict[str, Set[str]]]:
    """operationId -> {"input": set, "output": set}."""
    cand = list((assets_dir / "openapi").glob("*.y*ml")) if (assets_dir / "openapi").is_dir() else []
    if not cand:
        return {}
    try:
        spec = yaml.safe_load(cand[0].read_text())
    except Exception as e:
        print(f"WARN: openapi parse failed: {e}", file=sys.stderr)
        return {}
    out: Dict[str, Dict[str, Set[str]]] = {}
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method.startswith("x-") or not isinstance(details, dict):
                continue
            op_id = details.get("operationId") or path
            in_f: Set[str] = set()
            out_f: Set[str] = set()
            rb = details.get("requestBody") or {}
            for _, sw in (rb.get("content") or {}).items():
                if isinstance(sw, dict) and "schema" in sw:
                    in_f |= _schema_props(spec, sw["schema"])
            for code in ("200", "201", 200, 201):
                resp = (details.get("responses") or {}).get(code) or {}
                for _, sw in (resp.get("content") or {}).items():
                    if isinstance(sw, dict) and "schema" in sw:
                        out_f |= _schema_props(spec, sw["schema"])
            out[op_id] = {"input": in_f, "output": out_f}
    return out


# --------------------------------------------------------------------------
# D2: Lambda IAM permission check
#
# Root cause this guards against (live workshop QA): a generated Lambda calls an
# AWS API at runtime (e.g. update_q_session calling connect:DescribeContact +
# wisdom:UpdateSessionData) but the CFN role for that function only carries
# AWSLambdaBasicExecutionRole → AccessDeniedException mid-call.
# --------------------------------------------------------------------------

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


def _extract_required_iam_actions(code: str) -> Set[str]:
    """Derive IAM actions a Lambda handler needs from its AWS SDK calls.

    Handles Python boto3 (client/resource method calls) and JS SDK v3
    (``new XClient`` + ``XCommand``). Best-effort static analysis: unknown
    services fall back to using the SDK service name as the IAM prefix.
    """
    required: Set[str] = set()

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
    js_prefixes = {_JS_CLIENT_PREFIXES[c] for c in _JS_CLIENT_PREFIXES if c in code}
    if js_prefixes:
        commands = set(re.findall(r'new\s+(\w+)Command\(', code))
        for cmd in commands:
            if len(js_prefixes) == 1:
                required.add(f"{next(iter(js_prefixes))}:{cmd}")
                continue
            # Several clients imported — attribute each command to the import
            # block that names it.
            for m in re.finditer(
                    r'(?:const|import)\s*\{([^}]*)\}\s*'
                    r'(?:=\s*require\([\'"]@aws-sdk/client-([\w-]+)[\'"]\)'
                    r'|from\s+[\'"]@aws-sdk/client-([\w-]+)[\'"])',
                    code):
                names, pkg = m.group(1), (m.group(2) or m.group(3) or "")
                if f"{cmd}Command" in names:
                    svc = pkg.replace("qconnect", "wisdom")
                    prefix = _IAM_PREFIX_OVERRIDES.get(svc, svc)
                    required.add(f"{prefix}:{cmd}")
                    break

    return {a for a in required if a not in _IMPLICITLY_GRANTED}


def _extract_role_actions_from_template(infra_yaml: str) -> Dict[str, Set[str]]:
    """Map each Lambda function logical id to the IAM actions its role grants.

    Best-effort YAML parse tolerant of CFN intrinsics (!Sub/!Ref/!GetAtt).
    """
    try:
        sanitized = re.sub(
            r'!(Sub|Ref|GetAtt|Join|If|ImportValue|Select|FindInMap)\b', '', infra_yaml)
        doc = yaml.safe_load(sanitized) or {}
    except Exception as e:
        print(f"WARN: could not parse infrastructure template for IAM check: {e}",
              file=sys.stderr)
        return {}

    resources = doc.get("Resources", {}) or {}

    role_actions: Dict[str, Set[str]] = {}
    for rid, res in resources.items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::IAM::Role":
            continue
        actions: Set[str] = set()
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

    fn_actions: Dict[str, Set[str]] = {}
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


def _action_granted(action: str, granted: Set[str]) -> bool:
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


# --------------------------------------------------------------------------
# D4: SQL identifiers vs the scanned/provided schema
#
# Only reports a column when the owning table is unambiguous — either the
# reference is alias-qualified, or the statement touches exactly one known
# table. Columns the schema summary simply omitted are ignored, so it cannot
# false-positive on an incomplete column list.
# --------------------------------------------------------------------------
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


def _string_consts(tree) -> Dict[str, str]:
    consts: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    return consts


def _as_text(node, consts: Dict[str, str]) -> Optional[str]:
    """Best-effort static text of a string expression (handles implicit concat)."""
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
        left, right = _as_text(node.left, consts), _as_text(node.right, consts)
        if left is not None and right is not None:
            return left + right
    return None


def _extract_sql_statements(code: str) -> List[str]:
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

    consts = _string_consts(tree)
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in list(node.args) + [k.value for k in node.keywords if k.arg == "sql"]:
                text = _as_text(arg, consts)
                if text and _SQL_KEYWORD_RE.match(text):
                    found.append(text)
    for text in consts.values():
        if _SQL_KEYWORD_RE.match(text) and text not in found:
            found.append(text)
    return found


# --------------------------------------------------------------------------
# D5: Data API parameter type-binding check
#
# Root cause this guards against (found in live E2E): the handler bound a BIGINT
# key as a string —
#     parameters=[{"name": "orderId", "value": {"stringValue": str(order_id)}}]
# — and PostgreSQL rejected the query with
#     operator does not exist: bigint = text  (SQLState 42883)
# on the first invocation. MySQL silently coerces instead, which hides the bug
# but throws away index usage on the compared column.
# --------------------------------------------------------------------------
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


def _extract_sql_with_bindings(code: str) -> List[tuple]:
    """Return [(sql_text, {param_name: dataApiValueKey})] per SQL call site."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    consts = _string_consts(tree)

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
            text = _as_text(arg, consts)
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
    by_column: Dict[str, Set[str]] = {}
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


def _check_sql_param_types(op_id: str, code: str, type_index: Dict[tuple, str]) -> List[dict]:
    """Report Data API parameters bound with a type the column cannot accept."""
    issues: List[dict] = []
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
                "check": "sql_param_type_mismatch", "operation_id": op_id,
                "field": f":{param}",
                "issue": f"Lambda '{op_id}' binds `:{param}` as `{bound}` but "
                         f"`{(table + '.') if table else ''}{column}` is "
                         f"`{sql_type}`, which needs `{expected}`. PostgreSQL "
                         f"rejects this outright (e.g. 'operator does not exist: "
                         f"bigint = text', SQLState 42883); MySQL silently coerces "
                         f"and stops using the index. Bind it as {expected}.",
            })
    return issues


def _schema_column_index(schema: dict) -> Dict[str, Set[str]]:
    """table_name -> set(column names) from the infrastructure schema."""
    index: Dict[str, Set[str]] = {}
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    if isinstance(tables, dict):
        tables = list(tables.values())
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("table_name") or t.get("tableName")
        if not name:
            continue
        cols: Set[str] = set()
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


def _unqualified_columns(sql: str) -> List[str]:
    """Column identifiers used without a table qualifier.

    Read from the SELECT list, the INSERT column list, and the left-hand side of
    WHERE/AND/OR comparisons — never the whole statement, so SQL keywords,
    function names and literals are not mistaken for columns.
    """
    names: List[str] = []
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
    for ident in _SQL_PREDICATE_LHS_RE.findall(sql):
        ident = ident.strip('"`[]')
        if re.fullmatch(r'[A-Za-z_]\w*', ident) and ident.lower() not in _SQL_RESERVED:
            names.append(ident)
    return names


def _check_sql_identifiers(op_id: str, code: str, col_index: Dict[str, Set[str]]) -> List[dict]:
    """Report SQL columns that the table they are used against does not have.

    Covers both `alias.column` references and unqualified columns — the latter
    only when the statement touches exactly one known table, which makes the
    owning table unambiguous. Observed live: a handler selected `product_name`
    straight out of `order_items` (it lives on `products`) and inserted
    `created_at` into `returns` (that column is `requested_at`).
    """
    issues: List[dict] = []
    if not col_index:
        return issues
    owner: Dict[str, Set[str]] = {}
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
            "check": "sql_schema_mismatch", "operation_id": op_id, "field": shown,
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


def _check_sql_casts(op_id: str, code: str, enum_types: Set[str]) -> List[dict]:
    """Report ``::type`` casts to a type the schema does not define.

    Observed live: the generator correctly cast a real PostgreSQL enum
    (``:reason::return_reason``) and then invented the same pattern for two plain
    VARCHAR columns (``:approvalStatus::approval_status``). Postgres rejects
    those with ``type "approval_status" does not exist`` on the first write.
    """
    issues: List[dict] = []
    for sql in _extract_sql_statements(code):
        for raw in _SQL_CAST_RE.findall(sql):
            type_name = raw.strip().lower().rstrip("[]")
            base = type_name.replace("[]", "").strip()
            if not base or base in _PG_BUILTIN_TYPES or base in enum_types:
                continue
            if re.match(r'^(varchar|char|numeric|decimal|timestamp|time)\s*\(', base):
                continue
            issues.append({
                "check": "sql_type_mismatch", "operation_id": op_id,
                "field": f"::{raw.strip()}",
                "issue": f"Lambda '{op_id}' SQL casts to type `{raw.strip()}`, which is "
                         f"neither a built-in type nor one of the schema's enum types "
                         f"({sorted(enum_types) if enum_types else 'none'}). Postgres "
                         f"fails with 'type \"{raw.strip()}\" does not exist' on the first "
                         f"call. If that column is a plain VARCHAR, drop the cast.",
            })
    return issues


def _schema_enum_types(schema: dict) -> Set[str]:
    """Collect enum type names the schema declares."""
    names: Set[str] = set()
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


def _check_sql_insert_required_columns(op_id: str, code: str, schema: dict) -> List[dict]:
    """Report INSERTs that omit a NOT NULL column having no default.

    Observed live: the generated `create_return` INSERT listed every column
    except `returns.quantity`, which is NOT NULL with no default, so the first
    write failed with 'null value in column "quantity" violates not-null
    constraint'.
    """
    issues: List[dict] = []
    required: Dict[str, Set[str]] = {}
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
        needed: Set[str] = set()
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
                    "check": "sql_missing_required_column", "operation_id": op_id,
                    "field": ", ".join(missing),
                    "issue": f"Lambda '{op_id}' INSERTs into `{table}` without the NOT NULL "
                             f"column(s) {missing}, which have no default. The write fails "
                             f"with 'null value in column ... violates not-null "
                             f"constraint'. Add them to the INSERT column list and "
                             f"parameters.",
                })
    return issues


# --------------------------------------------------------------------------
# D3: RDS Data API contract
# --------------------------------------------------------------------------
_RDS_ENV_CANON = ("DB_CLUSTER_ARN", "DB_SECRET_ARN", "DB_NAME")
_RDS_ENV_WRONG = {
    "RDS_CLUSTER_ARN": "DB_CLUSTER_ARN",
    "RDS_SECRET_ARN": "DB_SECRET_ARN",
    "RDS_DATABASE_NAME": "DB_NAME",
    "RDS_DB_NAME": "DB_NAME",
    "CLUSTER_ARN": "DB_CLUSTER_ARN",
    "SECRET_ARN": "DB_SECRET_ARN",
}


def _check_rds_contract(op_id: str, code: str, infra_yaml: Optional[str]) -> List[dict]:
    """Deterministic RDS Data API checks; only runs on rds-data handlers.

    Catches: env var name drift (KeyError on cold start), execute_statement
    without includeResultMetadata=True (rows readable only by position),
    positional row access, and SQL built by string interpolation.
    """
    issues: List[dict] = []
    if "rds-data" not in code and "rds_data" not in code:
        return issues

    env_reads = set(re.findall(
        r'os\.environ(?:\.get)?[\[\(]\s*["\']([A-Z0-9_]+)["\']', code))

    for wrong, right in _RDS_ENV_WRONG.items():
        if wrong in env_reads and right not in env_reads:
            issues.append({
                "check": "rds_env_contract", "operation_id": op_id, "field": wrong,
                "issue": f"Lambda '{op_id}' reads os.environ['{wrong}'] but the RDS "
                         f"env var contract is '{right}'. Rename it in the handler and "
                         f"make sure infrastructure.yaml sets the same name, otherwise "
                         f"the function raises KeyError on cold start.",
            })

    if infra_yaml:
        for env_name in sorted(env_reads & set(_RDS_ENV_CANON)):
            if env_name not in infra_yaml:
                issues.append({
                    "check": "rds_env_contract", "operation_id": op_id, "field": env_name,
                    "issue": f"Lambda '{op_id}' reads os.environ['{env_name}'] but "
                             f"infrastructure.yaml never defines it — the function "
                             f"fails with KeyError at runtime. Add it to the function's "
                             f"Environment.Variables.",
                })

    if "execute_statement" in code and "includeResultMetadata" not in code:
        issues.append({
            "check": "rds_data_api", "operation_id": op_id, "field": "includeResultMetadata",
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
        issues.append({
            "check": "rds_data_api", "operation_id": op_id, "field": "",
            "issue": f"Lambda '{op_id}' reads Data API rows by position "
                     f"(record[0], records[0][1], …). Read columns by name using "
                     f"columnMetadata instead — positional access silently returns the "
                     f"wrong column when the table changes.",
        })

    injection = (
        re.search(r'\bsql\s*=\s*f["\']', code)
        or re.search(r'\bsql\s*=\s*["\'][^"\']*["\']\s*(?:\+|%|\.format\()', code)
        or re.search(r'(?:execute_sql|execute_statement)\s*\(\s*f["\']', code)
        or re.search(r'(?:execute_sql|execute_statement)\s*\(\s*["\'][^"\']*["\']\s*(?:\+|%|\.format\()', code)
    )
    if injection:
        issues.append({
            "check": "rds_data_api", "operation_id": op_id, "field": "",
            "issue": f"Lambda '{op_id}' builds SQL by string interpolation/concatenation "
                     f"— SQL injection risk. Use Data API named parameters "
                     f"(:param) with the parameters=[...] argument.",
        })
    return issues


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def validate(output_dir: Path) -> List[dict]:
    state = output_dir / "state"
    assets = assets_root(output_dir)
    specs = load_specs(state)
    infra = load_infra_schema(state)
    code_sets = load_lambda_code(assets)
    lam = code_sets["spec"]          # spec-driven handlers only
    lam_all = code_sets["all"]       # + update_q_session / customer_lookup
    infra_yaml = load_infra_template(assets)
    oapi = load_openapi(assets)
    tools = collect_tools(specs)

    if not specs:
        print("WARN: no specs found under state/specs/", file=sys.stderr)

    mismatches: List[dict] = []
    expected = {
        op_id: {
            "input": _field_set(s.get("input_fields", [])),
            "output": _field_set(s.get("output_fields", [])),
        }
        for op_id, s in specs.items()
    }
    tool_expected = {
        tool_id: {
            "input": _field_set(t.get("input_fields", [])),
            "output": _field_set(t.get("output_fields", [])),
        }
        for tool_id, t in tools.items()
    }

    # 1) Lambda vs spec
    for op_id, fields in expected.items():
        code = lam.get(op_id)
        if not code:
            continue
        lf = _extract_lambda_fields(code)
        resp_keys = set(re.findall(r"""['\"](\w+)['\"]\s*:""", code))
        for name in fields["input"]:
            if name not in lf:
                mismatches.append({
                    "check": "lambda_input", "operation_id": op_id, "field": name,
                    "issue": f"Spec input '{name}' not read in Lambda handler",
                })
        for name in fields["output"]:
            if name not in resp_keys:
                mismatches.append({
                    "check": "lambda_output", "operation_id": op_id, "field": name,
                    "issue": f"Spec output '{name}' not present in Lambda response body",
                })

    # 2) OpenAPI vs spec (with kebab-case fallback)
    for op_id, fields in expected.items():
        matched = oapi.get(op_id) or oapi.get("/" + op_id.replace("_", "-"))
        if not matched:
            continue
        for name in fields["input"]:
            if name not in matched["input"]:
                mismatches.append({
                    "check": "openapi_input", "operation_id": op_id, "field": name,
                    "issue": f"Spec input '{name}' missing from OpenAPI requestBody",
                })
        for name in fields["output"]:
            if name not in matched["output"]:
                mismatches.append({
                    "check": "openapi_output", "operation_id": op_id, "field": name,
                    "issue": f"Spec output '{name}' missing from OpenAPI response schema",
                })

    # Collect infra GSI names + env-var → table mappings for checks 6 & 7.
    infra_gsi_names: Set[str] = set()        # all GSI index names across all tables
    infra_env_vars: Set[str] = set()         # all *_TABLE_NAME env var names

    # 3) Infrastructure keys vs spec.data_source
    if infra:
        tables = infra.get("tables") or infra.get("dynamodb_tables") or {}
        if isinstance(tables, list):
            tables = {t.get("table_name") or t.get("tableName") or t.get("name"): t
                      for t in tables}
        for tdef in tables.values():
            if not isinstance(tdef, dict):
                continue
            for gsi in (tdef.get("gsi") or tdef.get("global_secondary_indexes") or tdef.get("gsi_indexes") or []):
                if isinstance(gsi, dict):
                    gname = gsi.get("index_name") or gsi.get("indexName") or gsi.get("name")
                    if gname:
                        infra_gsi_names.add(gname)
            env_name = tdef.get("env_var_name") or tdef.get("envVarName") or tdef.get("env_var")
            if env_name:
                infra_env_vars.add(env_name)
        for op_id, spec in specs.items():
            ds = spec.get("data_source") or {}
            tname = ds.get("table_name") or ds.get("tableName")
            if not tname or tname not in tables:
                continue
            tdef = tables[tname]
            infra_keys: Set[str] = set()
            for k in (tdef.get("keys") or tdef.get("key_schema") or []):
                if isinstance(k, dict):
                    infra_keys.add(k.get("attribute_name") or k.get("attributeName") or k.get("name", ""))
                elif isinstance(k, str):
                    infra_keys.add(k)
            for gsi in (tdef.get("gsi") or tdef.get("global_secondary_indexes") or tdef.get("gsi_indexes") or []):
                for k in (gsi.get("keys") or gsi.get("key_schema") or []):
                    if isinstance(k, dict):
                        infra_keys.add(k.get("attribute_name") or k.get("attributeName") or k.get("name", ""))
            pk = ds.get("primary_key") or ds.get("primaryKey") or ds.get("partition_key")
            if pk and infra_keys and pk not in infra_keys:
                mismatches.append({
                    "check": "infra_pk", "operation_id": op_id, "field": pk,
                    "issue": f"Spec primary_key '{pk}' not in infra keys for table '{tname}': {sorted(infra_keys)}",
                })

    # 6) Lambda IndexName=/IndexName: vs infra GSI names
    if infra_gsi_names:
        for op_id, code in lam.items():
            for idx_name in set(re.findall(r"IndexName\s*[=:]\s*['\"](\w[\w-]*)['\"]", code)):
                if idx_name not in infra_gsi_names:
                    mismatches.append({
                        "check": "lambda_gsi", "operation_id": op_id, "field": idx_name,
                        "issue": f"Lambda uses IndexName='{idx_name}' but it is not an infra GSI: {sorted(infra_gsi_names)}",
                    })

    # 7) Lambda os.environ["X_TABLE_NAME"] vs infra env vars
    if infra_env_vars:
        for op_id, code in lam.items():
            env_refs = set(re.findall(r'os\.environ\s*\[\s*[\'"](\w+_TABLE_NAME)[\'"]\s*\]', code))
            env_refs.update(re.findall(r'os\.environ\.get\s*\(\s*[\'"](\w+_TABLE_NAME)[\'"]', code))
            for env_name in env_refs:
                if env_name not in infra_env_vars:
                    mismatches.append({
                        "check": "lambda_env", "operation_id": op_id, "field": env_name,
                        "issue": f"Lambda references env var '{env_name}' but it is not an infra env var: {sorted(infra_env_vars)}",
                    })

    # 8) Lambda response wrapper (data vs flat) vs OpenAPI response shape
    for op_id, code in lam.items():
        matched = oapi.get(op_id) or oapi.get("/" + op_id.replace("_", "-"))
        if not matched:
            continue
        lambda_has_data = bool(re.search(r'''['"]data['"]\s*:''', code))
        openapi_has_data = "data" in matched.get("output", set())
        if lambda_has_data != openapi_has_data:
            mismatches.append({
                "check": "response_structure", "operation_id": op_id, "field": "data",
                "issue": (
                    f"Response wrapper mismatch: Lambda {'uses' if lambda_has_data else 'omits'} a "
                    f"'data' wrapper but OpenAPI {'has' if openapi_has_data else 'omits'} a 'data' property"
                ),
            })

    # 9) Count parity — tool-level when the specs declare more tools than
    #    operations (multi-tool operations), operation-level otherwise.
    target = tool_expected if len(tool_expected) > len(expected) else expected
    label = "tool" if target is tool_expected else "spec"
    if lam and len(lam) < len(target):
        mismatches.append({
            "check": "count_lambda", "operation_id": "__all__", "field": "",
            "issue": f"Lambda count ({len(lam)}) < {label} count ({len(target)}). Missing: {sorted(set(target) - set(lam))}",
        })
    if oapi and len(oapi) < len(target):
        mismatches.append({
            "check": "count_openapi", "operation_id": "__all__", "field": "",
            "issue": f"OpenAPI path count ({len(oapi)}) < {label} count ({len(target)}). Missing: {sorted(set(target) - set(oapi))}",
        })

    # 10) ToolSpec-level Lambda field consistency (one handler per tool_id)
    for tool_id, fields in tool_expected.items():
        code = lam.get(tool_id)
        if not code:
            continue
        lf = _extract_lambda_fields(code)
        for name in fields["input"]:
            if name not in lf:
                mismatches.append({
                    "check": "lambda_tool", "operation_id": tool_id, "field": name,
                    "issue": f"ToolSpec input field '{name}' not found in Lambda handler for tool '{tool_id}'",
                })

    # D2) Lambda runtime AWS calls vs IAM role grants in the CFN template
    if infra_yaml and lam_all:
        fn_actions = _extract_role_actions_from_template(infra_yaml)
        # index CFN function grants by normalized name for fuzzy matching
        norm_grants = {re.sub(r'(function|lambda)$', '', k.lower()): v
                       for k, v in fn_actions.items()}
        for op_id, code in lam_all.items():
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
                    "check": "iam_permissions", "operation_id": op_id, "field": "",
                    "issue": f"Lambda '{op_id}' calls AWS APIs requiring {missing} but its "
                             f"IAM role in the CloudFormation template does not grant them "
                             f"— this fails with AccessDeniedException at runtime. Add an "
                             f"inline policy with these actions to the function's role.",
                })

    # D3) RDS Data API contract (only for handlers that use rds-data)
    for op_id, code in lam_all.items():
        mismatches.extend(_check_rds_contract(op_id, code, infra_yaml))

    # D4/D5) SQL vs the scanned/provided database schema
    if infra and lam_all:
        col_index = _schema_column_index(infra)
        enum_types = _schema_enum_types(infra)
        type_index = _column_type_index(infra)
        for op_id, code in lam_all.items():
            mismatches.extend(_check_sql_identifiers(op_id, code, col_index))
            mismatches.extend(_check_sql_casts(op_id, code, enum_types))
            mismatches.extend(_check_sql_insert_required_columns(op_id, code, infra))
            mismatches.extend(_check_sql_param_types(op_id, code, type_index))

    return mismatches


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv[1:]
    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <output_dir> [--json]", file=sys.stderr)
        return 2
    out_dir = Path(args[0]).resolve()
    if not out_dir.is_dir():
        print(f"ERROR: not a directory: {out_dir}", file=sys.stderr)
        return 2
    mismatches = validate(out_dir)
    if as_json:
        print(json.dumps({"success": not mismatches, "mismatches": mismatches}, indent=2))
        return 1 if mismatches else 0
    if not mismatches:
        print(f"OK — no consistency issues found in {out_dir}")
        return 0
    print(f"FAIL — {len(mismatches)} consistency issue(s) found in {out_dir}:\n")
    for m in mismatches:
        print(f"  [{m['check']:<27}] {m['operation_id']}.{m['field']}: {m['issue']}")
    print("\nFix simple renames by patching the file; for structural drift re-run the "
          "affected generator with a modification_request.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
