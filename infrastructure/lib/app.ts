#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { AiccBuilderStack } from "./aicc-builder-stack";
import { KnowledgeBaseStack } from "./knowledge-base-stack";
import { EcsStack } from "./ecs-stack";

const app = new cdk.App();

// Stage support: deploy.sh --stage dev → -c stage=dev
const stage = app.node.tryGetContext("stage") as string | undefined;
const suffix = stage ? `-${stage}` : "";

// Environment configuration
// Region priority: CDK context (-c targetRegion=...) > CDK_DEFAULT_REGION > default
const targetRegion = app.node.tryGetContext("targetRegion") as string | undefined;
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: targetRegion || process.env.CDK_DEFAULT_REGION || "ap-northeast-2",
};

// Shared SSM parameter for ALB DNS — written by EcsStack, read by AiccBuilderStack.
// Using SSM dynamic reference keeps the dependency edge single-direction
// (AiccBuilderStack → EcsStack via assetsBucket) with no CFN circular ref.
const mainStackId = `AiccBuilderStack${suffix}`;
const ecsStackId = `AiccBuilderEcs${suffix}`;
const albDnsSsmParamName = `/aicc-builder${suffix}/alb-dns`;

// Main AICC Builder Stack first — owns AssetsBucket, Cognito, CloudFront, Lambda API.
// CloudFront reads ALB DNS via SSM dynamic reference (no CFN cross-stack edge).
const mainStack = new AiccBuilderStack(app, mainStackId, {
  env,
  description: "AICC Builder - AI-powered customization platform for Amazon Connect workshops",
  albDnsSsmParamName,
});

// ECS Fargate stack — receives the AssetsBucket from AiccBuilderStack and owns
// all S3 Files resources (FileSystem, MountTargets, TaskDef volume) + ALB.
const ecrRepoName = `${ecsStackId.toLowerCase()}-repo`;
new EcsStack(app, ecsStackId, {
  env,
  description: "AICC Builder ECS Fargate - AI agent runtime with WebSocket support",
  ecrRepoName,
  assetsBucket: mainStack.assetsBucket,
  albDnsSsmParamName,
});

// Knowledge Base Stack for Contact Flow Generator RAG
const enableKnowledgeBase = process.env.ENABLE_KNOWLEDGE_BASE !== "false";
if (enableKnowledgeBase) {
  new KnowledgeBaseStack(app, `AiccBuilderKnowledgeBase${suffix}`, {
    env,
    description: "AICC Builder Knowledge Base - RAG for Contact Flow generation",
  });
}

app.synth();
