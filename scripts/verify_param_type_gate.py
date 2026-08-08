#!/usr/bin/env python3
"""
Verification for the Data API parameter type-binding gate
(validate_consistency._check_sql_param_types).

Runs three things:
  A. the gate against synthetic handlers covering every binding combination
  B. the gate against the handlers the generator actually produced
  C. the same SQL executed on a live Aurora PostgreSQL cluster, to confirm the
     database really does reject what the gate flags and accept what it passes

Usage:
    python3 verify_param_type_gate.py                 # A + B
    python3 verify_param_type_gate.py --live          # A + B + C (needs AWS creds)
"""
import argparse
import ast
import glob
import json
import pathlib
import re
import sys
from typing import Dict, Optional

REPO = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR = REPO / "backend/ecs/src/tools/validate_consistency.py"


def load_gate():
    """Exec just the helper block out of validate_consistency (no heavy imports)."""
    src = VALIDATOR.read_text()
    start = src.index("_SQL_KEYWORD_RE = re.compile(")
    end = src.index("@tool")
    ns = {"re": re, "ast": ast, "json": json, "Dict": Dict, "Optional": Optional}
    exec(src[start:end], ns)
    return ns


# --------------------------------------------------------------------------- A
# A schema mirroring the shapes that matter: bigint key, varchar, numeric,
# boolean, date, and a PostgreSQL enum.
SYNTHETIC_SCHEMA = {
    "tables": [{
        "name": "orders",
        "columns": [
            {"name": "order_id", "sql_type": "bigint"},
            {"name": "order_number", "sql_type": "character varying"},
            {"name": "total_amount", "sql_type": "numeric"},
            {"name": "is_paid", "sql_type": "boolean"},
            {"name": "placed_at", "sql_type": "timestamp with time zone"},
            {"name": "status", "sql_type": "order_status"},
        ],
    }],
    "enum_types": {"order_status": ["PENDING", "PAID"]},
}

CASES = [
    # (name, should_flag, code)
    ("bigint bound as stringValue (the live production failure)", True, '''
def q(order_id):
    return execute_sql(
        "SELECT line_number FROM orders WHERE order_id = :orderId",
        parameters=[{"name": "orderId", "value": {"stringValue": str(order_id)}}],
    )
'''),
    ("bigint bound as longValue", False, '''
def q(order_id):
    return execute_sql(
        "SELECT line_number FROM orders WHERE order_id = :orderId",
        parameters=[{"name": "orderId", "value": {"longValue": int(order_id)}}],
    )
'''),
    ("varchar bound as stringValue", False, '''
def q(n):
    return execute_sql(
        "SELECT order_id FROM orders WHERE order_number = :orderNumber",
        parameters=[{"name": "orderNumber", "value": {"stringValue": n}}],
    )
'''),
    ("varchar bound as longValue", True, '''
def q(n):
    return execute_sql(
        "SELECT order_id FROM orders WHERE order_number = :orderNumber",
        parameters=[{"name": "orderNumber", "value": {"longValue": n}}],
    )
'''),
    ("boolean bound as stringValue", True, '''
def q(p):
    return execute_sql(
        "SELECT order_id FROM orders WHERE is_paid = :isPaid",
        parameters=[{"name": "isPaid", "value": {"stringValue": p}}],
    )
'''),
    ("boolean bound as booleanValue", False, '''
def q(p):
    return execute_sql(
        "SELECT order_id FROM orders WHERE is_paid = :isPaid",
        parameters=[{"name": "isPaid", "value": {"booleanValue": bool(p)}}],
    )
'''),
    ("numeric bound as stringValue (valid for the Data API)", False, '''
def q(a):
    return execute_sql(
        "SELECT order_id FROM orders WHERE total_amount = :totalAmount",
        parameters=[{"name": "totalAmount", "value": {"stringValue": str(a)}}],
    )
'''),
    ("timestamptz bound as stringValue (ISO string is correct)", False, '''
def q(t):
    return execute_sql(
        "SELECT order_id FROM orders WHERE placed_at = :placedAt",
        parameters=[{"name": "placedAt", "value": {"stringValue": t}}],
    )
'''),
    ("PostgreSQL enum bound as stringValue", False, '''
def q(s):
    return execute_sql(
        "SELECT order_id FROM orders WHERE status = :status",
        parameters=[{"name": "status", "value": {"stringValue": s}}],
    )
'''),
    ("INSERT: bigint column fed a stringValue", True, '''
def q(oid, num):
    return execute_sql(
        "INSERT INTO orders (order_id, order_number) VALUES (:orderId, :orderNumber)",
        parameters=[
            {"name": "orderId", "value": {"stringValue": str(oid)}},
            {"name": "orderNumber", "value": {"stringValue": num}},
        ],
    )
'''),
    ("INSERT: correct bindings", False, '''
def q(oid, num):
    return execute_sql(
        "INSERT INTO orders (order_id, order_number) VALUES (:orderId, :orderNumber)",
        parameters=[
            {"name": "orderId", "value": {"longValue": int(oid)}},
            {"name": "orderNumber", "value": {"stringValue": num}},
        ],
    )
'''),
    ("aliased table, bigint bound as stringValue", True, '''
def q(oid):
    return execute_sql(
        "SELECT o.order_number FROM orders o WHERE o.order_id = :orderId",
        parameters=[{"name": "orderId", "value": {"stringValue": str(oid)}}],
    )
'''),
    ("unknown column — must stay silent, never guess", False, '''
def q(x):
    return execute_sql(
        "SELECT 1 FROM orders WHERE some_unmapped_column = :x",
        parameters=[{"name": "x", "value": {"stringValue": x}}],
    )
'''),
]


def run_synthetic(gate) -> tuple:
    tidx = gate["_column_type_index"](SYNTHETIC_SCHEMA)
    passed = failed = 0
    print("A. Synthetic binding matrix")
    print("-" * 78)
    for name, should_flag, code in CASES:
        issues = gate["_check_sql_param_types"]("t", code, tidx)
        flagged = len(issues) > 0
        ok = flagged == should_flag
        passed += ok
        failed += not ok
        verdict = "PASS" if ok else "FAIL"
        want = "flag" if should_flag else "allow"
        print(f"  [{verdict}] expect {want:5s} -> {'flagged' if flagged else 'clean':7s}  {name}")
        if not ok and issues:
            print(f"          unexpected: {issues[0]['issue'][:120]}")
    print(f"\n  {passed}/{passed + failed} synthetic cases behaved as expected\n")
    return passed, failed


# --------------------------------------------------------------------------- B
def build_live_schema(introspection_path) -> Optional[dict]:
    p = pathlib.Path(introspection_path)
    if not p.exists():
        return None
    res = json.loads(p.read_text())
    key = next((k for k in res if "PostgreSQL" in k and "all" in k), None)
    if not key or not res[key].get("success"):
        return None
    pg = res[key]
    return {
        "tables": [{
            "name": t["table_name"],
            "columns": [{"name": c["name"], "sql_type": c.get("full_type")}
                        for c in t["columns"]],
        } for t in pg["tables"]],
        "enum_types": pg.get("enum_types"),
    }


def run_generated(gate, schema, pattern, label) -> int:
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"  (no handlers at {pattern})")
        return 0
    tidx = gate["_column_type_index"](schema)
    total = 0
    print(f"  {label}")
    for f in files:
        op = pathlib.Path(f).parent.name
        issues = gate["_check_sql_param_types"](op, pathlib.Path(f).read_text(), tidx)
        mark = "❌" if issues else "✅"
        print(f"    {mark} {op}")
        for i in issues:
            total += 1
            print(f"        {i['field']} — {i['issue'][:150]}")
    print(f"    → {total} finding(s)\n")
    return total


# --------------------------------------------------------------------------- C
def run_live(cluster_arn, secret_arn, database):
    """Prove the database agrees with the gate."""
    import boto3
    rds = boto3.client("rds-data", region_name="ap-northeast-1")

    def run(sql, params):
        return rds.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=database,
            sql=sql, parameters=params, includeResultMetadata=True)

    sql = "SELECT line_number FROM order_items WHERE order_id = :orderId LIMIT 1"
    print("C. Live Aurora PostgreSQL cross-check")
    print("-" * 78)
    print(f"  SQL: {sql}")
    for label, value, expect in (
        ("stringValue (gate flags this)", {"stringValue": "1"}, "reject"),
        ("longValue   (gate allows this)", {"longValue": 1}, "accept"),
    ):
        try:
            r = run(sql, [{"name": "orderId", "value": value}])
            got = "accept"
            detail = f"{len(r.get('records', []))} row(s)"
        except Exception as e:
            got = "reject"
            m = re.search(r"ERROR: (.*?);", str(e))
            detail = (m.group(1) if m else str(e))[:110]
        agree = "PASS" if got == expect else "FAIL"
        print(f"  [{agree}] {label}: database {got}s — {detail}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--introspection", default="/tmp/aicc-dbtest/introspect_results.json")
    ap.add_argument("--before", default="/tmp/aicc-dbtest/orig/*/index.py")
    ap.add_argument("--after", default="/tmp/aicc-dbtest/gen/lambda/*/index.py")
    ap.add_argument("--cluster-arn", default="")
    ap.add_argument("--secret-arn", default="")
    ap.add_argument("--database", default="retaildb")
    a = ap.parse_args()

    gate = load_gate()
    _, failed = run_synthetic(gate)

    schema = build_live_schema(a.introspection)
    print("B. Handlers produced by the generator")
    print("-" * 78)
    if schema:
        before = run_generated(gate, schema, a.before, "as generated:")
        after = run_generated(gate, schema, a.after, "after the fix:")
        print(f"  {before} finding(s) before, {after} after\n")
    else:
        print(f"  (skipped — no introspection result at {a.introspection})\n")

    if a.live and a.cluster_arn and a.secret_arn:
        run_live(a.cluster_arn, a.secret_arn, a.database)

    sys.exit(1 if failed else 0)
