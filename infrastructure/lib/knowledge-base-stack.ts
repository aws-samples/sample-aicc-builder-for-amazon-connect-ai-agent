import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { bedrock } from "@cdklabs/generative-ai-cdk-constructs";

/**
 * Knowledge Base Stack for Contact Flow Generator RAG
 *
 * Uses @cdklabs/generative-ai-cdk-constructs to automatically provision:
 * - OpenSearch Serverless collection + vector index
 * - Bedrock Knowledge Base + S3 Data Source
 */
export class KnowledgeBaseStack extends cdk.Stack {
  public readonly docsBucketName: cdk.CfnOutput;
  public readonly agentCoreKbPolicyArn: cdk.CfnOutput;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // S3 Bucket for Knowledge Base Documents
    // No explicit bucketName — let CloudFormation auto-generate to avoid conflicts on re-create
    const docsBucket = new s3.Bucket(this, "KnowledgeBaseDocsBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
    });

    // Knowledge Base (auto-creates OpenSearch Serverless collection + vector index)
    const kb = new bedrock.VectorKnowledgeBase(this, "ContactFlowKB", {
      embeddingsModel: bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_1024,
      description: "Curated Amazon Connect Contact Flow documentation for RAG-enhanced generation",
      instruction: "Use this knowledge base to answer questions about Amazon Connect contact flow blocks, patterns, and best practices.",
    });

    // S3 Data Source
    const dataSource = new bedrock.S3DataSource(this, "ContactFlowDataSource", {
      bucket: docsBucket,
      knowledgeBase: kb,
      dataSourceName: "contact-flow-docs",
      inclusionPrefixes: ["contact-flow/"],
      chunkingStrategy: bedrock.ChunkingStrategy.fixedSize({
        maxTokens: 300,
        overlapPercentage: 20,
      }),
    });

    // IAM Policy for AgentCore to retrieve from KB
    // No explicit managedPolicyName — let CloudFormation auto-generate to avoid conflicts
    const agentCoreKbPolicy = new iam.ManagedPolicy(this, "AgentCoreKbPolicy", {
      statements: [
        new iam.PolicyStatement({
          actions: ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
          resources: [kb.knowledgeBaseArn],
        }),
      ],
    });

    // Outputs (maintain compatibility with existing scripts)
    this.docsBucketName = new cdk.CfnOutput(this, "KnowledgeBaseDocsBucketName", {
      value: docsBucket.bucketName,
    });

    this.agentCoreKbPolicyArn = new cdk.CfnOutput(this, "AgentCoreKbPolicyArn", {
      value: agentCoreKbPolicy.managedPolicyArn,
    });

    new cdk.CfnOutput(this, "ContactFlowKnowledgeBaseId", {
      value: kb.knowledgeBaseId,
    });

    new cdk.CfnOutput(this, "DataSourceId", {
      value: dataSource.dataSourceId,
    });
  }
}
