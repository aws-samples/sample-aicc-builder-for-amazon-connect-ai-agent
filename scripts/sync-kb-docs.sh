#!/bin/bash
# sync-kb-docs.sh
# Syncs knowledge base documents to S3 and triggers ingestion

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
DOCS_DIR="$PROJECT_DIR/knowledge-base-docs/contact-flow"
# Allow caller to override via CDK_OUTPUTS_FILE env (set by deploy.sh with stage suffix)
CDK_OUTPUTS="${CDK_OUTPUTS_FILE:-$PROJECT_DIR/cdk-outputs.json}"
# Stage-aware KB stack key (default: AiccBuilderKnowledgeBase, with --stage prod: AiccBuilderKnowledgeBase-prod)
KB_STACK_KEY="${KB_STACK_NAME:-AiccBuilderKnowledgeBase}"
# Region: inherit from deploy.sh (AWS_DEFAULT_REGION) — fall back to CLI config, then ap-northeast-2
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-northeast-2)}}"
export AWS_DEFAULT_REGION="$REGION"

echo -e "${CYAN}=========================================="
echo "  Knowledge Base Document Sync"
echo -e "==========================================${NC}"

# Surface the AWS identity this sync will hit so a wrong AWS_PROFILE is
# obvious instead of silently failing halfway through.
CALLER=$(aws sts get-caller-identity --output json 2>&1) || {
    echo -e "${RED}ERROR: aws sts get-caller-identity failed:${NC}"
    echo "$CALLER"
    echo -e "${YELLOW}Check AWS_PROFILE (current: ${AWS_PROFILE:-<unset>}) and ~/.aws/credentials.${NC}"
    exit 1
}
echo -e "${BLUE}Profile: ${AWS_PROFILE:-<default>}  Account: $(echo "$CALLER" | jq -r .Account)  Region: ${AWS_DEFAULT_REGION:-<unset>}${NC}"

# Check if documents directory exists
if [ ! -d "$DOCS_DIR" ]; then
    echo -e "${RED}Error: Documents directory not found: $DOCS_DIR${NC}"
    exit 1
fi

# Check if CDK outputs exist
if [ ! -f "$CDK_OUTPUTS" ]; then
    echo -e "${RED}Error: CDK outputs not found. Run deploy.sh first.${NC}"
    exit 1
fi

# Extract Knowledge Base configuration from CDK outputs
KB_ID=$(jq -r --arg s "$KB_STACK_KEY" '.[$s].ContactFlowKnowledgeBaseId // empty' "$CDK_OUTPUTS")
DOCS_BUCKET=$(jq -r --arg s "$KB_STACK_KEY" '.[$s].KnowledgeBaseDocsBucketName // empty' "$CDK_OUTPUTS")
DATA_SOURCE_ID=$(jq -r --arg s "$KB_STACK_KEY" '.[$s].DataSourceId // empty' "$CDK_OUTPUTS")

if [ -z "$KB_ID" ] || [ -z "$DOCS_BUCKET" ] || [ -z "$DATA_SOURCE_ID" ]; then
    echo -e "${YELLOW}Knowledge Base not deployed yet.${NC}"
    echo "Run the following to deploy:"
    echo "  cd infrastructure && npm run deploy"
    echo ""
    echo "Or set ENABLE_KNOWLEDGE_BASE=false to skip KB deployment."
    exit 1
fi

echo -e "${BLUE}Knowledge Base ID: $KB_ID${NC}"
echo -e "${BLUE}Documents Bucket: $DOCS_BUCKET${NC}"
echo -e "${BLUE}Data Source ID: $DATA_SOURCE_ID${NC}"
echo -e "${BLUE}Region: $REGION${NC}"
echo ""

# Count documents
DOC_COUNT=$(find "$DOCS_DIR" -name "*.md" -type f | wc -l | tr -d ' ')
echo -e "${CYAN}Found $DOC_COUNT markdown documents${NC}"

# Sync documents to S3
echo -e "\n${YELLOW}Syncing documents to S3...${NC}"
aws s3 sync "$DOCS_DIR" "s3://$DOCS_BUCKET/contact-flow/" \
    --delete \
    --exclude "*.DS_Store" \
    --exclude "*.tmp" \
    --region "$REGION"

echo -e "${GREEN}Documents synced successfully${NC}"

# Start ingestion job
echo -e "\n${YELLOW}Starting ingestion job...${NC}"

# Check current ingestion status
CURRENT_STATUS=$(aws bedrock-agent list-ingestion-jobs \
    --knowledge-base-id "$KB_ID" \
    --data-source-id "$DATA_SOURCE_ID" \
    --query 'ingestionJobSummaries[0].status' \
    --output text \
    --region "$REGION" 2>/dev/null || echo "NONE")

if [ "$CURRENT_STATUS" = "IN_PROGRESS" ]; then
    echo -e "${YELLOW}An ingestion job is already in progress. Waiting for completion...${NC}"

    # Wait for current job to complete
    while [ "$CURRENT_STATUS" = "IN_PROGRESS" ]; do
        sleep 10
        CURRENT_STATUS=$(aws bedrock-agent list-ingestion-jobs \
            --knowledge-base-id "$KB_ID" \
            --data-source-id "$DATA_SOURCE_ID" \
            --query 'ingestionJobSummaries[0].status' \
            --output text \
            --region "$REGION" 2>/dev/null || echo "COMPLETE")
        echo -n "."
    done
    echo ""
fi

# Start new ingestion job
set +e
INGESTION_RESULT=$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "$KB_ID" \
<<<<<<< Updated upstream
    --data-source-id "$DATA_SOURCE_ID" 2>&1)
INGESTION_RC=$?
=======
    --data-source-id "$DATA_SOURCE_ID" \
    --region "$REGION" 2>&1)
INGESTION_RC=$?
set -e

if [ $INGESTION_RC -ne 0 ]; then
    echo -e "${RED}start-ingestion-job failed (exit $INGESTION_RC):${NC}"
    echo "$INGESTION_RESULT"
    exit 1
fi
>>>>>>> Stashed changes

INGESTION_JOB_ID=$(echo "$INGESTION_RESULT" | jq -r '.ingestionJob.ingestionJobId // empty' 2>/dev/null)

if [ -z "$INGESTION_JOB_ID" ]; then
    echo -e "${YELLOW}Failed to start ingestion job (exit=$INGESTION_RC):${NC}"
    echo "$INGESTION_RESULT"
    echo -e "${YELLOW}Skipping ingestion — documents are in S3, KB can be re-indexed later via:${NC}"
    echo "  aws bedrock-agent start-ingestion-job --knowledge-base-id $KB_ID --data-source-id $DATA_SOURCE_ID"
    exit 0
fi

echo -e "${GREEN}Ingestion job started: $INGESTION_JOB_ID${NC}"

# Option to wait for completion
if [ "$1" = "--wait" ]; then
    echo -e "\n${CYAN}Waiting for ingestion to complete...${NC}"

    STATUS="IN_PROGRESS"
    while [ "$STATUS" = "IN_PROGRESS" ]; do
        sleep 15
        STATUS=$(aws bedrock-agent get-ingestion-job \
            --knowledge-base-id "$KB_ID" \
            --data-source-id "$DATA_SOURCE_ID" \
            --ingestion-job-id "$INGESTION_JOB_ID" \
            --query 'ingestionJob.status' \
            --output text \
            --region "$REGION")
        echo -n "."
    done
    echo ""

    if [ "$STATUS" = "COMPLETE" ]; then
        # Get statistics
        STATS=$(aws bedrock-agent get-ingestion-job \
            --knowledge-base-id "$KB_ID" \
            --data-source-id "$DATA_SOURCE_ID" \
            --ingestion-job-id "$INGESTION_JOB_ID" \
            --query 'ingestionJob.statistics' \
            --region "$REGION")

        echo -e "${GREEN}Ingestion completed successfully!${NC}"
        echo -e "${BLUE}Statistics:${NC}"
        echo "$STATS" | jq '.'
    else
        echo -e "${RED}Ingestion failed with status: $STATUS${NC}"

        # Get failure reasons
        FAILURE=$(aws bedrock-agent get-ingestion-job \
            --knowledge-base-id "$KB_ID" \
            --data-source-id "$DATA_SOURCE_ID" \
            --ingestion-job-id "$INGESTION_JOB_ID" \
            --query 'ingestionJob.failureReasons' \
            --region "$REGION")

        if [ "$FAILURE" != "null" ]; then
            echo -e "${RED}Failure reasons:${NC}"
            echo "$FAILURE" | jq '.'
        fi
        exit 1
    fi
else
    echo -e "\n${CYAN}Ingestion running in background.${NC}"
    echo "Use --wait flag to wait for completion."
    echo ""
    echo "Check status with:"
    echo -e "  ${BLUE}aws bedrock-agent get-ingestion-job \\${NC}"
    echo -e "  ${BLUE}    --knowledge-base-id $KB_ID \\${NC}"
    echo -e "  ${BLUE}    --data-source-id $DATA_SOURCE_ID \\${NC}"
    echo -e "  ${BLUE}    --ingestion-job-id $INGESTION_JOB_ID${NC}"
fi

echo -e "\n${GREEN}=========================================="
echo "  Sync Complete!"
echo -e "==========================================${NC}"
echo ""
echo "Documents synced: $DOC_COUNT"
echo "Bucket: s3://$DOCS_BUCKET/contact-flow/"
echo ""
echo -e "${CYAN}To test retrieval:${NC}"
echo "  aws bedrock-agent-runtime retrieve \\"
echo "    --knowledge-base-id $KB_ID \\"
echo "    --retrieval-query '{\"text\": \"TransferContactToQueue error types\"}'"
