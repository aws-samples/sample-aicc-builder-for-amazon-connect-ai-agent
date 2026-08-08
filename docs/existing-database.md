# Building on an existing database

AICC Builder can generate its asset bundle against a database you already run,
instead of creating new tables. There are two ways in, and both end at the same
place: a schema contract that every downstream generator reads.

| Path | When to use it | Entry point |
|---|---|---|
| **Live scan** | The database is reachable from the deploying account | `introspect_database` |
| **Schema as document** | Direct DB access isn't permitted, or the DB doesn't exist yet | attach a DDL dump / ERD / data dictionary |

Supported: **DynamoDB**, and **every RDS and Aurora engine** — PostgreSQL,
MySQL, MariaDB, SQL Server, Oracle and Db2.

---

## 1. Live scan

`introspect_database` reads the schema and returns it in one shape regardless of
engine. It is read-only, so it is safe to call at any stage — during the
interview to design against the real schema, or mid-generation to re-check a
column name before patching an asset.

### The one-argument call

Pass the DB instance or Aurora cluster identifier and everything else is
resolved from RDS — engine, endpoint, port, master-user secret, and which
connection method to use:

```python
introspect_database(
    db_type="rds",                       # auto-detects the engine
    region="ap-northeast-1",
    rds_instance_identifier="my-db",
    rds_database_name="appdb",           # only when the instance has no default DB
)
```

### Connection method (chosen automatically)

| Target | Method | Needs a network path? |
|---|---|---|
| Aurora with the Data API (HTTP endpoint) enabled | RDS Data API | No |
| Aurora without it, and all plain RDS engines | driver connection | Yes — SG + subnet route |

Drivers ship in the image: `psycopg2` (PostgreSQL), `pymysql` (MySQL, MariaDB),
`pytds` (SQL Server), `oracledb` (Oracle, thin mode). Db2 needs `ibm-db` added.

To use the Data API on an Aurora cluster:

```bash
aws rds modify-db-cluster --db-cluster-identifier <id> --enable-http-endpoint
```

### Choosing what to scan

A production schema can have hundreds of tables, and only a handful matter to
the contact-center operations. Three ways to narrow it:

```python
# 1. cheap inventory first — names, row counts, comments; no column/index queries
introspect_database(db_type="rds", region=..., rds_instance_identifier="my-db",
                    tables_only=True)

# 2. then the tables that matter, in one call
introspect_database(db_type="rds", region=..., rds_instance_identifier="my-db",
                    table_name="orders,order_items,customers")

# 3. or a single table
introspect_database(db_type="rds", region=..., rds_instance_identifier="my-db",
                    table_name="orders")
```

Omitting `table_name` scans everything. Names match case-insensitively, so
`orders` finds Oracle's `ORDERS` without the caller knowing the storage casing.
DynamoDB takes the same comma-separated form via `dynamodb_table_name`.

### What it reads

**SQL engines**

- tables and views, with real row counts and sample rows
- exact column names, SQL types, nullability, defaults
- primary keys, including **composite** keys
- secondary indexes (unique, composite, fulltext), with index type
- **foreign keys and the reverse relationships** (`referenced_by`)
- allowed values — declared enums on PostgreSQL/MySQL, and for engines with no
  ENUM type (SQL Server, Oracle, Db2) the **distinct values actually present**
  in enum-like columns, marked `allowed_values_source: "observed"`
- check constraints and generated columns with their expressions
- table and column comments

**DynamoDB**

- key schema, including sort keys
- global **and local** secondary indexes, with projection type and projected attributes
- non-key attributes discovered by sampling, with nested map / list / set typing
- stream configuration and billing mode

### Requirements

The runtime role needs `rds:DescribeDBInstances` / `DescribeDBClusters` for
discovery, `secretsmanager:GetSecretValue` for credentials, plus
`rds-data:ExecuteStatement` on the Data API path, and
`dynamodb:DescribeTable` + `dynamodb:Scan` for DynamoDB.

A scan that finds nothing returns an error listing the tables that *do* exist
and the schema/owner it searched. Failures are distinguished so they are
actionable: `TARGET_NOT_FOUND`, `DATA_API_NOT_ENABLED`, `CONNECTION_FAILED`,
`DRIVER_NOT_INSTALLED`, `ACCESS_DENIED`, `SECRET_UNREADABLE`,
`NO_TABLES_FOUND`, `MISSING_PARAMETERS`.

---

## 2. Schema as a document

Attach a DDL dump, an ERD image, or a data dictionary and no scan is attempted.
The agent parses the document into the same contract, then **echoes back every
table and column it read** for confirmation before generating anything. It will
ask rather than invent an identifier the document did not state.

---

## 3. Schema fidelity

Once read, the schema is the contract. Two things in particular travel with it:

**Column comments become business rules.** A comment like

```sql
COMMENT ON TABLE returns IS
  'Return requests. Auto-approve under 500,000 KRW; above needs manager approval.';
```

is carried into the OperationSpec, so the generated Lambda enforces the
threshold rather than inventing one.

**Sampled values become data conventions.** A phone number stored as
`821012345678` stays in that format through the Lambda, the AI prompt and the
Contact Flow, instead of being normalized to `+8210...` and silently failing
every lookup.

**Observed values stop invented enums.** SQL Server, Oracle and Db2 have no
ENUM type — a status column is just `VARCHAR` — so the scan reports the distinct
values actually present. Without that the generator guesses: in testing it wrote
`{"DAMAGE", "LOST"}` for a column whose real values are `DAMAGE`, `LOSS`,
`DELAY`, `WRONG_DELIVERY`, which rejected every valid request.

---

## 4. Deploying against a driver-based engine

On the Data API path the generated Lambdas need no networking. On the driver
path (plain RDS, or Aurora with the HTTP endpoint off) they need three things,
and the generated CloudFormation emits all of them:

- `VpcConfig` with at least two subnets in the DB's VPC and a dedicated Lambda
  security group
- a `SecurityGroupIngress` letting that Lambda SG reach the DB security group on
  the engine's port
- a **Secrets Manager interface VPC endpoint**
  (`com.amazonaws.<region>.secretsmanager`, 443 from the Lambda SG), or private
  subnets behind a NAT gateway

That last one is easy to miss and fails opaquely: a VPC-attached Lambda has no
route to public AWS endpoints, so `GetSecretValue` hangs and the function dies at
its timeout before running a single query. In testing every invocation returned
`Task timed out after 30.00 seconds` until the endpoint existed.

---

## 5. Validation gates

A correct schema in context is not sufficient — a language model will still
write a column onto the wrong table. These checks run deterministically in
`validate_parameter_consistency` before anything is deployed.

| Check | What it catches | Failure it prevents |
|---|---|---|
| `sql_schema_mismatch` | a column referenced on a table that does not own it | `column ... does not exist` (42703) |
| `sql_type_mismatch` | a cast to a type the schema does not define | `type "..." does not exist` |
| `sql_missing_required_column` | an `INSERT` omitting a `NOT NULL` column with no default | `null value in column ... violates not-null constraint` |
| `sql_param_type_mismatch` | a parameter bound with a type the column cannot accept | `operator does not exist: bigint = text` (42883) |
| `rds_env_contract` / `rds_data_api` | env var drift, missing `includeResultMetadata`, positional row access, interpolated SQL | `KeyError` on cold start; wrong column returned after a schema change |

Each check is conservative by design: it reports only when the schema gives an
unambiguous answer. A column the schema does not describe is left alone rather
than guessed at.

Two classes of mistake are repaired at merge time instead of reported, because
they arise from independently generated fragments disagreeing:

- RDS environment variables are unified on `DB_CLUSTER_ARN` / `DB_SECRET_ARN` /
  `DB_NAME` — the names the generated handlers actually read.
- An inline Lambda whose `Handler` names a function its code does not define
  gets the alias it needs.

---

## 6. Verifying the gates

`scripts/verify_param_type_gate.py` exercises the parameter type-binding gate
against a synthetic matrix, against real generated handlers, and — with
`--live` — against an Aurora cluster, to confirm the database agrees with the
gate's verdict.

```bash
python3 scripts/verify_param_type_gate.py

# also cross-check the verdict against a live Aurora PostgreSQL cluster
python3 scripts/verify_param_type_gate.py --live \
  --cluster-arn arn:aws:rds:<region>:<account>:cluster:<id> \
  --secret-arn  arn:aws:secretsmanager:<region>:<account>:secret:<name> \
  --database    <db>
```

### Recorded run

Against an Aurora PostgreSQL schema of 14 tables (enums, composite keys,
foreign keys, generated columns, check constraints):

```
A. Synthetic binding matrix
------------------------------------------------------------------------------
  [PASS] expect flag  -> flagged  bigint bound as stringValue
  [PASS] expect allow -> clean    bigint bound as longValue
  [PASS] expect allow -> clean    varchar bound as stringValue
  [PASS] expect flag  -> flagged  varchar bound as longValue
  [PASS] expect flag  -> flagged  boolean bound as stringValue
  [PASS] expect allow -> clean    boolean bound as booleanValue
  [PASS] expect allow -> clean    numeric bound as stringValue (valid for the Data API)
  [PASS] expect allow -> clean    timestamptz bound as stringValue (ISO string is correct)
  [PASS] expect allow -> clean    PostgreSQL enum bound as stringValue
  [PASS] expect flag  -> flagged  INSERT: bigint column fed a stringValue
  [PASS] expect allow -> clean    INSERT: correct bindings
  [PASS] expect flag  -> flagged  aliased table, bigint bound as stringValue
  [PASS] expect allow -> clean    unknown column — must stay silent, never guess

  13/13 synthetic cases behaved as expected

C. Live Aurora PostgreSQL cross-check
------------------------------------------------------------------------------
  SQL: SELECT line_number FROM order_items WHERE order_id = :orderId LIMIT 1
  [PASS] stringValue (gate flags this): database rejects — operator does not exist: bigint = text
  [PASS] longValue   (gate allows this): database accepts — 1 row(s)
```

The synthetic matrix is the part that matters for false positives: a
`stringValue` bound to a `numeric`, `timestamptz` or enum column is the correct
Data API representation and must stay clean, while the same binding on a
`bigint` key must be flagged.

### All schema gates on one bundle

Run against the six handlers generated for a four-operation build on the same
schema — first as generated, then after the reported findings were addressed:

```
as generated:
  ❌ create_return
       sql_type_mismatch               ::approval_status
       sql_missing_required_column     quantity
  ❌ create_warranty_claim
       sql_schema_mismatch             pv.serial_number
       sql_type_mismatch               ::claim_status
  ✅ customer_lookup
  ❌ get_order
       sql_schema_mismatch             oi.product_name
  ❌ list_order_items
       sql_param_type_mismatch         :orderId
  ✅ track_shipment
  → 6 finding(s)

after fixes:
  ✅ create_return
  ✅ create_warranty_claim
  ✅ customer_lookup
  ✅ get_order
  ✅ list_order_items
  ✅ track_shipment
  → 0 finding(s)
```

Every finding corresponds to a statement the database refuses to run:
`product_name` lives on `products` (not `order_items`), `serial_number` lives on
`warranty_claims` (not `product_variants`), `approval_status` and `claim_status`
are plain `VARCHAR` columns rather than enum types, `returns.quantity` is
`NOT NULL` without a default, and `order_items.order_id` is `bigint`.

---

## 7. Engine coverage

`introspect_database` is exercised against a live instance of every engine the
account can host, over both connection methods, plus the table-selection modes
and the failure paths:

```
================================================================================
ENGINE COVERAGE
================================================================================
✅ Aurora PostgreSQL — Data API, explicit ARNs
✅ Aurora MySQL — Data API, explicit ARNs
✅ Aurora PostgreSQL — resolved from identifier
✅ Aurora MySQL — resolved from identifier
✅ RDS PostgreSQL — psycopg2 driver
✅ RDS MySQL — PyMySQL driver
✅ RDS MariaDB — PyMySQL driver
✅ RDS SQL Server — python-tds driver
✅ RDS MariaDB — engine auto-detected via db_type='rds'
✅ RDS SQL Server — engine auto-detected via db_type='rds'
✅ RDS PostgreSQL — caller said mysql, RDS corrects it
✅ RDS PostgreSQL — single table filter
✅ DynamoDB — three tables in one call
✅ DynamoDB — single table
✅ RDS PostgreSQL — multi-table filter (3 of 6)
✅ RDS MySQL — multi-table filter
✅ RDS SQL Server — multi-table filter
✅ Aurora PostgreSQL — multi-table filter over Data API
✅ RDS PostgreSQL — case-insensitive table name (ACCOUNTS)
✅ RDS MariaDB — mixed-case list (Patients, DOCTORS)
✅ RDS PostgreSQL — tables_only inventory
✅ Aurora MySQL — tables_only inventory over Data API
✅ RDS SQL Server — tables_only inventory
✅ RDS MariaDB — include_sample_rows=False
================================================================================
ERROR HANDLING (every one of these must fail cleanly, with a code)
================================================================================
✅ unknown engine name: [None] Unsupported database type: cassandra
✅ rds without an identifier: [MISSING_PARAMETERS] db_type 'rds'/'aurora' needs rds_instance_identifier so the engine can be detected, or name the engine explicitly (one o
✅ identifier that does not exist: [TARGET_NOT_FOUND] No RDS DB instance or Aurora cluster named 'no-such-db-xyz' in this region.
✅ table filter matching nothing: [NO_TABLES_FOUND] No tables found in database 'retaildb2' matching ['nope']. Discovered 6 table(s) total: ['accounts', 'bills', 'meters', 
✅ driver path with no credentials at all: [MISSING_PARAMETERS] No credentials. Pass rds_secret_arn (a Secrets Manager secret holding username/password — RDS-managed master user secret
ENGINES OK: 24   FAILED: 0
ERROR CASES HANDLED: 5/5
```

Not covered by a live run: **Oracle** and **Db2** query sets are implemented but
the engines were unavailable in the test account (`oracle-se2` is not offered
there, and Db2 needs the `ibm-db` driver added to the image). Everything above
ran against real databases.

