#!/bin/bash
set -e

echo "=========================================="
echo "  AICC Builder Deployment Script"
echo "  (ECS Fargate + CloudFront)"
echo "=========================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Get the script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ENV_LOCAL="$SCRIPT_DIR/.env.local"

# ========================================
# Docker Runtime Setup
# ========================================
setup_docker() {
    # Check if Docker is already accessible
    if docker info &>/dev/null; then
        echo -e "${GREEN}Docker is running${NC}"
        return 0
    fi

    # Detect available runtimes
    local has_colima=false
    local has_desktop=false
    [ -x "$(command -v colima 2>/dev/null || echo /opt/homebrew/bin/colima)" ] && has_colima=true
    [ -S "/var/run/docker.sock" ] || [ -S "$HOME/.docker/run/docker.sock" ] && has_desktop=true

    echo -e "${YELLOW}Docker is not running. Select a Docker runtime:${NC}"
    echo "  1) Colima (lightweight, recommended)"
    echo "  2) Docker Desktop"
    echo "  3) Skip (will fail if Docker is needed)"
    read -p "Choice [1]: " DOCKER_CHOICE
    DOCKER_CHOICE="${DOCKER_CHOICE:-1}"

    case $DOCKER_CHOICE in
        1)
            local COLIMA_BIN=$(command -v colima 2>/dev/null || echo /opt/homebrew/bin/colima)
            if [ ! -x "$COLIMA_BIN" ]; then
                echo "Installing colima..."
                brew install colima docker 2>/dev/null || /opt/homebrew/bin/brew install colima docker
            fi
            echo -e "${BLUE}Starting Colima...${NC}"
            PATH="/opt/homebrew/bin:$PATH" colima start --cpu 2 --memory 4 2>&1 | grep -E "level=(info|fatal)" | tail -3
            export DOCKER_HOST="unix://$HOME/.colima/docker.sock"
            ;;
        2)
            echo -e "${YELLOW}Please start Docker Desktop manually, then press Enter.${NC}"
            read -p ""
            if ! docker info &>/dev/null; then
                echo -e "${RED}Docker Desktop is still not running. Aborting.${NC}"
                exit 1
            fi
            ;;
        3)
            echo -e "${YELLOW}Skipping Docker setup${NC}"
            ;;
    esac
}

# ========================================
# Local environment cache (.env.local)
# ========================================
load_env_local() {
    if [ -f "$ENV_LOCAL" ]; then
        set -a
        source "$ENV_LOCAL"
        set +a
    fi
}

save_env_local() {
    local key=$1
    local value=$2
    if [ -f "$ENV_LOCAL" ]; then
        # Update existing key or append
        if grep -q "^${key}=" "$ENV_LOCAL" 2>/dev/null; then
            sed -i '' "s|^${key}=.*|${key}=${value}|" "$ENV_LOCAL"
        else
            echo "${key}=${value}" >> "$ENV_LOCAL"
        fi
    else
        echo "${key}=${value}" > "$ENV_LOCAL"
    fi
}

# Load cached environment
load_env_local

# ========================================
# Parse command line arguments
# ========================================
DEPLOY_BACKEND=true
DEPLOY_INFRA=true
DEPLOY_FRONTEND=true
FORCE_BUILD=false
SKIP_CHECKS=false
STAGE=""

print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --backend-only     Deploy only ECS backend"
    echo "  --frontend-only    Deploy only frontend (build + S3 + CloudFront)"
    echo "  --infra-only       Deploy only CDK infrastructure"
    echo "  --force            Force rebuild even if no changes detected"
    echo "  --skip-checks      Skip hash checks (faster but may rebuild unnecessarily)"
    echo "  --stage <name>     Deploy to a named stage (e.g., dev, staging). Default: production (no suffix)"
    echo "  --help             Show this help message"
    echo ""
    echo "Environment variables:"
    echo "  ENABLE_KNOWLEDGE_BASE=false   Skip Knowledge Base deployment"
    echo "  AWS_DEFAULT_REGION=us-east-1  Set AWS region (default: ap-northeast-2 / Seoul)"
    echo "  BRAVE_API_KEY=xxx             Brave Search API key for Research Agent"
    echo ""
    echo "Examples:"
    echo "  $0                                    # Full deployment (Seoul)"
    echo "  $0 --backend-only                     # Only redeploy ECS backend"
    echo "  $0 --frontend-only                    # Only rebuild and deploy frontend"
    echo "  $0 --force                            # Force full rebuild"
    echo "  $0 --stage prod                       # Deploy to prod stage"
    echo "  ENABLE_KNOWLEDGE_BASE=false $0        # Deploy without Knowledge Base"
    echo "  AWS_DEFAULT_REGION=ap-northeast-1 $0 --stage prod  # Deploy to Tokyo prod stack"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --backend-only)
            DEPLOY_INFRA=false
            DEPLOY_FRONTEND=false
            shift
            ;;
        --frontend-only)
            DEPLOY_BACKEND=false
            DEPLOY_INFRA=false
            shift
            ;;
        --infra-only)
            DEPLOY_BACKEND=false
            DEPLOY_FRONTEND=false
            shift
            ;;
        --force)
            FORCE_BUILD=true
            shift
            ;;
        --skip-checks)
            SKIP_CHECKS=true
            shift
            ;;
        --stage)
            STAGE="$2"
            shift 2
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            print_usage
            exit 1
            ;;
    esac
done

echo -e "${CYAN}Deploy targets: backend=$DEPLOY_BACKEND, infra=$DEPLOY_INFRA, frontend=$DEPLOY_FRONTEND${NC}"
echo -e "${CYAN}Force build: $FORCE_BUILD, Skip checks: $SKIP_CHECKS${NC}"

# ========================================
# CLI version check (s3files requires AWS CLI >= 2.34.27)
# ========================================
CLI_VERSION=$(aws --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
CLI_MAJOR=$(echo "$CLI_VERSION" | cut -d. -f1)
CLI_MINOR=$(echo "$CLI_VERSION" | cut -d. -f2)
CLI_PATCH=$(echo "$CLI_VERSION" | cut -d. -f3)
if [ "$CLI_MAJOR" -lt 2 ] || { [ "$CLI_MAJOR" -eq 2 ] && [ "$CLI_MINOR" -lt 34 ]; } || \
   { [ "$CLI_MAJOR" -eq 2 ] && [ "$CLI_MINOR" -eq 34 ] && [ "$CLI_PATCH" -lt 27 ]; }; then
    echo -e "${RED}ERROR: AWS CLI >= 2.34.27 required (current: $CLI_VERSION)${NC}"
    echo -e "${YELLOW}The 'aws s3files' commands were added in CLI 2.34.27.${NC}"
    echo -e "${YELLOW}Update with: curl 'https://awscli.amazonaws.com/AWSCLIV2.pkg' -o AWSCLIV2.pkg && sudo installer -pkg AWSCLIV2.pkg -target /${NC}"
    exit 1
fi
echo -e "${GREEN}AWS CLI version: $CLI_VERSION (s3files supported)${NC}"

# Stage-derived variables
STAGE_SUFFIX=""
CDK_CONTEXT_ARGS=""
if [ -n "$STAGE" ]; then
    STAGE_SUFFIX="-${STAGE}"
    CDK_CONTEXT_ARGS="-c stage=${STAGE}"
    echo -e "${CYAN}Stage: ${STAGE} (stack suffix: ${STAGE_SUFFIX})${NC}"
fi

STACK_NAME="AiccBuilderStack${STAGE_SUFFIX}"
KB_STACK_NAME="AiccBuilderKnowledgeBase${STAGE_SUFFIX}"
CDK_OUTPUTS_FILE="$SCRIPT_DIR/cdk-outputs${STAGE_SUFFIX}.json"

# ========================================
# Hash check functions for incremental builds
# ========================================
HASH_DIR="$SCRIPT_DIR/.deploy-hashes"
mkdir -p "$HASH_DIR"

compute_hash() {
    local dir=$1
    local pattern=$2
    find "$dir" -name "$pattern" -type f -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d' ' -f1
}

check_hash_changed() {
    local name=$1
    local current_hash=$2
    local hash_file="$HASH_DIR/$name.hash"

    if [ "$FORCE_BUILD" = true ] || [ "$SKIP_CHECKS" = true ]; then
        return 0  # Changed (force rebuild)
    fi

    if [ -f "$hash_file" ]; then
        local stored_hash=$(cat "$hash_file")
        if [ "$stored_hash" = "$current_hash" ]; then
            return 1  # Not changed
        fi
    fi
    return 0  # Changed
}

save_hash() {
    local name=$1
    local hash=$2
    echo "$hash" > "$HASH_DIR/$name.hash"
}

# ========================================
# Environment setup
# ========================================
# ECS mode: default Seoul, but user can override with AWS_DEFAULT_REGION
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-northeast-2}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-$AWS_DEFAULT_REGION}"

# ─── Show the AWS identity we're about to deploy with ──────────────────────
# Honors AWS_PROFILE / AWS_DEFAULT_PROFILE if set, so running this script from
# a different directory with a different profile doesn't silently deploy to
# the wrong account.
CALLER_JSON=$(aws sts get-caller-identity --output json 2>&1) || {
    echo -e "${RED}ERROR: 'aws sts get-caller-identity' failed:${NC}"
    echo "$CALLER_JSON"
    echo -e "${YELLOW}Check AWS_PROFILE (current: ${AWS_PROFILE:-<unset>}), AWS_DEFAULT_PROFILE, or ~/.aws/credentials.${NC}"
    exit 1
}
CALLER_ACCOUNT=$(echo "$CALLER_JSON" | jq -r '.Account')
CALLER_ARN=$(echo "$CALLER_JSON" | jq -r '.Arn')
echo -e "${CYAN}AWS Identity:${NC}"
echo -e "  Profile: ${BLUE}${AWS_PROFILE:-<default>}${NC}"
echo -e "  Account: ${BLUE}${CALLER_ACCOUNT}${NC}"
echo -e "  Region:  ${BLUE}${AWS_DEFAULT_REGION}${NC}"
echo -e "  Caller:  ${BLUE}${CALLER_ARN}${NC}"
# Interactive confirmation unless caller opted out (CI / --skip-checks)
if [ -t 0 ] && [ "$SKIP_CHECKS" != true ] && [ "$FORCE_BUILD" != true ]; then
    read -p "Proceed with this account/region? [y/N]: " CONFIRM
    CONFIRM=$(echo "$CONFIRM" | tr '[:upper:]' '[:lower:]')
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
fi

# Brave Search API Key (optional, cached in .env.local)
if [ -z "$BRAVE_API_KEY" ]; then
    echo -e "${YELLOW}Brave Search API Key not set.${NC}"
    echo "The Research Agent uses Brave Search for web research capabilities."
    echo "Get your API key from: https://api.search.brave.com/app/keys"
    echo ""
    read -p "Enter Brave Search API Key (or press Enter to skip): " BRAVE_API_KEY_INPUT
    if [ -n "$BRAVE_API_KEY_INPUT" ]; then
        export BRAVE_API_KEY="$BRAVE_API_KEY_INPUT"
        save_env_local "BRAVE_API_KEY" "$BRAVE_API_KEY_INPUT"
        echo -e "${GREEN}Brave Search API Key configured and saved to .env.local${NC}"
    else
        echo -e "${YELLOW}Skipping Brave Search - Research Agent will have limited functionality${NC}"
        export BRAVE_API_KEY=""
    fi
else
    echo -e "${GREEN}Brave Search API Key loaded from cache${NC}"
fi

echo -e "${BLUE}Target Region: ${AWS_DEFAULT_REGION}${NC}"
echo ""

# ========================================
# Check prerequisites
# ========================================
echo -e "${YELLOW}Checking prerequisites...${NC}"

# Docker setup (needed for CDK Knowledge Base stack)
if [ "$DEPLOY_INFRA" = true ]; then
    setup_docker
fi

if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq not found${NC}"
    echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
    exit 1
fi

echo -e "${GREEN}All prerequisites met${NC}"

# Pre-compute values needed by multiple steps
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ASSETS_BUCKET_NAME="aiccbuilderstack${STAGE_SUFFIX}-assets-${ACCOUNT_ID}-${AWS_DEFAULT_REGION}"
export ASSETS_BUCKET_NAME

# ========================================
# Parallel npm install (background jobs)
# ========================================
echo -e "\n${YELLOW}Installing dependencies in parallel...${NC}"

INFRA_NPM_PID=""
FRONTEND_NPM_PID=""

# Compute hashes for dependency checks
INFRA_PKG_HASH=$(md5sum "$SCRIPT_DIR/infrastructure/package-lock.json" 2>/dev/null | cut -d' ' -f1 || echo "none")
FRONTEND_PKG_HASH=$(md5sum "$SCRIPT_DIR/frontend/package-lock.json" 2>/dev/null | cut -d' ' -f1 || echo "none")

# Infrastructure npm install (if needed)
if [ "$DEPLOY_INFRA" = true ]; then
    if check_hash_changed "infra-pkg" "$INFRA_PKG_HASH"; then
        echo -e "${BLUE}[BG] Installing infrastructure dependencies...${NC}"
        (cd "$SCRIPT_DIR/infrastructure" && npm install --prefer-offline --no-audit 2>&1 | tail -1) &
        INFRA_NPM_PID=$!
    else
        echo -e "${GREEN}[SKIP] Infrastructure dependencies unchanged${NC}"
    fi
fi

# Frontend npm install (if needed)
if [ "$DEPLOY_FRONTEND" = true ]; then
    if check_hash_changed "frontend-pkg" "$FRONTEND_PKG_HASH"; then
        echo -e "${BLUE}[BG] Installing frontend dependencies...${NC}"
        (cd "$SCRIPT_DIR/frontend" && npm install --prefer-offline --no-audit 2>&1 | tail -1) &
        FRONTEND_NPM_PID=$!
    else
        echo -e "${GREEN}[SKIP] Frontend dependencies unchanged${NC}"
    fi
fi

# Wait for infrastructure npm install before CDK deploy
if [ -n "$INFRA_NPM_PID" ]; then
    echo -e "${CYAN}Waiting for infrastructure dependencies...${NC}"
    wait $INFRA_NPM_PID && save_hash "infra-pkg" "$INFRA_PKG_HASH"
fi

# ========================================
# Step 0.5: Pre-create ECR repo & push image (ECS mode only)
# Must happen BEFORE cdk deploy so ECS service can pull the image at creation time.
# ========================================
ECS_STACK_NAME="AiccBuilderEcs${STAGE_SUFFIX}"
ECR_REPO_NAME="$(echo "$ECS_STACK_NAME" | tr '[:upper:]' '[:lower:]')-repo"
ECR_REPO_URI=""

if [ "$DEPLOY_BACKEND" = true ] || [ "$DEPLOY_INFRA" = true ]; then
    echo -e "\n${YELLOW}Step 0.5: Pre-creating ECR repository & pushing Docker image...${NC}"

    # 0.5a) Create ECR repo if it doesn't exist
    EXISTING_REPO=$(aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" --region "$AWS_DEFAULT_REGION" 2>/dev/null | jq -r '.repositories[0].repositoryUri // empty')

    if [ -n "$EXISTING_REPO" ]; then
        ECR_REPO_URI="$EXISTING_REPO"
        echo -e "${GREEN}ECR repo exists: $ECR_REPO_URI${NC}"
    else
        echo "Creating ECR repository: $ECR_REPO_NAME"
        CREATE_OUTPUT=$(aws ecr create-repository \
            --repository-name "$ECR_REPO_NAME" \
            --region "$AWS_DEFAULT_REGION" \
            --image-scanning-configuration scanOnPush=false \
            --encryption-configuration encryptionType=AES256 2>&1)
        ECR_REPO_URI=$(echo "$CREATE_OUTPUT" | jq -r '.repository.repositoryUri // empty')
        if [ -n "$ECR_REPO_URI" ]; then
            echo -e "${GREEN}ECR repo created: $ECR_REPO_URI${NC}"
            # Set lifecycle policy (keep only 5 images)
            aws ecr put-lifecycle-policy \
                --repository-name "$ECR_REPO_NAME" \
                --region "$AWS_DEFAULT_REGION" \
                --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep only 5 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' \
                > /dev/null 2>&1 || true
        else
            echo -e "${RED}Failed to create ECR repository${NC}"
            echo "$CREATE_OUTPUT"
        fi
    fi

    # 0.5b) Build and push Docker image (if repo exists and backend sources changed)
    if [ -n "$ECR_REPO_URI" ]; then
        BACKEND_SRC_HASH=$(compute_hash "$SCRIPT_DIR/backend/ecs/src" "*.py")
        ECS_APP_HASH=$(md5sum "$SCRIPT_DIR/backend/ecs/app.py" 2>/dev/null | cut -d' ' -f1 || echo "none")
        BACKEND_HASH="${BACKEND_SRC_HASH}-${ECS_APP_HASH}"

        # Check if image already exists in ECR (skip build if unchanged)
        IMAGE_EXISTS=$(aws ecr describe-images --repository-name "$ECR_REPO_NAME" --image-ids imageTag=latest --region "$AWS_DEFAULT_REGION" 2>/dev/null && echo "yes" || echo "no")

        if [ "$IMAGE_EXISTS" = "no" ] || check_hash_changed "ecs-backend-src${STAGE_SUFFIX}" "$BACKEND_HASH"; then
            echo "Preparing ECS build context..."
            # backend/ecs/src/ is the source of truth for ECS mode.

            cd "$SCRIPT_DIR/backend/ecs"

            # Extract USER_POOL_ID and USER_POOL_CLIENT_ID from existing CDK outputs (if available)
            if [ -f "$CDK_OUTPUTS_FILE" ]; then
                USER_POOL_ID=${USER_POOL_ID:-$(jq -r --arg s "$STACK_NAME" '.[$s].UserPoolId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)}
                USER_POOL_CLIENT_ID=${USER_POOL_CLIENT_ID:-$(jq -r --arg s "$STACK_NAME" '.[$s].UserPoolClientId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)}
            fi

            echo "Building Docker image (ARM64)..."
            docker build --platform linux/arm64 \
                -t aicc-builder-ecs:latest \
                --build-arg ASSETS_BUCKET_NAME="${ASSETS_BUCKET_NAME:-}" \
                --build-arg USER_POOL_ID="${USER_POOL_ID:-}" \
                --build-arg USER_POOL_CLIENT_ID="${USER_POOL_CLIENT_ID:-}" \
                --build-arg CONTACT_FLOW_KB_ID="${CONTACT_FLOW_KB_ID:-}" \
                --build-arg BRAVE_API_KEY="${BRAVE_API_KEY:-}" \
                .

            echo "Pushing to ECR..."
            ECR_REGISTRY=$(echo "$ECR_REPO_URI" | cut -d'/' -f1)
            aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
            docker tag aicc-builder-ecs:latest "${ECR_REPO_URI}:latest"
            docker push "${ECR_REPO_URI}:latest"

            echo -e "${GREEN}Docker image pushed to ECR (pre-CDK)${NC}"
            save_hash "ecs-backend-src${STAGE_SUFFIX}" "$BACKEND_HASH"
        else
            echo -e "${GREEN}[SKIP] Docker image unchanged, already in ECR${NC}"
        fi
    fi
fi

# ========================================
# Step 1: Deploy CDK Infrastructure (AFTER ECR image is ready)
# ========================================
if [ "$DEPLOY_INFRA" = true ]; then
    echo -e "\n${YELLOW}Step 1: Deploying CDK Infrastructure...${NC}"
    cd "$SCRIPT_DIR/infrastructure"

    # Check CDK bootstrap
    if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region "$AWS_DEFAULT_REGION" > /dev/null 2>&1; then
        echo "Bootstrapping CDK..."
        npx cdk bootstrap "aws://${ACCOUNT_ID}/${AWS_DEFAULT_REGION}"
    fi

    # Ensure the ALB-DNS SSM parameter exists BEFORE the first cdk deploy so
    # AiccBuilderStack's CloudFront can resolve it via dynamic reference.
    # EcsStack later overwrites this value with the real ALB DNS.
    ALB_DNS_PARAM_NAME="/aicc-builder${STAGE_SUFFIX}/alb-dns"
    if ! aws ssm get-parameter --name "$ALB_DNS_PARAM_NAME" --region "$AWS_DEFAULT_REGION" > /dev/null 2>&1; then
        echo "Creating placeholder SSM parameter: $ALB_DNS_PARAM_NAME"
        aws ssm put-parameter --name "$ALB_DNS_PARAM_NAME" --value "placeholder.invalid" --type String \
            --region "$AWS_DEFAULT_REGION" > /dev/null
    fi

    # Pass target region via CDK context (CDK CLI can override CDK_DEFAULT_REGION)
    CDK_CONTEXT_ARGS="$CDK_CONTEXT_ARGS -c targetRegion=${AWS_DEFAULT_REGION}"

    echo "Deploying CDK stack..."
    if ! npx cdk deploy --all $CDK_CONTEXT_ARGS --require-approval never --outputs-file "$CDK_OUTPUTS_FILE"; then
        echo -e "${RED}CDK deploy failed. Check errors above.${NC}"
        if [ ! -f "$CDK_OUTPUTS_FILE" ]; then
            echo -e "${RED}No outputs file generated. Subsequent steps may fail.${NC}"
        fi
    fi

    # Publish ALB DNS to the SSM parameter so CloudFront (in AiccBuilderStack)
    # will pick it up on its next deploy. On the very first deploy the parameter
    # is still `pending`, so redeploy AiccBuilderStack to refresh the dynamic
    # reference on CloudFront behaviors.
    ECS_STACK_NAME="AiccBuilderEcs${STAGE_SUFFIX}"
    NEW_ALB_DNS=$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].AlbDnsName // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null || true)
    if [ -n "$NEW_ALB_DNS" ]; then
        CURRENT_ALB_DNS=$(aws ssm get-parameter --name "$ALB_DNS_PARAM_NAME" --region "$AWS_DEFAULT_REGION" \
            --query 'Parameter.Value' --output text 2>/dev/null || echo "")
        if [ "$CURRENT_ALB_DNS" != "$NEW_ALB_DNS" ]; then
            echo "Updating SSM ALB DNS parameter: $NEW_ALB_DNS"
            aws ssm put-parameter --name "$ALB_DNS_PARAM_NAME" --value "$NEW_ALB_DNS" --type String \
                --overwrite --region "$AWS_DEFAULT_REGION" > /dev/null
            echo "Redeploying $STACK_NAME so CloudFront picks up the real ALB DNS..."
            npx cdk deploy "$STACK_NAME" --exclusively $CDK_CONTEXT_ARGS --require-approval never \
                --outputs-file "$CDK_OUTPUTS_FILE" || \
                echo -e "${YELLOW}Warning: CloudFront refresh deploy failed — rerun ./deploy.sh --infra-only${NC}"
        fi
    fi
fi

# ========================================
# Step 1.1: Sync Knowledge Base Documents (if KB deployed)
# ========================================
ENABLE_KNOWLEDGE_BASE="${ENABLE_KNOWLEDGE_BASE:-true}"

if [ "$DEPLOY_INFRA" = true ] && [ "$ENABLE_KNOWLEDGE_BASE" != "false" ]; then
    # KB is created automatically by CDK Custom Resource
    CONTACT_FLOW_KB_ID=$(jq -r --arg s "$KB_STACK_NAME" '.[$s].ContactFlowKnowledgeBaseId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)

    if [ -n "$CONTACT_FLOW_KB_ID" ]; then
        echo -e "\n${GREEN}Knowledge Base deployed: $CONTACT_FLOW_KB_ID${NC}"

        # Sync KB documents if docs directory exists
        if [ -d "$SCRIPT_DIR/knowledge-base-docs/contact-flow" ] && [ -x "$SCRIPT_DIR/scripts/sync-kb-docs.sh" ]; then
            echo -e "${YELLOW}Step 1.1: Syncing Knowledge Base documents...${NC}"
            # Run in a subshell with `set +e` so a KB sync failure never kills
            # the whole deploy — KB is non-critical and can be retried via
            # `./scripts/sync-kb-docs.sh --wait` after the fact.
            (
                set +e
                CDK_OUTPUTS_FILE="$CDK_OUTPUTS_FILE" KB_STACK_NAME="$KB_STACK_NAME" \
                    "$SCRIPT_DIR/scripts/sync-kb-docs.sh" --wait
                rc=$?
                if [ $rc -ne 0 ]; then
                    echo -e "${YELLOW}Warning: KB sync exited with $rc — continuing. Re-run manually:${NC}"
                    echo "  CDK_OUTPUTS_FILE='$CDK_OUTPUTS_FILE' KB_STACK_NAME='$KB_STACK_NAME' $SCRIPT_DIR/scripts/sync-kb-docs.sh --wait"
                fi
            )
        fi
    fi
fi

# Extract values from CDK outputs for backend deployment
if [ -f "$CDK_OUTPUTS_FILE" ]; then
    ASSETS_BUCKET_NAME=$(jq -r --arg s "$STACK_NAME" '.[$s].AssetsBucketName // empty' "$CDK_OUTPUTS_FILE")
    CONTACT_FLOW_KB_ID=$(jq -r --arg s "$KB_STACK_NAME" '.[$s].ContactFlowKnowledgeBaseId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)
fi

# ========================================
# Step 3: Deploy Backend (AFTER CDK)
# ========================================
ALB_DNS_NAME=""

if [ "$DEPLOY_BACKEND" = true ]; then
    echo -e "\n${YELLOW}Step 3: Configuring ECS Fargate Backend...${NC}"

    # Extract ECS outputs (ECR_REPO_URI may already be set from Step 0.5)
    if [ -f "$CDK_OUTPUTS_FILE" ]; then
        ECR_REPO_URI=${ECR_REPO_URI:-$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].EcrRepositoryUri // empty' "$CDK_OUTPUTS_FILE")}
        ALB_DNS_NAME=$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].AlbDnsName // empty' "$CDK_OUTPUTS_FILE")
        ECS_CLUSTER_NAME=$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].EcsClusterName // empty' "$CDK_OUTPUTS_FILE")
        ECS_SERVICE_NAME=$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].EcsServiceName // empty' "$CDK_OUTPUTS_FILE")
        TASK_DEF_ARN=$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].TaskDefinitionArn // empty' "$CDK_OUTPUTS_FILE")
        TASK_ROLE_ARN=$(jq -r --arg s "$ECS_STACK_NAME" '.[$s].TaskRoleArn // empty' "$CDK_OUTPUTS_FILE")
    fi

    if [ -z "$ECR_REPO_URI" ]; then
        echo -e "${RED}ECR Repository URI not found. Run with --infra-only first.${NC}"
        exit 1
    fi

    # Docker image was already built & pushed in Step 0.5 (pre-CDK).
    # This section handles post-CDK configuration: S3 Files volume + force deployment.
    BACKEND_SRC_HASH=$(compute_hash "$SCRIPT_DIR/backend/ecs/src" "*.py")
    ECS_APP_HASH=$(md5sum "$SCRIPT_DIR/backend/ecs/app.py" 2>/dev/null | cut -d' ' -f1 || echo "none")
    BACKEND_HASH="${BACKEND_SRC_HASH}-${ECS_APP_HASH}"

    if check_hash_changed "ecs-backend-cfg${STAGE_SUFFIX}" "$BACKEND_HASH"; then

        # Patch ECS task definition with runtime env vars
        # (S3 Files volume is now managed entirely by CDK — see infrastructure/lib/ecs-stack.ts)
        if [ -n "$TASK_DEF_ARN" ]; then
            TASK_DEF_FAMILY=$(echo "$TASK_DEF_ARN" | sed 's|.*/||' | sed 's|:[0-9]*$||')

            # Extract env var values from CDK outputs (may have been set after Docker build)
            if [ -f "$CDK_OUTPUTS_FILE" ]; then
                USER_POOL_ID=${USER_POOL_ID:-$(jq -r --arg s "$STACK_NAME" '.[$s].UserPoolId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)}
                USER_POOL_CLIENT_ID=${USER_POOL_CLIENT_ID:-$(jq -r --arg s "$STACK_NAME" '.[$s].UserPoolClientId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)}
                ASSETS_BUCKET_NAME=${ASSETS_BUCKET_NAME:-$(jq -r --arg s "$STACK_NAME" '.[$s].AssetsBucketName // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)}
                CONTACT_FLOW_KB_ID=${CONTACT_FLOW_KB_ID:-$(jq -r --arg s "$KB_STACK_NAME" '.[$s].ContactFlowKnowledgeBaseId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)}
            fi

            echo "Patching task definition (runtime env vars)..."

            # Build jq filter — inject env vars into the task def
            JQ_FILTER='.taskDefinition
                | del(.taskDefinitionArn, .revision, .status, .registeredAt, .registeredBy, .compatibilities, .requiresAttributes)'

            # Always inject/update runtime env vars into container definition
            # Uses reduce to upsert: update existing var or append new one
            JQ_FILTER="${JQ_FILTER}
                | .containerDefinitions[0].environment as \$env
                | .containerDefinitions[0].environment = (
                    [\$env[] | select(.name | IN(\"ASSETS_BUCKET_NAME\",\"USER_POOL_ID\",\"USER_POOL_CLIENT_ID\",\"CONTACT_FLOW_KB_ID\",\"BRAVE_API_KEY\") | not)]
                    + [{\"name\":\"ASSETS_BUCKET_NAME\",\"value\":\$bucket},
                       {\"name\":\"USER_POOL_ID\",\"value\":\$pool},
                       {\"name\":\"USER_POOL_CLIENT_ID\",\"value\":\$poolclient},
                       {\"name\":\"CONTACT_FLOW_KB_ID\",\"value\":\$kbid},
                       {\"name\":\"BRAVE_API_KEY\",\"value\":\$brave}]
                  )"

            aws ecs describe-task-definition --task-definition "$TASK_DEF_FAMILY" --region "$AWS_DEFAULT_REGION" \
                | jq --arg bucket "${ASSETS_BUCKET_NAME:-}" \
                     --arg pool "${USER_POOL_ID:-}" \
                     --arg poolclient "${USER_POOL_CLIENT_ID:-}" \
                     --arg kbid "${CONTACT_FLOW_KB_ID:-}" \
                     --arg brave "${BRAVE_API_KEY:-}" \
                     "$JQ_FILTER" > /tmp/patched-task-def.json

            PATCHED_TASK_DEF_ARN=$(aws ecs register-task-definition --cli-input-json file:///tmp/patched-task-def.json \
                --region "$AWS_DEFAULT_REGION" --query 'taskDefinition.taskDefinitionArn' --output text 2>&1)
            if [ $? -eq 0 ] && [ -n "$PATCHED_TASK_DEF_ARN" ]; then
                echo -e "${GREEN}Task definition patched: $PATCHED_TASK_DEF_ARN (env vars: ASSETS_BUCKET_NAME=${ASSETS_BUCKET_NAME:+set}, USER_POOL_ID=${USER_POOL_ID:+set}, USER_POOL_CLIENT_ID=${USER_POOL_CLIENT_ID:+set})${NC}"
            else
                echo -e "${YELLOW}Warning: Task definition patch failed — may need manual config${NC}"
                PATCHED_TASK_DEF_ARN=""
            fi
            rm -f /tmp/patched-task-def.json
        fi

        # Force new deployment with the patched task definition
        echo "Forcing ECS service update..."
        if [ -n "$ECS_CLUSTER_NAME" ] && [ -n "$ECS_SERVICE_NAME" ]; then
            # Build update-service args — include patched task def if available
            UPDATE_ARGS="--cluster $ECS_CLUSTER_NAME --service $ECS_SERVICE_NAME --force-new-deployment --region $AWS_DEFAULT_REGION"
            if [ -n "$PATCHED_TASK_DEF_ARN" ]; then
                UPDATE_ARGS="$UPDATE_ARGS --task-definition $PATCHED_TASK_DEF_ARN"
            fi
            aws ecs update-service $UPDATE_ARGS > /dev/null 2>&1 && \
                echo -e "${GREEN}ECS service update triggered${NC}" || \
                echo -e "${YELLOW}Warning: ECS service update failed — service may need to be recreated via CDK${NC}"
        fi
        save_hash "ecs-backend-cfg${STAGE_SUFFIX}" "$BACKEND_HASH"
    else
        echo -e "${GREEN}[SKIP] ECS backend config unchanged${NC}"
    fi

    if [ -n "$ALB_DNS_NAME" ]; then
        echo -e "${GREEN}ALB DNS: $ALB_DNS_NAME${NC}"
    fi
fi

# ========================================
# Extract CDK outputs (needed for frontend)
# ========================================
if [ -f "$CDK_OUTPUTS_FILE" ]; then
    FRONTEND_URL=$(jq -r --arg s "$STACK_NAME" '.[$s].FrontendUrl // empty' "$CDK_OUTPUTS_FILE")
    FRONTEND_BUCKET=$(jq -r --arg s "$STACK_NAME" '.[$s].FrontendBucketName // empty' "$CDK_OUTPUTS_FILE")
    USER_POOL_ID=$(jq -r --arg s "$STACK_NAME" '.[$s].UserPoolId // empty' "$CDK_OUTPUTS_FILE")
    USER_POOL_CLIENT_ID=$(jq -r --arg s "$STACK_NAME" '.[$s].UserPoolClientId // empty' "$CDK_OUTPUTS_FILE")
    IDENTITY_POOL_ID=$(jq -r --arg s "$STACK_NAME" '.[$s].IdentityPoolId // empty' "$CDK_OUTPUTS_FILE")
    SESSION_API_URL=$(jq -r --arg s "$STACK_NAME" '.[$s].SessionApiUrl // empty' "$CDK_OUTPUTS_FILE")
    ASSETS_BUCKET_NAME=$(jq -r --arg s "$STACK_NAME" '.[$s].AssetsBucketName // empty' "$CDK_OUTPUTS_FILE")

    # Knowledge Base outputs (from KnowledgeBaseStack)
    CONTACT_FLOW_KB_ID=$(jq -r --arg s "$KB_STACK_NAME" '.[$s].ContactFlowKnowledgeBaseId // empty' "$CDK_OUTPUTS_FILE")
    KB_DOCS_BUCKET_NAME=$(jq -r --arg s "$KB_STACK_NAME" '.[$s].KnowledgeBaseDocsBucketName // empty' "$CDK_OUTPUTS_FILE")
fi

# Wait for frontend npm install before build
if [ -n "$FRONTEND_NPM_PID" ]; then
    echo -e "${CYAN}Waiting for frontend dependencies...${NC}"
    wait $FRONTEND_NPM_PID && save_hash "frontend-pkg" "$FRONTEND_PKG_HASH"
fi

# ========================================
# Step 4: Build and Deploy Frontend
# ========================================
if [ "$DEPLOY_FRONTEND" = true ]; then
    echo -e "\n${YELLOW}Step 4: Building Frontend...${NC}"
    cd "$SCRIPT_DIR/frontend"

    # Create .env file (ECS mode — WebSocket via CloudFront→ALB same-origin)
    cat > .env << EOF
# AICC Builder Frontend Configuration (ECS Mode)
# Generated by deploy.sh on $(date)
# WebSocket: wss://cloudfront-domain/ws (same-origin, CloudFront proxies to ALB)

VITE_WS_MODE=alb
VITE_USER_POOL_ID=${USER_POOL_ID}
VITE_USER_POOL_CLIENT_ID=${USER_POOL_CLIENT_ID}
VITE_IDENTITY_POOL_ID=${IDENTITY_POOL_ID}
VITE_COGNITO_REGION=${AWS_DEFAULT_REGION}
VITE_SESSION_API_URL=${SESSION_API_URL}
EOF

    # Compute frontend source hash
    FRONTEND_SRC_HASH=$(compute_hash "$SCRIPT_DIR/frontend/src" "*.ts*")
    FRONTEND_ENV_HASH=$(md5sum "$SCRIPT_DIR/frontend/.env" | cut -d' ' -f1)
    FRONTEND_HASH="${FRONTEND_SRC_HASH}-${FRONTEND_ENV_HASH}"

    if check_hash_changed "frontend-src${STAGE_SUFFIX}" "$FRONTEND_HASH"; then
        echo "Building frontend..."
        npm run build
        save_hash "frontend-src${STAGE_SUFFIX}" "$FRONTEND_HASH"
    else
        echo -e "${GREEN}[SKIP] Frontend source unchanged, using existing build${NC}"
        # Still need dist/ directory
        if [ ! -d "dist" ]; then
            echo "No existing build found, building..."
            npm run build
            save_hash "frontend-src${STAGE_SUFFIX}" "$FRONTEND_HASH"
        fi
    fi

    # Step 5: Deploy to S3
    echo -e "\n${YELLOW}Step 5: Deploying Frontend to S3...${NC}"
    if [ -z "$FRONTEND_BUCKET" ]; then
        echo -e "${RED}Error: FRONTEND_BUCKET is empty. CDK outputs may be missing. Run full deploy first.${NC}"
    else
        aws s3 sync dist/ "s3://$FRONTEND_BUCKET/" --delete --region "$AWS_DEFAULT_REGION"
    fi

    # Step 6: Invalidate CloudFront
    echo -e "\n${YELLOW}Step 6: Invalidating CloudFront cache...${NC}"

    # Read distribution ID from CDK outputs (reliable) instead of list-distributions query
    DISTRIBUTION_ID=""
    if [ -f "$CDK_OUTPUTS_FILE" ]; then
        DISTRIBUTION_ID=$(jq -r --arg s "$STACK_NAME" '.[$s].CloudFrontDistributionId // empty' "$CDK_OUTPUTS_FILE")
    fi

    # Fallback: query by origin bucket name
    if [ -z "$DISTRIBUTION_ID" ] && [ -n "$FRONTEND_BUCKET" ]; then
        DISTRIBUTION_ID=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?contains(Origins.Items[].DomainName, '${FRONTEND_BUCKET}')].Id | [0]" \
            --output text 2>/dev/null || true)
    fi

    if [ -n "$DISTRIBUTION_ID" ] && [ "$DISTRIBUTION_ID" != "None" ] && [ "$DISTRIBUTION_ID" != "null" ]; then
        aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*" > /dev/null
        echo -e "${GREEN}CloudFront cache invalidated (${DISTRIBUTION_ID})${NC}"
    else
        echo -e "${YELLOW}Warning: Could not find CloudFront distribution ID${NC}"
    fi
fi

# ========================================
# Step 8: Save deployment outputs
# ========================================
echo -e "\n${YELLOW}Step 8: Saving deployment configuration...${NC}"

if [ -f "$CDK_OUTPUTS_FILE" ]; then
    ECS_STACK_NAME="AiccBuilderEcs${STAGE_SUFFIX}"
    jq --arg albdns "${ALB_DNS_NAME:-}" \
       --arg mode "ecs" \
       --arg sessapi "$SESSION_API_URL" \
       --arg braveenabled "$([ -n "$BRAVE_API_KEY" ] && echo "true" || echo "false")" \
       --arg sn "$STACK_NAME" \
        '.[$sn].DeployMode = $mode |
         .[$sn].AlbDnsName = $albdns |
         .[$sn].SessionApiUrl = $sessapi |
         .[$sn].BraveSearchEnabled = $braveenabled' \
        "$CDK_OUTPUTS_FILE" > "${CDK_OUTPUTS_FILE}.tmp" && \
        mv "${CDK_OUTPUTS_FILE}.tmp" "$CDK_OUTPUTS_FILE"
fi

# ========================================
# Done!
# ========================================
echo -e "\n${GREEN}=========================================="
echo "  Deployment Complete!"
echo "==========================================${NC}"
echo ""
if [ -n "$FRONTEND_URL" ]; then
    echo "Access your AICC Builder at:"
    echo -e "  ${GREEN}$FRONTEND_URL${NC}"
    echo ""
fi
echo "Configuration:"
echo -e "  Region: ${BLUE}${AWS_DEFAULT_REGION}${NC}"
if [ -n "$USER_POOL_ID" ]; then
    echo -e "  User Pool ID: ${BLUE}${USER_POOL_ID}${NC}"
fi
if [ -n "$ALB_DNS_NAME" ]; then
    echo -e "  ALB DNS: ${BLUE}${ALB_DNS_NAME}${NC}"
    echo -e "  WebSocket URL: ${BLUE}ws://${ALB_DNS_NAME}/ws${NC}"
fi

# Re-read KB ID in case it was just created
CONTACT_FLOW_KB_ID=$(jq -r --arg s "$KB_STACK_NAME" '.[$s].ContactFlowKnowledgeBaseId // empty' "$CDK_OUTPUTS_FILE" 2>/dev/null)
if [ -n "$CONTACT_FLOW_KB_ID" ]; then
    echo -e "  Knowledge Base ID: ${BLUE}${CONTACT_FLOW_KB_ID}${NC}"
fi
echo ""
echo -e "${CYAN}Quick redeploy commands:${NC}"
echo "  ./deploy.sh --backend-only   # Redeploy backend only"
echo "  ./deploy.sh --frontend-only  # Redeploy frontend only"
echo "  ./deploy.sh --force          # Force full rebuild"
echo ""
echo "Outputs saved to: $CDK_OUTPUTS_FILE"
