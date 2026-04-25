# AICC Builder Infrastructure

<div align="center">

**AWS CDK Infrastructure for AICC Builder**

[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-2.x-orange?style=flat&logo=amazonaws)](https://aws.amazon.com/cdk/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-blue?style=flat&logo=typescript)](https://typescriptlang.org/)

</div>

---

## Overview

Three CDK stacks provision the AWS infrastructure for AICC Builder. The AgentCore Runtime itself is deployed separately via `agentcore launch` (called by `deploy.sh`), not through CDK.

```
infrastructure/
├── lib/
│   ├── app.ts                    # CDK app entry point (stage support)
│   ├── aicc-builder-stack.ts     # Main stack (41KB)
│   ├── redis-stack.ts            # Optional Redis stack
│   └── knowledge-base-stack.ts   # Bedrock Knowledge Base stack
├── package.json
├── cdk.json
└── tsconfig.json
```

---

## Stacks

### 1. AiccBuilderStack (Main)

Always deployed. Contains all core infrastructure.

| Resource | Service | Purpose |
|----------|---------|---------|
| User Pool | Cognito | User authentication (self-sign-up disabled) |
| User Pool Client | Cognito | Frontend auth client |
| Identity Pool | Cognito | AWS credentials for AgentCore WebSocket |
| Frontend Bucket | S3 | React app hosting |
| Assets Bucket | S3 | Generated assets + project workspace state |
| CloudFront Distribution | CloudFront | Frontend CDN |
| Sessions Table | DynamoDB | Session metadata |
| Assets Table | DynamoDB | Asset metadata |
| Session API | Lambda + API Gateway | REST API for session CRUD |
| IAM Policies | IAM | AgentCore role policies (S3, DynamoDB) |

### 2. RedisStack (Optional — disabled by default)

Deployed when `ENABLE_REDIS=true`. Provides optional session context caching. Not required — the backend uses S3-backed `ProjectWorkspace` for state persistence and in-memory fallback for session context.

| Resource | Service | Purpose |
|----------|---------|---------|
| VPC | EC2 | Network isolation |
| ElastiCache Serverless | ElastiCache | Redis for session state (8hr TTL) |
| Security Groups | EC2 | Network access control |

### 3. KnowledgeBaseStack (Default: enabled)

Deployed by default. Provides RAG for Contact Flow generation.

| Resource | Service | Purpose |
|----------|---------|---------|
| Docs Bucket | S3 | Knowledge base source documents |
| Vector Collection | OpenSearch Serverless | Vector index for RAG |
| Knowledge Base | Bedrock | RAG retrieval for Contact Flow docs |
| IAM Policy | IAM | AgentCore KB access policy |

---

## Stage Support

Each stage creates isolated stacks with separate resources:

```bash
./deploy.sh --stage dev     # AiccBuilderStack-dev, AiccBuilderKnowledgeBase-dev
./deploy.sh --stage prod    # AiccBuilderStack-prod, AiccBuilderKnowledgeBase-prod
./deploy.sh                 # AiccBuilderStack (no suffix, default)
```

Stage is passed via CDK context: `-c stage=dev`

---

## Deployment

### Via deploy.sh (recommended)

```bash
./deploy.sh                    # Full deployment (CDK + AgentCore + frontend)
./deploy.sh --infra-only       # CDK stacks only
```

### Manual CDK commands

```bash
cd infrastructure
npm install

# Deploy all stacks
npx cdk deploy --all --require-approval never

# Deploy specific stack
npx cdk deploy AiccBuilderStack

# Deploy with stage
npx cdk deploy --all -c stage=dev

# Preview changes
npx cdk diff

# Destroy
npx cdk destroy --all
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CDK_DEFAULT_REGION` | `ap-northeast-1` | Target region |
| `CDK_DEFAULT_ACCOUNT` | (from AWS CLI) | Target account |
| `ENABLE_REDIS` | `false` | Deploy Redis stack |
| `ENABLE_KNOWLEDGE_BASE` | `true` | Deploy KB stack |

---

## Outputs

CDK outputs are saved to `cdk-outputs[-<stage>].json` by deploy.sh:

| Output | Used By |
|--------|---------|
| `FrontendUrl` | Browser access URL |
| `FrontendBucketName` | S3 sync target |
| `UserPoolId` | Frontend auth config |
| `UserPoolClientId` | Frontend auth config |
| `IdentityPoolId` | Frontend AWS credentials |
| `AssetsBucketName` | Backend asset + project workspace storage |
| `SessionApiUrl` | Frontend session API |
| `AgentCoreS3PolicyArn` | Attached to AgentCore role |
| `AgentCoreDbPolicyArn` | Attached to AgentCore role |
| `ContactFlowKnowledgeBaseId` | Backend KB RAG |
| `AgentCoreKbPolicyArn` | Attached to AgentCore role |
| `CloudFrontDistributionId` | Cache invalidation |

---

## Region

Deployed to `ap-northeast-1` (Tokyo) because AgentCore Runtime is available there. AgentCore supported regions: us-east-1, us-east-2, us-west-2, ap-south-1, ap-southeast-1, ap-southeast-2, ap-northeast-1, eu-central-1, eu-west-1.

---

<div align="center">

**Built with AWS CDK**

</div>
