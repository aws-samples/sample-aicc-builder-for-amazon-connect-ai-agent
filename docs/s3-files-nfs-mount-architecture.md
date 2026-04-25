# S3 Files NFS Mount Architecture

## 개요 (Overview)

AICC Builder의 ECS Fargate 컨테이너는 **AWS S3 Files** 서비스를 통해 S3 버킷을 NFS 파일시스템으로 마운트합니다.
이것은 단순한 Docker host bind mount가 **아닙니다**. AWS가 2024년 말에 출시한 **S3 Files** 서비스를 사용하여 S3 버킷을 NFS v4.1 프로토콜로 접근할 수 있게 하는 구조입니다.

### 핵심 차이점: Host Bind Mount vs S3 Files NFS

| 구분 | Host Bind Mount | S3 Files NFS (현재 사용) |
|------|----------------|--------------------------|
| 동작 방식 | 호스트 OS의 디렉터리를 컨테이너에 매핑 | S3 버킷을 NFS 프로토콜로 마운트 |
| Fargate 지원 | 불가 (호스트 없음) | 지원 (`s3filesVolumeConfiguration`) |
| 데이터 영속성 | 호스트 수명에 종속 | S3에 영구 저장 |
| 다중 컨테이너 공유 | 같은 호스트에서만 가능 | VPC 내 모든 서비스에서 접근 가능 |
| 접근 방식 | 로컬 파일시스템 | NFS v4.1 over TCP (포트 2049) |

---

## 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────────────────┐
│                         AWS Cloud                               │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  VPC (Private Subnets)                                   │   │
│  │                                                          │   │
│  │  ┌─────────────────────┐     ┌────────────────────────┐  │   │
│  │  │  ECS Fargate Task   │     │  S3 Files Service      │  │   │
│  │  │                     │     │                        │  │   │
│  │  │  ┌───────────────┐  │ NFS │  ┌──────────────────┐  │  │   │
│  │  │  │  Container    │  │◄───►│  │  Mount Targets   │  │  │   │
│  │  │  │  /mnt/s3/     │  │v4.1 │  │  (per subnet)    │  │  │   │
│  │  │  └───────────────┘  │     │  └──────────────────┘  │  │   │
│  │  └─────────────────────┘     │           │            │  │   │
│  │                              │           ▼            │  │   │
│  │                              │  ┌──────────────────┐  │  │   │
│  │                              │  │  S3 Files IAM    │  │  │   │
│  │                              │  │  Role            │  │  │   │
│  │                              │  └───────┬──────────┘  │  │   │
│  │                              └──────────┼─────────────┘  │   │
│  └─────────────────────────────────────────┼────────────────┘   │
│                                            ▼                    │
│                               ┌─────────────────────┐           │
│                               │  S3 Bucket          │           │
│                               │  aiccbuilder-assets  │           │
│                               │                     │           │
│                               │  /sessions/{id}/    │           │
│                               │    state/           │           │
│                               │    context/         │           │
│                               │    uploads/         │           │
│                               │    assets/          │           │
│                               └─────────────────────┘           │
└─────────────────────────────────────────────────────────────────┘
```

---

## 구성 요소별 코드 분석

### 1. IAM Role 설정 (CDK)

**파일:** `infrastructure/lib/ecs-stack.ts:214-294`

S3 Files 서비스가 S3 버킷에 접근하기 위한 IAM Role을 생성합니다. `elasticfilesystem.amazonaws.com` 서비스 프린시펄이 이 역할을 assume합니다.

```typescript
// S3 Files 서비스가 assume하는 역할
const s3FilesRole = new iam.Role(this, "S3FilesRole", {
  assumedBy: new iam.ServicePrincipal("elasticfilesystem.amazonaws.com", {
    conditions: {
      StringEquals: { "aws:SourceAccount": this.account },
      ArnLike: { "aws:SourceArn": `arn:aws:s3files:${this.region}:${this.account}:file-system/*` },
    },
  }),
});

// S3 버킷/객체 접근 권한
s3FilesRole.addToPolicy(new iam.PolicyStatement({
  actions: ["s3:ListBucket", "s3:ListBucketVersions"],
  resources: ["arn:aws:s3:::aiccbuilder*"],
}));

s3FilesRole.addToPolicy(new iam.PolicyStatement({
  actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", /* ... */],
  resources: ["arn:aws:s3:::aiccbuilder*/*"],
}));

// EventBridge 권한 (S3 Files 동기화에 필요)
s3FilesRole.addToPolicy(new iam.PolicyStatement({
  actions: ["events:PutRule", "events:PutTargets", /* ... */],
  resources: ["arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*"],
}));
```

**ECS Task Role에는 S3 Files 클라이언트 권한이 별도로 부여됩니다:**

```typescript
// infrastructure/lib/ecs-stack.ts:123-126
taskRole.addManagedPolicy(
  iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonS3FilesClientFullAccess")
);
```

> 두 가지 IAM 역할이 필요한 이유:
> - **S3FilesRole**: S3 Files *서비스*가 S3 버킷을 읽고 쓸 수 있도록
> - **TaskRole + S3FilesClientFullAccess**: ECS *태스크*가 NFS 마운트를 수행할 수 있도록

### 2. ECS Task Definition (CDK)

**파일:** `infrastructure/lib/ecs-stack.ts:299-341`

```typescript
const taskDefinition = new ecs.FargateTaskDefinition(this, "TaskDef", {
  memoryLimitMiB: 4096,
  cpu: 2048,
  runtimePlatform: {
    cpuArchitecture: ecs.CpuArchitecture.ARM64,
    operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
  },
});

const appContainer = taskDefinition.addContainer("app", {
  image: ecs.ContainerImage.fromEcrRepository(ecrRepo, "latest"),
  environment: {
    S3FILES_MOUNT_PATH: "/mnt/s3",           // NFS 마운트 경로
    SESSION_STORE_BACKEND: "s3files",         // 스토어 백엔드 선택
  },
});
```

> **주의:** CDK L2 construct는 아직 `s3filesVolumeConfiguration`을 지원하지 않습니다.
> 따라서 볼륨/마운트포인트 설정은 deploy.sh에서 AWS CLI로 패치합니다.

### 3. 배포 스크립트 — deploy.sh 전체 파이프라인

**파일:** `deploy.sh`

S3 Files 리소스는 CDK가 아닌 **deploy.sh에서 AWS CLI로 직접 생성**합니다.
CDK L2 construct가 `s3filesVolumeConfiguration`을 아직 지원하지 않기 때문입니다.

#### 전체 배포 순서 (S3 Files 관련만)

```
deploy.sh 실행
│
├─ [사전 검증] AWS CLI 버전 >= 2.34.27 확인     ← deploy.sh:192-207
│
├─ Step 0.5: ECR 리포지토리 생성 + Docker 이미지 빌드/푸시  ← deploy.sh:366-449
│   ├─ ECR 리포지토리 생성 (없으면)
│   ├─ Docker build --platform linux/arm64
│   │   └─ ENV S3FILES_MOUNT_PATH=/mnt/s3 가 이미지에 포함됨
│   └─ docker push → ECR
│
├─ Step 1: CDK 배포                              ← deploy.sh:454-491
│   ├─ S3FilesRole (IAM) 생성
│   ├─ TaskRole + AmazonS3FilesClientFullAccess 부여
│   ├─ ECS Cluster, Service, Task Definition 생성
│   ├─ VPC + Private Subnets 생성
│   └─ CDK Outputs → cdk-outputs.json 으로 내보내기
│       ├─ S3FilesRoleArn
│       ├─ PrivateSubnetIds
│       ├─ EcsSecurityGroupId
│       └─ TaskDefinitionArn
│
├─ Step 3: ECS 백엔드 구성 (Post-CDK)            ← deploy.sh:534-746
│   │
│   ├─ 3.1a) S3 Files FileSystem 생성            ← deploy.sh:571-597
│   ├─ 3.1b) Mount Target 생성 (서브넷별)         ← deploy.sh:599-651
│   ├─ 3.1c) Task Definition 패치                ← deploy.sh:653-726
│   └─ ECS Service force-new-deployment           ← deploy.sh:728-737
│
└─ 완료
```

#### 사전 검증: AWS CLI 버전 체크

**`deploy.sh:192-207`** — `aws s3files` 명령은 AWS CLI 2.34.27에서 추가되었습니다.

```bash
# ECS 모드일 때만 CLI 버전 검증
if [ "$DEPLOY_MODE" = "ecs" ]; then
    CLI_VERSION=$(aws --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    # ... major.minor.patch 파싱 후 비교 ...
    if [ 버전 < 2.34.27 ]; then
        echo "ERROR: ECS mode requires AWS CLI >= 2.34.27"
        echo "The 'aws s3files' commands were added in CLI 2.34.27."
        exit 1
    fi
fi
```

#### Step 0.5: Docker 이미지 빌드 (CDK 배포 전)

**`deploy.sh:366-449`** — CDK보다 먼저 ECR에 이미지를 올려야 ECS 서비스가 생성 시 pull할 수 있습니다.

```bash
# ECR 리포지토리 생성 (멱등)
aws ecr create-repository --repository-name "$ECR_REPO_NAME" \
    --image-scanning-configuration scanOnPush=false

# 공유 소스 복사: backend/src → backend/ecs/src
cp -r "$SCRIPT_DIR/backend/src" "$SCRIPT_DIR/backend/ecs/src"

# ARM64 이미지 빌드 (S3FILES_MOUNT_PATH=/mnt/s3 가 ENV으로 bake됨)
docker build --platform linux/arm64 \
    -t aicc-builder-ecs:latest \
    --build-arg ASSETS_BUCKET_NAME="${ASSETS_BUCKET_NAME:-}" \
    --build-arg USER_POOL_ID="${USER_POOL_ID:-}" \
    .

# ECR 로그인 후 푸시
aws ecr get-login-password | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker tag aicc-builder-ecs:latest "${ECR_REPO_URI}:latest"
docker push "${ECR_REPO_URI}:latest"
```

#### Step 1: CDK 배포 — IAM Role + ECS 인프라

**`deploy.sh:454-491`** — CDK가 VPC, ECS 클러스터, Task Definition, S3FilesRole을 생성합니다.
CDK Outputs로 후속 단계에 필요한 ARN/ID들을 내보냅니다.

```bash
cd "$SCRIPT_DIR/infrastructure"
npx cdk deploy --all --require-approval never \
    --outputs-file "$CDK_OUTPUTS_FILE"

# CDK Outputs에서 S3 Files 구성에 필요한 값 추출
S3FILES_ROLE_ARN=$(jq -r '.AiccBuilderEcs.S3FilesRoleArn' "$CDK_OUTPUTS_FILE")
PRIVATE_SUBNET_IDS=$(jq -r '.AiccBuilderEcs.PrivateSubnetIds' "$CDK_OUTPUTS_FILE")
ECS_SG_ID=$(jq -r '.AiccBuilderEcs.EcsSecurityGroupId' "$CDK_OUTPUTS_FILE")
TASK_DEF_ARN=$(jq -r '.AiccBuilderEcs.TaskDefinitionArn' "$CDK_OUTPUTS_FILE")
```

#### Step 3.1a: S3 Files FileSystem 생성

**`deploy.sh:571-597`** — S3 버킷을 NFS로 노출하는 파일시스템을 생성합니다.

```bash
ASSETS_BUCKET_ARN="arn:aws:s3:::${ASSETS_BUCKET_NAME}"

# 기존 파일시스템 확인 (멱등성)
EXISTING_FS=$(aws s3files list-file-systems --region "$AWS_DEFAULT_REGION" \
    | jq -r --arg b "$ASSETS_BUCKET_ARN" \
      '.fileSystems[] | select(.bucket == $b) | .fileSystemId' | head -1)

if [ -n "$EXISTING_FS" ]; then
    S3FILES_FS_ID="$EXISTING_FS"
    echo "S3 Files filesystem exists: $S3FILES_FS_ID"
else
    # 새 파일시스템 생성
    FS_OUTPUT=$(aws s3files create-file-system \
        --bucket "$ASSETS_BUCKET_ARN" \        # 마운트할 S3 버킷
        --role-arn "$S3FILES_ROLE_ARN" \        # S3 Files 서비스가 사용할 IAM Role
        --region "$AWS_DEFAULT_REGION")
    S3FILES_FS_ID=$(echo "$FS_OUTPUT" | jq -r '.fileSystemId')
fi
```

> 생성되는 리소스: `arn:aws:s3files:{region}:{account}:file-system/{fs-id}`
> 이 파일시스템은 지정된 S3 버킷과 1:1로 매핑됩니다.

#### Step 3.1b: Mount Target 생성 (서브넷별)

**`deploy.sh:599-651`** — 각 프라이빗 서브넷에 ENI(네트워크 인터페이스)를 배치합니다.
ECS 태스크가 NFS 트래픽을 이 ENI를 통해 S3 Files 서비스로 보냅니다.

```bash
IFS=',' read -ra SUBNETS <<< "$PRIVATE_SUBNET_IDS"
for SUBNET_ID in "${SUBNETS[@]}"; do
    # 서브넷별 기존 마운트 타겟 확인 (멱등성)
    EXISTING_MT=$(aws s3files list-mount-targets \
        --file-system-id "$S3FILES_FS_ID" \
        --region "$AWS_DEFAULT_REGION" \
        | jq -r --arg sid "$SUBNET_ID" \
          '.mountTargets[] | select(.subnetId == $sid) | .mountTargetId' | head -1)

    if [ -z "$EXISTING_MT" ]; then
        aws s3files create-mount-target \
            --file-system-id "$S3FILES_FS_ID" \
            --subnet-id "$SUBNET_ID" \           # 배치할 서브넷
            --security-groups "$ECS_SG_ID" \     # ECS와 동일한 보안그룹
            --region "$AWS_DEFAULT_REGION"
    fi
done

# Mount Target이 available이 될 때까지 폴링 (최대 ~5분)
for i in $(seq 1 30); do
    ALL_AVAILABLE=true
    MT_STATUS=$(aws s3files list-mount-targets \
        --file-system-id "$S3FILES_FS_ID" \
        | jq -r '.mountTargets[].status')
    for STATUS in $MT_STATUS; do
        if [ "$STATUS" != "available" ]; then ALL_AVAILABLE=false; break; fi
    done
    if [ "$ALL_AVAILABLE" = true ]; then echo "All mount targets available"; break; fi
    sleep 10
done
```

> **왜 서브넷마다 Mount Target이 필요한가?**
> ECS Fargate 태스크는 VPC 내 프라이빗 서브넷에서 실행됩니다.
> NFS 트래픽은 같은 서브넷(또는 같은 AZ) 내의 Mount Target ENI로 라우팅되므로,
> 태스크가 배치될 수 있는 모든 서브넷에 Mount Target을 만들어야 합니다.

#### Step 3.1c: Task Definition 패치 — S3 Files Volume 주입

**`deploy.sh:653-726`** — CDK가 생성한 Task Definition에 S3 Files 볼륨과 마운트포인트를 주입합니다.
CDK L2가 이 필드를 지원하지 않으므로 jq로 JSON을 직접 조작합니다.

```bash
TASK_DEF_FAMILY=$(echo "$TASK_DEF_ARN" | sed 's|.*/||' | sed 's|:[0-9]*$||')
S3FILES_FS_ARN="arn:aws:s3files:${AWS_DEFAULT_REGION}:${ACCOUNT_ID}:file-system/${S3FILES_FS_ID}"

# 이미 볼륨이 설정되어 있는지 확인
EXISTING_VOLUMES=$(aws ecs describe-task-definition --task-definition "$TASK_DEF_FAMILY" \
    --query 'taskDefinition.volumes[?name==`s3files`].name' --output text)

# jq 필터로 Task Definition JSON 패치
JQ_FILTER='.taskDefinition
    | del(.taskDefinitionArn, .revision, .status, .registeredAt, .registeredBy,
          .compatibilities, .requiresAttributes)'

if [ "$EXISTING_VOLUMES" != "s3files" ]; then
    JQ_FILTER="${JQ_FILTER}
        # ── S3 Files 볼륨 추가 ──
        | .volumes += [{
            \"name\": \"s3files\",
            \"configuredAtLaunch\": false,
            \"s3filesVolumeConfiguration\": {
                \"fileSystemArn\": \$fs_arn,
                \"rootDirectory\": \"/\"
            }
          }]
        # ── 컨테이너 마운트포인트 추가 ──
        | .containerDefinitions[0].mountPoints += [{
            \"sourceVolume\": \"s3files\",
            \"containerPath\": \"/mnt/s3\",
            \"readOnly\": false
          }]"
fi

# 런타임 환경변수도 함께 주입 (CDK 배포 후 확정되는 값들)
JQ_FILTER="${JQ_FILTER}
    | .containerDefinitions[0].environment = (
        [기존 env에서 덮어쓸 키 제외]
        + [{\"name\":\"ASSETS_BUCKET_NAME\", \"value\":\$bucket},
           {\"name\":\"USER_POOL_ID\",       \"value\":\$pool},
           {\"name\":\"USER_POOL_CLIENT_ID\", \"value\":\$poolclient},
           {\"name\":\"CONTACT_FLOW_KB_ID\",  \"value\":\$kbid},
           {\"name\":\"BRAVE_API_KEY\",       \"value\":\$brave}]
      )"

# 현재 Task Def 읽기 → 패치 → 새 리비전 등록
aws ecs describe-task-definition --task-definition "$TASK_DEF_FAMILY" \
    | jq --arg fs_arn "$S3FILES_FS_ARN" \
         --arg bucket "$ASSETS_BUCKET_NAME" \
         --arg pool "$USER_POOL_ID" \
         --arg poolclient "$USER_POOL_CLIENT_ID" \
         --arg kbid "$CONTACT_FLOW_KB_ID" \
         --arg brave "$BRAVE_API_KEY" \
         "$JQ_FILTER" > /tmp/patched-task-def.json

# 패치된 Task Definition을 새 리비전으로 등록
aws ecs register-task-definition --cli-input-json file:///tmp/patched-task-def.json
```

> **패치 결과 Task Definition JSON 구조:**
> ```json
> {
>   "family": "AiccBuilderEcs-TaskDef",
>   "volumes": [{
>     "name": "s3files",
>     "configuredAtLaunch": false,
>     "s3filesVolumeConfiguration": {
>       "fileSystemArn": "arn:aws:s3files:ap-northeast-2:123456789:file-system/fs-abc123",
>       "rootDirectory": "/"
>     }
>   }],
>   "containerDefinitions": [{
>     "name": "app",
>     "mountPoints": [{
>       "sourceVolume": "s3files",
>       "containerPath": "/mnt/s3",
>       "readOnly": false
>     }],
>     "environment": [
>       {"name": "S3FILES_MOUNT_PATH", "value": "/mnt/s3"},
>       {"name": "SESSION_STORE_BACKEND", "value": "s3files"},
>       {"name": "ASSETS_BUCKET_NAME", "value": "aiccbuilder-assets-xxx"},
>       ...
>     ]
>   }]
> }
> ```

#### 최종: ECS Service 강제 재배포

**`deploy.sh:728-737`** — 새 Task Definition 리비전을 적용하기 위해 서비스를 강제 재배포합니다.

```bash
aws ecs update-service \
    --cluster "$ECS_CLUSTER_NAME" \
    --service "$ECS_SERVICE_NAME" \
    --force-new-deployment
```

> 이 시점에서 새 태스크가 시작되면 ECS가 자동으로:
> 1. S3 Files Mount Target에 NFS 연결
> 2. 컨테이너의 `/mnt/s3`에 마운트
> 3. 컨테이너 프로세스 시작 (uvicorn)

#### 생성되는 AWS 리소스 요약

```
deploy.sh가 생성하는 리소스 (CDK 외부):
┌─────────────────────────────────────────────────────────┐
│ 리소스                    │ AWS API               │ 수량 │
├─────────────────────────────────────────────────────────┤
│ S3 Files FileSystem       │ aws s3files            │ 1개  │
│  └─ S3 버킷과 1:1 매핑                                   │
│                                                         │
│ S3 Files Mount Target     │ aws s3files            │ N개  │
│  └─ 프라이빗 서브넷 수만큼 (보통 2~3개)                     │
│                                                         │
│ ECS Task Definition Rev.  │ aws ecs                │ 1개  │
│  └─ 기존 CDK Task Def에 볼륨/마운트/환경변수 패치            │
└─────────────────────────────────────────────────────────┘

CDK가 생성하는 리소스 (infrastructure/lib/ecs-stack.ts):
┌─────────────────────────────────────────────────────────┐
│ S3FilesRole (IAM Role)    │ elasticfilesystem 용    │ 1개 │
│ TaskRole (IAM Role)       │ S3FilesClientFullAccess │ 1개 │
│ ECS Cluster               │                        │ 1개 │
│ ECS Service               │                        │ 1개 │
│ ECS Task Definition       │ 초기 버전 (볼륨 없음)     │ 1개 │
│ VPC + Private Subnets     │                        │ 1개 │
│ ALB + Target Group        │                        │ 1개 │
│ Security Group            │ NFS 포트 2049 허용       │ 1개 │
└─────────────────────────────────────────────────────────┘
```

### 4. Dockerfile

**파일:** `backend/ecs/Dockerfile`

```dockerfile
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm

# 마운트 경로 및 스토어 백엔드 설정
ENV S3FILES_MOUNT_PATH=/mnt/s3
ENV SESSION_STORE_BACKEND=s3files

# 컨테이너 시작 시 /mnt/s3는 S3 Files NFS로 자동 마운트됨
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

> 컨테이너 내부에서 별도의 mount 명령이 필요 없습니다. ECS가 Task Definition의 `s3filesVolumeConfiguration`에 따라 컨테이너 시작 전에 NFS 마운트를 수행합니다.

---

## 애플리케이션 레벨 스토리지 구조

### 5. 3-Tier 세션 스토어

**파일:** `backend/ecs/src/context/s3files_store.py`

```
Tier 1: In-Memory Cache (Python dict)    — 가장 빠름, 컨테이너 재시작 시 소멸
Tier 2: NFS (/mnt/s3/sessions/{id}/)     — 영속적, sub-100ms 접근
Tier 3: DynamoDB (프론트엔드 관리)          — 메타데이터만
```

#### NFS 디렉터리 레이아웃

```
/mnt/s3/
└── sessions/
    └── {session_id}/
        ├── state/                          # 프로젝트 상태
        │   ├── project.json                # 세션 컨텍스트 전체
        │   ├── progress.json               # 진행률
        │   ├── specs/{op_id}.json          # 오퍼레이션별 스펙
        │   ├── requirements/{doc}.txt      # 요구사항 문서
        │   └── schemas/infrastructure.json # 인프라 스키마
        ├── context/                        # 대화 컨텍스트
        │   ├── conversation_history.json   # 대화 기록 (최대 60개)
        │   ├── shared_state.json           # 공유 상태
        │   └── all_results.txt             # 전체 결과 로그
        ├── uploads/                        # 사용자 업로드 파일
        └── assets/                         # 에이전트 생성 파일
            └── v1/
                ├── lambda/
                ├── connect-flows/
                └── ...
```

#### 핵심 코드: 읽기/쓰기 패턴

```python
class S3FilesContextStore:
    def __init__(self, mount_path: str = "/mnt/s3"):
        self._mount_path = mount_path
        self._memory_cache: Dict[str, SessionContext] = {}   # Tier 1

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        # Tier 1: 메모리 캐시
        if session_id in self._memory_cache:
            return self._memory_cache[session_id]
        # Tier 2: NFS 읽기
        path = self._state_path(session_id) / "project.json"
        data = self._read_json(path)
        if data:
            ctx = SessionContext.from_dict(data)
            self._memory_cache[session_id] = ctx  # 캐시에 올림
            return ctx
        return None

    def save_session(self, context: SessionContext) -> None:
        self._memory_cache[context.session_id] = context         # Tier 1
        path = self._state_path(context.session_id) / "project.json"
        self._write_json(path, context.to_dict())                # Tier 2

    def _write_json(self, path: Path, data: Any) -> bool:
        """Atomic write: tmp 파일 작성 후 rename"""
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        tmp.rename(path)  # 원자적 교체
```

### 6. NFS Workspace File Tools (에이전트용)

**파일:** `backend/ecs/src/tools/workspace_file_tools.py`

AI 에이전트(Strands Agent)가 세션 워크스페이스 내 파일을 직접 조작할 수 있는 도구 세트입니다.
모든 도구는 `@tool` 데코레이터(Strands SDK)로 래핑되어 있으며, LLM이 function-calling으로 호출합니다.

| 도구 | 입력 | 설명 | 반환 요약 |
|------|------|------|-----------|
| `read_workspace_file` | session_id, path | 파일 읽기 (UTF-8) | `{content, size, summary}` |
| `write_workspace_file` | session_id, path, content | 원자적 파일 쓰기 (tmp + rename) | `{path, size, summary}` |
| `append_workspace_file` | session_id, path, content | 파일에 내용 추가 | `{path, new_size, summary}` |
| `list_workspace_dir` | session_id, path | 디렉터리 목록 조회 | `{entries, count, summary}` |
| `patch_workspace_file` | session_id, path, search, replace | 텍스트 찾기/바꾸기 (전체 치환) | `{replacements_made, changed_lines, summary}` |
| `find_workspace_files` | session_id, pattern, path | glob 패턴으로 파일 검색 (최대 200개) | `{matches, count, summary}` |
| `grep_workspace` | session_id, pattern, path, file_pattern | 정규식으로 파일 내용 검색 (최대 100개) | `{results, count, summary}` |

#### 왜 직접 파일 읽기/쓰기가 아닌 도구(tool)를 사용하는가?

LLM 에이전트가 NFS 파일시스템에 직접 접근하는 것이 아니라, 별도의 tool 레이어를 두는 이유:

**1. 보안 격리 (Session Sandboxing)**

에이전트가 받는 `path` 파라미터는 항상 **상대 경로**입니다. 도구 내부에서 세션 루트(`/mnt/s3/sessions/{session_id}/`)를 기준으로 resolve한 뒤, 해당 범위를 벗어나지 않는지 검증합니다. LLM이 `../../etc/passwd` 같은 경로를 생성해도 차단됩니다.

**2. 원자적 쓰기 (Atomic Writes)**

NFS 위에서 파일 손상을 방지하기 위해, 모든 쓰기 작업이 `tempfile.mkstemp()` → write → `os.rename()` 패턴을 따릅니다. NFS v4.1의 rename은 같은 디렉터리 내에서 원자적이므로, 에이전트가 중단되어도 반쪽짜리 파일이 남지 않습니다.

**3. 실시간 스트리밍 (WebSocket 이벤트 연동)**

`write_workspace_file`과 `patch_workspace_file`은 파일 저장 후 WebSocket을 통해 프론트엔드에 두 가지 이벤트를 전송합니다:
- **`workspace_update`**: 파일 트리(File Explorer) UI를 갱신
- **`asset_preview`**: 파일 내용을 인라인 미리보기로 표시

이것은 Sub-Agent가 Lambda 코드나 CloudFormation 템플릿을 생성할 때, 사용자가 실시간으로 파일이 생기는 것을 볼 수 있게 합니다.

**4. LLM에 구조화된 결과 반환**

모든 도구가 `{"success": bool, "summary": str, ...}` 형태의 dict를 반환합니다. `summary` 필드는 프론트엔드의 tool call UI에 한 줄 요약으로 표시되며, LLM도 이 필드를 보고 다음 행동을 결정합니다.

**5. project_workspace와의 역할 분리**

| | `workspace_file_tools` | `project_workspace` |
|---|---|---|
| 저장 위치 | NFS 직접 (`/mnt/s3/sessions/{id}/`) | NFS + S3 API 이중 쓰기 |
| 대상 | 에이전트가 생성한 코드/에셋 파일 | 구조화된 프로젝트 상태 (specs, schemas) |
| 경로 스타일 | 자유 상대경로 (e.g., `assets/v1/lambda/index.py`) | 고정 레이아웃 (e.g., `state/specs/{op_id}.json`) |
| 캐싱 | 없음 (매번 NFS 직접 접근) | 인메모리 캐시 (`_specs_cache`, `_schema_cache`) |
| Fallback | NFS 불가 시 에러 반환 | NFS 실패 시 S3 API fallback |
| 용도 | 파일 CRUD, 검색, 패치 | specs/requirements/progress 관리 |

#### 도구 등록 및 호출 흐름

```
backend/ecs/src/tools/
├── __init__.py                    ← workspace_file_tools를 re-export
├── workspace_file_tools.py        ← @tool 데코레이터로 7개 도구 정의
└── project_workspace.py           ← @tool로 save/load_requirement_document 정의

backend/ecs/app.py
├── from tools import read_workspace_file, ...    ← Line 66-72
├── from tools.project_workspace import save_requirement_document, ...  ← Line 74-77
│
└── tools = [                                     ← Line 424-461
        ...,
        save_requirement_document,
        load_requirement_document,
        read_workspace_file,
        write_workspace_file,
        append_workspace_file,
        list_workspace_dir,
        patch_workspace_file,
        find_workspace_files,
        grep_workspace,
        ...,
    ]
    agent = Agent(model=model, system_prompt=system_prompt, tools=tools)
```

LLM이 tool call을 생성하면 Strands SDK가 해당 Python 함수를 직접 실행합니다.
`session_id`는 각 도구 호출 시 자동으로 주입되며, 에이전트의 system prompt에 사용 가이드가 포함되어 있습니다.

#### 도구별 상세 코드 분석

##### `write_workspace_file` — 원자적 쓰기 + 이벤트 발행

```python
@tool
def write_workspace_file(session_id: str, path: str, content: str) -> dict:
    target = _resolve_safe_path(session_id, path)          # ① 경로 검증
    os.makedirs(target.parent, exist_ok=True)              # ② 디렉터리 생성

    # ③ Atomic write: 임시 파일에 쓴 뒤 rename
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.rename(tmp_path, target)                        # ④ 원자적 교체
    except Exception:
        os.unlink(tmp_path)                                # ⑤ 실패 시 임시 파일 정리
        raise

    _emit_workspace_event("write", session_id, path, len(content))  # ⑥ 파일트리 갱신
    if _should_show_preview(path, len(content)):
        _emit_file_preview("write", session_id, path, content)      # ⑦ 인라인 프리뷰
    return {"success": True, "path": path, "size": len(content),
            "summary": f"Wrote {len(content)} bytes to {path}"}
```

`_emit_workspace_event`와 `_emit_file_preview`는 streaming_callback 모듈의 글로벌 콜백을 통해
WebSocket → 프론트엔드로 이벤트를 전달합니다:

```python
# _emit_workspace_event 내부
callback(
    asset_type="workspace_update",
    content=json.dumps({"action": "write", "path": path, "size": size}),
    ...
)

# _emit_file_preview 내부 — 50KB 이하의 텍스트 파일만
callback(
    asset_type="workspace_file",
    content=preview_content,       # 파일 전체 내용 (50KB cap)
    file_name=path,                # 확장자로 syntax highlighting 결정
    operation_id="write",
    ...
)
```

##### `patch_workspace_file` — 찾기/바꾸기 + diff 정보

```python
@tool
def patch_workspace_file(session_id: str, path: str, search: str, replace: str) -> dict:
    target = _resolve_safe_path(session_id, path)
    original = target.read_text(encoding="utf-8")

    if search not in original:
        return {"success": False, "error": "Search text not found in file"}

    count = original.count(search)
    modified = original.replace(search, replace)     # 모든 occurrence 치환

    # Atomic write (write_workspace_file과 동일 패턴)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(modified)
    os.rename(tmp_path, target)

    # 변경된 라인 번호 수집 (프론트엔드 diff view용, 최대 10개)
    changed_lines = []
    for i, line in enumerate(modified.splitlines(), 1):
        if replace in line:
            changed_lines.append(i)
            if len(changed_lines) >= 10:
                break

    return {
        "success": True,
        "replacements_made": count,
        "search": search[:200],          # echo-back (LLM이 뭘 바꿨는지 확인)
        "replace": replace[:200],
        "summary": f"Replaced '{search[:50]}' → '{replace[:50]}' ({count} occurrence(s))",
        "changed_lines": changed_lines,  # 프론트엔드 diff 표시용
    }
```

`patch_workspace_file`은 **literal string match**를 사용합니다 (regex 아님).
이유: LLM이 생성하는 search 텍스트에 regex 특수 문자가 포함되면 예기치 않은 매칭이 발생할 수 있고,
requirements 문서의 em-dash(`—`) 같은 Unicode 문자가 regex 엔진에서 문제를 일으킬 수 있습니다.

##### `grep_workspace` — 재귀 검색 + regex

```python
@tool
def grep_workspace(session_id: str, pattern: str, path: str = "", file_pattern: str = "*") -> dict:
    search_root = _resolve_safe_path(session_id, path) if path else _get_session_root(session_id)

    try:
        compiled = re.compile(pattern)             # regex 시도
    except re.error:
        compiled = re.compile(re.escape(pattern))  # 실패 시 literal fallback

    results = []
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]  # 숨김 디렉터리 스킵
        for filename in files:
            if not fnmatch.fnmatch(filename, file_pattern):  # 파일 패턴 필터
                continue
            if file_size > 1MB or file_size == 0:            # 대용량/빈 파일 스킵
                continue
            # UTF-8 디코딩 가능한 파일만 검색 (바이너리 자동 스킵)
            for line_num, line in enumerate(content.splitlines(), 1):
                if compiled.search(line):
                    results.append({"path": rel_path, "line_number": line_num, "line": line[:500]})
                    if len(results) >= 100:
                        break
    return {"success": True, "results": results, "count": len(results),
            "summary": f"Found {len(results)} match(es) for '{pattern[:50]}'"}
```

`grep_workspace`는 `patch_workspace_file`과 달리 **regex를 지원**합니다.
검색은 읽기 전용이므로 regex의 유연성이 이점이 됩니다.
`file_pattern` 파라미터로 `"*.py"`처럼 특정 확장자만 검색할 수 있어,
에이전트가 "Lambda 코드에서 phoneNumber를 사용하는 곳"을 효율적으로 찾을 수 있습니다.

##### `find_workspace_files` — glob 기반 파일 탐색

```python
@tool
def find_workspace_files(session_id: str, pattern: str, path: str = "") -> dict:
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for filename in files:
            if fnmatch.fnmatch(filename, pattern):       # glob 패턴 매칭
                matches.append({"path": rel_path, "size": size})
                if len(matches) >= 200:                  # 최대 200개 제한
                    break
    return {"success": True, "matches": sorted_matches,
            "summary": f"Found {len(matches)} file(s) matching '{pattern}'"}
```

에이전트가 "어떤 파일이 있는지"를 파악할 때 사용합니다.
`list_workspace_dir`은 단일 레벨만 보여주지만, `find_workspace_files`는 재귀적으로 탐색합니다.

#### 보안 보호 장치

```python
def _resolve_safe_path(session_id: str, relative_path: str) -> Path:
    # 1. 절대 경로 차단
    if relative_path.startswith('/'):
        raise ValueError("Absolute paths not allowed")
    # 2. 경로 순회 차단
    if '..' in relative_path:
        raise ValueError("Path traversal not allowed")
    # 3. symlink 탈출 방지 (resolve 후 bounds 확인)
    session_root = _get_session_root(session_id)
    target = (session_root / relative_path).resolve()
    target.relative_to(session_root.resolve())  # 벗어나면 ValueError
    return target
```

세 단계 방어:

1. **절대 경로 차단**: `/etc/passwd` 등 직접 경로 접근 방지
2. **경로 순회 차단**: `../../` 패턴으로 상위 디렉터리 탈출 방지
3. **symlink 탈출 방지**: `resolve()`로 심볼릭 링크를 따라간 최종 경로가 세션 루트 내에 있는지 검증

`session_id` 자체도 `_get_session_root()`에서 `/`, `\`, `..`을 제거하여 sanitize합니다.

#### 프리뷰 설정

```python
_MIN_PREVIEW_SIZE = 50       # 50B 미만은 프리뷰 생략
_MAX_PREVIEW_CONTENT = 50000 # 50KB 이상은 잘라서 전송
_SKIP_EXTENSIONS = {'.tmp', '.log', '.lock', '.bak', '.pyc'}  # 프리뷰 제외 확장자
_TEXT_EXTENSIONS = {'.py', '.ts', '.js', '.json', '.yaml', ...} # 프리뷰 대상 확장자
```

`write`/`patch`/`append` 시 `_should_show_preview()`가 이 설정을 참고하여,
코드 파일은 프론트엔드에 인라인 미리보기를 보내고, 바이너리나 임시 파일은 건너뜁니다.

#### 에이전트 사용 패턴 (system prompt에 가이드됨)

```
1. Find → Read:     find_workspace_files("*.py") → read_workspace_file("assets/v1/lambda/index.py")
2. Search → Patch:  grep_workspace("old_field") → patch_workspace_file(search="old_field", replace="new_field")
3. Inspect:         find_workspace_files("*.py", "assets") → 생성된 에셋 전체 목록 확인
4. Cross-check:     grep_workspace("field_name") → 모든 에셋에서 해당 필드 사용 현황 확인 → 일괄 patch
```

이 패턴들은 `system_prompt.py`의 "Workspace File Tools Usage Patterns" 섹션에 정의되어 있어,
에이전트가 자율적으로 파일을 탐색하고 수정할 수 있게 합니다.

---

## 데이터 흐름 시퀀스

```
사용자 메시지 → WebSocket → FastAPI (app.py)
                              │
                              ├─ 세션 복원: memory → NFS → (S3 fallback)
                              │
                              ├─ 에이전트 실행 (Strands Agent)
                              │   ├─ write_workspace_file("assets/v1/lambda/index.py", code)
                              │   │   └─ /mnt/s3/sessions/{id}/assets/v1/lambda/index.py  ← NFS 쓰기
                              │   │   └─ → WebSocket workspace_update 이벤트 → 프론트엔드 파일트리 갱신
                              │   │
                              │   └─ save_session(context)
                              │       └─ /mnt/s3/sessions/{id}/state/project.json          ← NFS 쓰기
                              │
                              └─ Graceful Shutdown 시
                                  ├─ conversation_history.json → NFS 플러시
                                  └─ project.json → NFS 플러시
```

---

## FAQ

### Q: 단순 Docker bind mount와 같은 건가요?

**아닙니다.** Docker bind mount는 호스트 머신의 파일시스템 경로를 컨테이너에 매핑합니다. 하지만 **ECS Fargate에는 호스트 머신이라는 개념이 없습니다** (서버리스). 이 시스템은:

1. AWS S3 Files 서비스가 S3 버킷에 대한 **NFS v4.1 엔드포인트**를 생성
2. 각 VPC 서브넷에 **Mount Target** (ENI)이 배치됨
3. ECS Task Definition의 `s3filesVolumeConfiguration`이 컨테이너 시작 시 자동으로 NFS 마운트 수행
4. 컨테이너 내부에서는 일반 파일시스템처럼 `/mnt/s3/`에 접근

### Q: EFS(Elastic File System)와 다른 건가요?

**다릅니다.** EFS는 별도의 관리형 NFS 서비스이고, S3 Files는 **S3 버킷 자체를 NFS로 노출**합니다.

| 구분 | EFS | S3 Files |
|------|-----|----------|
| 스토리지 백엔드 | 전용 NFS 스토리지 | S3 버킷 |
| 비용 모델 | GB/월 + I/O 요금 | S3 요금만 |
| S3 API 호환 | 없음 | 있음 (동일 버킷) |
| 데이터 접근 | NFS만 | NFS + S3 API 동시 |
| IAM 서비스 프린시펄 | `elasticfilesystem.amazonaws.com` | 동일 (S3 Files가 EFS 인프라 위에 구축) |

### Q: NFS를 사용할 수 없을 때는 어떻게 되나요?

S3 Files가 해당 리전에서 사용 불가하거나 마운트 실패 시:
- deploy.sh가 경고 메시지 출력: `"S3 Files not available in this region — skipping volume config"`
- 애플리케이션은 S3 API 직접 호출로 **fallback** 동작
- `_check_nfs_available()` 함수가 매 파일 조작마다 마운트 상태 확인

### Q: AWS CLI 버전 요구사항은?

`aws s3files` 명령은 **AWS CLI 2.34.27+** 에서 지원됩니다. deploy.sh가 배포 전에 버전을 검증합니다.

---

## 관련 파일 목록

| 파일 | 역할 |
|------|------|
| `infrastructure/lib/ecs-stack.ts` | CDK — IAM Role, Task Definition, VPC, ALB |
| `deploy.sh` | S3 Files 파일시스템/마운트타겟 생성, Task Def 패치 |
| `backend/ecs/Dockerfile` | 컨테이너 이미지 — 환경변수 설정 |
| `backend/ecs/app.py` | FastAPI 앱 — 세션 관리, WebSocket, 도구 등록 (Line 424-461) |
| `backend/ecs/src/context/s3files_store.py` | 3-Tier 세션 스토어 (메모리 + NFS) |
| `backend/ecs/src/tools/__init__.py` | 도구 모듈 re-export (workspace_file_tools 포함) |
| `backend/ecs/src/tools/workspace_file_tools.py` | NFS 기반 워크스페이스 파일 도구 7종 |
| `backend/ecs/src/tools/project_workspace.py` | 구조화된 프로젝트 상태 관리 (specs, requirements, NFS+S3 이중 쓰기) |
| `backend/ecs/src/tools/streaming_callback.py` | 글로벌 스트리밍 콜백 — 도구 → WebSocket → 프론트엔드 이벤트 파이프 |
| `backend/ecs/src/prompts/system_prompt.py` | 에이전트 시스템 프롬프트 — workspace 도구 사용 가이드 포함 |
