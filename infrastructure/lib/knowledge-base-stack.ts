import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { bedrock, s3vectors } from "@cdklabs/generative-ai-cdk-constructs";

/**
 * Knowledge Base Stack for Contact Flow Generator RAG
 *
 * Uses @cdklabs/generative-ai-cdk-constructs to provision:
 * - Amazon S3 Vectors bucket + vector index (cost-optimized vector store —
 *   up to ~90% cheaper than OpenSearch Serverless, which has an always-on
 *   minimum OCU cost). This KB is small (a few dozen curated docs) and queried
 *   infrequently, so S3 Vectors' pay-per-use model is a much better fit.
 * - Bedrock Knowledge Base + S3 Data Source
 *
 * Migration note: switching the vector store replaces the Knowledge Base, so
 * the ContactFlowKnowledgeBaseId output changes — redeploy then re-run the data
 * source sync. The dimension (1024) must match the embeddings model
 * (TITAN_EMBED_TEXT_V2_1024).
 */
export interface KnowledgeBaseStackProps extends cdk.StackProps {
  /**
   * The concrete deploy region (e.g. "ap-northeast-1"), passed from app.ts so the
   * KB execution role name can be made unique per region. IAM roles are global,
   * and the cdklabs construct derives the role name from the construct path only
   * (no region), so identical stack names across regions would collide.
   */
  resolvedRegion?: string;
}

export class KnowledgeBaseStack extends cdk.Stack {
  public readonly docsBucketName: cdk.CfnOutput;
  public readonly agentCoreKbPolicyArn: cdk.CfnOutput;

  constructor(scope: Construct, id: string, props?: KnowledgeBaseStackProps) {
    super(scope, id, props);

    // S3 Bucket for Knowledge Base Documents
    // No explicit bucketName — let CloudFormation auto-generate to avoid conflicts on re-create
    const docsBucket = new s3.Bucket(this, "KnowledgeBaseDocsBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
    });

    // S3 Vectors store: a vector bucket + an index sized to the embeddings model.
    // NOTE: autoDeleteObjects is intentionally OFF — the S3 Vectors auto-delete
    // provider bundles a Lambda via Docker at synth time, which would force a
    // Docker dependency on every deploy. The workshop cleanup script empties the
    // vector bucket explicitly, so we keep synth/deploy Docker-free here.
    const vectorBucket = new s3vectors.VectorBucket(this, "ContactFlowVectorBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const vectorIndex = new s3vectors.VectorIndex(this, "ContactFlowVectorIndex", {
      vectorBucket,
      // TITAN_EMBED_TEXT_V2_1024 → 1024-dimensional embeddings.
      dimension: 1024,
      distanceMetric: s3vectors.VectorIndexDistanceMetric.COSINE,
      // S3 Vectors caps FILTERABLE metadata at 2048 bytes per vector. Bedrock
      // stores the full chunk text + its source metadata blob as metadata, which
      // easily exceeds that — causing "Filterable metadata must have at most
      // 2048 bytes" and a 100%-failed ingestion. Mark Bedrock's reserved keys as
      // NON-filterable so they don't count against the limit (they're retrieved,
      // not used as query filters).
      nonFilterableMetadataKeys: [
        "AMAZON_BEDROCK_TEXT",
        "AMAZON_BEDROCK_METADATA",
      ],
    });

    // Knowledge Base backed by the S3 Vectors index (instead of OpenSearch Serverless).
    const kb = new bedrock.VectorKnowledgeBase(this, "ContactFlowKB", {
      embeddingsModel: bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_1024,
      vectorStore: vectorIndex,
      description: "Curated Amazon Connect Contact Flow documentation for RAG-enhanced generation",
      instruction: "Use this knowledge base to answer questions about Amazon Connect contact flow blocks, patterns, and best practices.",
    });

    // MULTI-REGION FIX: the cdklabs construct derives the KB execution role name
    // from the construct path (stack id + construct id) only — NOT the region.
    // Since the stack name is identical across regions (AiccBuilderKnowledgeBase-<stage>),
    // Seoul and Tokyo would generate the SAME global IAM role name. IAM roles are
    // global, so the second region collides with the first region's role — whose
    // trust policy pins aws:SourceArn to the first region — and Bedrock then can't
    // assume it ("unable to assume the given role"). Make the role name unique per
    // region so each region owns its own correctly-scoped role.
    const roleRegion = props?.resolvedRegion || cdk.Stack.of(this).region;
    const cfnKbRole = kb.role.node.defaultChild as iam.CfnRole;
    cfnKbRole.addPropertyOverride(
      "RoleName",
      `AmazonBedrockExecKB-${id}-${roleRegion}`.slice(0, 64),
    );

    // The cdklabs construct grants the KB role PutVectors/GetVectors/QueryVectors
    // but NOT DeleteVectors. Re-ingestion (after docs change) must delete the old
    // vectors first, so without this the FIRST ingest works but every UPDATE
    // fails with AccessDenied on s3vectors:DeleteVectors. Grant the full vector
    // read/write/delete set on this index + bucket explicitly.
    kb.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "s3vectors:PutVectors",
          "s3vectors:GetVectors",
          "s3vectors:QueryVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:ListVectors",
          "s3vectors:GetIndex",
          "s3vectors:GetVectorBucket",
        ],
        resources: [
          vectorBucket.vectorBucketArn,
          `${vectorBucket.vectorBucketArn}/*`,
          vectorIndex.vectorIndexArn,
        ],
      })
    );

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
