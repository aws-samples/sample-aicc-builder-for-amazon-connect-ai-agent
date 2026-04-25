"""
Database Introspector Tool

Connects to customer databases (DynamoDB, RDS MySQL/PostgreSQL)
and discovers schema information to help generate accurate Lambda functions.
"""

import json
from typing import Optional
from strands import tool
import boto3


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
) -> dict:
    """
    Introspect a database to discover its schema structure.

    This tool connects to the specified database and returns detailed schema
    information including tables, columns, types, keys, and indexes.

    Use this BEFORE generating Lambda functions to understand the data structure.

    Args:
        db_type: Database type - 'dynamodb', 'rds_mysql', or 'rds_postgresql'
        table_name: Specific table to introspect (optional, introspects all if not specified)
        region: AWS region where the database is located
        dynamodb_table_name: For DynamoDB - the table name to describe
        rds_secret_arn: For RDS - Secrets Manager ARN containing connection credentials
        rds_database_name: For RDS - the database name
        rds_cluster_arn: For RDS Data API - the cluster ARN

    Returns:
        Schema information including:
        - Table names
        - Column/attribute definitions
        - Primary keys and sort keys
        - Global Secondary Indexes (GSIs)
        - Sample data structure
        - Estimated item count
    """

    if db_type == "dynamodb":
        return _introspect_dynamodb(dynamodb_table_name or table_name, region)
    elif db_type in ("rds_mysql", "rds_postgresql"):
        return _introspect_rds(
            db_type=db_type,
            secret_arn=rds_secret_arn,
            database_name=rds_database_name,
            cluster_arn=rds_cluster_arn,
            table_name=table_name,
            region=region
        )
    else:
        return {
            "success": False,
            "error": f"Unsupported database type: {db_type}",
            "supported_types": ["dynamodb", "rds_mysql", "rds_postgresql"]
        }


def _introspect_dynamodb(table_name: str, region: str) -> dict:
    """Introspect a DynamoDB table."""
    if not table_name:
        return {
            "success": False,
            "error": "table_name is required for DynamoDB introspection"
        }

    try:
        dynamodb = boto3.client("dynamodb", region_name=region)

        # Describe the table
        response = dynamodb.describe_table(TableName=table_name)
        table = response["Table"]

        # Extract key schema
        key_schema = []
        for key in table.get("KeySchema", []):
            key_type = "partition_key" if key["KeyType"] == "HASH" else "sort_key"
            key_schema.append({
                "attribute_name": key["AttributeName"],
                "key_type": key_type
            })

        # Extract attribute definitions
        attributes = []
        for attr in table.get("AttributeDefinitions", []):
            attr_type_map = {"S": "string", "N": "number", "B": "binary"}
            attributes.append({
                "name": attr["AttributeName"],
                "type": attr_type_map.get(attr["AttributeType"], attr["AttributeType"])
            })

        # Extract GSIs
        gsis = []
        for gsi in table.get("GlobalSecondaryIndexes", []):
            gsi_keys = []
            for key in gsi.get("KeySchema", []):
                key_type = "partition_key" if key["KeyType"] == "HASH" else "sort_key"
                gsi_keys.append({
                    "attribute_name": key["AttributeName"],
                    "key_type": key_type
                })
            gsis.append({
                "index_name": gsi["IndexName"],
                "key_schema": gsi_keys,
                "projection_type": gsi.get("Projection", {}).get("ProjectionType", "ALL")
            })

        # Sample some items to infer full schema
        sample_attributes = set()
        try:
            scan_response = dynamodb.scan(
                TableName=table_name,
                Limit=10
            )
            for item in scan_response.get("Items", []):
                for attr_name, attr_value in item.items():
                    # Determine type from value
                    if "S" in attr_value:
                        sample_attributes.add((attr_name, "string"))
                    elif "N" in attr_value:
                        sample_attributes.add((attr_name, "number"))
                    elif "BOOL" in attr_value:
                        sample_attributes.add((attr_name, "boolean"))
                    elif "L" in attr_value:
                        sample_attributes.add((attr_name, "list"))
                    elif "M" in attr_value:
                        sample_attributes.add((attr_name, "map"))
                    elif "SS" in attr_value:
                        sample_attributes.add((attr_name, "string_set"))
                    elif "NS" in attr_value:
                        sample_attributes.add((attr_name, "number_set"))
        except Exception as scan_error:
            # Non-fatal - we just won't have sample data
            pass

        # Combine defined attributes with sampled ones
        all_attributes = {attr["name"]: attr["type"] for attr in attributes}
        for attr_name, attr_type in sample_attributes:
            if attr_name not in all_attributes:
                all_attributes[attr_name] = attr_type

        return {
            "success": True,
            "db_type": "dynamodb",
            "table_name": table_name,
            "table_status": table.get("TableStatus"),
            "item_count": table.get("ItemCount", 0),
            "table_size_bytes": table.get("TableSizeBytes", 0),
            "key_schema": key_schema,
            "attributes": [
                {"name": name, "type": type_}
                for name, type_ in sorted(all_attributes.items())
            ],
            "global_secondary_indexes": gsis,
            "billing_mode": table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
            "stream_enabled": table.get("StreamSpecification", {}).get("StreamEnabled", False),
            "suggestions": _generate_dynamodb_suggestions(key_schema, gsis, all_attributes)
        }

    except dynamodb.exceptions.ResourceNotFoundException:
        return {
            "success": False,
            "error": f"Table '{table_name}' not found in region {region}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to introspect DynamoDB table: {str(e)}"
        }


def _introspect_rds(
    db_type: str,
    secret_arn: str,
    database_name: str,
    cluster_arn: str,
    table_name: Optional[str],
    region: str
) -> dict:
    """Introspect an RDS database using the Data API."""
    if not all([secret_arn, database_name, cluster_arn]):
        return {
            "success": False,
            "error": "rds_secret_arn, rds_database_name, and rds_cluster_arn are all required for RDS introspection"
        }

    try:
        rds_data = boto3.client("rds-data", region_name=region)

        # Query to get table information
        if db_type == "rds_mysql":
            tables_query = """
                SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, DATA_LENGTH
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = :database
            """
            columns_query = """
                SELECT
                    TABLE_NAME,
                    COLUMN_NAME,
                    DATA_TYPE,
                    IS_NULLABLE,
                    COLUMN_KEY,
                    COLUMN_DEFAULT,
                    CHARACTER_MAXIMUM_LENGTH,
                    NUMERIC_PRECISION,
                    NUMERIC_SCALE,
                    COLUMN_COMMENT
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :database
                ORDER BY TABLE_NAME, ORDINAL_POSITION
            """
            indexes_query = """
                SELECT
                    TABLE_NAME,
                    INDEX_NAME,
                    COLUMN_NAME,
                    NON_UNIQUE,
                    SEQ_IN_INDEX
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = :database
                ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """
        else:  # PostgreSQL
            tables_query = """
                SELECT tablename as TABLE_NAME, 'BASE TABLE' as TABLE_TYPE
                FROM pg_tables
                WHERE schemaname = 'public'
            """
            columns_query = """
                SELECT
                    table_name as TABLE_NAME,
                    column_name as COLUMN_NAME,
                    data_type as DATA_TYPE,
                    is_nullable as IS_NULLABLE,
                    column_default as COLUMN_DEFAULT,
                    character_maximum_length as CHARACTER_MAXIMUM_LENGTH,
                    numeric_precision as NUMERIC_PRECISION,
                    numeric_scale as NUMERIC_SCALE
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
            """
            indexes_query = """
                SELECT
                    t.relname as TABLE_NAME,
                    i.relname as INDEX_NAME,
                    a.attname as COLUMN_NAME,
                    ix.indisunique as IS_UNIQUE
                FROM pg_class t, pg_class i, pg_index ix, pg_attribute a
                WHERE t.oid = ix.indrelid
                    AND i.oid = ix.indexrelid
                    AND a.attrelid = t.oid
                    AND a.attnum = ANY(ix.indkey)
                    AND t.relkind = 'r'
                ORDER BY t.relname, i.relname
            """

        # Execute queries
        params = [{"name": "database", "value": {"stringValue": database_name}}]

        tables_result = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database_name,
            sql=tables_query,
            parameters=params
        )

        columns_result = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database_name,
            sql=columns_query,
            parameters=params
        )

        indexes_result = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database_name,
            sql=indexes_query,
            parameters=params
        )

        # Parse results
        tables = _parse_rds_result(tables_result)
        columns = _parse_rds_result(columns_result)
        indexes = _parse_rds_result(indexes_result)

        # Organize by table
        table_schemas = {}
        for table in tables:
            table_name_val = table.get("TABLE_NAME")
            if table_name and table_name_val != table_name:
                continue

            table_schemas[table_name_val] = {
                "table_name": table_name_val,
                "table_type": table.get("TABLE_TYPE"),
                "estimated_rows": table.get("TABLE_ROWS"),
                "columns": [],
                "indexes": [],
                "primary_key": []
            }

        # Add columns to tables
        for col in columns:
            table_name_val = col.get("TABLE_NAME")
            if table_name_val not in table_schemas:
                continue

            column_info = {
                "name": col.get("COLUMN_NAME"),
                "type": col.get("DATA_TYPE"),
                "nullable": col.get("IS_NULLABLE") == "YES",
                "default": col.get("COLUMN_DEFAULT"),
                "max_length": col.get("CHARACTER_MAXIMUM_LENGTH"),
                "precision": col.get("NUMERIC_PRECISION"),
                "scale": col.get("NUMERIC_SCALE"),
                "comment": col.get("COLUMN_COMMENT")
            }

            # Check if primary key
            if col.get("COLUMN_KEY") == "PRI":
                table_schemas[table_name_val]["primary_key"].append(col.get("COLUMN_NAME"))
                column_info["is_primary_key"] = True

            table_schemas[table_name_val]["columns"].append(column_info)

        # Add indexes to tables
        current_index = {}
        for idx in indexes:
            table_name_val = idx.get("TABLE_NAME")
            if table_name_val not in table_schemas:
                continue

            index_name = idx.get("INDEX_NAME")
            if index_name not in current_index:
                current_index[index_name] = {
                    "name": index_name,
                    "table": table_name_val,
                    "columns": [],
                    "unique": not idx.get("NON_UNIQUE", True)
                }
            current_index[index_name]["columns"].append(idx.get("COLUMN_NAME"))

        for idx in current_index.values():
            if idx["table"] in table_schemas:
                table_schemas[idx["table"]]["indexes"].append({
                    "name": idx["name"],
                    "columns": idx["columns"],
                    "unique": idx["unique"]
                })

        return {
            "success": True,
            "db_type": db_type,
            "database_name": database_name,
            "table_count": len(table_schemas),
            "tables": list(table_schemas.values()),
            "suggestions": _generate_rds_suggestions(table_schemas)
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to introspect RDS database: {str(e)}"
        }


def _parse_rds_result(result: dict) -> list[dict]:
    """Parse RDS Data API result into list of dicts."""
    records = result.get("records", [])
    column_metadata = result.get("columnMetadata", [])

    parsed = []
    for record in records:
        row = {}
        for i, field in enumerate(record):
            col_name = column_metadata[i]["name"] if i < len(column_metadata) else f"col_{i}"
            # Extract value from the typed field
            if "stringValue" in field:
                row[col_name] = field["stringValue"]
            elif "longValue" in field:
                row[col_name] = field["longValue"]
            elif "doubleValue" in field:
                row[col_name] = field["doubleValue"]
            elif "booleanValue" in field:
                row[col_name] = field["booleanValue"]
            elif "isNull" in field and field["isNull"]:
                row[col_name] = None
            else:
                row[col_name] = str(field)
        parsed.append(row)

    return parsed


def _generate_dynamodb_suggestions(key_schema: list, gsis: list, attributes: dict) -> list[str]:
    """Generate helpful suggestions based on DynamoDB schema."""
    suggestions = []

    # Find partition key
    pk = next((k["attribute_name"] for k in key_schema if k["key_type"] == "partition_key"), None)
    sk = next((k["attribute_name"] for k in key_schema if k["key_type"] == "sort_key"), None)

    if pk:
        suggestions.append(f"Primary lookup should use '{pk}' as the main query parameter")

    if sk:
        suggestions.append(f"Range queries are possible on '{sk}' within a partition")
    else:
        suggestions.append("No sort key defined - each partition key maps to exactly one item")

    if gsis:
        for gsi in gsis:
            gsi_pk = next((k["attribute_name"] for k in gsi["key_schema"] if k["key_type"] == "partition_key"), None)
            suggestions.append(f"GSI '{gsi['index_name']}' enables querying by '{gsi_pk}'")
    else:
        suggestions.append("No GSIs defined - consider adding indexes for common query patterns")

    return suggestions


def convert_to_infrastructure_schema(introspection_result: dict) -> dict:
    """
    Convert raw introspect_database() result to infrastructure_schema format.

    This allows generators (Lambda, OpenAPI, Prompt) to use existing DB schema
    in the same format as if Infrastructure Generator had produced it.

    Args:
        introspection_result: Output from introspect_database()

    Returns:
        infrastructure_schema compatible dict
    """
    if not introspection_result.get("success"):
        return {"error": "Introspection failed", "tables": []}

    table_name = introspection_result["table_name"]

    # Convert to entity name for env var (e.g., "hotel-reservations" → "RESERVATIONS")
    entity = table_name.split("-")[-1].upper() if "-" in table_name else table_name.upper()
    env_var_name = f"{entity}_TABLE_NAME"

    # Convert key_schema
    primary_key = None
    sort_key = None
    for key in introspection_result.get("key_schema", []):
        if key["key_type"] == "partition_key":
            primary_key = {"name": key["attribute_name"], "type": "S"}
        elif key["key_type"] == "sort_key":
            sort_key = {"name": key["attribute_name"], "type": "S"}

    # Convert GSIs
    gsi_indexes = []
    for gsi in introspection_result.get("global_secondary_indexes", []):
        gsi_entry = {
            "name": gsi["index_name"],
            "projection": gsi.get("projection_type", "ALL"),
        }
        for key in gsi.get("key_schema", []):
            if key["key_type"] == "partition_key":
                gsi_entry["partition_key"] = {"name": key["attribute_name"], "type": "S"}
            elif key["key_type"] == "sort_key":
                gsi_entry["sort_key"] = {"name": key["attribute_name"], "type": "S"}
        gsi_indexes.append(gsi_entry)

    # Build data_conventions from attributes
    data_conventions = {}
    for attr in introspection_result.get("attributes", []):
        name = attr["name"]
        if "phone" in name.lower():
            data_conventions[name] = {
                "format": "E.164 without +",
                "example": "821012345678",
                "gsi": next((g["name"] for g in gsi_indexes
                           if g.get("partition_key", {}).get("name") == name), None)
            }

    table_entry = {
        "logical_id": f"{entity.title()}Table",
        "table_name": table_name,
        "env_var_name": env_var_name,
        "primary_key": primary_key,
        "gsi_indexes": gsi_indexes,
        "existing": True,
    }
    if sort_key:
        table_entry["sort_key"] = sort_key

    return {
        "tables": [table_entry],
        "environment_variables": {
            env_var_name: table_name,  # Direct value, not !Ref
        },
        "data_conventions": data_conventions,
    }


def _generate_rds_suggestions(tables: dict) -> list[str]:
    """Generate helpful suggestions based on RDS schema."""
    suggestions = []

    for table_name, table_info in tables.items():
        pk = table_info.get("primary_key", [])
        if pk:
            suggestions.append(f"Table '{table_name}': Use {pk} for direct lookups")

        indexes = table_info.get("indexes", [])
        for idx in indexes:
            if idx["name"] != "PRIMARY":
                suggestions.append(
                    f"Table '{table_name}': Index '{idx['name']}' on {idx['columns']} for efficient queries"
                )

    return suggestions
