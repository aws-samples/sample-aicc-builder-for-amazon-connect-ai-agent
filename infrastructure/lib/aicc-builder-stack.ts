import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as iam from "aws-cdk-lib/aws-iam";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import * as path from "path";

export interface AiccBuilderStackProps extends cdk.StackProps {
  /**
   * SSM parameter name from which CloudFront will read the ALB DNS name.
   * The ECS stack publishes to this same parameter at deploy time.
   * Using SSM (dynamic reference) avoids a CFN cross-stack reference from
   * this stack back into EcsStack, which would create a circular dependency
   * (this stack owns the AssetsBucket that EcsStack consumes).
   */
  albDnsSsmParamName: string;
}

/**
 * AICC Builder Stack with AgentCore Runtime
 *
 * This stack deploys the frontend infrastructure for AICC Builder:
 * - Cognito User Pool for authentication
 * - Cognito Identity Pool for AWS credentials (to access AgentCore WebSocket)
 * - S3 + CloudFront for frontend hosting
 *
 * The Agent is deployed separately via AgentCore CLI (agentcore launch).
 *
 * Architecture:
 * 1. Frontend (React) hosted on CloudFront/S3
 * 2. Cognito User Pool for user authentication
 * 3. Cognito Identity Pool for AWS credentials
 * 4. AgentCore Runtime for AI agent (deployed via CLI)
 */
export class AiccBuilderStack extends cdk.Stack {
  public readonly frontendUrl: cdk.CfnOutput;
  public readonly userPoolId: cdk.CfnOutput;
  public readonly userPoolClientId: cdk.CfnOutput;
  public readonly identityPoolId: cdk.CfnOutput;
  public readonly assetsBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props?: AiccBuilderStackProps) {
    super(scope, id, props);

    // ========================================
    // Cognito User Pool (Self-Sign-Up Disabled)
    // ========================================

    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: `${id}-users`,
      selfSignUpEnabled: false,
      signInAliases: {
        email: true,
        username: true,
      },
      autoVerify: {
        email: true,
      },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = new cognito.UserPoolClient(this, "UserPoolClient", {
      userPool,
      userPoolClientName: `${id}-frontend-client`,
      authFlows: {
        userPassword: true,
        userSrp: true,
      },
      generateSecret: false,
      preventUserExistenceErrors: true,
    });

    // ========================================
    // Cognito Identity Pool (for AWS Credentials)
    // ========================================

    const identityPool = new cognito.CfnIdentityPool(this, "IdentityPool", {
      identityPoolName: `${id}_identity_pool`,
      allowUnauthenticatedIdentities: false,
      cognitoIdentityProviders: [
        {
          clientId: userPoolClient.userPoolClientId,
          providerName: userPool.userPoolProviderName,
        },
      ],
    });

    // IAM role for authenticated users
    const authenticatedRole = new iam.Role(this, "AuthenticatedRole", {
      assumedBy: new iam.FederatedPrincipal(
        "cognito-identity.amazonaws.com",
        {
          StringEquals: {
            "cognito-identity.amazonaws.com:aud": identityPool.ref,
          },
          "ForAnyValue:StringLike": {
            "cognito-identity.amazonaws.com:amr": "authenticated",
          },
        },
        "sts:AssumeRoleWithWebIdentity"
      ),
    });

    // ECS mode: WebSocket goes through ALB with Cognito JWT — no SigV4 / AgentCore policy needed.

    // Attach role to identity pool
    new cognito.CfnIdentityPoolRoleAttachment(
      this,
      "IdentityPoolRoleAttachment",
      {
        identityPoolId: identityPool.ref,
        roles: {
          authenticated: authenticatedRole.roleArn,
        },
      }
    );

    // ========================================
    // S3 Bucket for Generated Assets (ZIP files)
    // ========================================

    const assetsBucket = new s3.Bucket(this, "AssetsBucket", {
      bucketName: `${id.toLowerCase()}-assets-${this.account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      versioned: true, // Required by S3 Files (NFS mount)
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT],
          allowedOrigins: ["*"],
          allowedHeaders: ["*"],
          exposedHeaders: ["ETag"],
          maxAge: 3000,
        },
      ],
      lifecycleRules: [
        {
          // Auto-delete assets after 30 days
          expiration: cdk.Duration.days(30),
          noncurrentVersionExpiration: cdk.Duration.days(7),
          prefix: "assets/",
        },
        {
          // Auto-delete uploaded files after 1 day (they're processed immediately)
          expiration: cdk.Duration.days(1),
          noncurrentVersionExpiration: cdk.Duration.days(1),
          prefix: "uploads/",
        },
      ],
    });
    this.assetsBucket = assetsBucket;

    // ========================================
    // Create a managed policy that can be attached to the AgentCore Runtime role
    // The role is created by AgentCore SDK, so we create the policy here and
    // attach it via deploy.sh after AgentCore creates the role
    // No explicit managedPolicyName — let CloudFormation auto-generate to avoid conflicts
    const agentCoreS3Policy = new iam.ManagedPolicy(this, "AgentCoreS3Policy", {
      description: "Allows AgentCore Runtime to read/write assets to S3",
      statements: [
        new iam.PolicyStatement({
          sid: "AllowS3AssetOperations",
          effect: iam.Effect.ALLOW,
          actions: [
            "s3:PutObject",
            "s3:GetObject",
            "s3:DeleteObject",
            "s3:ListBucket",
          ],
          resources: [
            assetsBucket.bucketArn,
            `${assetsBucket.bucketArn}/*`,
          ],
        }),
      ],
    });

    // ========================================
    // IAM Policy for AgentCore Runtime DB Access
    // ========================================

    // No explicit managedPolicyName — let CloudFormation auto-generate to avoid conflicts
    const agentCoreDbPolicy = new iam.ManagedPolicy(this, "AgentCoreDbPolicy", {
      description: "Allows AgentCore Runtime to introspect customer databases",
      statements: [
        // DynamoDB read access for introspection
        new iam.PolicyStatement({
          sid: "AllowDynamoDBIntrospection",
          effect: iam.Effect.ALLOW,
          actions: [
            "dynamodb:DescribeTable",
            "dynamodb:Scan",
            "dynamodb:Query",
          ],
          resources: ["*"], // Customer tables - can be scoped down per deployment
        }),
        // RDS Data API access
        new iam.PolicyStatement({
          sid: "AllowRDSDataAPI",
          effect: iam.Effect.ALLOW,
          actions: [
            "rds-data:ExecuteStatement",
            "rds-data:BatchExecuteStatement",
          ],
          resources: ["*"], // Customer RDS clusters
        }),
        // Secrets Manager for RDS credentials
        new iam.PolicyStatement({
          sid: "AllowSecretsManagerRead",
          effect: iam.Effect.ALLOW,
          actions: ["secretsmanager:GetSecretValue"],
          resources: ["*"], // Customer secrets for RDS
        }),
      ],
    });

    // ========================================
    // DynamoDB Table for User Sessions
    // ========================================

    const sessionsTable = new dynamodb.Table(this, "UserSessionsTable", {
      tableName: `${id}-user-sessions`,
      partitionKey: {
        name: "userId",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "sessionId",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
    });

    // GSI for querying sessions by lastMessageAt (for sorting)
    sessionsTable.addGlobalSecondaryIndex({
      indexName: "userId-lastMessageAt-index",
      partitionKey: {
        name: "userId",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "lastMessageAt",
        type: dynamodb.AttributeType.NUMBER,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ========================================
    // Lambda for Session Management API
    // ========================================

    const sessionApiLambda = new lambda.Function(this, "SessionApiLambda", {
      functionName: `${id}-session-api`,
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: "index.handler",
      code: lambda.Code.fromInline(`
import json
import re
import boto3
import os
import time
import gzip
import base64
from decimal import Decimal
from botocore.config import Config
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
_region = os.environ.get('AWS_REGION', 'ap-northeast-1')
s3_client = boto3.client('s3', region_name=_region,
    endpoint_url=f'https://s3.{_region}.amazonaws.com',
    config=Config(signature_version='s3v4'))
table = dynamodb.Table(os.environ['SESSIONS_TABLE'])
assets_bucket = os.environ.get('ASSETS_BUCKET', '')

def decimal_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError

def get_user_id(event):
    """Extract user ID from Cognito authorizer claims."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        return claims.get('sub')
    except:
        return None

def handler(event, context):
    http_method = event.get('httpMethod', '')
    path = event.get('path', '')
    user_id = get_user_id(event)

    if not user_id:
        return {
            'statusCode': 401,
            'headers': cors_headers(),
            'body': json.dumps({'error': 'Unauthorized'})
        }

    try:
        # History endpoints
        if '/history' in path:
            parts = path.split('/sessions/')
            if len(parts) > 1:
                session_id = parts[1].replace('/history', '')
                if http_method == 'GET':
                    return get_history(user_id, session_id)
                elif http_method == 'PUT':
                    body = json.loads(event.get('body', '{}'))
                    return save_history(user_id, session_id, body)

        # Assets endpoints
        if '/assets' in path:
            parts = path.split('/sessions/')
            if len(parts) > 1:
                session_id = parts[1].replace('/assets', '')
                if http_method == 'GET':
                    return get_assets(user_id, session_id)
                elif http_method == 'PUT':
                    body = json.loads(event.get('body', '{}'))
                    return save_assets(user_id, session_id, body)

        # Session data endpoints (company, operations, progress state)
        if '/data' in path:
            parts = path.split('/sessions/')
            if len(parts) > 1:
                session_id = parts[1].replace('/data', '')
                if http_method == 'GET':
                    return get_session_data(user_id, session_id)
                elif http_method == 'PUT':
                    body = json.loads(event.get('body', '{}'))
                    return save_session_data(user_id, session_id, body)

        # Presigned URL generation endpoint (download)
        if '/presigned' in path and '/upload-presigned' not in path:
            parts = path.split('/sessions/')
            if len(parts) > 1:
                session_id = parts[1].replace('/presigned', '')
                if http_method == 'POST':
                    body = json.loads(event.get('body', '{}'))
                    return generate_presigned_url(user_id, session_id, body)

        # Upload presigned URL generation endpoint (for multimodal file uploads)
        if '/upload-presigned' in path:
            parts = path.split('/sessions/')
            if len(parts) > 1:
                session_id = parts[1].replace('/upload-presigned', '')
                if http_method == 'POST':
                    body = json.loads(event.get('body', '{}'))
                    return generate_upload_presigned_url(user_id, session_id, body)

        if http_method == 'GET' and path == '/sessions':
            return list_sessions(user_id)
        elif http_method == 'POST' and path == '/sessions':
            body = json.loads(event.get('body', '{}'))
            return create_session(user_id, body)
        elif http_method == 'PUT' and '/sessions/' in path:
            session_id = path.split('/sessions/')[-1]
            body = json.loads(event.get('body', '{}'))
            return update_session(user_id, session_id, body)
        elif http_method == 'DELETE' and '/sessions/' in path:
            session_id = path.split('/sessions/')[-1]
            print(f"[ROUTE] DELETE path={path}, extracted session_id={session_id}")
            return delete_session(user_id, session_id)
        elif http_method == 'OPTIONS':
            return {'statusCode': 200, 'headers': cors_headers(), 'body': ''}
        else:
            return {
                'statusCode': 404,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Not found'})
            }
    except Exception as e:
        import traceback
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e), 'trace': traceback.format_exc()})
        }

def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
    }

def list_sessions(user_id):
    """List all sessions for a user, sorted by lastMessageAt descending."""
    response = table.query(
        IndexName='userId-lastMessageAt-index',
        KeyConditionExpression='userId = :uid',
        ExpressionAttributeValues={':uid': user_id},
        ScanIndexForward=False,  # Descending order
        Limit=50
    )

    sessions = response.get('Items', [])
    # Don't include conversation history in list response (too large)
    for s in sessions:
        s.pop('conversationHistory', None)
    return {
        'statusCode': 200,
        'headers': cors_headers(),
        'body': json.dumps({'sessions': sessions}, default=decimal_default)
    }

def create_session(user_id, body):
    """Create a new session."""
    session_id = body.get('sessionId')
    title = body.get('title', 'New Chat')

    if not session_id:
        return {
            'statusCode': 400,
            'headers': cors_headers(),
            'body': json.dumps({'error': 'sessionId is required'})
        }

    now = int(time.time() * 1000)
    ttl = int(time.time()) + (30 * 24 * 60 * 60)  # 30 days

    item = {
        'userId': user_id,
        'sessionId': session_id,
        'title': title,
        'createdAt': now,
        'lastMessageAt': now,
        'messageCount': 0,
        'conversationHistory': '[]',  # Empty JSON array, will be compressed later
        'ttl': ttl
    }

    table.put_item(Item=item)

    return {
        'statusCode': 201,
        'headers': cors_headers(),
        'body': json.dumps({'session': item}, default=decimal_default)
    }

def update_session(user_id, session_id, body):
    """Update session metadata (title, lastMessageAt, messageCount)."""
    update_expr = []
    expr_values = {}
    expr_names = {}

    if 'title' in body:
        update_expr.append('#t = :title')
        expr_values[':title'] = body['title']
        expr_names['#t'] = 'title'

    if 'lastMessageAt' in body:
        update_expr.append('lastMessageAt = :lma')
        expr_values[':lma'] = body['lastMessageAt']

    if 'messageCount' in body:
        update_expr.append('messageCount = :mc')
        expr_values[':mc'] = body['messageCount']

    if not update_expr:
        return {
            'statusCode': 400,
            'headers': cors_headers(),
            'body': json.dumps({'error': 'No fields to update'})
        }

    try:
        # Build update_item kwargs - only include ExpressionAttributeNames if not empty
        update_kwargs = {
            'Key': {'userId': user_id, 'sessionId': session_id},
            'UpdateExpression': 'SET ' + ', '.join(update_expr),
            'ExpressionAttributeValues': expr_values,
            'ReturnValues': 'ALL_NEW'
        }
        if expr_names:
            update_kwargs['ExpressionAttributeNames'] = expr_names

        response = table.update_item(**update_kwargs)
        result = response.get('Attributes', {})
        result.pop('conversationHistory', None)  # Don't return history
        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({'session': result}, default=decimal_default)
        }
    except Exception as e:
        import traceback
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e), 'trace': traceback.format_exc()})
        }

def delete_session(user_id, session_id):
    """Delete a session."""
    print(f"[DELETE] user_id={user_id}, session_id={session_id}")

    # Verify item exists before delete
    existing = table.get_item(Key={'userId': user_id, 'sessionId': session_id})
    if 'Item' not in existing:
        print(f"[DELETE] Item NOT FOUND - userId={user_id}, sessionId={session_id}")
        return {
            'statusCode': 404,
            'headers': cors_headers(),
            'body': json.dumps({'error': 'Session not found', 'userId': user_id, 'sessionId': session_id})
        }

    print(f"[DELETE] Item found, deleting...")
    table.delete_item(Key={'userId': user_id, 'sessionId': session_id})
    print(f"[DELETE] Successfully deleted session={session_id}")

    return {
        'statusCode': 200,
        'headers': cors_headers(),
        'body': json.dumps({'deleted': True, 'sessionId': session_id})
    }

def get_history(user_id, session_id):
    """Get conversation history for a session."""
    try:
        response = table.get_item(
            Key={'userId': user_id, 'sessionId': session_id}
        )
        item = response.get('Item')
        if not item:
            return {
                'statusCode': 404,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Session not found'})
            }

        history_data = item.get('conversationHistory', '[]')

        # Check if it's compressed (base64 encoded gzip)
        if history_data.startswith('H4sI'):  # gzip magic bytes in base64
            try:
                compressed = base64.b64decode(history_data)
                history_data = gzip.decompress(compressed).decode('utf-8')
            except:
                pass  # Not compressed, use as-is

        try:
            history = json.loads(history_data)
        except:
            history = []

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'sessionId': session_id,
                'history': history,
                'messageCount': len(history)
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def save_history(user_id, session_id, body):
    """Save conversation history for a session (compressed if large)."""
    try:
        history = body.get('history', [])
        history_json = json.dumps(history)

        # Compress if larger than 10KB
        if len(history_json) > 10000:
            compressed = gzip.compress(history_json.encode('utf-8'))
            history_data = base64.b64encode(compressed).decode('utf-8')
        else:
            history_data = history_json

        now = int(time.time() * 1000)

        response = table.update_item(
            Key={'userId': user_id, 'sessionId': session_id},
            UpdateExpression='SET conversationHistory = :ch, lastMessageAt = :lma, messageCount = :mc',
            ExpressionAttributeValues={
                ':ch': history_data,
                ':lma': now,
                ':mc': len(history)
            },
            ReturnValues='UPDATED_NEW'
        )

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'success': True,
                'messageCount': len(history)
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def get_assets(user_id, session_id):
    """Get stored assets for a session."""
    try:
        response = table.get_item(
            Key={'userId': user_id, 'sessionId': session_id}
        )
        item = response.get('Item')
        if not item:
            return {
                'statusCode': 404,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Session not found'})
            }

        assets_data = item.get('generatedAssets', '[]')

        # Check if it's compressed (base64 encoded gzip)
        if assets_data.startswith('H4sI'):  # gzip magic bytes in base64
            try:
                compressed = base64.b64decode(assets_data)
                assets_data = gzip.decompress(compressed).decode('utf-8')
            except:
                pass  # Not compressed, use as-is

        try:
            assets = json.loads(assets_data)
        except:
            assets = []

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'sessionId': session_id,
                'assets': assets,
                'assetCount': len(assets)
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def save_assets(user_id, session_id, body):
    """Save generated assets for a session (compressed if large)."""
    try:
        assets = body.get('assets', [])
        assets_json = json.dumps(assets)

        # Compress if larger than 10KB
        if len(assets_json) > 10000:
            compressed = gzip.compress(assets_json.encode('utf-8'))
            assets_data = base64.b64encode(compressed).decode('utf-8')
        else:
            assets_data = assets_json

        now = int(time.time() * 1000)

        response = table.update_item(
            Key={'userId': user_id, 'sessionId': session_id},
            UpdateExpression='SET generatedAssets = :ga, lastMessageAt = :lma',
            ExpressionAttributeValues={
                ':ga': assets_data,
                ':lma': now
            },
            ReturnValues='UPDATED_NEW'
        )

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'success': True,
                'assetCount': len(assets)
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def get_session_data(user_id, session_id):
    """Get session data (company info, operations, progress state)."""
    try:
        response = table.get_item(
            Key={'userId': user_id, 'sessionId': session_id}
        )
        item = response.get('Item')
        if not item:
            return {
                'statusCode': 404,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Session not found'})
            }

        session_data_str = item.get('sessionData', '{}')

        # Check if it's compressed (base64 encoded gzip)
        if session_data_str.startswith('H4sI'):
            try:
                compressed = base64.b64decode(session_data_str)
                session_data_str = gzip.decompress(compressed).decode('utf-8')
            except:
                pass

        try:
            session_data = json.loads(session_data_str)
        except:
            session_data = {}

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'sessionId': session_id,
                'sessionData': session_data
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def save_session_data(user_id, session_id, body):
    """Save session data (company info, operations, progress state)."""
    try:
        session_data = body.get('sessionData', {})
        session_data_json = json.dumps(session_data)

        # Compress if larger than 10KB
        if len(session_data_json) > 10000:
            compressed = gzip.compress(session_data_json.encode('utf-8'))
            session_data_str = base64.b64encode(compressed).decode('utf-8')
        else:
            session_data_str = session_data_json

        now = int(time.time() * 1000)

        response = table.update_item(
            Key={'userId': user_id, 'sessionId': session_id},
            UpdateExpression='SET sessionData = :sd, lastMessageAt = :lma',
            ExpressionAttributeValues={
                ':sd': session_data_str,
                ':lma': now
            },
            ReturnValues='UPDATED_NEW'
        )

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'success': True
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def generate_presigned_url(user_id, session_id, body):
    """Generate a presigned URL for downloading assets from S3."""
    try:
        s3_key = body.get('s3Key')

        if not s3_key:
            return {
                'statusCode': 400,
                'headers': cors_headers(),
                'body': json.dumps({'error': 's3Key is required'})
            }

        if not assets_bucket:
            return {
                'statusCode': 500,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Assets bucket not configured'})
            }

        # Verify the S3 key belongs to this session (security check)
        # Expected format: assets/{session_id}/{filename}.zip
        if not s3_key.startswith(f'assets/{session_id}/'):
            return {
                'statusCode': 403,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Access denied: S3 key does not belong to this session'})
            }

        # Check if the object exists
        try:
            s3_client.head_object(Bucket=assets_bucket, Key=s3_key)
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return {
                    'statusCode': 404,
                    'headers': cors_headers(),
                    'body': json.dumps({'error': 'Asset file not found in S3'})
                }
            raise

        # Extract filename from s3_key for download
        filename = s3_key.split('/')[-1] if '/' in s3_key else s3_key

        # Sanitize filename for Content-Disposition (must be ISO-8859-1 compatible)
        # Use RFC 5987 encoding for non-ASCII filenames
        try:
            filename.encode('iso-8859-1')
            disposition = 'attachment; filename="' + filename + '"'
        except UnicodeEncodeError:
            from urllib.parse import quote
            safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
            encoded_name = quote(filename)
            disposition = 'attachment; filename="' + safe_name + '"; filename*=UTF-8' + "''" + encoded_name

        # Generate presigned URL (valid for 24 hours)
        expiration_seconds = 24 * 60 * 60
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': assets_bucket,
                'Key': s3_key,
                'ResponseContentDisposition': disposition
            },
            ExpiresIn=expiration_seconds
        )

        expires_at = int(time.time()) + expiration_seconds

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'success': True,
                'downloadUrl': presigned_url,
                'expiresAt': expires_at,
                'expiresInHours': 24
            })
        }
    except ClientError as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': f'S3 error: {str(e)}'})
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def generate_upload_presigned_url(user_id, session_id, body):
    """Generate a presigned URL for uploading files to S3 (for multimodal processing)."""
    try:
        filename = body.get('filename')
        content_type = body.get('contentType', 'application/octet-stream')
        file_size = body.get('fileSize', 0)

        if not filename:
            return {
                'statusCode': 400,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'filename is required'})
            }

        if not assets_bucket:
            return {
                'statusCode': 500,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Assets bucket not configured'})
            }

        # Validate file size (max 100MB as per AgentCore Runtime limit)
        max_size = 100 * 1024 * 1024  # 100MB
        if file_size > max_size:
            return {
                'statusCode': 400,
                'headers': cors_headers(),
                'body': json.dumps({'error': f'File size exceeds maximum allowed ({max_size / 1024 / 1024:.0f}MB)'})
            }

        # Generate unique S3 key for upload
        # Format: uploads/{session_id}/{timestamp}_{filename}
        import uuid
        unique_id = str(uuid.uuid4())[:8]
        timestamp = int(time.time())
        safe_filename = filename.replace(' ', '_').replace('/', '_').replace('\\\\', '_')
        s3_key = f'uploads/{session_id}/{timestamp}_{unique_id}_{safe_filename}'

        # Generate presigned URL for PUT (valid for 15 minutes)
        expiration_seconds = 15 * 60
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': assets_bucket,
                'Key': s3_key,
                'ContentType': content_type,
            },
            ExpiresIn=expiration_seconds
        )

        expires_at = int(time.time()) + expiration_seconds

        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({
                'success': True,
                'uploadUrl': presigned_url,
                's3Key': s3_key,
                'bucket': assets_bucket,
                'expiresAt': expires_at,
                'expiresInMinutes': 15
            })
        }
    except ClientError as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': f'S3 error: {str(e)}'})
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }
`),
      environment: {
        SESSIONS_TABLE: sessionsTable.tableName,
        ASSETS_BUCKET: assetsBucket.bucketName,
      },
      timeout: cdk.Duration.seconds(30),
    });

    sessionsTable.grantReadWriteData(sessionApiLambda);
    assetsBucket.grantRead(sessionApiLambda);
    assetsBucket.grantPut(sessionApiLambda); // For upload presigned URL generation

    // ========================================
    // API Gateway for Session Management
    // ========================================

    const sessionApi = new apigateway.RestApi(this, "SessionApi", {
      restApiName: `${id}-session-api`,
      description: "API for managing user chat sessions",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
    });

    // Cognito authorizer for API Gateway
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(
      this,
      "SessionApiAuthorizer",
      {
        cognitoUserPools: [userPool],
        identitySource: "method.request.header.Authorization",
      }
    );

    const sessionsResource = sessionApi.root.addResource("sessions");
    const sessionIdResource = sessionsResource.addResource("{sessionId}");

    const lambdaIntegration = new apigateway.LambdaIntegration(sessionApiLambda);

    // GET /sessions - List sessions
    sessionsResource.addMethod("GET", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // POST /sessions - Create session
    sessionsResource.addMethod("POST", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // PUT /sessions/{sessionId} - Update session
    sessionIdResource.addMethod("PUT", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // DELETE /sessions/{sessionId} - Delete session
    sessionIdResource.addMethod("DELETE", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // History endpoints for conversation persistence
    const historyResource = sessionIdResource.addResource("history");

    // GET /sessions/{sessionId}/history - Get conversation history
    historyResource.addMethod("GET", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // PUT /sessions/{sessionId}/history - Save conversation history
    historyResource.addMethod("PUT", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Assets endpoints for generated assets persistence
    const assetsResource = sessionIdResource.addResource("assets");

    // GET /sessions/{sessionId}/assets - Get generated assets
    assetsResource.addMethod("GET", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // PUT /sessions/{sessionId}/assets - Save generated assets
    assetsResource.addMethod("PUT", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Session data endpoints for context persistence (company, operations, progress)
    const dataResource = sessionIdResource.addResource("data");

    // GET /sessions/{sessionId}/data - Get session data
    dataResource.addMethod("GET", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // PUT /sessions/{sessionId}/data - Save session data
    dataResource.addMethod("PUT", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Presigned URL endpoint for S3 asset downloads
    const presignedResource = sessionIdResource.addResource("presigned");

    // POST /sessions/{sessionId}/presigned - Generate presigned URL for download
    presignedResource.addMethod("POST", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Upload presigned URL endpoint for multimodal file uploads
    const uploadPresignedResource =
      sessionIdResource.addResource("upload-presigned");

    // POST /sessions/{sessionId}/upload-presigned - Generate presigned URL for upload
    uploadPresignedResource.addMethod("POST", lambdaIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // ========================================
    // S3 Bucket for Frontend
    // ========================================

    const frontendBucket = new s3.Bucket(this, "FrontendBucket", {
      bucketName: `${id.toLowerCase()}-frontend-${this.account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
    });

    // ========================================
    // CloudFront Distribution for Frontend
    // ========================================

    const originAccessIdentity = new cloudfront.OriginAccessIdentity(
      this,
      "OAI",
      {
        comment: "OAI for AICC Builder frontend",
      }
    );

    frontendBucket.grantRead(originAccessIdentity);

    // Build additional behaviors for CloudFront
    const additionalBehaviors: Record<string, cloudfront.BehaviorOptions> = {};

    // Proxy /ws and /api/* through CloudFront to the ALB (solves Mixed Content).
    // ALB DNS is read from an SSM parameter (dynamic reference) populated by
    // the ECS stack — keeps this stack free of any CFN reference into EcsStack.
    if (props?.albDnsSsmParamName) {
      const albDnsName = ssm.StringParameter.valueForStringParameter(
        this, props.albDnsSsmParamName
      );
      const albOrigin = new origins.HttpOrigin(albDnsName, {
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      });
      const albBehavior: cloudfront.BehaviorOptions = {
        origin: albOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
      };
      additionalBehaviors["/ws"] = albBehavior;
      additionalBehaviors["/api/*"] = albBehavior;
    }

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      defaultBehavior: {
        origin: new origins.S3Origin(frontendBucket, {
          originAccessIdentity,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      additionalBehaviors,
      defaultRootObject: "index.html",
      errorResponses: [
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.minutes(5),
        },
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.minutes(5),
        },
      ],
    });

    // ========================================
    // Outputs
    // ========================================

    this.frontendUrl = new cdk.CfnOutput(this, "FrontendUrl", {
      value: `https://${distribution.distributionDomainName}`,
      description: "CloudFront URL for the frontend",
      exportName: `${id}-frontend-url`,
    });

    this.userPoolId = new cdk.CfnOutput(this, "UserPoolId", {
      value: userPool.userPoolId,
      description: "Cognito User Pool ID",
      exportName: `${id}-user-pool-id`,
    });

    this.userPoolClientId = new cdk.CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
      description: "Cognito User Pool Client ID",
      exportName: `${id}-user-pool-client-id`,
    });

    this.identityPoolId = new cdk.CfnOutput(this, "IdentityPoolId", {
      value: identityPool.ref,
      description: "Cognito Identity Pool ID for AWS credentials",
      exportName: `${id}-identity-pool-id`,
    });

    new cdk.CfnOutput(this, "FrontendBucketName", {
      value: frontendBucket.bucketName,
      description: "S3 bucket for frontend hosting",
    });

    new cdk.CfnOutput(this, "CloudFrontDistributionId", {
      value: distribution.distributionId,
      description: "CloudFront distribution ID for cache invalidation",
    });

    new cdk.CfnOutput(this, "Region", {
      value: this.region,
      description: "Deployment region",
    });

    new cdk.CfnOutput(this, "AuthenticatedRoleArn", {
      value: authenticatedRole.roleArn,
      description: "IAM role for authenticated users",
    });

    new cdk.CfnOutput(this, "SessionApiUrl", {
      value: sessionApi.url,
      description: "API Gateway URL for session management",
    });

    new cdk.CfnOutput(this, "SessionsTableName", {
      value: sessionsTable.tableName,
      description: "DynamoDB table for user sessions",
    });

    new cdk.CfnOutput(this, "AssetsBucketName", {
      value: assetsBucket.bucketName,
      description: "S3 bucket for generated assets (ZIP packages)",
    });

    new cdk.CfnOutput(this, "AgentCoreS3PolicyArn", {
      value: agentCoreS3Policy.managedPolicyArn,
      description: "IAM Policy ARN for AgentCore Runtime S3 access",
    });

    new cdk.CfnOutput(this, "AgentCoreDbPolicyArn", {
      value: agentCoreDbPolicy.managedPolicyArn,
      description: "IAM Policy ARN for AgentCore Runtime database access",
    });
  }
}
