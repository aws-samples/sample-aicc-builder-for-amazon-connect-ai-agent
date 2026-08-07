"""
Database Introspector Tool

Connects to customer databases (DynamoDB, RDS/Aurora MySQL & PostgreSQL)
and discovers schema information to help generate accurate Lambda functions.

RDS introspection goes through the **RDS Data API**, which is only available on
Aurora Serverless v2 / Aurora provisioned clusters with the HTTP endpoint
enabled. Plain RDS instances are not reachable this way and produce an
actionable error instead of a silent empty result.
"""

import json
import re
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from strands import tool

# Number of rows/items sampled per table to infer real-world value formats
SAMPLE_LIMIT = 5


@tool
def introspect_database(
    db_type: str,
    table_name: Optional[str] = None,
    region: str = "us-west-2",
    # For DynamoDB
    dynamodb_table_name: Optional[str] = None,
    # For RDS
    rds_secret_arn: Optional[str] = None,
    rds_database_name: Optional[str] = None,
    rds_cluster_arn: Optional[str] = None,
    include_sample_rows: bool = True,
) -> dict:
    """
    Introspect a database to discover its schema structure.

    This tool connects to the specified database and returns detailed schema
    information including tables, columns, types, keys, indexes, foreign keys,
    enum values, comments and sample rows.

    Use this BEFORE generating Lambda functions to understand the data structure.

    Args:
        db_type: Database type - 'dynamodb', 'rds_mysql', or 'rds_postgresql'
        table_name: Specific table(s) to introspect. Comma-separated for several.
                    Omit to introspect every table.
        region: AWS region where the database is located
        dynamodb_table_name: For DynamoDB - table name(s), comma-separated
        rds_secret_arn: For RDS - Secrets Manager ARN with the DB credentials
        rds_database_name: For RDS - the database (schema) name
        rds_cluster_arn: For RDS Data API - the Aurora cluster ARN
        include_sample_rows: Sample a few rows/items to infer value formats

    Returns:
        Schema information including:
        - Table names
        - Column/attribute definitions (types, nullability, defaults, comments)
        - Primary keys, sort keys, composite keys
        - Indexes (GSIs/LSIs for DynamoDB, secondary indexes for SQL)
        - Foreign keys / relationships (SQL)
        - Enum / allowed values (SQL)
        - Sample data
    """
    db_type = (db_type or "").strip().lower().replace("-", "_")

    # tolerate the aliases the interview/spec layer uses
    aliases = {
        "ddb": "dynamodb",
        "dynamo": "dynamodb",
        "mysql": "rds_mysql",
        "aurora_mysql": "rds_mysql",
        "rds_aurora_mysql": "rds_mysql",
        "postgres": "rds_postgresql",
        "postgresql": "rds_postgresql",
        "aurora_postgresql": "rds_postgresql",
        "rds_postgres": "rds_postgresql",
        "aurora_postgres": "rds_postgresql",
    }
    db_type = aliases.get(db_type, db_type)

    if db_type == "dynamodb":
        names = _split_names(dynamodb_table_name or table_name)
        if not names:
            return {
                "success": False,
                "error": "dynamodb_table_name (or table_name) is required for DynamoDB introspection",
            }
        if len(names) == 1:
            return _introspect_dynamodb(names[0], region, include_sample_rows)
        results = [_introspect_dynamodb(n, region, include_sample_rows) for n in names]
        failed = [r for r in results if not r.get("success")]
        return {
            "success": len(failed) < len(results),
            "db_type": "dynamodb",
            "table_count": len([r for r in results if r.get("success")]),
            "tables": results,
            "errors": [r.get("error") for r in failed] or None,
        }

    if db_type in ("rds_mysql", "rds_postgresql"):
        return _introspect_rds(
            db_type=db_type,
            secret_arn=rds_secret_arn,
            database_name=rds_database_name,
            cluster_arn=rds_cluster_arn,
            table_name=table_name,
            region=region,
            include_sample_rows=include_sample_rows,
        )

    return {
        "success": False,
        "error": f"Unsupported database type: {db_type}",
        "supported_types": ["dynamodb", "rds_mysql", "rds_postgresql"],
    }


def _split_names(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [n.strip() for n in str(value).split(",") if n.strip()]


# ======================================================================
# DynamoDB
# ======================================================================

def _index_keys(key_schema: list) -> list[dict]:
    out = []
    for key in key_schema or []:
        out.append({
            "attribute_name": key["AttributeName"],
            "key_type": "partition_key" if key["KeyType"] == "HASH" else "sort_key",
        })
    return out


def _describe_ddb_value(attr_value: dict) -> tuple[str, object]:
    """Return (inferred_type, plain_python_value) for a DynamoDB AttributeValue."""
    if "S" in attr_value:
        return "string", attr_value["S"]
    if "N" in attr_value:
        return "number", attr_value["N"]
    if "BOOL" in attr_value:
        return "boolean", attr_value["BOOL"]
    if "NULL" in attr_value:
        return "null", None
    if "L" in attr_value:
        return "list", [_describe_ddb_value(v)[1] for v in attr_value["L"]]
    if "M" in attr_value:
        return "map", {k: _describe_ddb_value(v)[1] for k, v in attr_value["M"].items()}
    if "SS" in attr_value:
        return "string_set", attr_value["SS"]
    if "NS" in attr_value:
        return "number_set", attr_value["NS"]
    if "BS" in attr_value:
        return "binary_set", ["<binary>"]
    if "B" in attr_value:
        return "binary", "<binary>"
    return "unknown", None


def _introspect_dynamodb(table_name: str, region: str, include_sample_rows: bool = True) -> dict:
    """Introspect a DynamoDB table."""
    if not table_name:
        return {"success": False, "error": "table_name is required for DynamoDB introspection"}

    try:
        dynamodb = boto3.client("dynamodb", region_name=region)
    except Exception as e:  # pragma: no cover - client construction rarely fails
        return {"success": False, "error": f"Could not create DynamoDB client: {e}"}

    try:
        table = dynamodb.describe_table(TableName=table_name)["Table"]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            return {
                "success": False,
                "error": f"Table '{table_name}' not found in region {region}. "
                         f"Check the table name and that the region is correct.",
            }
        if code in ("AccessDeniedException", "AccessDenied", "UnrecognizedClientException"):
            return {
                "success": False,
                "error": f"Access denied calling dynamodb:DescribeTable on '{table_name}' "
                         f"in {region}. The runtime role needs dynamodb:DescribeTable and "
                         f"dynamodb:Scan on arn:aws:dynamodb:{region}:*:table/{table_name}. "
                         f"Details: {e}",
                "error_code": "ACCESS_DENIED",
            }
        return {"success": False, "error": f"Failed to describe DynamoDB table: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to introspect DynamoDB table: {e}"}

    key_schema = _index_keys(table.get("KeySchema", []))

    attr_type_map = {"S": "string", "N": "number", "B": "binary"}
    attributes = [
        {"name": a["AttributeName"], "type": attr_type_map.get(a["AttributeType"], a["AttributeType"])}
        for a in table.get("AttributeDefinitions", [])
    ]

    gsis = []
    for gsi in table.get("GlobalSecondaryIndexes", []):
        projection = gsi.get("Projection", {})
        gsis.append({
            "index_name": gsi["IndexName"],
            "key_schema": _index_keys(gsi.get("KeySchema", [])),
            "projection_type": projection.get("ProjectionType", "ALL"),
            "projected_attributes": projection.get("NonKeyAttributes"),
            "status": gsi.get("IndexStatus"),
        })

    lsis = []
    for lsi in table.get("LocalSecondaryIndexes", []):
        projection = lsi.get("Projection", {})
        lsis.append({
            "index_name": lsi["IndexName"],
            "key_schema": _index_keys(lsi.get("KeySchema", [])),
            "projection_type": projection.get("ProjectionType", "ALL"),
            "projected_attributes": projection.get("NonKeyAttributes"),
        })

    # Sample items to discover attributes that are not part of any key
    sampled_types: dict[str, set] = {}
    sample_items: list[dict] = []
    sampled_count = 0
    sample_error = None
    if include_sample_rows:
        try:
            scan = dynamodb.scan(TableName=table_name, Limit=25)
            items = scan.get("Items", [])
            sampled_count = len(items)
            for item in items:
                plain = {}
                for name, value in item.items():
                    inferred, py_value = _describe_ddb_value(value)
                    sampled_types.setdefault(name, set()).add(inferred)
                    plain[name] = py_value
                if len(sample_items) < SAMPLE_LIMIT:
                    sample_items.append(plain)
        except ClientError as e:
            sample_error = (
                f"Could not scan '{table_name}' for sample data ({e.response.get('Error', {}).get('Code')}). "
                f"Schema keys/indexes were still read from DescribeTable, but non-key "
                f"attributes could not be discovered. Grant dynamodb:Scan to see them."
            )
        except Exception as e:  # pragma: no cover
            sample_error = f"Could not scan '{table_name}' for sample data: {e}"

    all_attributes = {a["name"]: a["type"] for a in attributes}
    for name, types in sampled_types.items():
        types = {t for t in types if t != "null"} or {"null"}
        merged = sorted(types)[0] if len(types) == 1 else "|".join(sorted(types))
        all_attributes.setdefault(name, merged)

    key_attribute_names = {k["attribute_name"] for k in key_schema}
    for idx in gsis + lsis:
        key_attribute_names.update(k["attribute_name"] for k in idx["key_schema"])

    result = {
        "success": True,
        "db_type": "dynamodb",
        "table_name": table_name,
        "table_arn": table.get("TableArn"),
        "region": region,
        "table_status": table.get("TableStatus"),
        "item_count": table.get("ItemCount", 0),
        "item_count_note": "DynamoDB updates ItemCount roughly every 6 hours; "
                           "sampled_item_count reflects what was actually read now.",
        "sampled_item_count": sampled_count,
        "table_size_bytes": table.get("TableSizeBytes", 0),
        "key_schema": key_schema,
        "attributes": [{"name": n, "type": t} for n, t in sorted(all_attributes.items())],
        "non_key_attributes": sorted(set(all_attributes) - key_attribute_names),
        "global_secondary_indexes": gsis,
        "local_secondary_indexes": lsis,
        "billing_mode": table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
        "stream_enabled": table.get("StreamSpecification", {}).get("StreamEnabled", False),
        "stream_view_type": table.get("StreamSpecification", {}).get("StreamViewType"),
        "sample_items": sample_items,
        "suggestions": _generate_dynamodb_suggestions(key_schema, gsis, lsis, all_attributes),
    }
    if sample_error:
        result["warnings"] = [sample_error]
    return result


# ======================================================================
# RDS / Aurora (Data API)
# ======================================================================

_MYSQL_QUERIES = {
    "tables": """
        SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, DATA_LENGTH, TABLE_COMMENT
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = :database
        ORDER BY TABLE_NAME
    """,
    "columns": """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE,
               COLUMN_KEY, COLUMN_DEFAULT, CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, NUMERIC_SCALE, COLUMN_COMMENT, EXTRA,
               GENERATION_EXPRESSION, ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :database
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """,
    "indexes": """
        SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, NON_UNIQUE, SEQ_IN_INDEX,
               INDEX_TYPE
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = :database
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
    """,
    "foreign_keys": """
        SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME,
               REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = :database AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
    """,
}

_POSTGRES_QUERIES = {
    "tables": """
        SELECT c.relname AS table_name,
               CASE c.relkind WHEN 'r' THEN 'BASE TABLE'
                              WHEN 'p' THEN 'PARTITIONED TABLE'
                              WHEN 'v' THEN 'VIEW'
                              WHEN 'm' THEN 'MATERIALIZED VIEW' END AS table_type,
               c.reltuples::bigint AS table_rows,
               pg_total_relation_size(c.oid) AS data_length,
               COALESCE(obj_description(c.oid, 'pg_class'), '') AS table_comment
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m')
        ORDER BY c.relname
    """,
    "columns": """
        SELECT c.table_name,
               c.column_name,
               c.data_type,
               CASE WHEN c.data_type = 'USER-DEFINED' THEN c.udt_name ELSE c.data_type END AS column_type,
               c.is_nullable,
               c.column_default,
               c.character_maximum_length,
               c.numeric_precision,
               c.numeric_scale,
               c.udt_name,
               c.is_generated,
               c.generation_expression,
               c.ordinal_position,
               COALESCE(col_description(fc.oid, c.ordinal_position::int), '') AS column_comment
        FROM information_schema.columns c
        JOIN pg_class fc ON fc.relname = c.table_name
        JOIN pg_namespace fn ON fn.oid = fc.relnamespace AND fn.nspname = c.table_schema
        WHERE c.table_schema = 'public'
        ORDER BY c.table_name, c.ordinal_position
    """,
    "indexes": """
        SELECT t.relname AS table_name,
               i.relname AS index_name,
               a.attname AS column_name,
               CASE WHEN ix.indisunique THEN 0 ELSE 1 END AS non_unique,
               k.ord AS seq_in_index,
               am.amname AS index_type,
               ix.indisprimary AS is_primary
        FROM pg_index ix
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_am am ON am.oid = i.relam
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE n.nspname = 'public' AND t.relkind IN ('r','p')
        ORDER BY t.relname, i.relname, k.ord
    """,
    "primary_keys": """
        SELECT t.relname AS table_name,
               a.attname AS column_name,
               k.ord AS ordinal_position
        FROM pg_constraint con
        JOIN pg_class t ON t.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE con.contype = 'p' AND n.nspname = 'public'
        ORDER BY t.relname, k.ord
    """,
    "foreign_keys": """
        SELECT t.relname AS table_name,
               con.conname AS constraint_name,
               a.attname AS column_name,
               rt.relname AS referenced_table_name,
               ra.attname AS referenced_column_name,
               k.ord AS ordinal_position
        FROM pg_constraint con
        JOIN pg_class t ON t.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_class rt ON rt.oid = con.confrelid
        JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        JOIN unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = k.ord
        JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = fk.attnum
        WHERE con.contype = 'f' AND n.nspname = 'public'
        ORDER BY t.relname, con.conname, k.ord
    """,
    "enums": """
        SELECT t.typname AS enum_name, e.enumlabel AS enum_value
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public'
        ORDER BY t.typname, e.enumsortorder
    """,
    "checks": """
        SELECT t.relname AS table_name,
               con.conname AS constraint_name,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class t ON t.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE con.contype = 'c' AND n.nspname = 'public'
        ORDER BY t.relname, con.conname
    """,
}


def _introspect_rds(
    db_type: str,
    secret_arn: str,
    database_name: str,
    cluster_arn: str,
    table_name: Optional[str],
    region: str,
    include_sample_rows: bool = True,
) -> dict:
    """Introspect an RDS/Aurora database using the Data API."""
    missing = [
        label for label, value in (
            ("rds_cluster_arn", cluster_arn),
            ("rds_secret_arn", secret_arn),
            ("rds_database_name", database_name),
        ) if not value
    ]
    if missing:
        return {
            "success": False,
            "error": f"Missing required RDS parameter(s): {', '.join(missing)}. "
                     f"RDS introspection uses the Data API and needs the Aurora cluster ARN, "
                     f"the Secrets Manager ARN holding the credentials, and the database name.",
            "error_code": "MISSING_PARAMETERS",
        }

    rds_data = boto3.client("rds-data", region_name=region)
    is_mysql = db_type == "rds_mysql"
    queries = _MYSQL_QUERIES if is_mysql else _POSTGRES_QUERIES

    def run(sql: str) -> list[dict]:
        kwargs = dict(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database_name,
            sql=sql,
            includeResultMetadata=True,   # ← without this there is no columnMetadata
        )
        if ":database" in sql:
            kwargs["parameters"] = [
                {"name": "database", "value": {"stringValue": database_name}}
            ]
        return _parse_rds_result(rds_data.execute_statement(**kwargs))

    try:
        tables = run(queries["tables"])
        columns = run(queries["columns"])
        indexes = run(queries["indexes"])
        foreign_keys = run(queries["foreign_keys"])
        primary_keys = [] if is_mysql else run(queries["primary_keys"])
        enums = {} if is_mysql else _group_enums(run(queries["enums"]))
        checks = [] if is_mysql else run(queries["checks"])
    except ClientError as e:
        return {"success": False, **_explain_rds_error(e, db_type, cluster_arn, region)}
    except Exception as e:
        return {"success": False, "error": f"Failed to introspect RDS database: {e}"}

    wanted = set(_split_names(table_name))

    table_schemas: dict[str, dict] = {}
    for row in tables:
        name = row.get("TABLE_NAME")
        if not name or (wanted and name not in wanted):
            continue
        table_schemas[name] = {
            "table_name": name,
            "table_type": row.get("TABLE_TYPE"),
            "table_comment": row.get("TABLE_COMMENT") or None,
            "estimated_rows": row.get("TABLE_ROWS"),
            "size_bytes": row.get("DATA_LENGTH"),
            "columns": [],
            "primary_key": [],
            "indexes": [],
            "foreign_keys": [],
            "referenced_by": [],
            "check_constraints": [],
        }

    if not table_schemas:
        return {
            "success": False,
            "error": (
                f"No tables found in database '{database_name}'"
                + (f" matching {sorted(wanted)}" if wanted else "")
                + f". Discovered {len(tables)} table(s) total"
                + (f": {sorted({r.get('TABLE_NAME') for r in tables if r.get('TABLE_NAME')})}" if tables else "")
                + ". Check the database name and the table spelling "
                  "(PostgreSQL introspection reads the 'public' schema)."
            ),
            "error_code": "NO_TABLES_FOUND",
            "db_type": db_type,
            "database_name": database_name,
        }

    # ---- columns
    pk_from_mysql: dict[str, list[str]] = {}
    for col in columns:
        tname = col.get("TABLE_NAME")
        if tname not in table_schemas:
            continue
        raw_type = col.get("COLUMN_TYPE") or col.get("DATA_TYPE")
        info = {
            "name": col.get("COLUMN_NAME"),
            "type": col.get("DATA_TYPE"),
            "full_type": raw_type,
            "nullable": str(col.get("IS_NULLABLE", "")).upper() in ("YES", "TRUE", "1"),
            "default": col.get("COLUMN_DEFAULT"),
            "max_length": col.get("CHARACTER_MAXIMUM_LENGTH"),
            "precision": col.get("NUMERIC_PRECISION"),
            "scale": col.get("NUMERIC_SCALE"),
            "comment": col.get("COLUMN_COMMENT") or None,
            "ordinal_position": col.get("ORDINAL_POSITION"),
        }

        allowed = _extract_allowed_values(raw_type, col.get("UDT_NAME"), enums)
        if allowed:
            info["allowed_values"] = allowed

        extra = str(col.get("EXTRA") or "")
        generated = str(col.get("IS_GENERATED") or "")
        if "auto_increment" in extra.lower():
            info["auto_increment"] = True
        # MySQL EXTRA is 'STORED GENERATED'/'VIRTUAL GENERATED' for real generated
        # columns; 'DEFAULT_GENERATED' merely means the column has a default
        # expression (e.g. CURRENT_TIMESTAMP) and must not be treated as generated.
        is_generated = (
            generated.upper().startswith("ALWAYS")
            or bool(re.search(r"\b(STORED|VIRTUAL)\s+GENERATED\b", extra, re.IGNORECASE))
        )
        if is_generated:
            info["generated"] = True
            expression = col.get("GENERATION_EXPRESSION") or extra
            if expression:
                info["generation_expression"] = expression

        if is_mysql and col.get("COLUMN_KEY") == "PRI":
            pk_from_mysql.setdefault(tname, []).append(col.get("COLUMN_NAME"))
            info["is_primary_key"] = True

        table_schemas[tname]["columns"].append(info)

    # ---- primary keys
    if is_mysql:
        for tname, cols in pk_from_mysql.items():
            table_schemas[tname]["primary_key"] = cols
    else:
        for row in primary_keys:
            tname = row.get("TABLE_NAME")
            if tname in table_schemas:
                table_schemas[tname]["primary_key"].append(row.get("COLUMN_NAME"))
        for tname, schema in table_schemas.items():
            pk = set(schema["primary_key"])
            for col in schema["columns"]:
                if col["name"] in pk:
                    col["is_primary_key"] = True

    # ---- indexes (keyed by table AND index name — names repeat across tables)
    grouped_indexes: dict[tuple, dict] = {}
    for idx in indexes:
        tname = idx.get("TABLE_NAME")
        iname = idx.get("INDEX_NAME")
        if tname not in table_schemas or not iname:
            continue
        key = (tname, iname)
        if key not in grouped_indexes:
            grouped_indexes[key] = {
                "name": iname,
                "table": tname,
                "columns": [],
                "unique": str(idx.get("NON_UNIQUE", 1)) in ("0", "False", "false"),
                "index_type": idx.get("INDEX_TYPE"),
                "is_primary": iname == "PRIMARY" or str(idx.get("IS_PRIMARY", "")).lower() == "true",
            }
        col_name = idx.get("COLUMN_NAME")
        if col_name and col_name not in grouped_indexes[key]["columns"]:
            grouped_indexes[key]["columns"].append(col_name)

    for (tname, _), idx in grouped_indexes.items():
        table_schemas[tname]["indexes"].append({
            "name": idx["name"],
            "columns": idx["columns"],
            "unique": idx["unique"],
            "index_type": idx["index_type"],
            "is_primary": idx["is_primary"],
        })

    # ---- foreign keys (+ reverse relationships)
    grouped_fks: dict[tuple, dict] = {}
    for fk in foreign_keys:
        tname = fk.get("TABLE_NAME")
        cname = fk.get("CONSTRAINT_NAME") or f"fk_{tname}"
        if tname not in table_schemas:
            continue
        key = (tname, cname)
        if key not in grouped_fks:
            grouped_fks[key] = {
                "constraint_name": cname,
                "table": tname,
                "columns": [],
                "referenced_table": fk.get("REFERENCED_TABLE_NAME"),
                "referenced_columns": [],
            }
        grouped_fks[key]["columns"].append(fk.get("COLUMN_NAME"))
        grouped_fks[key]["referenced_columns"].append(fk.get("REFERENCED_COLUMN_NAME"))

    for (tname, _), fk in grouped_fks.items():
        entry = {
            "constraint_name": fk["constraint_name"],
            "columns": fk["columns"],
            "referenced_table": fk["referenced_table"],
            "referenced_columns": fk["referenced_columns"],
        }
        table_schemas[tname]["foreign_keys"].append(entry)
        parent = fk["referenced_table"]
        if parent in table_schemas:
            table_schemas[parent]["referenced_by"].append({
                "table": tname,
                "columns": fk["columns"],
                "referenced_columns": fk["referenced_columns"],
            })

    # ---- check constraints (PostgreSQL)
    for row in checks:
        tname = row.get("TABLE_NAME")
        if tname in table_schemas:
            table_schemas[tname]["check_constraints"].append({
                "name": row.get("CONSTRAINT_NAME"),
                "definition": row.get("DEFINITION"),
            })

    # ---- sample rows
    warnings = []
    if include_sample_rows:
        quote = "`" if is_mysql else '"'
        for tname, schema in table_schemas.items():
            if schema["table_type"] not in ("BASE TABLE", "PARTITIONED TABLE", None):
                continue
            try:
                rows = run(f"SELECT * FROM {quote}{tname}{quote} LIMIT {SAMPLE_LIMIT}")
                schema["sample_rows"] = [_stringify_row(r) for r in rows]
                schema["actual_row_count"] = _count_rows(run, quote, tname)
            except Exception as e:
                warnings.append(f"Could not sample rows from '{tname}': {e}")

    relationships = [
        f"{fk['table']}.{','.join(fk['columns'])} → "
        f"{fk['referenced_table']}.{','.join(fk['referenced_columns'])}"
        for fk in grouped_fks.values()
    ]

    result = {
        "success": True,
        "db_type": db_type,
        "engine": "mysql" if is_mysql else "postgresql",
        "database_name": database_name,
        "cluster_arn": cluster_arn,
        "secret_arn": secret_arn,
        "region": region,
        "access_method": "rds-data-api",
        "table_count": len(table_schemas),
        "tables": list(table_schemas.values()),
        "relationships": sorted(relationships),
        "enum_types": enums or None,
        "suggestions": _generate_rds_suggestions(table_schemas, grouped_fks),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _count_rows(run, quote: str, tname: str):
    try:
        rows = run(f"SELECT COUNT(*) AS row_count FROM {quote}{tname}{quote}")
        if rows:
            return list(rows[0].values())[0]
    except Exception:
        return None
    return None


def _stringify_row(row: dict) -> dict:
    """Keep sample rows JSON-safe and compact."""
    out = {}
    for k, v in row.items():
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "…"
        out[k.lower()] = v
    return out


def _group_enums(rows: list[dict]) -> dict:
    enums: dict[str, list[str]] = {}
    for row in rows:
        name = row.get("ENUM_NAME")
        value = row.get("ENUM_VALUE")
        if name:
            enums.setdefault(name, []).append(value)
    return enums


def _extract_allowed_values(raw_type, udt_name, enums: dict) -> Optional[list[str]]:
    """Pull allowed values out of MySQL enum()/set() types or PG enum types."""
    if udt_name and udt_name in enums:
        return enums[udt_name]
    if raw_type and raw_type in enums:
        return enums[raw_type]
    if raw_type:
        m = re.match(r"^\s*(enum|set)\((.*)\)\s*$", str(raw_type), re.IGNORECASE | re.DOTALL)
        if m:
            return [v.strip().strip("'\"") for v in m.group(2).split("','")] if "','" in m.group(2) \
                else [v.strip().strip("'\"") for v in m.group(2).split(",")]
    return None


def _explain_rds_error(e: ClientError, db_type: str, cluster_arn: str, region: str) -> dict:
    code = e.response.get("Error", {}).get("Code", "")
    message = e.response.get("Error", {}).get("Message", str(e))

    if code in ("BadRequestException",) and "HttpEndpoint" in message or "not enabled" in message.lower():
        return {
            "error": f"The RDS Data API (HTTP endpoint) is not enabled on {cluster_arn}. "
                     f"Enable it with: aws rds modify-db-cluster --db-cluster-identifier "
                     f"<id> --enable-http-endpoint --region {region}. "
                     f"Note the Data API is only available on Aurora clusters "
                     f"(Serverless v2 or provisioned), not on plain RDS instances. "
                     f"Original error: {message}",
            "error_code": "DATA_API_NOT_ENABLED",
        }
    if code in ("AccessDeniedException", "AccessDenied"):
        return {
            "error": f"Access denied calling the RDS Data API. The runtime role needs "
                     f"rds-data:ExecuteStatement on the cluster and "
                     f"secretsmanager:GetSecretValue on the credentials secret. "
                     f"Original error: {message}",
            "error_code": "ACCESS_DENIED",
        }
    if code in ("DatabaseNotFoundException", "ResourceNotFoundException", "DatabaseErrorException"):
        return {
            "error": f"Database or cluster not reachable ({code}): {message}. "
                     f"Verify the cluster ARN region ({region}), the database name, "
                     f"and that the secret matches this cluster.",
            "error_code": code,
        }
    if code == "StatementTimeoutException":
        return {"error": f"Data API statement timed out: {message}", "error_code": code}
    return {"error": f"Failed to introspect RDS database ({code}): {message}", "error_code": code}


def _parse_rds_result(result: dict) -> list[dict]:
    """
    Parse an RDS Data API result into a list of dicts.

    Column names are normalized to UPPER CASE because PostgreSQL folds
    unquoted identifiers to lower case while MySQL's information_schema
    returns them upper case.
    """
    records = result.get("records", [])
    column_metadata = result.get("columnMetadata", [])
    names = [
        (c.get("label") or c.get("name") or f"col_{i}").upper()
        for i, c in enumerate(column_metadata)
    ]

    parsed = []
    for record in records:
        row = {}
        for i, field in enumerate(record):
            col_name = names[i] if i < len(names) else f"COL_{i}"
            if field.get("isNull"):
                row[col_name] = None
            elif "stringValue" in field:
                row[col_name] = field["stringValue"]
            elif "longValue" in field:
                row[col_name] = field["longValue"]
            elif "doubleValue" in field:
                row[col_name] = field["doubleValue"]
            elif "booleanValue" in field:
                row[col_name] = field["booleanValue"]
            elif "blobValue" in field:
                row[col_name] = "<blob>"
            elif "arrayValue" in field:
                row[col_name] = _parse_array_value(field["arrayValue"])
            else:
                row[col_name] = None
        parsed.append(row)

    return parsed


def _parse_array_value(array_value: dict):
    for key in ("stringValues", "longValues", "doubleValues", "booleanValues"):
        if key in array_value:
            return array_value[key]
    if "arrayValues" in array_value:
        return [_parse_array_value(v) for v in array_value["arrayValues"]]
    return []


# ======================================================================
# Suggestions
# ======================================================================

def _generate_dynamodb_suggestions(key_schema: list, gsis: list, lsis: list, attributes: dict) -> list[str]:
    suggestions = []
    pk = next((k["attribute_name"] for k in key_schema if k["key_type"] == "partition_key"), None)
    sk = next((k["attribute_name"] for k in key_schema if k["key_type"] == "sort_key"), None)

    if pk:
        suggestions.append(f"Primary lookup should use '{pk}' as the main query parameter")
    if sk:
        suggestions.append(f"Range queries are possible on '{sk}' within a partition "
                           f"(use Query with KeyConditionExpression, never Scan)")
    else:
        suggestions.append("No sort key defined - each partition key maps to exactly one item")

    for gsi in gsis:
        gsi_pk = next((k["attribute_name"] for k in gsi["key_schema"] if k["key_type"] == "partition_key"), None)
        gsi_sk = next((k["attribute_name"] for k in gsi["key_schema"] if k["key_type"] == "sort_key"), None)
        text = f"GSI '{gsi['index_name']}' enables querying by '{gsi_pk}'"
        if gsi_sk:
            text += f" with range on '{gsi_sk}'"
        if gsi["projection_type"] != "ALL":
            text += (f" (projection {gsi['projection_type']}"
                     + (f": {gsi['projected_attributes']}" if gsi.get("projected_attributes") else "")
                     + " — a follow-up GetItem is needed for other attributes)")
        suggestions.append(text)
    if not gsis:
        suggestions.append("No GSIs defined - consider adding indexes for common query patterns")

    for lsi in lsis:
        lsi_sk = next((k["attribute_name"] for k in lsi["key_schema"] if k["key_type"] == "sort_key"), None)
        suggestions.append(f"LSI '{lsi['index_name']}' allows sorting/filtering by '{lsi_sk}' "
                           f"within the same partition key")

    phone_attrs = [a for a in attributes if "phone" in a.lower()]
    if phone_attrs:
        gsi_backed = [
            a for a in phone_attrs
            if any(k["attribute_name"] == a for g in gsis for k in g["key_schema"])
        ]
        if gsi_backed:
            suggestions.append(f"Contact-center lookup by caller ID should Query the GSI on "
                               f"{gsi_backed} instead of scanning")
        else:
            suggestions.append(f"Attribute(s) {phone_attrs} look like a caller-ID lookup key but "
                               f"have no GSI — a Scan would be required")
    return suggestions


def _generate_rds_suggestions(tables: dict, grouped_fks: dict) -> list[str]:
    suggestions = []
    for table_name, table_info in tables.items():
        pk = table_info.get("primary_key", [])
        if pk:
            suggestions.append(f"Table '{table_name}': use {pk} for direct lookups")
        else:
            suggestions.append(f"Table '{table_name}': no primary key detected "
                               f"(view or key-less table) — verify before writing UPDATE/DELETE")
        for idx in table_info.get("indexes", []):
            if idx.get("is_primary"):
                continue
            suggestions.append(
                f"Table '{table_name}': index '{idx['name']}' on {idx['columns']}"
                + (" (unique)" if idx["unique"] else "")
                + " for efficient queries"
            )
        for col in table_info.get("columns", []):
            if "phone" in (col["name"] or "").lower():
                indexed = any(col["name"] in i["columns"] for i in table_info.get("indexes", []))
                suggestions.append(
                    f"Table '{table_name}.{col['name']}' looks like a caller-ID lookup key"
                    + (" and is indexed" if indexed else " but is NOT indexed — add an index"
                                                          " or expect a full table scan")
                )
    for fk in grouped_fks.values():
        suggestions.append(
            f"Join '{fk['table']}' to '{fk['referenced_table']}' on "
            f"{fk['columns']} = {fk['referenced_columns']}"
        )
    return suggestions


# ======================================================================
# Conversion to infrastructure_schema
# ======================================================================

def convert_to_infrastructure_schema(introspection_result: dict) -> dict:
    """
    Convert raw introspect_database() result to infrastructure_schema format.

    This allows generators (Lambda, OpenAPI, Prompt) to use existing DB schema
    in the same format as if Infrastructure Generator had produced it.

    Handles DynamoDB (single or multiple tables) and RDS/Aurora results.

    Args:
        introspection_result: Output from introspect_database()

    Returns:
        infrastructure_schema compatible dict
    """
    if not isinstance(introspection_result, dict):
        return {"error": "introspection_result must be a dict", "tables": []}

    if not introspection_result.get("success"):
        return {
            "error": introspection_result.get("error", "Introspection failed"),
            "error_code": introspection_result.get("error_code"),
            "tables": [],
        }

    db_type = introspection_result.get("db_type")

    if db_type == "dynamodb":
        # multi-table result from a comma-separated request
        if "tables" in introspection_result and "table_name" not in introspection_result:
            merged = {"tables": [], "environment_variables": {}, "data_conventions": {},
                      "db_type": "dynamodb"}
            for sub in introspection_result.get("tables", []):
                if not sub.get("success"):
                    continue
                part = convert_to_infrastructure_schema(sub)
                merged["tables"].extend(part.get("tables", []))
                merged["environment_variables"].update(part.get("environment_variables", {}))
                merged["data_conventions"].update(part.get("data_conventions", {}))
            _dedupe_env_vars(merged["tables"], merged["environment_variables"])
            return merged
        return _convert_dynamodb(introspection_result)

    if db_type in ("rds_mysql", "rds_postgresql"):
        return _convert_rds(introspection_result)

    return {"error": f"Cannot convert db_type '{db_type}'", "tables": []}


_GENERIC_NAME_TAILS = {
    "history", "data", "info", "table", "log", "logs", "record", "records",
    "detail", "details", "list", "item", "items", "entry", "entries", "master",
    "summary", "audit", "archive", "tmp", "temp", "v1", "v2", "prod", "dev",
}


def _entity_from_name(name: str) -> str:
    """
    Derive a stable entity token from a table name.

    'hotel-reservations'    -> RESERVATIONS
    'telco-billing-history' -> BILLING_HISTORY   (last token is too generic alone)
    'orders'                -> ORDERS
    """
    tokens = [t for t in re.split(r"[-_.\s]+", str(name)) if t]
    if not tokens:
        return "TABLE"
    tail = tokens[-1]
    if len(tokens) > 1 and (tail.lower() in _GENERIC_NAME_TAILS or len(tail) < 4):
        tokens = tokens[-2:]
    else:
        tokens = tokens[-1:]
    entity = "_".join(re.sub(r"[^A-Za-z0-9]", "", t) for t in tokens).upper()
    return entity.strip("_") or "TABLE"


def _logical_id(entity: str) -> str:
    return "".join(part.capitalize() for part in entity.split("_") if part) + "Table"


def _dedupe_env_vars(tables: list[dict], env_vars: dict) -> None:
    """
    Ensure env var names / logical ids are unique across tables.

    Two tables like 'a-orders' and 'b-orders' both reduce to ORDERS; when that
    happens fall back to the full table name so nothing silently overwrites.
    """
    seen: dict[str, dict] = {}
    for entry in tables:
        name = entry.get("env_var_name")
        if not name:
            continue
        if name in seen:
            for conflicting in (seen[name], entry):
                full = re.sub(r"[^A-Za-z0-9]+", "_", str(conflicting["table_name"])).upper().strip("_")
                old = conflicting["env_var_name"]
                conflicting["env_var_name"] = f"{full}_TABLE_NAME"
                conflicting["logical_id"] = _logical_id(full)
                if old in env_vars:
                    env_vars[conflicting["env_var_name"]] = env_vars.pop(old)
                else:
                    env_vars[conflicting["env_var_name"]] = conflicting["table_name"]
        else:
            seen[name] = entry


def _convert_dynamodb(r: dict) -> dict:
    table_name = r["table_name"]
    entity = _entity_from_name(table_name)
    env_var_name = f"{entity}_TABLE_NAME"

    type_map = {"string": "S", "number": "N", "binary": "B"}
    attr_types = {a["name"]: type_map.get(a["type"], "S") for a in r.get("attributes", [])}

    primary_key = None
    sort_key = None
    for key in r.get("key_schema", []):
        entry = {"name": key["attribute_name"],
                 "type": attr_types.get(key["attribute_name"], "S")}
        if key["key_type"] == "partition_key":
            primary_key = entry
        else:
            sort_key = entry

    def convert_index(idx):
        entry = {"name": idx["index_name"], "projection": idx.get("projection_type", "ALL")}
        if idx.get("projected_attributes"):
            entry["projected_attributes"] = idx["projected_attributes"]
        for key in idx.get("key_schema", []):
            slot = "partition_key" if key["key_type"] == "partition_key" else "sort_key"
            entry[slot] = {"name": key["attribute_name"],
                           "type": attr_types.get(key["attribute_name"], "S")}
        return entry

    gsi_indexes = [convert_index(g) for g in r.get("global_secondary_indexes", [])]
    lsi_indexes = [convert_index(l) for l in r.get("local_secondary_indexes", [])]

    # data conventions: derive real formats from sampled values
    data_conventions = {}
    samples = r.get("sample_items", [])
    for attr in r.get("attributes", []):
        name = attr["name"]
        example = next((s[name] for s in samples if isinstance(s.get(name), (str, int, float))), None)
        if "phone" in name.lower():
            data_conventions[name] = {
                "format": "E.164 without +" if isinstance(example, str) and example.isdigit()
                          else "as stored in the existing table",
                "example": example,
                "gsi": next((g["name"] for g in gsi_indexes
                             if g.get("partition_key", {}).get("name") == name), None),
            }
        elif example is not None and any(
            token in name.lower() for token in ("date", "_at", "time", "month", "status", "id", "code")
        ):
            data_conventions[name] = {"example": example}

    table_entry = {
        "logical_id": _logical_id(entity),
        "table_name": table_name,
        "table_arn": r.get("table_arn"),
        "env_var_name": env_var_name,
        "primary_key": primary_key,
        "gsi_indexes": gsi_indexes,
        "existing": True,
        "attributes": r.get("attributes", []),
    }
    if sort_key:
        table_entry["sort_key"] = sort_key
    if lsi_indexes:
        table_entry["lsi_indexes"] = lsi_indexes

    return {
        "db_type": "dynamodb",
        "region": r.get("region"),
        "tables": [table_entry],
        "environment_variables": {env_var_name: table_name},
        "data_conventions": data_conventions,
        "access_notes": r.get("suggestions", []),
    }


def _convert_rds(r: dict) -> dict:
    engine = r.get("engine") or ("mysql" if r["db_type"] == "rds_mysql" else "postgresql")
    database_name = r.get("database_name")

    tables = []
    conventions = {}
    for t in r.get("tables", []):
        entity = _entity_from_name(t["table_name"])
        entry = {
            "logical_id": _logical_id(entity),
            "table_name": t["table_name"],
            "table_type": t.get("table_type"),
            "description": t.get("table_comment"),
            "primary_key": t.get("primary_key", []),
            "columns": [
                {
                    "name": c["name"],
                    "sql_type": c.get("full_type") or c.get("type"),
                    "nullable": c.get("nullable"),
                    "default": c.get("default"),
                    "allowed_values": c.get("allowed_values"),
                    "generated": c.get("generated", False),
                    "description": c.get("comment"),
                }
                for c in t.get("columns", [])
            ],
            "indexes": t.get("indexes", []),
            "foreign_keys": t.get("foreign_keys", []),
            "referenced_by": t.get("referenced_by", []),
            "check_constraints": t.get("check_constraints", []),
            "row_count": t.get("actual_row_count", t.get("estimated_rows")),
            "existing": True,
        }
        tables.append(entry)

        sample = (t.get("sample_rows") or [{}])[0]
        for c in t.get("columns", []):
            lname = (c["name"] or "").lower()
            example = sample.get(lname)
            if "phone" in lname and example is not None:
                conventions[f"{t['table_name']}.{c['name']}"] = {
                    "format": "E.164 without +" if isinstance(example, str) and example.isdigit()
                              else "as stored in the existing column",
                    "example": example,
                    "indexed": any(c["name"] in i.get("columns", []) for i in t.get("indexes", [])),
                }
            elif c.get("allowed_values"):
                conventions[f"{t['table_name']}.{c['name']}"] = {
                    "allowed_values": c["allowed_values"],
                    "example": example,
                }

    return {
        "db_type": r["db_type"],
        "engine": engine,
        "region": r.get("region"),
        "database_name": database_name,
        "access_method": "rds-data-api",
        "connection": {
            "cluster_arn": r.get("cluster_arn"),
            "secret_arn": r.get("secret_arn"),
            "database_name": database_name,
        },
        "environment_variables": {
            "DB_CLUSTER_ARN": r.get("cluster_arn"),
            "DB_SECRET_ARN": r.get("secret_arn"),
            "DB_NAME": database_name,
        },
        "iam_requirements": [
            "rds-data:ExecuteStatement",
            "rds-data:BatchExecuteStatement",
            "secretsmanager:GetSecretValue",
        ],
        "tables": tables,
        "relationships": r.get("relationships", []),
        "enum_types": r.get("enum_types"),
        "data_conventions": conventions,
        "access_notes": r.get("suggestions", []),
    }
