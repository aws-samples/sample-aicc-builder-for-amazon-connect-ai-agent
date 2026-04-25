import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as applicationautoscaling from "aws-cdk-lib/aws-applicationautoscaling";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

/**
 * ECS Fargate Stack for AICC Builder
 *
 * Deploys a single ECS Fargate service (monolith) with:
 * - VPC with 2 AZs, 1 NAT Gateway
 * - Internet-facing ALB with WebSocket support (4h idle timeout)
 * - ECS Cluster with Container Insights
 * - Fargate Task: 2 vCPU, 4GB RAM, ARM64 (Graviton)
 * - X-Ray sidecar for observability
 * - Auto-scaling based on ActiveWebSocketConnections metric
 * - S3 Files volume mount at /mnt/s3 (via CDK context or deploy.sh CLI fallback)
 */
export interface EcsStackProps extends cdk.StackProps {
  /**
   * Pre-created ECR repository name.
   * The repo must exist and have an image pushed BEFORE CDK deploy,
   * to avoid the chicken-and-egg problem (ECS service needs image at creation time).
   */
  ecrRepoName?: string;

  /**
   * S3 bucket to back the S3 Files (NFS) filesystem. Must be passed from
   * AiccBuilderStack so CDK can infer the deploy order (bucket first, then
   * FileSystem + MountTargets here). ALB DNS is published to SSM Parameter
   * Store so AiccBuilderStack can consume it without a circular reference.
   */
  assetsBucket: s3.IBucket;

  /**
   * SSM parameter name under which to publish the ALB DNS name.
   * AiccBuilderStack reads the same parameter via dynamic reference.
   */
  albDnsSsmParamName: string;
}

export class EcsStack extends cdk.Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albDnsName: cdk.CfnOutput;
  public readonly ecsClusterName: cdk.CfnOutput;
  public readonly ecsServiceName: cdk.CfnOutput;
  public readonly taskDefinitionArn: cdk.CfnOutput;

  constructor(scope: Construct, id: string, props?: EcsStackProps) {
    super(scope, id, props);

    // ========================================
    // VPC — 2 AZs, 1 NAT Gateway
    // ========================================
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
    });

    // ========================================
    // ECR Repository (pre-created by deploy.sh to avoid chicken-and-egg)
    // deploy.sh creates the repo + pushes image BEFORE cdk deploy,
    // so the ECS service can pull the image at creation time.
    // ========================================
    const ecrRepoName = props?.ecrRepoName || `${id.toLowerCase()}-repo`;
    const ecrRepo = ecr.Repository.fromRepositoryName(
      this,
      "EcrRepo",
      ecrRepoName,
    );

    // ========================================
    // ECS Cluster (Container Insights enabled)
    // ========================================
    const cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: `${id.toLowerCase()}-cluster`,
      vpc,
      containerInsights: true,
    });

    // ========================================
    // Task Execution Role
    // ========================================
    const executionRole = new iam.Role(this, "TaskExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy"
        ),
      ],
    });

    // ECR pull permission
    ecrRepo.grantPull(executionRole);

    // ========================================
    // Task Role (application permissions)
    // ========================================
    const taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });

    // S3 access (assets bucket — wildcard covers any stack naming)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ],
        resources: [
          "arn:aws:s3:::aiccbuilder*",
          "arn:aws:s3:::aiccbuilder*/*",
        ],
      })
    );

    // S3 Files client access (for NFS mount from ECS tasks)
    taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonS3FilesClientFullAccess")
    );

    // Bedrock access (model invocation)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: ["*"],
      })
    );

    // DynamoDB access (session table)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ],
        resources: ["arn:aws:dynamodb:*:*:table/aiccbuilder*"],
      })
    );

    // Secrets Manager (for DB introspection)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "secretsmanager:GetSecretValue",
        ],
        resources: ["*"],
      })
    );

    // RDS Data API (for DB introspection)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "rds-data:ExecuteStatement",
          "rds-data:BatchExecuteStatement",
        ],
        resources: ["*"],
      })
    );

    // CloudWatch metrics (for custom metrics)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "cloudwatch:PutMetricData",
        ],
        resources: ["*"],
      })
    );

    // X-Ray tracing
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ],
        resources: ["*"],
      })
    );

    // Bedrock Knowledge Base (for Contact Flow RAG)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:Retrieve",
          "bedrock:RetrieveAndGenerate",
        ],
        resources: ["*"],
      })
    );

    // ========================================
    // Task Definition — ARM64, 4 vCPU, 16GB
    // ========================================
    const taskDefinition = new ecs.FargateTaskDefinition(this, "TaskDef", {
      memoryLimitMiB: 16384,
      cpu: 4096,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      executionRole,
      taskRole,
    });

    // Log group
    const logGroup = new logs.LogGroup(this, "AppLogGroup", {
      logGroupName: `/ecs/${id.toLowerCase()}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Main container
    const appContainer = taskDefinition.addContainer("app", {
      image: ecs.ContainerImage.fromEcrRepository(ecrRepo, "latest"),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "app",
      }),
      environment: {
        AWS_REGION: this.region,
        AWS_DEFAULT_REGION: this.region,
        S3FILES_MOUNT_PATH: "/mnt/s3",
        SESSION_STORE_BACKEND: "s3files",
        PYTHONUNBUFFERED: "1",
      },
      portMappings: [
        { containerPort: 8080, protocol: ecs.Protocol.TCP },
      ],
      healthCheck: {
        command: ["CMD-SHELL", "python healthcheck.py"],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(30),
      },
    });

    // X-Ray sidecar container (Improvement D)
    const xrayLogGroup = new logs.LogGroup(this, "XRayLogGroup", {
      logGroupName: `/ecs/${id.toLowerCase()}/xray`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    taskDefinition.addContainer("xray-daemon", {
      image: ecs.ContainerImage.fromRegistry(
        "public.ecr.aws/xray/aws-xray-daemon:latest"
      ),
      logging: ecs.LogDrivers.awsLogs({
        logGroup: xrayLogGroup,
        streamPrefix: "xray",
      }),
      memoryLimitMiB: 256,
      cpu: 32,
      portMappings: [
        { containerPort: 2000, protocol: ecs.Protocol.UDP },
      ],
      essential: false,
    });

    // ========================================
    // ALB — Internet-facing, WebSocket support
    // ========================================
    const alb = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc,
      internetFacing: true,
      loadBalancerName: `${id.toLowerCase()}-alb`,
      idleTimeout: cdk.Duration.seconds(4000), // ALB max: 4000s (~66min), WS keepalive handles longer sessions
    });

    this.alb = alb;

    // Security group for ALB
    alb.connections.allowFromAnyIpv4(ec2.Port.tcp(80), "HTTP");
    alb.connections.allowFromAnyIpv4(ec2.Port.tcp(443), "HTTPS");

    // Target group with sticky sessions
    const targetGroup = new elbv2.ApplicationTargetGroup(this, "TargetGroup", {
      vpc,
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: "/ping",
        interval: cdk.Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        timeout: cdk.Duration.seconds(5),
      },
      stickinessCookieDuration: cdk.Duration.hours(8), // 8h sticky sessions
      deregistrationDelay: cdk.Duration.seconds(60),
    });

    // HTTP listener (for now — add HTTPS listener with ACM cert in production)
    alb.addListener("HttpListener", {
      port: 80,
      defaultTargetGroups: [targetGroup],
    });

    // ========================================
    // Fargate Service
    // ========================================
    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      serviceName: `${id.toLowerCase()}-svc`,
      enableExecuteCommand: true, // ECS Exec for debugging
    });

    service.attachToApplicationTargetGroup(targetGroup);

    // Allow ALB to reach ECS tasks
    service.connections.allowFrom(alb, ec2.Port.tcp(8080), "ALB to ECS");

    // Allow S3 Files NFS (TCP 2049) within the service SG.
    // S3 Files mount targets attach to the ECS task SG (see deploy.sh),
    // so a self-reference rule is required for tasks to mount /mnt/s3.
    service.connections.allowInternally(ec2.Port.tcp(2049), "S3 Files NFS (self)");

    // ========================================
    // S3 Files — NFS view of the assets bucket for ECS tasks (all L1 — CDK L2 not yet available)
    //   AWS::S3Files::FileSystem   (one per bucket)
    //   AWS::S3Files::MountTarget  (one per private subnet, so any AZ works)
    //   TaskDefinition.Volume.S3FilesVolumeConfiguration  (mount into container at /mnt/s3)
    // Bucket comes in via props from AiccBuilderStack — this gives a single
    // AiccBuilderStack → EcsStack dependency edge, no circular refs.
    // ========================================
    const s3FilesRole = new iam.Role(this, "S3FilesRole", {
      assumedBy: new iam.ServicePrincipal("elasticfilesystem.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": `arn:aws:s3files:${this.region}:${this.account}:file-system/*` },
        },
      }),
      description: "Allows S3 Files to access the AICC Builder assets bucket",
      inlinePolicies: {
        S3Access: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ["s3:ListBucket", "s3:ListBucketVersions"],
              resources: [props!.assetsBucket.bucketArn],
              conditions: { StringEquals: { "aws:ResourceAccount": this.account } },
            }),
            new iam.PolicyStatement({
              actions: [
                "s3:AbortMultipartUpload", "s3:DeleteObject", "s3:DeleteObjectVersion",
                "s3:GetObject", "s3:GetObjectVersion", "s3:ListMultipartUploadParts",
                "s3:PutObject", "s3:PutObjectAcl",
              ],
              resources: [`${props!.assetsBucket.bucketArn}/*`],
              conditions: { StringEquals: { "aws:ResourceAccount": this.account } },
            }),
            new iam.PolicyStatement({
              actions: [
                "events:DeleteRule", "events:DisableRule", "events:EnableRule",
                "events:PutRule", "events:PutTargets", "events:RemoveTargets",
              ],
              resources: [`arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*`],
              conditions: { StringEquals: { "events:ManagedBy": "elasticfilesystem.amazonaws.com" } },
            }),
            new iam.PolicyStatement({
              actions: [
                "events:DescribeRule", "events:ListRuleNamesByTarget",
                "events:ListRules", "events:ListTargetsByRule",
              ],
              resources: [`arn:aws:events:*:*:rule/*`],
            }),
          ],
        }),
      },
    });

    const s3FilesFs = new cdk.CfnResource(this, "S3FilesFileSystem", {
      type: "AWS::S3Files::FileSystem",
      properties: {
        Bucket: props!.assetsBucket.bucketArn,
        RoleArn: s3FilesRole.roleArn,
        AcceptBucketWarning: true,
        SynchronizationConfiguration: {
          // Default sizeLessThan is 128KB which blocks CFN/OpenAPI/Lambda bundles
          // from importing back into NFS. Raise to 10MB so all generated assets fit.
          ImportDataRules: [
            { Prefix: "", Trigger: "ON_DIRECTORY_FIRST_ACCESS", SizeLessThan: 10485760 },
          ],
          ExpirationDataRules: [{ DaysAfterLastAccess: 30 }],
        },
      },
    });
    s3FilesFs.node.addDependency(s3FilesRole);

    const s3FilesFsArn = s3FilesFs.getAtt("FileSystemArn").toString();
    const s3FilesFsId = s3FilesFs.getAtt("FileSystemId").toString();
    const ecsSgId = service.connections.securityGroups[0].securityGroupId;

    vpc.privateSubnets.forEach((subnet, i) => {
      new cdk.CfnResource(this, `S3FilesMountTarget${i}`, {
        type: "AWS::S3Files::MountTarget",
        properties: {
          FileSystemId: s3FilesFsId,
          SubnetId: subnet.subnetId,
          SecurityGroups: [ecsSgId],
        },
      });
    });

    const cfnTaskDef = taskDefinition.node.defaultChild as ecs.CfnTaskDefinition;
    const existingVolumes = Array.isArray(cfnTaskDef.volumes) ? (cfnTaskDef.volumes as any[]) : [];
    cfnTaskDef.volumes = [
      ...existingVolumes.filter((v: any) => v.name !== "s3files"),
      {
        name: "s3files",
        configuredAtLaunch: false,
        s3FilesVolumeConfiguration: { fileSystemArn: s3FilesFsArn, rootDirectory: "/" },
      },
    ];
    const cfnContainerDefs = cfnTaskDef.containerDefinitions as any[];
    if (cfnContainerDefs?.length) {
      const appContainerDef = cfnContainerDefs[0];
      const existingMounts = appContainerDef.mountPoints || [];
      if (!existingMounts.some((m: any) => m.sourceVolume === "s3files")) {
        appContainerDef.mountPoints = [
          ...existingMounts,
          { sourceVolume: "s3files", containerPath: "/mnt/s3", readOnly: false },
        ];
      }
    }

    // Note: the ALB DNS is published to the SSM parameter
    // `props!.albDnsSsmParamName` by deploy.sh after stack deployment,
    // not here. Writing it from CDK would create a parameter owned by this
    // stack; we instead treat the parameter as an external contract so
    // AiccBuilderStack can resolve it via dynamic reference at its own
    // deploy time (broken only on the first-ever deploy, where deploy.sh
    // pre-creates the parameter with a placeholder value).

    // ========================================
    // Auto-Scaling (Improvement G)
    // ========================================
    const scaling = service.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: 10,
    });

    // Scale on CPU utilization
    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(300),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // Custom metric-based scaling on ActiveWebSocketConnections
    const wsMetric = new cloudwatch.Metric({
      namespace: "AiccBuilder/ECS",
      metricName: "ActiveWebSocketConnections",
      statistic: "Average",
      period: cdk.Duration.minutes(1),
    });

    scaling.scaleOnMetric("WsConnectionScaling", {
      metric: wsMetric,
      scalingSteps: [
        { upper: 5, change: 0 },  // 0-5 connections: no scaling
        { lower: 5, change: +1 }, // 5+ connections: add 1 task
        { lower: 15, change: +2 }, // 15+ connections: add 2 more
        { lower: 30, change: +3 }, // 30+ connections: add 3 more
      ],
      adjustmentType:
        applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
    });

    // ========================================
    // Outputs
    // ========================================
    this.albDnsName = new cdk.CfnOutput(this, "AlbDnsName", {
      value: alb.loadBalancerDnsName,
      description: "ALB DNS name for WebSocket connections",
      exportName: `${id}:AlbDnsName`,
    });

    new cdk.CfnOutput(this, "AlbUrl", {
      value: `http://${alb.loadBalancerDnsName}`,
      description: "ALB URL",
    });

    this.ecsClusterName = new cdk.CfnOutput(this, "EcsClusterName", {
      value: cluster.clusterName,
      description: "ECS Cluster name",
    });

    this.ecsServiceName = new cdk.CfnOutput(this, "EcsServiceName", {
      value: service.serviceName,
      description: "ECS Service name",
    });

    this.taskDefinitionArn = new cdk.CfnOutput(this, "TaskDefinitionArn", {
      value: taskDefinition.taskDefinitionArn,
      description: "ECS Task Definition ARN",
    });

    new cdk.CfnOutput(this, "EcrRepositoryUri", {
      value: ecrRepo.repositoryUri,
      description: "ECR Repository URI",
    });

    new cdk.CfnOutput(this, "TaskRoleArn", {
      value: taskRole.roleArn,
      description: "ECS Task Role ARN (for policy attachment)",
    });

    new cdk.CfnOutput(this, "VpcId", {
      value: vpc.vpcId,
      description: "VPC ID",
    });
  }
}
