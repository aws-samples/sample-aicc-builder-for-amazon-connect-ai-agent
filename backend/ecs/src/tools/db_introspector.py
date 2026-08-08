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
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from strands import tool

# Number of rows/items sampled per table to infer real-world value formats
SAMPLE_LIMIT = 5

# ─────────────────────────────────────────────────────────────────────────────
# Engine registry
#
# Every RDS and Aurora engine is supported. Two connection methods exist and the
# right one is chosen automatically:
#   • RDS Data API  — Aurora only, and only when the HTTP endpoint is enabled.
#                     Needs no network path from the caller.
#   • direct driver — everything else. Needs TCP reachability to the endpoint.
# ─────────────────────────────────────────────────────────────────────────────

# db_type spelling → canonical type
_DB_TYPE_ALIASES = {
    "ddb": "dynamodb",
    "dynamo": "dynamodb",
    "dynamo_db": "dynamodb",
    # PostgreSQL
    "postgres": "rds_postgresql",
    "postgresql": "rds_postgresql",
    "rds_postgres": "rds_postgresql",
    "aurora_postgres": "aurora_postgresql",
    "aurora_postgresql": "aurora_postgresql",
    # MySQL
    "mysql": "rds_mysql",
    "aurora_mysql": "aurora_mysql",
    "rds_aurora_mysql": "aurora_mysql",
    # MariaDB
    "mariadb": "rds_mariadb",
    "maria": "rds_mariadb",
    # SQL Server
    "sqlserver": "rds_sqlserver",
    "mssql": "rds_sqlserver",
    "rds_mssql": "rds_sqlserver",
    "sql_server": "rds_sqlserver",
    "rds_sql_server": "rds_sqlserver",
    # Oracle
    "oracle": "rds_oracle",
    # Db2
    "db2": "rds_db2",
    "rds_ibm_db2": "rds_db2",
}

# canonical db_type → engine family (which SQL dialect / driver to use)
_DB_TYPE_FAMILY = {
    "rds_postgresql": "postgresql",
    "aurora_postgresql": "postgresql",
    "rds_mysql": "mysql",
    "aurora_mysql": "mysql",
    "rds_mariadb": "mysql",       # MariaDB speaks the MySQL protocol and information_schema
    "rds_sqlserver": "sqlserver",
    "rds_oracle": "oracle",
    "rds_db2": "db2",
}
_SQL_DB_TYPES = set(_DB_TYPE_FAMILY)

# RDS `Engine` value → engine family, for auto-detection
_RDS_ENGINE_FAMILY = {
    "postgres": "postgresql",
    "aurora-postgresql": "postgresql",
    "mysql": "mysql",
    "aurora-mysql": "mysql",
    "aurora": "mysql",            # legacy Aurora MySQL 5.6
    "mariadb": "mysql",
    "sqlserver-ex": "sqlserver",
    "sqlserver-web": "sqlserver",
    "sqlserver-se": "sqlserver",
    "sqlserver-ee": "sqlserver",
    "oracle-se2": "oracle",
    "oracle-se2-cdb": "oracle",
    "oracle-ee": "oracle",
    "oracle-ee-cdb": "oracle",
    "oracle-se": "oracle",
    "custom-oracle-ee": "oracle",
    "db2-ae": "db2",
    "db2-se": "db2",
}

# RDS `Engine` value → canonical db_type reported back to the caller
_RDS_ENGINE_DB_TYPE = {
    "postgres": "rds_postgresql",
    "aurora-postgresql": "aurora_postgresql",
    "mysql": "rds_mysql",
    "aurora-mysql": "aurora_mysql",
    "aurora": "aurora_mysql",
    "mariadb": "rds_mariadb",
    "sqlserver-ex": "rds_sqlserver",
    "sqlserver-web": "rds_sqlserver",
    "sqlserver-se": "rds_sqlserver",
    "sqlserver-ee": "rds_sqlserver",
    "oracle-se2": "rds_oracle",
    "oracle-se2-cdb": "rds_oracle",
    "oracle-ee": "rds_oracle",
    "oracle-ee-cdb": "rds_oracle",
    "oracle-se": "rds_oracle",
    "db2-ae": "rds_db2",
    "db2-se": "rds_db2",
}

_DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "sqlserver": 1433,
    "oracle": 1521,
    "db2": 50000,
}

# identifier quoting per family
_QUOTE = {
    "postgresql": ('"', '"'),
    "mysql": ("`", "`"),
    "sqlserver": ("[", "]"),
    "oracle": ('"', '"'),
    "db2": ('"', '"'),
}

# pip package needed for the direct-driver path, per family
_DRIVER_PACKAGE = {
    "postgresql": "psycopg2-binary",
    "mysql": "PyMySQL",
    "sqlserver": "python-tds",
    "oracle": "oracledb",
    "db2": "ibm-db",
}


@tool
def introspect_database(
    db_type: str,
    table_name: Optional[str] = None,
    region: str = "us-west-2",
    # For DynamoDB
    dynamodb_table_name: Optional[str] = None,
    # For RDS / Aurora — Data API path
    rds_secret_arn: Optional[str] = None,
    rds_database_name: Optional[str] = None,
    rds_cluster_arn: Optional[str] = None,
    # For RDS / Aurora — any engine, resolved automatically
    rds_instance_identifier: Optional[str] = None,
    rds_host: Optional[str] = None,
    rds_port: Optional[int] = None,
    rds_username: Optional[str] = None,
    rds_password: Optional[str] = None,
    tables_only: bool = False,
    include_sample_rows: bool = True,
) -> dict:
    """
    Introspect a database to discover its schema structure.

    This tool connects to the specified database and returns detailed schema
    information including tables, columns, types, keys, indexes, foreign keys,
    enum values, comments and sample rows.

    Use this BEFORE generating Lambda functions to understand the data structure.

    Supported databases:
      - DynamoDB
      - Every RDS and Aurora engine: PostgreSQL, MySQL, MariaDB,
        SQL Server, Oracle and Db2

    How it connects (chosen automatically):
      - Aurora with the Data API (HTTP endpoint) enabled → RDS Data API,
        no network path needed
      - everything else → a direct driver connection to the endpoint

    The simplest call passes just `rds_instance_identifier`: the engine,
    endpoint, port and master-user secret are all resolved from RDS, and the
    right connection method is picked for you.

    Choosing what to scan:
      - omit `table_name` to scan every table
      - pass one name to scan just that table
      - pass a comma-separated list to scan several
      - set `tables_only=True` first on an unfamiliar database: it returns a
        cheap inventory (names, row counts, column counts, comments) with no
        column, index or sample-row queries, so you can pick the handful of
        tables the operations actually need and then scan those by name.
      Table names are matched case-insensitively, so 'orders' finds Oracle's
      ORDERS without the caller having to know the storage casing.

    Args:
        db_type: 'dynamodb', or an RDS/Aurora engine. Accepted spellings include
            'rds_postgresql', 'rds_mysql', 'rds_mariadb', 'rds_sqlserver',
            'rds_oracle', 'rds_db2', 'aurora_postgresql', 'aurora_mysql', and
            plain 'rds'/'aurora' when the engine should be auto-detected.
        table_name: Specific table(s) to introspect. Comma-separated for several.
                    Omit to introspect every table.
        region: AWS region where the database is located
        dynamodb_table_name: For DynamoDB - table name(s), comma-separated
        rds_secret_arn: Secrets Manager ARN holding the DB credentials
        rds_database_name: The database (schema) name to inspect
        rds_cluster_arn: Aurora cluster ARN (Data API path)
        rds_instance_identifier: RDS DB instance or Aurora cluster identifier —
            everything else is resolved from it
        rds_host: Endpoint host, if not resolving from an identifier
        rds_port: Endpoint port (defaults to the engine's standard port)
        rds_username: DB user, if not using a Secrets Manager secret
        rds_password: DB password, if not using a Secrets Manager secret
        tables_only: Return just the table inventory, skipping column/index/
            foreign-key/sample queries. Use on a large unfamiliar schema.
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
    db_type = _DB_TYPE_ALIASES.get(db_type, db_type)

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

    if db_type in _SQL_DB_TYPES or db_type in ("rds", "aurora"):
        return _introspect_sql(
            db_type=db_type,
            secret_arn=rds_secret_arn,
            database_name=rds_database_name,
            cluster_arn=rds_cluster_arn,
            instance_identifier=rds_instance_identifier,
            host=rds_host,
            port=rds_port,
            username=rds_username,
            password=rds_password,
            table_name=table_name,
            region=region,
            tables_only=tables_only,
            include_sample_rows=include_sample_rows,
        )

    return {
        "success": False,
        "error": f"Unsupported database type: {db_type}",
        "supported_types": ["dynamodb"] + sorted(_SQL_DB_TYPES),
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


_SQLSERVER_QUERIES = {
    "tables": """
        SELECT t.name AS table_name,
               CASE WHEN o.type = 'V' THEN 'VIEW' ELSE 'BASE TABLE' END AS table_type,
               CAST(p.row_count AS bigint) AS table_rows,
               CAST(p.used_page_count * 8192 AS bigint) AS data_length,
               CAST(ISNULL(ep.value, '') AS nvarchar(2000)) AS table_comment
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.all_objects t ON t.object_id = o.object_id
        LEFT JOIN (
            SELECT object_id,
                   SUM(CASE WHEN index_id < 2 THEN row_count ELSE 0 END) AS row_count,
                   SUM(used_page_count) AS used_page_count
            FROM sys.dm_db_partition_stats GROUP BY object_id
        ) p ON p.object_id = o.object_id
        LEFT JOIN sys.extended_properties ep
               ON ep.major_id = o.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
        WHERE o.type IN ('U','V') AND s.name = 'dbo'
        ORDER BY t.name
    """,
    "columns": """
        SELECT t.name AS table_name,
               c.name AS column_name,
               ty.name AS data_type,
               ty.name + CASE
                   WHEN ty.name IN ('varchar','nvarchar','char','nchar')
                       THEN '(' + CASE WHEN c.max_length = -1 THEN 'max'
                                       ELSE CAST(c.max_length AS varchar(10)) END + ')'
                   WHEN ty.name IN ('decimal','numeric')
                       THEN '(' + CAST(c.precision AS varchar(10)) + ','
                                + CAST(c.scale AS varchar(10)) + ')'
                   ELSE '' END AS column_type,
               CASE WHEN c.is_nullable = 1 THEN 'YES' ELSE 'NO' END AS is_nullable,
               CAST(dc.definition AS nvarchar(2000)) AS column_default,
               c.max_length AS character_maximum_length,
               c.precision AS numeric_precision,
               c.scale AS numeric_scale,
               c.column_id AS ordinal_position,
               CAST(ISNULL(ep.value, '') AS nvarchar(2000)) AS column_comment,
               CASE WHEN c.is_identity = 1 THEN 'auto_increment' ELSE '' END AS extra,
               CAST(cc.definition AS nvarchar(2000)) AS generation_expression
        FROM sys.columns c
        JOIN sys.objects o ON o.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.all_objects t ON t.object_id = o.object_id
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
        LEFT JOIN sys.computed_columns cc
               ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.extended_properties ep
               ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
              AND ep.name = 'MS_Description'
        WHERE o.type IN ('U','V') AND s.name = 'dbo'
        ORDER BY t.name, c.column_id
    """,
    "indexes": """
        SELECT t.name AS table_name,
               i.name AS index_name,
               c.name AS column_name,
               CASE WHEN i.is_unique = 1 THEN 0 ELSE 1 END AS non_unique,
               ic.key_ordinal AS seq_in_index,
               i.type_desc AS index_type,
               CASE WHEN i.is_primary_key = 1 THEN 'true' ELSE 'false' END AS is_primary
        FROM sys.indexes i
        JOIN sys.objects o ON o.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.all_objects t ON t.object_id = i.object_id
        JOIN sys.index_columns ic
             ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c
             ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE o.type = 'U' AND s.name = 'dbo' AND i.name IS NOT NULL
              AND ic.is_included_column = 0
        ORDER BY t.name, i.name, ic.key_ordinal
    """,
    "primary_keys": """
        SELECT t.name AS table_name,
               c.name AS column_name,
               ic.key_ordinal AS ordinal_position
        FROM sys.indexes i
        JOIN sys.objects o ON o.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.all_objects t ON t.object_id = i.object_id
        JOIN sys.index_columns ic
             ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c
             ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.is_primary_key = 1 AND s.name = 'dbo'
        ORDER BY t.name, ic.key_ordinal
    """,
    "foreign_keys": """
        SELECT pt.name AS table_name,
               fk.name AS constraint_name,
               pc.name AS column_name,
               rt.name AS referenced_table_name,
               rc.name AS referenced_column_name,
               fkc.constraint_column_id AS ordinal_position
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.all_objects pt ON pt.object_id = fk.parent_object_id
        JOIN sys.all_objects rt ON rt.object_id = fk.referenced_object_id
        JOIN sys.columns pc
             ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.columns rc
             ON rc.object_id = fkc.referenced_object_id
            AND rc.column_id = fkc.referenced_column_id
        ORDER BY pt.name, fk.name, fkc.constraint_column_id
    """,
    "checks": """
        SELECT t.name AS table_name,
               cc.name AS constraint_name,
               CAST(cc.definition AS nvarchar(2000)) AS definition
        FROM sys.check_constraints cc
        JOIN sys.all_objects t ON t.object_id = cc.parent_object_id
        ORDER BY t.name, cc.name
    """,
}

_ORACLE_QUERIES = {
    "tables": """
        SELECT t.table_name AS table_name,
               'BASE TABLE' AS table_type,
               t.num_rows AS table_rows,
               NULL AS data_length,
               c.comments AS table_comment
        FROM user_tables t
        LEFT JOIN user_tab_comments c ON c.table_name = t.table_name
        UNION ALL
        SELECT v.view_name, 'VIEW', NULL, NULL, vc.comments
        FROM user_views v
        LEFT JOIN user_tab_comments vc ON vc.table_name = v.view_name
        ORDER BY 1
    """,
    "columns": """
        SELECT c.table_name AS table_name,
               c.column_name AS column_name,
               c.data_type AS data_type,
               CASE
                 WHEN c.data_type IN ('VARCHAR2','NVARCHAR2','CHAR','NCHAR')
                   THEN c.data_type || '(' || c.char_length || ')'
                 WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL
                   THEN c.data_type || '(' || c.data_precision || ',' ||
                        NVL(c.data_scale, 0) || ')'
                 ELSE c.data_type
               END AS column_type,
               c.nullable AS is_nullable,
               c.data_default AS column_default,
               c.char_length AS character_maximum_length,
               c.data_precision AS numeric_precision,
               c.data_scale AS numeric_scale,
               c.column_id AS ordinal_position,
               cc.comments AS column_comment,
               CASE WHEN c.identity_column = 'YES' THEN 'auto_increment' ELSE '' END AS extra,
               c.virtual_column AS is_generated
        FROM user_tab_cols c
        LEFT JOIN user_col_comments cc
               ON cc.table_name = c.table_name AND cc.column_name = c.column_name
        WHERE c.hidden_column = 'NO'
        ORDER BY c.table_name, c.column_id
    """,
    "indexes": """
        SELECT i.table_name AS table_name,
               i.index_name AS index_name,
               ic.column_name AS column_name,
               CASE WHEN i.uniqueness = 'UNIQUE' THEN 0 ELSE 1 END AS non_unique,
               ic.column_position AS seq_in_index,
               i.index_type AS index_type,
               CASE WHEN cn.constraint_type = 'P' THEN 'true' ELSE 'false' END AS is_primary
        FROM user_indexes i
        JOIN user_ind_columns ic ON ic.index_name = i.index_name
        LEFT JOIN user_constraints cn
               ON cn.index_name = i.index_name AND cn.constraint_type = 'P'
        ORDER BY i.table_name, i.index_name, ic.column_position
    """,
    "primary_keys": """
        SELECT cc.table_name AS table_name,
               cc.column_name AS column_name,
               cc.position AS ordinal_position
        FROM user_constraints c
        JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
        WHERE c.constraint_type = 'P'
        ORDER BY cc.table_name, cc.position
    """,
    "foreign_keys": """
        SELECT cc.table_name AS table_name,
               c.constraint_name AS constraint_name,
               cc.column_name AS column_name,
               rc.table_name AS referenced_table_name,
               rc.column_name AS referenced_column_name,
               cc.position AS ordinal_position
        FROM user_constraints c
        JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
        JOIN user_cons_columns rc
             ON rc.constraint_name = c.r_constraint_name AND rc.position = cc.position
        WHERE c.constraint_type = 'R'
        ORDER BY cc.table_name, c.constraint_name, cc.position
    """,
    "checks": """
        SELECT table_name AS table_name,
               constraint_name AS constraint_name,
               search_condition AS definition
        FROM user_constraints
        WHERE constraint_type = 'C' AND generated = 'USER NAME'
        ORDER BY table_name, constraint_name
    """,
}

_DB2_QUERIES = {
    "tables": """
        SELECT TABNAME AS TABLE_NAME,
               CASE TYPE WHEN 'V' THEN 'VIEW' ELSE 'BASE TABLE' END AS TABLE_TYPE,
               CARD AS TABLE_ROWS,
               NULL AS DATA_LENGTH,
               REMARKS AS TABLE_COMMENT
        FROM SYSCAT.TABLES
        WHERE TABSCHEMA = CURRENT SCHEMA AND TYPE IN ('T','V')
        ORDER BY TABNAME
    """,
    "columns": """
        SELECT TABNAME AS TABLE_NAME,
               COLNAME AS COLUMN_NAME,
               TYPENAME AS DATA_TYPE,
               TYPENAME AS COLUMN_TYPE,
               NULLS AS IS_NULLABLE,
               DEFAULT AS COLUMN_DEFAULT,
               LENGTH AS CHARACTER_MAXIMUM_LENGTH,
               LENGTH AS NUMERIC_PRECISION,
               SCALE AS NUMERIC_SCALE,
               COLNO AS ORDINAL_POSITION,
               REMARKS AS COLUMN_COMMENT,
               CASE WHEN IDENTITY = 'Y' THEN 'auto_increment' ELSE '' END AS EXTRA,
               GENERATED AS IS_GENERATED
        FROM SYSCAT.COLUMNS
        WHERE TABSCHEMA = CURRENT SCHEMA
        ORDER BY TABNAME, COLNO
    """,
    "indexes": """
        SELECT i.TABNAME AS TABLE_NAME,
               i.INDNAME AS INDEX_NAME,
               c.COLNAME AS COLUMN_NAME,
               CASE WHEN i.UNIQUERULE IN ('U','P') THEN 0 ELSE 1 END AS NON_UNIQUE,
               c.COLSEQ AS SEQ_IN_INDEX,
               'BTREE' AS INDEX_TYPE,
               CASE WHEN i.UNIQUERULE = 'P' THEN 'true' ELSE 'false' END AS IS_PRIMARY
        FROM SYSCAT.INDEXES i
        JOIN SYSCAT.INDEXCOLUSE c ON c.INDNAME = i.INDNAME AND c.INDSCHEMA = i.INDSCHEMA
        WHERE i.TABSCHEMA = CURRENT SCHEMA
        ORDER BY i.TABNAME, i.INDNAME, c.COLSEQ
    """,
    "primary_keys": """
        SELECT k.TABNAME AS TABLE_NAME,
               k.COLNAME AS COLUMN_NAME,
               k.COLSEQ AS ORDINAL_POSITION
        FROM SYSCAT.KEYCOLUSE k
        JOIN SYSCAT.TABCONST t
             ON t.CONSTNAME = k.CONSTNAME AND t.TABNAME = k.TABNAME
        WHERE t.TYPE = 'P' AND k.TABSCHEMA = CURRENT SCHEMA
        ORDER BY k.TABNAME, k.COLSEQ
    """,
    "foreign_keys": """
        SELECT r.TABNAME AS TABLE_NAME,
               r.CONSTNAME AS CONSTRAINT_NAME,
               fk.COLNAME AS COLUMN_NAME,
               r.REFTABNAME AS REFERENCED_TABLE_NAME,
               pk.COLNAME AS REFERENCED_COLUMN_NAME,
               fk.COLSEQ AS ORDINAL_POSITION
        FROM SYSCAT.REFERENCES r
        JOIN SYSCAT.KEYCOLUSE fk ON fk.CONSTNAME = r.CONSTNAME AND fk.TABNAME = r.TABNAME
        JOIN SYSCAT.KEYCOLUSE pk
             ON pk.CONSTNAME = r.REFKEYNAME AND pk.COLSEQ = fk.COLSEQ
        WHERE r.TABSCHEMA = CURRENT SCHEMA
        ORDER BY r.TABNAME, r.CONSTNAME, fk.COLSEQ
    """,
    "checks": """
        SELECT TABNAME AS TABLE_NAME,
               CONSTNAME AS CONSTRAINT_NAME,
               TEXT AS DEFINITION
        FROM SYSCAT.CHECKS
        WHERE TABSCHEMA = CURRENT SCHEMA
        ORDER BY TABNAME, CONSTNAME
    """,
}

_QUERY_SETS = {
    "postgresql": _POSTGRES_QUERIES,
    "mysql": _MYSQL_QUERIES,
    "sqlserver": _SQLSERVER_QUERIES,
    "oracle": _ORACLE_QUERIES,
    "db2": _DB2_QUERIES,
}


class _DriverMissing(Exception):
    """Raised when the pip package for an engine's driver is not installed."""


def _resolve_sql_target(
    db_type: str,
    region: str,
    instance_identifier: Optional[str],
    cluster_arn: Optional[str],
    secret_arn: Optional[str],
    database_name: Optional[str],
    host: Optional[str],
    port: Optional[int],
    username: Optional[str],
    password: Optional[str],
) -> dict:
    """Work out engine, endpoint, credentials and connection method.

    Given only an instance/cluster identifier this asks RDS for everything else,
    so the caller does not have to know whether the target is Aurora, whether
    the Data API is on, or which port the engine listens on.
    """
    rds = boto3.client("rds", region_name=region)
    resolved_engine = None
    data_api = False
    endpoint = host
    endpoint_port = port

    identifier = instance_identifier
    if not identifier and cluster_arn:
        identifier = cluster_arn.split(":")[-1]

    if identifier:
        described = _describe_rds_target(rds, identifier)
        if described.get("error"):
            return {"success": False, "error": described["error"],
                    "error_code": "TARGET_NOT_FOUND"}
        resolved_engine = described["engine"]
        data_api = described["data_api"]
        endpoint = endpoint or described["endpoint"]
        endpoint_port = endpoint_port or described["port"]
        secret_arn = secret_arn or described["secret_arn"]
        database_name = database_name or described["database_name"]
        cluster_arn = cluster_arn or described["cluster_arn"]

    if resolved_engine:
        family = _RDS_ENGINE_FAMILY.get(resolved_engine)
        if not family:
            return {
                "success": False,
                "error": f"Unrecognized RDS engine '{resolved_engine}'. Supported "
                         f"engines: {sorted(set(_RDS_ENGINE_FAMILY))}.",
                "error_code": "UNSUPPORTED_ENGINE",
            }
        if db_type in ("rds", "aurora"):
            db_type = _RDS_ENGINE_DB_TYPE.get(resolved_engine, db_type)
        elif _DB_TYPE_FAMILY.get(db_type) and _DB_TYPE_FAMILY[db_type] != family:
            # trust what RDS reports over what the caller guessed
            db_type = _RDS_ENGINE_DB_TYPE.get(resolved_engine, db_type)
    else:
        if db_type in ("rds", "aurora"):
            return {
                "success": False,
                "error": "db_type 'rds'/'aurora' needs rds_instance_identifier so the "
                         "engine can be detected, or name the engine explicitly "
                         f"(one of {sorted(_SQL_DB_TYPES)}).",
                "error_code": "MISSING_PARAMETERS",
            }
        family = _DB_TYPE_FAMILY[db_type]

    if not database_name and family != "oracle":
        return {
            "success": False,
            "error": "rds_database_name is required (the database/schema to inspect). "
                     "Pass rds_instance_identifier to have it resolved automatically "
                     "when the instance declares a default database.",
            "error_code": "MISSING_PARAMETERS",
        }

    # Data API path — Aurora with the HTTP endpoint enabled
    if data_api and cluster_arn and secret_arn:
        return {
            "success": True, "method": "data_api", "family": family,
            "db_type": db_type, "database_name": database_name,
            "cluster_arn": cluster_arn, "secret_arn": secret_arn,
            "region": region, "engine": resolved_engine,
        }

    # Driver path — needs an endpoint and credentials
    if not endpoint:
        hint = ("The Data API is not enabled on this cluster, so a direct connection "
                "is required. ") if cluster_arn else ""
        return {
            "success": False,
            "error": f"{hint}No endpoint to connect to. Pass rds_instance_identifier "
                     f"(preferred — the endpoint and credentials are resolved from RDS) "
                     f"or rds_host explicitly.",
            "error_code": "MISSING_PARAMETERS",
        }

    if not (username and password):
        if not secret_arn:
            return {
                "success": False,
                "error": "No credentials. Pass rds_secret_arn (a Secrets Manager secret "
                         "holding username/password — RDS-managed master user secrets "
                         "work as-is) or rds_username plus rds_password.",
                "error_code": "MISSING_PARAMETERS",
            }
        creds = _read_db_secret(secret_arn, region)
        if creds.get("error"):
            return {"success": False, "error": creds["error"],
                    "error_code": "SECRET_UNREADABLE"}
        username = username or creds.get("username")
        password = password or creds.get("password")
        endpoint = endpoint or creds.get("host")
        endpoint_port = endpoint_port or creds.get("port")
        database_name = database_name or creds.get("dbname")

    return {
        "success": True, "method": "driver", "family": family, "db_type": db_type,
        "database_name": database_name, "host": endpoint,
        "port": int(endpoint_port or _DEFAULT_PORTS[family]),
        "username": username, "password": password,
        "secret_arn": secret_arn, "cluster_arn": cluster_arn,
        "region": region, "engine": resolved_engine,
    }


def _describe_rds_target(rds, identifier: str) -> dict:
    """Look up a DB instance or Aurora cluster and pull out what we need."""
    try:
        clusters = rds.describe_db_clusters(DBClusterIdentifier=identifier)["DBClusters"]
        if clusters:
            c = clusters[0]
            return {
                "engine": c.get("Engine"),
                "data_api": bool(c.get("HttpEndpointEnabled")),
                "endpoint": c.get("Endpoint"),
                "port": c.get("Port"),
                "secret_arn": (c.get("MasterUserSecret") or {}).get("SecretArn"),
                "database_name": c.get("DatabaseName"),
                "cluster_arn": c.get("DBClusterArn"),
            }
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in (
                "DBClusterNotFoundFault", "DBClusterNotFound", "InvalidParameterValue"):
            return {"error": f"Could not describe cluster '{identifier}': {e}"}
    except Exception as e:
        return {"error": f"Could not describe cluster '{identifier}': {e}"}

    try:
        instances = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("DBInstanceNotFound", "DBInstanceNotFoundFault"):
            return {"error": f"No RDS DB instance or Aurora cluster named "
                             f"'{identifier}' in this region."}
        return {"error": f"Could not describe instance '{identifier}': {e}"}
    except Exception as e:
        return {"error": f"Could not describe instance '{identifier}': {e}"}

    if not instances:
        return {"error": f"No RDS DB instance named '{identifier}' in this region."}
    i = instances[0]
    endpoint = i.get("Endpoint") or {}
    cluster_id = i.get("DBClusterIdentifier")
    data_api = False
    cluster_arn = None
    if cluster_id:
        # an Aurora writer instance — the Data API lives on its cluster
        try:
            c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
            data_api = bool(c.get("HttpEndpointEnabled"))
            cluster_arn = c.get("DBClusterArn")
        except Exception:
            pass
    return {
        "engine": i.get("Engine"),
        "data_api": data_api,
        "endpoint": endpoint.get("Address"),
        "port": endpoint.get("Port"),
        "secret_arn": (i.get("MasterUserSecret") or {}).get("SecretArn"),
        "database_name": i.get("DBName"),
        "cluster_arn": cluster_arn,
    }


def _read_db_secret(secret_arn: str, region: str) -> dict:
    """Read username/password (and host/port/dbname when present) from a secret."""
    try:
        sm = boto3.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=secret_arn).get("SecretString") or "{}"
        data = json.loads(raw)
    except ClientError as e:
        return {"error": f"Could not read secret {secret_arn}: "
                         f"{e.response.get('Error', {}).get('Message', e)}. The runtime "
                         f"role needs secretsmanager:GetSecretValue on it."}
    except Exception as e:
        return {"error": f"Could not parse secret {secret_arn}: {e}"}
    if not data.get("username") or not data.get("password"):
        return {"error": f"Secret {secret_arn} has no username/password keys. "
                         f"Found: {sorted(data)}"}
    return {
        "username": data.get("username"),
        "password": data.get("password"),
        "host": data.get("host"),
        "port": data.get("port"),
        "dbname": data.get("dbname") or data.get("dbName"),
    }


def _make_runner(target: dict):
    """Return (run(sql) -> list[dict], close()) for the resolved target."""
    if target["method"] == "data_api":
        return _data_api_runner(target)
    return _driver_runner(target)


def _data_api_runner(target: dict):
    rds_data = boto3.client("rds-data", region_name=target["region"])

    def run(sql: str) -> list[dict]:
        kwargs = dict(
            resourceArn=target["cluster_arn"],
            secretArn=target["secret_arn"],
            database=target["database_name"],
            sql=sql,
            includeResultMetadata=True,   # ← without this there is no columnMetadata
        )
        if ":database" in sql:
            kwargs["parameters"] = [
                {"name": "database", "value": {"stringValue": target["database_name"]}}
            ]
        return _parse_rds_result(rds_data.execute_statement(**kwargs))

    return run, lambda: None


def _driver_runner(target: dict):
    """Open a direct connection and return a query runner over it."""
    family = target["family"]
    host, port = target["host"], target["port"]
    user, pwd = target["username"], target["password"]
    database = target["database_name"]

    if family == "postgresql":
        try:
            import psycopg2
        except ImportError as e:
            raise _DriverMissing(_driver_missing_message(family)) from e
        conn = psycopg2.connect(host=host, port=port, user=user, password=pwd,
                                dbname=database, connect_timeout=15)
        conn.set_session(readonly=True, autocommit=True)
        paramstyle = "pyformat"

    elif family == "mysql":
        try:
            import pymysql
        except ImportError as e:
            raise _DriverMissing(_driver_missing_message(family)) from e
        conn = pymysql.connect(host=host, port=port, user=user, password=pwd,
                               database=database, connect_timeout=15,
                               charset="utf8mb4")
        paramstyle = "pyformat"

    elif family == "sqlserver":
        try:
            import pytds
        except ImportError as e:
            raise _DriverMissing(_driver_missing_message(family)) from e
        conn = pytds.connect(server=host, port=port, user=user, password=pwd,
                             database=database, login_timeout=15, autocommit=True)
        paramstyle = "pyformat"

    elif family == "oracle":
        try:
            import oracledb
        except ImportError as e:
            raise _DriverMissing(_driver_missing_message(family)) from e
        conn = oracledb.connect(user=user, password=pwd,
                                dsn=f"{host}:{port}/{database}")
        paramstyle = "named"

    elif family == "db2":
        try:
            import ibm_db_dbi
        except ImportError as e:
            raise _DriverMissing(_driver_missing_message(family)) from e
        dsn = (f"DATABASE={database};HOSTNAME={host};PORT={port};"
               f"PROTOCOL=TCPIP;UID={user};PWD={pwd};")
        conn = ibm_db_dbi.connect(dsn, "", "")
        paramstyle = "qmark"

    else:  # pragma: no cover - guarded by the registry
        raise _DriverMissing(f"No driver mapping for engine family '{family}'")

    def run(sql: str) -> list[dict]:
        statement, params = _adapt_named_params(sql, paramstyle, database)
        cur = conn.cursor()
        try:
            cur.execute(statement, params) if params else cur.execute(statement)
            if cur.description is None:
                return []
            names = [str(d[0]).upper() for d in cur.description]
            return [dict(zip(names, _normalize_row(row))) for row in cur.fetchall()]
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def close():
        try:
            conn.close()
        except Exception:
            pass

    return run, close


def _adapt_named_params(sql: str, paramstyle: str, database: str):
    """Rewrite the `:database` placeholder for the driver's paramstyle."""
    if ":database" not in sql:
        return sql, None
    if paramstyle == "pyformat":
        return sql.replace(":database", "%(database)s"), {"database": database}
    if paramstyle == "qmark":
        return sql.replace(":database", "?"), (database,)
    return sql, {"database": database}          # oracledb takes :named as-is


def _normalize_row(row) -> list:
    """Make driver row values JSON-safe (Decimal, datetime, bytes, LOBs)."""
    out = []
    for v in row:
        if v is None or isinstance(v, (str, int, float, bool)):
            out.append(v)
        elif isinstance(v, Decimal):
            out.append(str(v))
        elif isinstance(v, (bytes, bytearray, memoryview)):
            out.append("<binary>")
        elif isinstance(v, (datetime, date, time)):
            out.append(v.isoformat())
        elif hasattr(v, "read"):                # Oracle LOB
            try:
                out.append(str(v.read())[:500])
            except Exception:
                out.append("<lob>")
        else:
            out.append(str(v))
    return out


def _driver_missing_message(family: str) -> str:
    package = _DRIVER_PACKAGE[family]
    return (f"The {family} driver is not installed, so a direct connection cannot be "
            f"opened. Add `{package}` to backend/ecs/requirements.txt and redeploy. "
            f"(Aurora clusters can avoid the driver entirely by enabling the RDS "
            f"Data API.)")


def _explain_connect_error(e: Exception, target: dict) -> str:
    text = str(e)
    where = f"{target.get('host')}:{target.get('port')}"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return (f"Timed out connecting to {where}. The endpoint is not reachable from "
                f"the runtime: check the security group allows inbound "
                f"{target.get('port')} from this VPC/CIDR, that the subnets have a "
                f"route, and that the instance is not private-only. Original: {text}")
    if "password authentication" in text.lower() or "access denied" in text.lower():
        return (f"Authentication rejected by {where}. Verify the credentials in the "
                f"secret match this database. Original: {text}")
    if "does not exist" in text.lower() or "unknown database" in text.lower():
        return (f"Database '{target.get('database_name')}' does not exist on {where}. "
                f"Original: {text}")
    return f"Could not connect to {where}: {text}"


def _introspect_sql(
    db_type: str,
    secret_arn: str,
    database_name: str,
    cluster_arn: str,
    table_name: Optional[str],
    region: str,
    instance_identifier: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    tables_only: bool = False,
    include_sample_rows: bool = True,
) -> dict:
    """Introspect any RDS or Aurora database.

    Uses the RDS Data API when the target is an Aurora cluster with the HTTP
    endpoint enabled, and a direct driver connection otherwise.
    """
    target = _resolve_sql_target(
        db_type=db_type, region=region, instance_identifier=instance_identifier,
        cluster_arn=cluster_arn, secret_arn=secret_arn, database_name=database_name,
        host=host, port=port, username=username, password=password,
    )
    if not target.get("success"):
        return target

    family = target["family"]
    db_type = target["db_type"]
    database_name = target["database_name"]
    queries = _QUERY_SETS[family]

    try:
        runner, close = _make_runner(target)
    except _DriverMissing as e:
        return {
            "success": False,
            "error": str(e),
            "error_code": "DRIVER_NOT_INSTALLED",
            "db_type": db_type,
        }
    except ClientError as e:
        return {"success": False, **_explain_rds_error(e, db_type, target.get("cluster_arn"), region)}
    except Exception as e:
        return {
            "success": False,
            "error": _explain_connect_error(e, target),
            "error_code": "CONNECTION_FAILED",
            "db_type": db_type,
        }

    def run(sql: str) -> list[dict]:
        return runner(sql)

    try:
        tables = run(queries["tables"])
    except ClientError as e:
        close()
        return {"success": False, **_explain_rds_error(e, db_type, target.get("cluster_arn"), region)}
    except Exception as e:
        close()
        return {
            "success": False,
            "error": f"Failed to list tables in {db_type} database '{database_name}': {e}",
            "error_code": "QUERY_FAILED",
            "db_type": db_type,
        }

    # Table names are matched case-insensitively so callers do not need to know
    # the storage casing (Oracle and Db2 fold unquoted identifiers to upper case).
    wanted_raw = _split_names(table_name)
    wanted = {w.lower() for w in wanted_raw}
    discovered = [r.get("TABLE_NAME") for r in tables if r.get("TABLE_NAME")]
    selected = [n for n in discovered if not wanted or n.lower() in wanted]

    if not selected:
        close()
        return {
            "success": False,
            "error": (
                f"No tables found in database '{database_name}'"
                + (f" matching {sorted(wanted_raw)}" if wanted_raw else "")
                + f". Discovered {len(discovered)} table(s) total"
                + (f": {sorted(discovered)}" if discovered else "")
                + ". Check the database name and the table spelling "
                + _schema_scope_hint(family)
            ),
            "error_code": "NO_TABLES_FOUND",
            "db_type": db_type,
            "database_name": database_name,
            "available_tables": sorted(discovered),
        }

    if tables_only:
        inventory = []
        for row in tables:
            name = row.get("TABLE_NAME")
            if name not in selected:
                continue
            inventory.append({
                "table_name": name,
                "table_type": row.get("TABLE_TYPE"),
                "table_comment": row.get("TABLE_COMMENT") or None,
                "estimated_rows": row.get("TABLE_ROWS"),
                "size_bytes": row.get("DATA_LENGTH"),
            })
        close()
        return {
            "success": True,
            "db_type": db_type,
            "engine": family,
            "rds_engine": target.get("engine"),
            "database_name": database_name,
            "region": region,
            "access_method": ("rds-data-api" if target["method"] == "data_api"
                              else f"{family}-driver"),
            "mode": "tables_only",
            "table_count": len(inventory),
            "tables": inventory,
            "next_step": ("Pick the tables the operations need and call again with "
                          "table_name='a,b,c' for the full column/index/foreign-key "
                          "detail."),
        }

    try:
        columns = run(queries["columns"])
        indexes = run(queries["indexes"])
        foreign_keys = run(queries["foreign_keys"])
        primary_keys = [] if family == "mysql" else run(queries["primary_keys"])
        enums = _group_enums(run(queries["enums"])) if family == "postgresql" else {}
        checks = run(queries["checks"]) if "checks" in queries else []
    except ClientError as e:
        close()
        return {"success": False, **_explain_rds_error(e, db_type, target.get("cluster_arn"), region)}
    except Exception as e:
        close()
        return {
            "success": False,
            "error": f"Failed to introspect {db_type} database '{database_name}': {e}",
            "error_code": "QUERY_FAILED",
            "db_type": db_type,
        }

    is_mysql = family == "mysql"
    selected_set = set(selected)

    table_schemas: dict[str, dict] = {}
    for row in tables:
        name = row.get("TABLE_NAME")
        if not name or name not in selected_set:
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
        open_q, close_q = _QUOTE[family]
        for tname, schema in table_schemas.items():
            if schema["table_type"] not in ("BASE TABLE", "PARTITIONED TABLE", None):
                continue
            quoted = f"{open_q}{tname}{close_q}"
            try:
                rows = run(_limited_select(family, quoted, SAMPLE_LIMIT))
                schema["sample_rows"] = [_stringify_row(r) for r in rows]
                schema["actual_row_count"] = _count_rows(run, quoted)
            except Exception as e:
                warnings.append(f"Could not sample rows from '{tname}': {e}")
            try:
                _collect_observed_values(run, family, schema)
            except Exception as e:
                warnings.append(f"Could not read distinct values for '{tname}': {e}")

    relationships = [
        f"{fk['table']}.{','.join(fk['columns'])} → "
        f"{fk['referenced_table']}.{','.join(fk['referenced_columns'])}"
        for fk in grouped_fks.values()
    ]

    close()

    result = {
        "success": True,
        "db_type": db_type,
        "engine": family,
        "rds_engine": target.get("engine"),
        "database_name": database_name,
        "cluster_arn": target.get("cluster_arn"),
        "secret_arn": target.get("secret_arn"),
        "host": target.get("host"),
        "port": target.get("port"),
        "region": region,
        "access_method": "rds-data-api" if target["method"] == "data_api" else f"{family}-driver",
        "table_count": len(table_schemas),
        "tables": list(table_schemas.values()),
        "relationships": sorted(relationships),
        "enum_types": enums or None,
        "suggestions": _generate_rds_suggestions(table_schemas, grouped_fks),
    }
    if warnings:
        result["warnings"] = warnings
    return result


_ENUM_LIKE_NAME_RE = re.compile(
    r'(^|_)(status|state|type|kind|category|code|level|tier|grade|reason|'
    r'method|channel|result|stage|priority|flag|mode)(_|$)', re.I)
# beyond this many rows a DISTINCT scan is too expensive to be worth it
OBSERVED_VALUES_ROW_LIMIT = 200_000
OBSERVED_VALUES_MAX_COLUMNS = 12
OBSERVED_VALUES_MAX_DISTINCT = 25


def _collect_observed_values(run, family: str, schema: dict) -> None:
    """Fill `observed_values` for enum-like columns that declare no allowed set.

    PostgreSQL and MySQL expose real enum types, so the allowed values are read
    straight from the catalog. SQL Server, Oracle and Db2 have no ENUM — a status
    column is just VARCHAR — so without this the generator has nothing to go on
    and invents values (observed live: it wrote `LOST` for a column whose actual
    values are `LOSS`, `DAMAGE`, `DELAY`, `WRONG_DELIVERY`, and the INSERT then
    stored a value the business never uses).

    Deliberately cheap: only text columns whose name looks categorical, only on
    tables small enough to scan, capped per table.
    """
    open_q, close_q = _QUOTE[family]
    rows_known = schema.get("actual_row_count")
    if isinstance(rows_known, (int, float)) and rows_known > OBSERVED_VALUES_ROW_LIMIT:
        return
    table = f"{open_q}{schema['table_name']}{close_q}"
    probed = 0
    for col in schema.get("columns", []):
        if probed >= OBSERVED_VALUES_MAX_COLUMNS:
            break
        name = col.get("name") or ""
        if col.get("allowed_values") or not _ENUM_LIKE_NAME_RE.search(name):
            continue
        raw_type = str(col.get("full_type") or col.get("type") or "").lower()
        if not any(t in raw_type for t in ("char", "text", "string")):
            continue
        quoted_col = f"{open_q}{name}{close_q}"
        sql = (f"SELECT DISTINCT {quoted_col} AS v FROM {table} "
               f"WHERE {quoted_col} IS NOT NULL")
        try:
            values = run(_limit_distinct(family, sql, OBSERVED_VALUES_MAX_DISTINCT + 1))
        except Exception:
            continue
        probed += 1
        found = [list(r.values())[0] for r in values if list(r.values())[0] is not None]
        if found and len(found) <= OBSERVED_VALUES_MAX_DISTINCT:
            col["observed_values"] = sorted(str(v) for v in found)
            col["observed_values_note"] = (
                "distinct values currently present in the column; this engine has no "
                "declared enum, so use these exact spellings rather than inventing any")


def _limit_distinct(family: str, sql: str, limit: int) -> str:
    if family == "sqlserver":
        return sql.replace("SELECT DISTINCT", f"SELECT DISTINCT TOP {limit}", 1)
    if family in ("oracle", "db2"):
        return f"{sql} FETCH FIRST {limit} ROWS ONLY"
    return f"{sql} LIMIT {limit}"


def _schema_scope_hint(family: str) -> str:
    """Tell the caller which schema/owner the queries actually look at."""
    return {
        "postgresql": "(PostgreSQL introspection reads the 'public' schema).",
        "mysql": "(MySQL/MariaDB introspection reads the database you named).",
        "sqlserver": "(SQL Server introspection reads the 'dbo' schema).",
        "oracle": "(Oracle introspection reads objects owned by the connecting user).",
        "db2": "(Db2 introspection reads CURRENT SCHEMA).",
    }.get(family, "")


def _limited_select(family: str, quoted_table: str, limit: int) -> str:
    """`SELECT * ... LIMIT n` in the dialect the engine actually accepts."""
    if family == "sqlserver":
        return f"SELECT TOP {limit} * FROM {quoted_table}"
    if family == "oracle":
        return f"SELECT * FROM {quoted_table} FETCH FIRST {limit} ROWS ONLY"
    if family == "db2":
        return f"SELECT * FROM {quoted_table} FETCH FIRST {limit} ROWS ONLY"
    return f"SELECT * FROM {quoted_table} LIMIT {limit}"


def _count_rows(run, quoted_table: str):
    try:
        rows = run(f"SELECT COUNT(*) AS row_count FROM {quoted_table}")
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

    if db_type in _SQL_DB_TYPES:
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
    engine = r.get("engine") or _DB_TYPE_FAMILY.get(r.get("db_type"), "postgresql")
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
                    "allowed_values": c.get("allowed_values") or c.get("observed_values"),
                    "allowed_values_source": ("declared" if c.get("allowed_values")
                                              else ("observed" if c.get("observed_values")
                                                    else None)),
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
