"""
CDK Infrastructure Generator Tool

Generates AWS CDK (TypeScript) infrastructure code for deploying the POC:
- DynamoDB tables with proper key structure and GSIs
- Lambda functions with appropriate IAM roles
- API Gateway REST API with Lambda integration
- Optional: Cognito, S3, etc.
"""

import json
from typing import Optional
from strands import tool
from .spec_manager import get_all_specs
from .streaming_callback import stream_asset, complete_asset


@tool
def generate_cdk_infrastructure(
    project_name: str,
    company_name: Optional[str] = None,
    include_api_gateway: bool = True,
    include_dynamodb: bool = True,
    include_cognito: bool = False,
    aws_region: str = "ap-northeast-1",
) -> dict:
    """
    Generate complete AWS CDK infrastructure code for the POC.

    This creates a deployable CDK project with:
    - DynamoDB tables based on operation specifications
    - Lambda functions for each operation
    - API Gateway REST API
    - Proper IAM roles with least privilege
    - Environment variable configuration

    The generated code follows AWS best practices and is ready for `cdk deploy`.

    Args:
        project_name: Name for the CDK project (used in stack names)
        company_name: Company name for resource naming
        include_api_gateway: Whether to include API Gateway
        include_dynamodb: Whether to include DynamoDB tables
        include_cognito: Whether to include Cognito authentication
        aws_region: Target AWS region

    Returns:
        - files: Dictionary of filename -> content
        - deployment_instructions: How to deploy
        - architecture_diagram: Mermaid diagram of the architecture
    """

    specs = get_all_specs()
    if not specs:
        return {
            "success": False,
            "error": "No operation specifications found. Please define at least one operation first."
        }

    # Extract unique tables and their configurations
    tables = _extract_table_configs(specs)
    operations = list(specs.keys())

    # Generate all CDK files
    files = {}

    # 1. package.json
    files["package.json"] = _generate_package_json(project_name)
    stream_asset("cdk", "package.json", files["package.json"])

    # 2. cdk.json
    files["cdk.json"] = _generate_cdk_json()
    stream_asset("cdk", "cdk.json", files["cdk.json"])

    # 3. tsconfig.json
    files["tsconfig.json"] = _generate_tsconfig()
    stream_asset("cdk", "tsconfig.json", files["tsconfig.json"])

    # 4. bin/app.ts - CDK app entry point
    files["bin/app.ts"] = _generate_app_ts(project_name)
    stream_asset("cdk", "bin/app.ts", files["bin/app.ts"])

    # 5. lib/main-stack.ts - Main stack
    files["lib/main-stack.ts"] = _generate_main_stack(
        project_name=project_name,
        company_name=company_name,
        tables=tables,
        operations=operations,
        include_api_gateway=include_api_gateway,
        include_dynamodb=include_dynamodb,
        include_cognito=include_cognito,
        aws_region=aws_region,
        specs=specs
    )
    stream_asset("cdk", "lib/main-stack.ts", files["lib/main-stack.ts"])

    # 6. README.md - Deployment instructions
    readme, instructions = _generate_readme(
        project_name=project_name,
        tables=tables,
        operations=operations,
        include_api_gateway=include_api_gateway,
        include_cognito=include_cognito
    )
    files["README.md"] = readme
    stream_asset("cdk", "README.md", files["README.md"])

    # 7. Architecture diagram
    architecture = _generate_architecture_diagram(
        project_name=project_name,
        tables=tables,
        operations=operations,
        include_api_gateway=include_api_gateway,
        include_cognito=include_cognito
    )
    files["ARCHITECTURE.md"] = architecture
    stream_asset("cdk", "ARCHITECTURE.md", files["ARCHITECTURE.md"])

    # Mark CDK generation as complete
    complete_asset("cdk")

    return {
        "success": True,
        "files": files,
        "file_count": len(files),
        "deployment_instructions": instructions,
        "tables_created": list(tables.keys()),
        "lambdas_created": operations,
        "message": f"CDK infrastructure generated: {len(tables)} tables, {len(operations)} Lambda functions"
    }


def _extract_table_configs(specs: dict) -> dict:
    """Extract unique DynamoDB table configurations from specs."""
    tables = {}

    for op_id, spec in specs.items():
        ds = spec.data_source
        table_name = ds.table_name

        if table_name not in tables:
            tables[table_name] = {
                "partition_key": ds.partition_key,
                "sort_key": ds.sort_key,
                "gsi_indexes": ds.gsi_indexes or [],
                "db_type": ds.db_type,
                "operations": []
            }

        tables[table_name]["operations"].append(op_id)

    return tables


def _generate_package_json(project_name: str) -> str:
    """Generate package.json for CDK project."""
    package = {
        "name": project_name.lower().replace(" ", "-"),
        "version": "1.0.0",
        "description": f"CDK infrastructure for {project_name}",
        "scripts": {
            "build": "tsc",
            "watch": "tsc -w",
            "cdk": "cdk",
            "deploy": "cdk deploy --all --require-approval never",
            "destroy": "cdk destroy --all --force"
        },
        "devDependencies": {
            "@types/node": "^20.0.0",
            "typescript": "^5.0.0",
            "aws-cdk": "^2.170.0"
        },
        "dependencies": {
            "aws-cdk-lib": "^2.170.0",
            "constructs": "^10.0.0"
        }
    }
    return json.dumps(package, indent=2)


def _generate_cdk_json() -> str:
    """Generate cdk.json configuration."""
    config = {
        "app": "npx ts-node --prefer-ts-exts bin/app.ts",
        "watch": {
            "include": ["**"],
            "exclude": [
                "README.md",
                "cdk*.json",
                "**/*.d.ts",
                "**/*.js",
                "tsconfig.json",
                "package*.json",
                "node_modules",
                "cdk.out"
            ]
        },
        "context": {
            "@aws-cdk/aws-lambda:recognizeVersionProps": True,
            "@aws-cdk/core:stackRelativeExports": True
        }
    }
    return json.dumps(config, indent=2)


def _generate_tsconfig() -> str:
    """Generate tsconfig.json."""
    config = {
        "compilerOptions": {
            "target": "ES2020",
            "module": "commonjs",
            "lib": ["ES2020"],
            "declaration": True,
            "strict": True,
            "noImplicitAny": True,
            "strictNullChecks": True,
            "noImplicitThis": True,
            "alwaysStrict": True,
            "noUnusedLocals": False,
            "noUnusedParameters": False,
            "noImplicitReturns": True,
            "noFallthroughCasesInSwitch": False,
            "inlineSourceMap": True,
            "inlineSources": True,
            "experimentalDecorators": True,
            "strictPropertyInitialization": False,
            "outDir": "./cdk.out",
            "typeRoots": ["./node_modules/@types"]
        },
        "exclude": ["node_modules", "cdk.out"]
    }
    return json.dumps(config, indent=2)


def _generate_app_ts(project_name: str) -> str:
    """Generate bin/app.ts entry point."""
    stack_name = project_name.replace(" ", "").replace("-", "")

    return f'''#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import {{ {stack_name}Stack }} from "../lib/main-stack";

const app = new cdk.App();

new {stack_name}Stack(app, "{stack_name}Stack", {{
  env: {{
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "ap-northeast-1",
  }},
  description: "{project_name} - AI Contact Center POC Infrastructure",
}});

app.synth();
'''


def _generate_main_stack(
    project_name: str,
    company_name: Optional[str],
    tables: dict,
    operations: list,
    include_api_gateway: bool,
    include_dynamodb: bool,
    include_cognito: bool,
    aws_region: str,
    specs: dict
) -> str:
    """Generate the main CDK stack."""

    stack_name = project_name.replace(" ", "").replace("-", "")
    resource_prefix = project_name.lower().replace(" ", "-")[:20]

    # Build imports
    imports = [
        'import * as cdk from "aws-cdk-lib";',
        'import * as lambda from "aws-cdk-lib/aws-lambda";',
        'import * as iam from "aws-cdk-lib/aws-iam";',
        'import { Construct } from "constructs";',
    ]

    if include_dynamodb:
        imports.append('import * as dynamodb from "aws-cdk-lib/aws-dynamodb";')

    if include_api_gateway:
        imports.append('import * as apigateway from "aws-cdk-lib/aws-apigateway";')

    if include_cognito:
        imports.append('import * as cognito from "aws-cdk-lib/aws-cognito";')

    # Build DynamoDB tables
    dynamodb_code = ""
    if include_dynamodb:
        dynamodb_code = _generate_dynamodb_constructs(tables, resource_prefix)

    # Build Lambda functions
    lambda_code = _generate_lambda_constructs(operations, specs, resource_prefix, tables)

    # Build API Gateway
    api_code = ""
    if include_api_gateway:
        api_code = _generate_api_gateway_constructs(operations, specs, resource_prefix)

    # Build Cognito
    cognito_code = ""
    if include_cognito:
        cognito_code = _generate_cognito_constructs(resource_prefix)

    # Build outputs
    outputs_code = _generate_outputs(
        tables=tables,
        operations=operations,
        include_api_gateway=include_api_gateway,
        include_cognito=include_cognito
    )

    return f'''{chr(10).join(imports)}

/**
 * {project_name} Infrastructure Stack
 *
 * Generated by AICC Builder for {company_name or 'POC'}
 *
 * This stack creates:
 * - {len(tables)} DynamoDB table(s)
 * - {len(operations)} Lambda function(s)
 * - API Gateway REST API
 */
export class {stack_name}Stack extends cdk.Stack {{
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {{
    super(scope, id, props);

    // ========================================
    // DynamoDB Tables
    // ========================================
{dynamodb_code}

    // ========================================
    // Lambda Functions
    // ========================================
{lambda_code}

    // ========================================
    // API Gateway
    // ========================================
{api_code}

{cognito_code}

    // ========================================
    // Outputs
    // ========================================
{outputs_code}
  }}
}}
'''


def _generate_dynamodb_constructs(tables: dict, resource_prefix: str) -> str:
    """Generate DynamoDB table constructs."""
    code_lines = []

    for table_name, config in tables.items():
        var_name = _to_camel_case(table_name) + "Table"
        construct_id = table_name.replace("_", "-").replace(" ", "-")

        # Partition key
        pk_type = "dynamodb.AttributeType.STRING"

        code = f'''
    const {var_name} = new dynamodb.Table(this, "{construct_id}", {{
      tableName: `${{id}}-{table_name}`,
      partitionKey: {{
        name: "{config['partition_key']}",
        type: {pk_type},
      }},'''

        # Sort key if present
        if config.get("sort_key"):
            code += f'''
      sortKey: {{
        name: "{config['sort_key']}",
        type: {pk_type},
      }},'''

        code += '''
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });'''

        # Add GSIs
        for gsi in config.get("gsi_indexes", []):
            if isinstance(gsi, dict):
                gsi_name = gsi.get("name", gsi.get("index_name", "gsi"))
                gsi_pk = gsi.get("partition_key", gsi.get("pk"))
                gsi_sk = gsi.get("sort_key", gsi.get("sk"))

                code += f'''

    {var_name}.addGlobalSecondaryIndex({{
      indexName: "{gsi_name}",
      partitionKey: {{
        name: "{gsi_pk}",
        type: dynamodb.AttributeType.STRING,
      }},'''
                if gsi_sk:
                    code += f'''
      sortKey: {{
        name: "{gsi_sk}",
        type: dynamodb.AttributeType.STRING,
      }},'''
                code += '''
      projectionType: dynamodb.ProjectionType.ALL,
    });'''

        code_lines.append(code)

    return "\n".join(code_lines)


def _generate_lambda_constructs(operations: list, specs: dict, resource_prefix: str, tables: dict) -> str:
    """Generate Lambda function constructs."""
    code_lines = []

    # Lambda execution role
    code_lines.append('''
    // Shared Lambda execution role
    const lambdaRole = new iam.Role(this, "LambdaExecutionRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
    });

    // Grant DynamoDB access''')

    # Add DynamoDB permissions for each table
    for table_name in tables:
        var_name = _to_camel_case(table_name) + "Table"
        code_lines.append(f"    {var_name}.grantReadWriteData(lambdaRole);")

    code_lines.append("")

    # Generate Lambda for each operation
    for op_id in operations:
        spec = specs[op_id]
        var_name = _to_camel_case(op_id) + "Fn"
        construct_id = op_id.replace("_", "-")
        table_name = spec.data_source.table_name
        table_var = _to_camel_case(table_name) + "Table"

        code_lines.append(f'''
    const {var_name} = new lambda.Function(this, "{construct_id}", {{
      functionName: `${{id}}-{op_id}`,
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(`../lambda/{op_id}`),
      role: lambdaRole,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {{
        TABLE_NAME: {table_var}.tableName,
        LOG_LEVEL: "INFO",
      }},
    }});''')

    return "\n".join(code_lines)


def _generate_api_gateway_constructs(operations: list, specs: dict, resource_prefix: str) -> str:
    """Generate API Gateway constructs."""
    code_lines = []

    code_lines.append('''
    const api = new apigateway.RestApi(this, "Api", {
      restApiName: `${id}-api`,
      description: "API for AI Contact Center POC",
      deployOptions: {
        stageName: "prod",
        throttlingRateLimit: 100,
        throttlingBurstLimit: 200,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
    });''')

    # Group operations by resource path
    resources = {}
    for op_id in operations:
        spec = specs[op_id]
        path = spec.path.strip("/").split("/")[0] if spec.path else op_id
        if path not in resources:
            resources[path] = []
        resources[path].append((op_id, spec))

    # Create resources and methods
    for resource_path, ops in resources.items():
        resource_var = _to_camel_case(resource_path) + "Resource"
        code_lines.append(f'''
    const {resource_var} = api.root.addResource("{resource_path}");''')

        for op_id, spec in ops:
            fn_var = _to_camel_case(op_id) + "Fn"
            method = spec.http_method.upper()

            code_lines.append(f'''
    {resource_var}.addMethod("{method}", new apigateway.LambdaIntegration({fn_var}));''')

    return "\n".join(code_lines)


def _generate_cognito_constructs(resource_prefix: str) -> str:
    """Generate Cognito constructs."""
    return '''
    // ========================================
    // Cognito (Optional)
    // ========================================

    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: `${id}-users`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = new cognito.UserPoolClient(this, "UserPoolClient", {
      userPool,
      authFlows: { userPassword: true, userSrp: true },
      generateSecret: false,
    });'''


def _generate_outputs(tables: dict, operations: list, include_api_gateway: bool, include_cognito: bool) -> str:
    """Generate CloudFormation outputs."""
    code_lines = []

    # Table outputs
    for table_name in tables:
        var_name = _to_camel_case(table_name) + "Table"
        output_name = table_name.replace("_", "").replace("-", "") + "TableName"
        code_lines.append(f'''
    new cdk.CfnOutput(this, "{output_name}", {{
      value: {var_name}.tableName,
      description: "DynamoDB table: {table_name}",
    }});''')

    # API Gateway output
    if include_api_gateway:
        code_lines.append('''
    new cdk.CfnOutput(this, "ApiUrl", {
      value: api.url,
      description: "API Gateway endpoint URL",
    });''')

    # Cognito outputs
    if include_cognito:
        code_lines.append('''
    new cdk.CfnOutput(this, "UserPoolId", {
      value: userPool.userPoolId,
      description: "Cognito User Pool ID",
    });

    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
      description: "Cognito User Pool Client ID",
    });''')

    return "\n".join(code_lines)


def _generate_readme(project_name: str, tables: dict, operations: list, include_api_gateway: bool, include_cognito: bool) -> tuple:
    """Generate README.md with deployment instructions."""

    instructions = [
        "1. Install dependencies: `npm install`",
        "2. Build the project: `npm run build`",
        "3. Deploy to AWS: `npm run deploy`",
    ]

    readme = f'''# {project_name} - CDK Infrastructure

Generated by AICC Builder

## Resources Created

### DynamoDB Tables
{chr(10).join([f"- `{t}` (PK: {c['partition_key']}, SK: {c.get('sort_key', 'N/A')})" for t, c in tables.items()])}

### Lambda Functions
{chr(10).join([f"- `{op}`" for op in operations])}

{"### API Gateway" if include_api_gateway else ""}
{"- REST API with CORS enabled" if include_api_gateway else ""}

{"### Cognito" if include_cognito else ""}
{"- User Pool for authentication" if include_cognito else ""}

## Prerequisites

- Node.js 18+
- AWS CLI configured with appropriate credentials
- AWS CDK CLI (`npm install -g aws-cdk`)

## Deployment

```bash
# Install dependencies
npm install

# Bootstrap CDK (first time only)
cdk bootstrap

# Build TypeScript
npm run build

# Deploy to AWS
npm run deploy
```

## Lambda Code

Place your Lambda function code in the `lambda/` directory:

```
lambda/
{chr(10).join([f"├── {op}/" for op in operations[:-1]])}
└── {operations[-1] if operations else "function"}/
    ├── handler.py
    └── requirements.txt
```

## Cleanup

```bash
npm run destroy
```

## Outputs

After deployment, the following values will be output:
- API Gateway URL
- DynamoDB table names
{"- Cognito User Pool ID" if include_cognito else ""}
'''

    return readme, instructions


def _generate_architecture_diagram(
    project_name: str,
    tables: dict,
    operations: list,
    include_api_gateway: bool,
    include_cognito: bool
) -> str:
    """Generate Mermaid architecture diagram."""

    lines = [
        "# Architecture Diagram",
        "",
        "```mermaid",
        "flowchart TB",
        f"    subgraph AWS[{project_name}]",
        "",
    ]

    # Client
    if include_cognito:
        lines.append("    Client[📱 Client] --> Cognito[🔐 Cognito]")
        lines.append("    Cognito --> APIGW")
    else:
        lines.append("    Client[📱 Client] --> APIGW")

    # API Gateway
    if include_api_gateway:
        lines.append("    APIGW[🌐 API Gateway]")
        lines.append("")

        # Lambda functions
        lines.append("    subgraph Lambda[Lambda Functions]")
        for op in operations[:6]:  # Limit for readability
            lines.append(f"        {op}[⚡ {op}]")
        lines.append("    end")
        lines.append("")

        lines.append("    APIGW --> Lambda")

    # DynamoDB
    if tables:
        lines.append("")
        lines.append("    subgraph DynamoDB[DynamoDB Tables]")
        for table in list(tables.keys())[:4]:
            lines.append(f"        {table}[(📊 {table})]")
        lines.append("    end")
        lines.append("")
        lines.append("    Lambda --> DynamoDB")

    lines.append("    end")
    lines.append("")

    # Styling
    lines.extend([
        "    style APIGW fill:#FF9900,color:#fff",
        "    style Lambda fill:#FF9900,color:#fff",
        "    style DynamoDB fill:#3B48CC,color:#fff",
    ])

    if include_cognito:
        lines.append("    style Cognito fill:#DD344C,color:#fff")

    lines.append("```")

    return "\n".join(lines)


def _to_camel_case(name: str) -> str:
    """Convert snake_case or kebab-case to camelCase."""
    parts = name.replace("-", "_").split("_")
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
