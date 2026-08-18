"""Regression test: the scanned access_method must survive the conversion.

`introspect_database()` picks its own connection method per target — the RDS Data
API on Aurora with the HTTP endpoint enabled, a real engine driver everywhere
else — and reports it as `access_method`. `convert_to_infrastructure_schema()`
used to throw that away and hardcode `"rds-data-api"` (plus the Data-API-only
`connection` / `environment_variables` / `iam_requirements`).

Consequence: every driver-only database — plain RDS PostgreSQL/MySQL/MariaDB,
SQL Server, Oracle, Db2, or Aurora with the endpoint off — presented downstream
as Data API. `lambda_generator`'s driver branch keys on
`access_method == "<engine>-driver"`, so it was unreachable: the generated
handler called `rds-data:ExecuteStatement` against a cluster that has no Data
API, was given a `DB_CLUSTER_ARN` that does not exist, and failed on its first
query. The CloudFormation also asked for `rds-data:*` IAM the function cannot
use and omitted the `VpcConfig` a driver connection needs.

The two paths carry different contracts, so the test pins both:

    rds-data-api     connection{cluster_arn, secret_arn, database_name}
                     env{DB_CLUSTER_ARN, DB_SECRET_ARN, DB_NAME}
                     iam[rds-data:*, secretsmanager:GetSecretValue]

    <engine>-driver  connection{host, port, secret_arn, database_name}
                     env{DB_SECRET_ARN, DB_HOST, DB_PORT, DB_NAME}
                     iam[secretsmanager:GetSecretValue]
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.db_introspector import convert_to_infrastructure_schema  # noqa: E402

SECRET_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:orders-db-AbCdEf"
CLUSTER_ARN = "arn:aws:rds:ap-northeast-2:111122223333:cluster:orders-aurora"

# One table is enough — this is about the connection contract, not the columns.
TABLES = [
    {
        "table_name": "orders",
        "table_type": "BASE TABLE",
        "primary_key": ["order_id"],
        "columns": [
            {"name": "order_id", "full_type": "bigint", "nullable": False},
            {"name": "status", "full_type": "varchar(20)", "nullable": False},
        ],
    }
]


def _scan(**overrides) -> dict:
    """A successful introspect_database() result, shaped like the real one."""
    base = {
        "success": True,
        "db_type": "aurora_postgresql",
        "engine": "postgresql",
        "rds_engine": "aurora-postgresql",
        "database_name": "ordersdb",
        "region": "ap-northeast-2",
        "cluster_arn": CLUSTER_ARN,
        "secret_arn": SECRET_ARN,
        "host": None,
        "port": None,
        "access_method": "rds-data-api",
        "table_count": len(TABLES),
        "tables": TABLES,
        "relationships": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Data API path — unchanged behaviour
# ---------------------------------------------------------------------------


def test_data_api_scan_keeps_the_data_api_contract():
    schema = convert_to_infrastructure_schema(_scan())

    assert schema["access_method"] == "rds-data-api"
    assert schema["connection"] == {
        "cluster_arn": CLUSTER_ARN,
        "secret_arn": SECRET_ARN,
        "database_name": "ordersdb",
    }
    assert schema["environment_variables"] == {
        "DB_CLUSTER_ARN": CLUSTER_ARN,
        "DB_SECRET_ARN": SECRET_ARN,
        "DB_NAME": "ordersdb",
    }
    assert "rds-data:ExecuteStatement" in schema["iam_requirements"]
    assert "secretsmanager:GetSecretValue" in schema["iam_requirements"]


# ---------------------------------------------------------------------------
# Driver path — the regression
# ---------------------------------------------------------------------------

# (db_type, engine) pairs that can only ever be reached by a driver.
DRIVER_ENGINES = [
    ("rds_postgresql", "postgresql"),
    ("rds_mysql", "mysql"),
    ("rds_mariadb", "mysql"),
    ("rds_sqlserver", "sqlserver"),
    ("rds_oracle", "oracle"),
    ("rds_db2", "db2"),
]


@pytest.mark.parametrize("db_type,engine", DRIVER_ENGINES)
def test_driver_scan_keeps_the_driver_contract(db_type, engine):
    host = f"orders.cluster-abc123.ap-northeast-2.rds.amazonaws.com"
    schema = convert_to_infrastructure_schema(
        _scan(
            db_type=db_type,
            engine=engine,
            rds_engine=db_type.replace("rds_", ""),
            access_method=f"{engine}-driver",
            cluster_arn=None,  # a non-Aurora instance has none
            host=host,
            port=5432,
        )
    )

    assert schema["access_method"] == f"{engine}-driver"
    assert schema["connection"] == {
        "host": host,
        "port": 5432,
        "secret_arn": SECRET_ARN,
        "database_name": "ordersdb",
    }
    assert schema["environment_variables"] == {
        "DB_SECRET_ARN": SECRET_ARN,
        "DB_HOST": host,
        "DB_PORT": "5432",  # env vars are strings; the handler int()s it
        "DB_NAME": "ordersdb",
    }
    # No rds-data:* — the function cannot use it and asking for it is a finding
    # for the IAM consistency check.
    assert schema["iam_requirements"] == ["secretsmanager:GetSecretValue"]
    assert not any(p.startswith("rds-data:") for p in schema["iam_requirements"])
    assert "DB_CLUSTER_ARN" not in schema["environment_variables"]


def test_aurora_with_the_http_endpoint_off_is_a_driver_target():
    """Same engine as the Data API case — only the scan's verdict differs."""
    schema = convert_to_infrastructure_schema(
        _scan(access_method="postgresql-driver", host="aur.example.rds.amazonaws.com", port=5432)
    )

    assert schema["access_method"] == "postgresql-driver"
    assert "DB_HOST" in schema["environment_variables"]
    assert schema["iam_requirements"] == ["secretsmanager:GetSecretValue"]


# ---------------------------------------------------------------------------
# Payloads that predate `access_method`
# ---------------------------------------------------------------------------


def test_missing_access_method_is_inferred_from_the_coordinates():
    """Hand-built / cached payloads omit it; infer, don't guess a fixed value."""
    data_api = _scan()
    data_api.pop("access_method")
    assert convert_to_infrastructure_schema(data_api)["access_method"] == "rds-data-api"

    driver = _scan(db_type="rds_mysql", engine="mysql", cluster_arn=None,
                   host="db.example.rds.amazonaws.com", port=3306)
    driver.pop("access_method")
    assert convert_to_infrastructure_schema(driver)["access_method"] == "mysql-driver"


def test_port_none_does_not_become_the_string_none():
    """A schema-as-document payload may know the host but not the port."""
    driver = _scan(db_type="rds_mysql", engine="mysql", access_method="mysql-driver",
                   cluster_arn=None, host="db.example.rds.amazonaws.com", port=None)
    env = convert_to_infrastructure_schema(driver)["environment_variables"]
    assert env["DB_PORT"] is None
