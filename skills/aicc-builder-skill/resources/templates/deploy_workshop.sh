#!/bin/bash
# =============================================================================
# AICC Builder - Full Automation Deployment Script (CloudShell)
#
# Commands:
#   ./deploy.sh           Deploy all workshop assets (default)
#   ./deploy.sh deploy    Same as above
#   ./deploy.sh cleanup   Tear down ALL deployed resources (reverse order)
#   ./deploy.sh status    Show current deployment status
#
# Deploys all workshop assets end-to-end:
#   1. CloudFormation stack (S3 upload for large templates)
#   2. Lambda function code updates (with retry)
#   3. OpenAPI spec update & S3 upload
#   4. FAQ document upload
#   5. Amazon Connect instance create/select
#   6. Q in Connect Assistant create/select
#   6.5 Knowledge Base connection (if FAQ exists)
#   7. Lambda environment variable injection
#   8. AgentCore Gateway (MCP Server) creation
#   9. Gateway audience update + target creation
#
# Usage:
#   1. Upload the assets ZIP to CloudShell
#   2. unzip <filename>.zip && cd <project>/
#   3. chmod +x deploy.sh && ./deploy.sh
#
# Environment variable overrides:
#   CONNECT_INSTANCE_ID  - Skip Connect instance selection
#   AI_ASSISTANT_ID      - Skip Q in Connect assistant creation
#   AWS_DEFAULT_REGION   - Override region (default: ap-northeast-2)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-ap-northeast-2}"
COMMAND="${1:-deploy}"

# Auto-detect project name from CloudFormation template directory
if [ -d "$SCRIPT_DIR/cloudformation" ]; then
    first_dir=$(ls "$SCRIPT_DIR/cloudformation" 2>/dev/null | head -1)
    if [ -n "$first_dir" ] && [ -d "$SCRIPT_DIR/cloudformation/$first_dir" ]; then
        PROJECT_NAME="$first_dir"
    fi
fi
PROJECT_NAME="${PROJECT_NAME:-aicc-poc}"
STACK_NAME="${PROJECT_NAME}-stack"
ENVIRONMENT="dev"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Find CloudFormation template (check nested or flat structure)
CFN_TEMPLATE=""
if [ -f "$SCRIPT_DIR/cloudformation/$PROJECT_NAME/infrastructure.yaml" ]; then
    CFN_TEMPLATE="$SCRIPT_DIR/cloudformation/$PROJECT_NAME/infrastructure.yaml"
elif [ -f "$SCRIPT_DIR/cloudformation/infrastructure.yaml" ]; then
    CFN_TEMPLATE="$SCRIPT_DIR/cloudformation/infrastructure.yaml"
fi

# Naming conventions (shared between deploy and cleanup)
TEMPLATE_BUCKET="${PROJECT_NAME}-cfn-${REGION}-${ACCOUNT_ID}"
ROLE_NAME="${PROJECT_NAME}-gateway-role"
CRED_PROVIDER_NAME="${PROJECT_NAME}-api-key"
GATEWAY_NAME="${PROJECT_NAME}-mcp-server"
TARGET_NAME="${PROJECT_NAME}-api"
KB_NAME="${PROJECT_NAME}-faq-kb"
UPDATE_Q_FUNC="${PROJECT_NAME}-update-qsession-${ENVIRONMENT}"

# =============================================================================
# Helper: get CloudFormation output
# =============================================================================
get_output() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || echo ""
}

# #############################################################################
#
#  CLEANUP COMMAND
#
# #############################################################################
do_cleanup() {
    echo "============================================="
    echo "  AICC Builder - Resource Cleanup"
    echo "  Project: $PROJECT_NAME"
    echo "  Region:  $REGION"
    echo "  Account: $ACCOUNT_ID"
    echo "============================================="
    echo ""
    echo "  ⚠️  이 명령은 아래 리소스를 모두 삭제합니다:"
    echo "     - AgentCore Gateway + Target + Credential Provider"
    echo "     - Gateway IAM Role"
    echo "     - Q in Connect Assistant + Knowledge Base"
    echo "     - WISDOM_ASSISTANT Integration Association"
    echo "     - CloudFormation Stack (Lambda, API GW, DynamoDB 등)"
    echo "     - S3 버킷 내 에셋 (OpenAPI, FAQ)"
    echo "     - CFN 템플릿 S3 버킷"
    echo ""
    echo "     ※ Connect Instance 자체는 삭제하지 않습니다."
    echo ""
    read -p "  정말 삭제하시겠습니까? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo "  취소되었습니다."
        exit 0
    fi
    echo ""

    # ─── Remove WISDOM_ASSISTANT integration associations ──────────────────
    echo "🔗 Removing WISDOM_ASSISTANT integration associations..."

    CLEANUP_CONNECT_ID="${CONNECT_INSTANCE_ID:-}"
    if [ -z "$CLEANUP_CONNECT_ID" ]; then
        INSTANCES_JSON=$(aws connect list-instances --region "$REGION" --output json 2>/dev/null || echo '{"InstanceSummaryList":[]}')
        INSTANCE_COUNT=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('InstanceSummaryList',[])))" 2>/dev/null || echo "0")
        if [ "$INSTANCE_COUNT" -eq 1 ]; then
            CLEANUP_CONNECT_ID=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['InstanceSummaryList'][0]['Id'])")
        elif [ "$INSTANCE_COUNT" -gt 1 ]; then
            echo "   Multiple Connect instances found. Specify CONNECT_INSTANCE_ID env var."
            echo "   Skipping integration cleanup."
        fi
    fi

    if [ -n "$CLEANUP_CONNECT_ID" ]; then
        WISDOM_ASSOCS=$(aws connect list-integration-associations \
            --instance-id "$CLEANUP_CONNECT_ID" --integration-type WISDOM_ASSISTANT \
            --region "$REGION" --output json 2>/dev/null || echo '{"IntegrationAssociationSummaryList":[]}')
        echo "$WISDOM_ASSOCS" | python3 -c "
import sys, json
assocs = json.load(sys.stdin).get('IntegrationAssociationSummaryList', [])
for a in assocs:
    print(a['IntegrationAssociationId'])
" 2>/dev/null | while read -r assoc_id; do
            [ -z "$assoc_id" ] && continue
            echo "   Deleting WISDOM_ASSISTANT association: $assoc_id"
            aws connect delete-integration-association \
                --instance-id "$CLEANUP_CONNECT_ID" \
                --integration-association-id "$assoc_id" \
                --region "$REGION" 2>/dev/null || true
        done
        echo "   ✅ Integration associations removed"
    fi

    # ─── Delete Gateway + Target + Credential Provider ───────────────────────
    echo ""
    echo "🌐 Removing AgentCore Gateway resources..."

    if aws bedrock-agentcore-control help &>/dev/null 2>&1; then
        # Find gateway by name
        GW_LIST=$(aws bedrock-agentcore-control list-gateways \
            --region "$REGION" --output json 2>/dev/null || echo '{"gateways":[]}')
        CLEANUP_GW_ID=$(echo "$GW_LIST" | python3 -c "
import sys, json
for gw in json.load(sys.stdin).get('gateways', []):
    if gw.get('name') == '${GATEWAY_NAME}':
        print(gw['gatewayId']); break
else: print('')
" 2>/dev/null || echo "")

        if [ -n "$CLEANUP_GW_ID" ]; then
            # Delete gateway targets first
            TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
                --gateway-identifier "$CLEANUP_GW_ID" \
                --region "$REGION" --output json 2>/dev/null || echo '{"targets":[]}')
            echo "$TARGETS" | python3 -c "
import sys, json
for t in json.load(sys.stdin).get('targets', []):
    print(t.get('targetId',''))
" 2>/dev/null | while read -r tid; do
                [ -z "$tid" ] && continue
                echo "   Deleting gateway target: $tid"
                aws bedrock-agentcore-control delete-gateway-target \
                    --gateway-identifier "$CLEANUP_GW_ID" \
                    --target-identifier "$tid" \
                    --region "$REGION" 2>/dev/null || true
            done

            # Delete gateway
            echo "   Deleting gateway: $CLEANUP_GW_ID"
            aws bedrock-agentcore-control delete-gateway \
                --gateway-identifier "$CLEANUP_GW_ID" \
                --region "$REGION" 2>/dev/null || true

            # Wait for gateway deletion
            echo -n "   Waiting for gateway deletion..."
            for i in $(seq 1 30); do
                if ! aws bedrock-agentcore-control get-gateway \
                    --gateway-identifier "$CLEANUP_GW_ID" \
                    --region "$REGION" &>/dev/null 2>&1; then
                    echo " ✅"
                    break
                fi
                echo -n "."
                sleep 5
            done
        else
            echo "   No gateway found with name: $GATEWAY_NAME"
        fi

        # Delete credential provider
        CRED_LIST=$(aws bedrock-agentcore-control list-api-key-credential-providers \
            --region "$REGION" --output json 2>/dev/null || echo '{"credentialProviders":[]}')
        CLEANUP_CRED_ARN=$(echo "$CRED_LIST" | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('credentialProviders', []):
    if p.get('name') == '${CRED_PROVIDER_NAME}':
        print(p['credentialProviderArn']); break
else: print('')
" 2>/dev/null || echo "")

        if [ -n "$CLEANUP_CRED_ARN" ]; then
            echo "   Deleting credential provider: $CRED_PROVIDER_NAME"
            aws bedrock-agentcore-control delete-api-key-credential-provider \
                --credential-provider-id "$CLEANUP_CRED_ARN" \
                --region "$REGION" 2>/dev/null || true
        fi
        echo "   ✅ Gateway resources removed"
    else
        echo "   ⚠️ bedrock-agentcore-control not available, skipping."
    fi

    # ─── Delete Gateway IAM Role ─────────────────────────────────────────────
    echo ""
    echo "🔑 Removing Gateway IAM role..."
    if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null 2>&1; then
        # Delete inline policies first
        POLICIES=$(aws iam list-role-policies --role-name "$ROLE_NAME" \
            --query 'PolicyNames' --output json 2>/dev/null || echo '[]')
        echo "$POLICIES" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    print(p)
" 2>/dev/null | while read -r pname; do
            [ -z "$pname" ] && continue
            aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$pname" 2>/dev/null || true
        done
        # Delete attached managed policies
        ATTACHED=$(aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
            --query 'AttachedPolicies[].PolicyArn' --output json 2>/dev/null || echo '[]')
        echo "$ATTACHED" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    print(p)
" 2>/dev/null | while read -r parn; do
            [ -z "$parn" ] && continue
            aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$parn" 2>/dev/null || true
        done
        aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true
        echo "   ✅ IAM role deleted: $ROLE_NAME"
    else
        echo "   No IAM role found: $ROLE_NAME"
    fi

    # ─── Step 6.5 reverse: Delete Knowledge Base ─────────────────────────────
    echo ""
    echo "📖 Removing Q in Connect Knowledge Base..."
    KB_LIST=$(aws qconnect list-knowledge-bases --region "$REGION" --output json 2>/dev/null || echo '{"knowledgeBaseSummaries":[]}')
    CLEANUP_KB_ID=$(echo "$KB_LIST" | python3 -c "
import sys, json
for kb in json.load(sys.stdin).get('knowledgeBaseSummaries', []):
    if kb.get('name') == '${KB_NAME}':
        print(kb['knowledgeBaseId']); break
else: print('')
" 2>/dev/null || echo "")

    if [ -n "$CLEANUP_KB_ID" ]; then
        echo "   Deleting knowledge base: $CLEANUP_KB_ID"
        aws qconnect delete-knowledge-base --knowledge-base-id "$CLEANUP_KB_ID" \
            --region "$REGION" 2>/dev/null || true
        echo "   ✅ Knowledge base deleted"
    else
        echo "   No knowledge base found: $KB_NAME"
    fi

    # ─── Step 6 reverse: Delete Q in Connect Assistant ───────────────────────
    echo ""
    echo "🤖 Removing Q in Connect Assistant..."
    ASST_LIST=$(aws qconnect list-assistants --region "$REGION" --output json 2>/dev/null || echo '{"assistantSummaries":[]}')
    CLEANUP_ASST_ID=$(echo "$ASST_LIST" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assistantSummaries', []):
    if a.get('name') == '${PROJECT_NAME}-assistant':
        print(a['assistantId']); break
else: print('')
" 2>/dev/null || echo "")

    if [ -n "$CLEANUP_ASST_ID" ]; then
        # Delete assistant associations first
        ASST_ASSOCS=$(aws qconnect list-assistant-associations \
            --assistant-id "$CLEANUP_ASST_ID" --region "$REGION" --output json 2>/dev/null || echo '{"assistantAssociationSummaries":[]}')
        echo "$ASST_ASSOCS" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assistantAssociationSummaries', []):
    print(a['assistantAssociationId'])
" 2>/dev/null | while read -r aaid; do
            [ -z "$aaid" ] && continue
            echo "   Deleting assistant association: $aaid"
            aws qconnect delete-assistant-association \
                --assistant-id "$CLEANUP_ASST_ID" \
                --assistant-association-id "$aaid" \
                --region "$REGION" 2>/dev/null || true
        done

        echo "   Deleting assistant: $CLEANUP_ASST_ID"
        aws qconnect delete-assistant --assistant-id "$CLEANUP_ASST_ID" \
            --region "$REGION" 2>/dev/null || true
        echo "   ✅ Assistant deleted"
    else
        echo "   No assistant found: ${PROJECT_NAME}-assistant"
    fi

    # ─── Step 3-4 reverse: Clean S3 assets ───────────────────────────────────
    echo ""
    echo "📚 Cleaning S3 assets..."
    KB_BUCKET=$(get_output "KnowledgeBaseBucketName")
    if [ -n "$KB_BUCKET" ]; then
        echo "   Emptying s3://$KB_BUCKET..."
        aws s3 rm "s3://$KB_BUCKET" --recursive --region "$REGION" 2>/dev/null || true
        echo "   ✅ S3 assets cleaned"
    else
        echo "   No KB bucket found in stack outputs"
    fi

    # ─── Step 1 reverse: Delete CloudFormation Stack ─────────────────────────
    echo ""
    echo "📦 Deleting CloudFormation stack: $STACK_NAME..."
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
        aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
        echo "   Waiting for stack deletion..."
        aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
        echo "   ✅ Stack deleted"
    else
        echo "   Stack not found: $STACK_NAME"
    fi

    # ─── Delete CFN template S3 bucket ───────────────────────────────────────
    echo ""
    echo "🪣 Cleaning CFN template bucket..."
    if aws s3 ls "s3://$TEMPLATE_BUCKET" --region "$REGION" &>/dev/null 2>&1; then
        aws s3 rm "s3://$TEMPLATE_BUCKET" --recursive --region "$REGION" 2>/dev/null || true
        aws s3api delete-bucket --bucket "$TEMPLATE_BUCKET" --region "$REGION" 2>/dev/null || true
        echo "   ✅ Template bucket deleted: $TEMPLATE_BUCKET"
    else
        echo "   Template bucket not found: $TEMPLATE_BUCKET"
    fi

    echo ""
    echo "============================================="
    echo "  ✅ Cleanup Complete!"
    echo "============================================="
    echo ""
    echo "  삭제된 리소스:"
    echo "    - CloudFormation stack: $STACK_NAME"
    echo "    - Gateway: $GATEWAY_NAME"
    echo "    - IAM role: $ROLE_NAME"
    echo "    - Credential provider: $CRED_PROVIDER_NAME"
    echo "    - Assistant: ${PROJECT_NAME}-assistant"
    echo "    - Knowledge Base: $KB_NAME"
    echo "    - S3 assets & template bucket"
    echo ""
    echo "  ※ Connect Instance는 삭제되지 않았습니다."
    echo "    수동 삭제: aws connect delete-instance --instance-id <ID>"
    echo ""
    echo "============================================="
}

# #############################################################################
#
#  STATUS COMMAND
#
# #############################################################################
do_status() {
    echo "============================================="
    echo "  AICC Builder - Deployment Status"
    echo "  Project: $PROJECT_NAME | Region: $REGION"
    echo "============================================="
    echo ""

    # CloudFormation
    STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")
    echo "  📦 CloudFormation Stack: $STACK_STATUS"

    if [ "$STACK_STATUS" != "NOT_FOUND" ]; then
        API_ENDPOINT=$(get_output "ApiEndpoint")
        echo "     API Endpoint: ${API_ENDPOINT:-N/A}"
    fi

    # Connect
    INSTANCES_JSON=$(aws connect list-instances --region "$REGION" --output json 2>/dev/null || echo '{"InstanceSummaryList":[]}')
    INSTANCE_COUNT=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('InstanceSummaryList',[])))" 2>/dev/null || echo "0")
    echo "  📞 Connect Instances: $INSTANCE_COUNT"

    # Q in Connect Assistant
    ASST_LIST=$(aws qconnect list-assistants --region "$REGION" --output json 2>/dev/null || echo '{"assistantSummaries":[]}')
    ASST_COUNT=$(echo "$ASST_LIST" | python3 -c "
import sys, json
count = sum(1 for a in json.load(sys.stdin).get('assistantSummaries', []) if a.get('name') == '${PROJECT_NAME}-assistant')
print(count)
" 2>/dev/null || echo "0")
    echo "  🤖 Q in Connect Assistant: ${ASST_COUNT} found"

    # Knowledge Base
    KB_LIST=$(aws qconnect list-knowledge-bases --region "$REGION" --output json 2>/dev/null || echo '{"knowledgeBaseSummaries":[]}')
    KB_EXISTS=$(echo "$KB_LIST" | python3 -c "
import sys, json
for kb in json.load(sys.stdin).get('knowledgeBaseSummaries', []):
    if kb.get('name') == '${KB_NAME}':
        print(kb.get('status','UNKNOWN')); break
else: print('NOT_FOUND')
" 2>/dev/null || echo "UNKNOWN")
    echo "  📖 Knowledge Base ($KB_NAME): $KB_EXISTS"

    # Gateway
    if aws bedrock-agentcore-control help &>/dev/null 2>&1; then
        GW_LIST=$(aws bedrock-agentcore-control list-gateways \
            --region "$REGION" --output json 2>/dev/null || echo '{"gateways":[]}')
        GW_STATUS=$(echo "$GW_LIST" | python3 -c "
import sys, json
for gw in json.load(sys.stdin).get('gateways', []):
    if gw.get('name') == '${GATEWAY_NAME}':
        print(gw.get('status','UNKNOWN')); break
else: print('NOT_FOUND')
" 2>/dev/null || echo "UNKNOWN")
        echo "  🌐 AgentCore Gateway ($GATEWAY_NAME): $GW_STATUS"
    else
        echo "  🌐 AgentCore Gateway: CLI not available"
    fi

    # IAM Role
    if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null 2>&1; then
        echo "  🔑 Gateway IAM Role ($ROLE_NAME): EXISTS"
    else
        echo "  🔑 Gateway IAM Role ($ROLE_NAME): NOT_FOUND"
    fi

    # Template bucket
    if aws s3 ls "s3://$TEMPLATE_BUCKET" --region "$REGION" &>/dev/null 2>&1; then
        echo "  🪣 CFN Template Bucket ($TEMPLATE_BUCKET): EXISTS"
    else
        echo "  🪣 CFN Template Bucket ($TEMPLATE_BUCKET): NOT_FOUND"
    fi

    echo ""
    echo "============================================="
}

# #############################################################################
#
#  DEPLOY COMMAND
#
# #############################################################################
do_deploy() {
    if [ -z "$CFN_TEMPLATE" ]; then
        echo "❌ CloudFormation template not found in cloudformation/"
        exit 1
    fi

    echo "============================================="
    echo "  AICC Builder - Full Automation Deployment"
    echo "  Project: $PROJECT_NAME"
    echo "  Region:  $REGION"
    echo "  Account: $ACCOUNT_ID"
    echo "  Profile: ${AWS_PROFILE:-<default>}"
    echo "  Caller:  $(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo '?')"
    echo "============================================="
    if [ -t 0 ] && [ -z "${AUTO_CONFIRM:-}" ]; then
        read -p "  Proceed with this account/region? [y/N]: " OK
        OK=$(echo "$OK" | tr '[:upper:]' '[:lower:]')
        case "$OK" in y|yes) ;; *) echo "  Aborted."; exit 0;; esac
    fi

    # =============================================================================
    # Step 1: Deploy CloudFormation
    # =============================================================================
    echo ""
    echo "📦 Step 1: Deploying CloudFormation stack..."

    # Create S3 bucket for large templates (>51KB limit)
    if ! aws s3 ls "s3://$TEMPLATE_BUCKET" --region "$REGION" 2>/dev/null; then
        echo "   Creating S3 bucket for CloudFormation templates..."
        if [ "$REGION" = "us-east-1" ]; then
            aws s3api create-bucket --bucket "$TEMPLATE_BUCKET" --region "$REGION"
        else
            aws s3api create-bucket --bucket "$TEMPLATE_BUCKET" --region "$REGION" \
                --create-bucket-configuration LocationConstraint="$REGION"
        fi
        aws s3api put-bucket-versioning --bucket "$TEMPLATE_BUCKET" \
            --versioning-configuration Status=Enabled --region "$REGION"
    fi

    # Upload template to S3
    TEMPLATE_KEY="infrastructure-$(date +%Y%m%d-%H%M%S).yaml"
    echo "   Uploading template to s3://$TEMPLATE_BUCKET/$TEMPLATE_KEY..."
    aws s3 cp "$CFN_TEMPLATE" "s3://$TEMPLATE_BUCKET/$TEMPLATE_KEY" --region "$REGION"
    TEMPLATE_URL="https://s3.${REGION}.amazonaws.com/${TEMPLATE_BUCKET}/${TEMPLATE_KEY}"

    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
        echo "   Stack exists, updating..."
        if aws cloudformation update-stack \
            --stack-name "$STACK_NAME" \
            --template-url "$TEMPLATE_URL" \
            --parameters ParameterKey=ProjectName,ParameterValue="$PROJECT_NAME" \
                         ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "$REGION" 2>/dev/null; then
            echo "   Waiting for stack update..."
            aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME" --region "$REGION"
        else
            echo "   No updates needed."
        fi
    else
        echo "   Creating new stack..."
        aws cloudformation create-stack \
            --stack-name "$STACK_NAME" \
            --template-url "$TEMPLATE_URL" \
            --parameters ParameterKey=ProjectName,ParameterValue="$PROJECT_NAME" \
                         ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "$REGION"
        echo "   Waiting for stack creation..."
        aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME" --region "$REGION"
    fi
    echo "   ✅ CloudFormation stack ready!"

    # ─── Extract Outputs ─────────────────────────────────────────────────────
    API_ENDPOINT=$(get_output "ApiEndpoint")
    API_KEY=$(get_output "ApiKeyValue")
    KB_BUCKET=$(get_output "KnowledgeBaseBucketName")
    CUSTOMER_LOOKUP_ARN=$(get_output "CustomerLookupFunctionArn")
    UPDATE_Q_SESSION_ARN=$(get_output "UpdateQSessionFunctionArn")

    echo ""
    echo "   API Endpoint: $API_ENDPOINT"
    echo "   API Key:      ${API_KEY:0:10}..."

    # =============================================================================
    # Step 2: Update Lambda Functions
    # =============================================================================
    echo ""
    echo "🔧 Step 2: Updating Lambda functions..."

    if [ -d "$SCRIPT_DIR/lambda" ]; then
        for func_dir in "$SCRIPT_DIR/lambda"/*/; do
            [ -d "$func_dir" ] || continue
            func_name=$(basename "$func_dir")

            # Find entry file: prefer index.py, then index.js, then first file
            if [ -f "$func_dir/index.py" ]; then
                entry_file="index.py"
            elif [ -f "$func_dir/index.js" ]; then
                entry_file="index.js"
            else
                entry_file=$(ls "$func_dir" | head -1)
            fi

            [ -z "$entry_file" ] && continue

            # Map function directory name to AWS Lambda function name
            case "$func_name" in
                customer_lookup)  aws_func="${PROJECT_NAME}-customer-lookup-${ENVIRONMENT}" ;;
                update_q_session) aws_func="${PROJECT_NAME}-update-qsession-${ENVIRONMENT}" ;;
                *)
                    kebab=$(echo "$func_name" | sed 's/_/-/g' | sed 's/\([A-Z]\)/-\L\1/g' | sed 's/^-//')
                    aws_func="${PROJECT_NAME}-${ENVIRONMENT}-${kebab}"
                    ;;
            esac

            # update_q_session is optional — only declared in the CFn template when
            # the interview captured a Q-in-Connect session-update requirement.
            # If the function isn't in the deployed stack, skip quietly with a
            # friendly message instead of failing Step 2 with ResourceNotFoundException.
            if [ "$func_name" = "update_q_session" ] && [ -z "${UPDATE_Q_SESSION_ARN:-}" ]; then
                echo "   ⏭️  $aws_func <- $func_name/$entry_file ... skipped (not declared in this stack's CloudFormation — Q session update is optional)"
                continue
            fi

            echo -n "   $aws_func <- $func_name/$entry_file ... "
            (cd "$func_dir" && zip -qj /tmp/_deploy.zip "$entry_file")

            # Update with retry (handle ResourceConflictException)
            retry_count=0
            max_retries=3
            while [ $retry_count -lt $max_retries ]; do
                if aws lambda update-function-code --function-name "$aws_func" \
                    --zip-file fileb:///tmp/_deploy.zip --region "$REGION" \
                    --output text --query 'FunctionName' &>/dev/null; then
                    aws lambda wait function-updated --function-name "$aws_func" --region "$REGION" 2>/dev/null || true
                    echo "✅"
                    break
                else
                    retry_count=$((retry_count + 1))
                    if [ $retry_count -lt $max_retries ]; then
                        echo -n "⏳(retry $retry_count) "
                        sleep 5
                    else
                        echo "⚠️ failed after $max_retries attempts"
                    fi
                fi
            done
            rm -f /tmp/_deploy.zip
        done
    fi

    # =============================================================================
    # Step 3: Update OpenAPI spec & upload to S3
    # =============================================================================
    echo ""
    echo "📝 Step 3: Updating OpenAPI spec..."

    if [ -d "$SCRIPT_DIR/openapi" ]; then
        # Normalize: strip scheme, strip trailing slash, strip a stray `/tools` suffix.
        # `ApiEndpoint` should be the stage root (`.../dev`). A trailing `/tools` would
        # collide with OpenAPI `paths: /tools/...` and produce `.../tools/tools/<op>` →
        # API Gateway returns 403 "Missing Authentication Token". Guard against that here
        # so a legacy/mis-generated CloudFormation Output doesn't silently break routing.
        API_HOST=$(echo "$API_ENDPOINT" | sed -E 's|^https?://||; s|/+$||; s|/tools/?$||')
        if [ "$API_HOST" != "$(echo "$API_ENDPOINT" | sed -E 's|^https?://||; s|/+$||')" ]; then
            echo "   ⚠️  Stripped trailing '/tools' from ApiEndpoint (OpenAPI paths already carry '/tools/...')"
        fi
        find "$SCRIPT_DIR/openapi" -name "openapi.yaml" | while read -r f; do
            sed -i.bak "s|{API_ENDPOINT}|$API_HOST|g" "$f" && rm -f "${f}.bak"
            echo "   ✅ Updated: $f"
        done
        # Upload to S3 for MCP Gateway
        if [ -n "${KB_BUCKET:-}" ]; then
            aws s3 sync "$SCRIPT_DIR/openapi" "s3://$KB_BUCKET/openapi/" \
                --exclude "*.DS_Store" --exclude "*.bak" --region "$REGION"
            echo "   ✅ Uploaded to s3://$KB_BUCKET/openapi/"
        fi
    fi

    # =============================================================================
    # Step 4: Upload FAQ documents
    # =============================================================================
    echo ""
    echo "📚 Step 4: Uploading FAQ documents..."

    FAQ_DIR="$SCRIPT_DIR/faq/knowledge_base"
    if [ -d "$FAQ_DIR" ] && [ -n "${KB_BUCKET:-}" ]; then
        count=$(find "$FAQ_DIR" -name "*.txt" | wc -l | tr -d ' ')
        aws s3 sync "$FAQ_DIR" "s3://$KB_BUCKET/faq/" --exclude "*.DS_Store" --region "$REGION"
        echo "   ✅ $count documents -> s3://$KB_BUCKET/faq/"
    else
        echo "   ⚠️ No FAQ directory or KB bucket, skipping."
    fi

    # =============================================================================
    # Step 5: Amazon Connect Instance - Create or Select
    # =============================================================================
    echo ""
    echo "📞 Step 5: Setting up Amazon Connect instance..."

    if [ -n "${CONNECT_INSTANCE_ID:-}" ]; then
        echo "   Using pre-configured CONNECT_INSTANCE_ID: $CONNECT_INSTANCE_ID"
    else
        # List existing Connect instances
        INSTANCES_JSON=$(aws connect list-instances --region "$REGION" --output json 2>/dev/null || echo '{"InstanceSummaryList":[]}')
        INSTANCE_COUNT=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('InstanceSummaryList',[])))" 2>/dev/null || echo "0")

        if [ "$INSTANCE_COUNT" -eq 0 ]; then
            # No instances — create one
            echo "   No Connect instances found. Creating a new one..."
            ALIAS="aicc-workshop-${ACCOUNT_ID: -4}"
            CREATE_RESULT=$(aws connect create-instance \
                --identity-management-type "CONNECT_MANAGED" \
                --instance-alias "$ALIAS" \
                --inbound-calls-enabled \
                --outbound-calls-enabled \
                --region "$REGION" --output json 2>&1)

            CONNECT_INSTANCE_ID=$(echo "$CREATE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['Id'])")
            echo "   Created Connect instance: $CONNECT_INSTANCE_ID (alias: $ALIAS)"

            # Wait for instance to become ACTIVE
            echo -n "   Waiting for instance to become ACTIVE..."
            for i in $(seq 1 60); do
                STATUS=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
                    --query 'Instance.InstanceStatus' --output text 2>/dev/null || echo "UNKNOWN")
                if [ "$STATUS" = "ACTIVE" ]; then
                    echo " ✅"
                    break
                fi
                echo -n "."
                sleep 10
            done

        elif [ "$INSTANCE_COUNT" -eq 1 ]; then
            # Exactly one instance — use it
            CONNECT_INSTANCE_ID=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['InstanceSummaryList'][0]['Id'])")
            CONNECT_ALIAS=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['InstanceSummaryList'][0].get('InstanceAlias','N/A'))")
            echo "   Found 1 Connect instance: $CONNECT_ALIAS ($CONNECT_INSTANCE_ID)"

        else
            # Multiple instances — interactive selection
            echo "   Found $INSTANCE_COUNT Connect instances:"
            echo "$INSTANCES_JSON" | python3 -c "
import sys, json
instances = json.load(sys.stdin)['InstanceSummaryList']
for i, inst in enumerate(instances):
    print(f\"   [{i+1}] {inst.get('InstanceAlias','N/A')} ({inst['Id']})\")
"
            echo ""
            read -p "   Select instance number [1-$INSTANCE_COUNT]: " SELECTION
            CONNECT_INSTANCE_ID=$(echo "$INSTANCES_JSON" | python3 -c "
import sys, json
instances = json.load(sys.stdin)['InstanceSummaryList']
idx = int('${SELECTION}') - 1
if 0 <= idx < len(instances):
    print(instances[idx]['Id'])
else:
    print('')
")
            if [ -z "$CONNECT_INSTANCE_ID" ]; then
                echo "   ❌ Invalid selection."
                exit 1
            fi
        fi

    fi

    # Enable all required instance attributes (Lex, monitoring, transcription, etc.)
    echo "   Enabling instance attributes..."
    for attr in ENHANCED_CONTACT_MONITORING USE_CUSTOM_TTS_VOICES AUTO_RESOLVE_BEST_VOICES \
                 CONTACTFLOW_LOGS CONTACT_LENS MULTI_PARTY_CONFERENCE \
                 HIGH_VOLUME_OUTBOUND EARLY_MEDIA; do
        aws connect update-instance-attribute \
            --instance-id "$CONNECT_INSTANCE_ID" \
            --attribute-type "$attr" \
            --value "true" \
            --region "$REGION" 2>/dev/null || true
    done

    # Configure storage for call recordings and chat transcripts
    echo "   Configuring storage for recordings and transcripts..."
    CONNECT_STORAGE_BUCKET="${PROJECT_NAME}-connect-${ACCOUNT_ID}-${REGION}"
    if ! aws s3 ls "s3://$CONNECT_STORAGE_BUCKET" --region "$REGION" 2>/dev/null; then
        echo "   Creating storage bucket: $CONNECT_STORAGE_BUCKET"
        if [ "$REGION" = "us-east-1" ]; then
            aws s3api create-bucket --bucket "$CONNECT_STORAGE_BUCKET" --region "$REGION" 2>/dev/null || true
        else
            aws s3api create-bucket --bucket "$CONNECT_STORAGE_BUCKET" --region "$REGION" \
                --create-bucket-configuration LocationConstraint="$REGION" 2>/dev/null || true
        fi
    fi
    for STORAGE_TYPE in CALL_RECORDINGS CHAT_TRANSCRIPTS; do
        aws connect associate-instance-storage-config \
            --instance-id "$CONNECT_INSTANCE_ID" \
            --resource-type "$STORAGE_TYPE" \
            --storage-config "{
                \"StorageType\": \"S3\",
                \"S3Config\": {
                    \"BucketName\": \"${CONNECT_STORAGE_BUCKET}\",
                    \"BucketPrefix\": \"${STORAGE_TYPE}\"
                }
            }" \
            --region "$REGION" 2>/dev/null || true
    done
    echo "   ✅ Storage configured: s3://$CONNECT_STORAGE_BUCKET"

    # Get instance alias for later use (discovery URL)
    CONNECT_ALIAS=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
        --query 'Instance.InstanceAlias' --output text 2>/dev/null || echo "")
    CONNECT_INSTANCE_ARN=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
        --query 'Instance.Arn' --output text 2>/dev/null || echo "")

    echo "   ✅ Connect Instance: $CONNECT_INSTANCE_ID (alias: ${CONNECT_ALIAS:-N/A})"

    # =============================================================================
    # Step 6: Q in Connect Assistant - Create or Select
    # =============================================================================
    echo ""
    echo "🤖 Step 6: Setting up Q in Connect Assistant..."

    if [ -n "${AI_ASSISTANT_ID:-}" ]; then
        echo "   Using pre-configured AI_ASSISTANT_ID: $AI_ASSISTANT_ID"
        ASSISTANT_ARN="arn:aws:wisdom:${REGION}:${ACCOUNT_ID}:assistant/${AI_ASSISTANT_ID}"
    else
        # Check for existing assistant integration
        EXISTING_ASSOC=$(aws connect list-integration-associations \
            --instance-id "$CONNECT_INSTANCE_ID" \
            --integration-type WISDOM_ASSISTANT \
            --region "$REGION" --output json 2>/dev/null || echo '{"IntegrationAssociationSummaryList":[]}')

        ASSOC_COUNT=$(echo "$EXISTING_ASSOC" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('IntegrationAssociationSummaryList',[])))" 2>/dev/null || echo "0")

        if [ "$ASSOC_COUNT" -gt 0 ]; then
            # Use existing assistant
            ASSISTANT_ARN=$(echo "$EXISTING_ASSOC" | python3 -c "import sys,json; print(json.load(sys.stdin)['IntegrationAssociationSummaryList'][0]['IntegrationArn'])")
            AI_ASSISTANT_ID=$(echo "$ASSISTANT_ARN" | awk -F'/' '{print $NF}')
            echo "   Found existing assistant: $AI_ASSISTANT_ID"
        else
            # Create new assistant
            echo "   Creating new Q in Connect assistant..."
            CREATE_ASSISTANT=$(aws qconnect create-assistant \
                --name "${PROJECT_NAME}-assistant" \
                --type AGENT \
                --description "AI assistant for ${PROJECT_NAME}" \
                --region "$REGION" --output json 2>&1)

            if echo "$CREATE_ASSISTANT" | python3 -c "import sys,json; json.load(sys.stdin)['assistant']" &>/dev/null; then
                AI_ASSISTANT_ID=$(echo "$CREATE_ASSISTANT" | python3 -c "import sys,json; print(json.load(sys.stdin)['assistant']['assistantId'])")
                ASSISTANT_ARN=$(echo "$CREATE_ASSISTANT" | python3 -c "import sys,json; print(json.load(sys.stdin)['assistant']['assistantArn'])")
                echo "   Created assistant: $AI_ASSISTANT_ID"

                # Wait for assistant to become ACTIVE
                echo -n "   Waiting for assistant to become ACTIVE..."
                for i in $(seq 1 30); do
                    ASTATUS=$(aws qconnect get-assistant --assistant-id "$AI_ASSISTANT_ID" --region "$REGION" \
                        --query 'assistant.status' --output text 2>/dev/null || echo "UNKNOWN")
                    if [ "$ASTATUS" = "ACTIVE" ]; then
                        echo " ✅"
                        break
                    fi
                    echo -n "."
                    sleep 5
                done

                # Create integration association with Connect
                echo "   Connecting assistant to Connect instance..."
                aws connect create-integration-association \
                    --instance-id "$CONNECT_INSTANCE_ID" \
                    --integration-type WISDOM_ASSISTANT \
                    --integration-arn "$ASSISTANT_ARN" \
                    --region "$REGION" 2>/dev/null || echo "   (Association may already exist)"
            else
                echo "   ⚠️ Failed to create assistant. Continuing without Q in Connect."
                echo "   Error: $CREATE_ASSISTANT"
                AI_ASSISTANT_ID=""
            fi
        fi
    fi

    echo "   ✅ AI Assistant ID: ${AI_ASSISTANT_ID:-N/A}"

    # =============================================================================
    # Step 6.5: Knowledge Base Connection (if FAQ exists)
    # =============================================================================
    if [ -d "$SCRIPT_DIR/faq" ] && [ -n "${AI_ASSISTANT_ID:-}" ]; then
        echo ""
        echo "📖 Step 6.5: Setting up Knowledge Base for FAQ..."

        # Check if KB already exists
        EXISTING_KB=$(aws qconnect list-knowledge-bases --region "$REGION" --output json 2>/dev/null || echo '{"knowledgeBaseSummaries":[]}')
        KB_ID=$(echo "$EXISTING_KB" | python3 -c "
import sys, json
kbs = json.load(sys.stdin).get('knowledgeBaseSummaries', [])
for kb in kbs:
    if kb.get('name') == '${KB_NAME}':
        print(kb['knowledgeBaseId'])
        break
else:
    print('')
" 2>/dev/null || echo "")

        if [ -z "$KB_ID" ]; then
            # Create knowledge base
            echo "   Creating knowledge base: $KB_NAME"
            CREATE_KB=$(aws qconnect create-knowledge-base \
                --name "$KB_NAME" \
                --knowledge-base-type CUSTOM \
                --description "FAQ knowledge base for ${PROJECT_NAME}" \
                --region "$REGION" --output json 2>&1)

            if echo "$CREATE_KB" | python3 -c "import sys,json; json.load(sys.stdin)['knowledgeBase']" &>/dev/null; then
                KB_ID=$(echo "$CREATE_KB" | python3 -c "import sys,json; print(json.load(sys.stdin)['knowledgeBase']['knowledgeBaseId'])")
                echo "   Created KB: $KB_ID"
            else
                echo "   ⚠️ Failed to create knowledge base: $CREATE_KB"
            fi
        else
            echo "   Found existing KB: $KB_ID"
        fi

        # Associate KB with assistant
        if [ -n "$KB_ID" ]; then
            echo "   Associating KB with assistant..."
            aws qconnect create-assistant-association \
                --assistant-id "$AI_ASSISTANT_ID" \
                --association-type KNOWLEDGE_BASE \
                --association "knowledgeBaseId=$KB_ID" \
                --region "$REGION" 2>/dev/null || echo "   (Association may already exist)"
            echo "   ✅ Knowledge Base connected to assistant"
            echo "   ℹ️  FAQ sync: S3 data source 연결은 콘솔에서 진행하세요."
            echo "        Bucket: s3://${KB_BUCKET:-N/A}/faq/"
        fi
    fi

    # =============================================================================
    # Step 7: Lambda Environment Variable Injection
    # =============================================================================
    echo ""
    echo "🔑 Step 7: Injecting Lambda environment variables..."

    if [ -z "${UPDATE_Q_SESSION_ARN:-}" ]; then
        echo "   ⏭️  Skipping — $UPDATE_Q_FUNC is not declared in this stack's CloudFormation"
        echo "      (Q session update is optional; only needed when the interview captured a"
        echo "      Q-in-Connect session-update requirement)."
    elif [ -n "${CONNECT_INSTANCE_ID:-}" ] && [ -n "${AI_ASSISTANT_ID:-}" ]; then
        echo "   Updating $UPDATE_Q_FUNC with CONNECT_INSTANCE_ID and AI_ASSISTANT_ID..."

        # Get existing environment variables to merge
        EXISTING_ENV=$(aws lambda get-function-configuration \
            --function-name "$UPDATE_Q_FUNC" --region "$REGION" \
            --query 'Environment.Variables' --output json 2>/dev/null || echo '{}')

        # Merge new variables with existing ones
        MERGED_ENV=$(echo "$EXISTING_ENV" | python3 -c "
import sys, json
raw = sys.stdin.read().strip()
try:
    env = json.loads(raw) if raw and raw != 'null' and raw != 'None' else {}
except:
    env = {}
if not isinstance(env, dict):
    env = {}
env['CONNECT_INSTANCE_ID'] = '${CONNECT_INSTANCE_ID}'
env['AI_ASSISTANT_ID'] = '${AI_ASSISTANT_ID}'
print(json.dumps({'Variables': env}))
" 2>/dev/null || echo "{\"Variables\":{\"CONNECT_INSTANCE_ID\":\"${CONNECT_INSTANCE_ID}\",\"AI_ASSISTANT_ID\":\"${AI_ASSISTANT_ID}\"}}")

        # Retry loop for environment variable update
        retry_count=0
        max_retries=3
        while [ $retry_count -lt $max_retries ]; do
            if aws lambda update-function-configuration \
                --function-name "$UPDATE_Q_FUNC" \
                --environment "$MERGED_ENV" \
                --region "$REGION" --output text --query 'FunctionName' &>/dev/null; then
                aws lambda wait function-updated --function-name "$UPDATE_Q_FUNC" --region "$REGION" 2>/dev/null || true
                echo "   ✅ Environment variables injected"
                break
            else
                retry_count=$((retry_count + 1))
                if [ $retry_count -lt $max_retries ]; then
                    echo "   ⏳ Retry $retry_count..."
                    sleep 5
                else
                    echo "   ⚠️ Failed to update environment variables after $max_retries attempts"
                fi
            fi
        done
    else
        echo "   ⚠️ Skipping — CONNECT_INSTANCE_ID or AI_ASSISTANT_ID not set."
    fi

    # =============================================================================
    # Step 8: AgentCore Gateway (MCP Server) Creation
    # =============================================================================
    echo ""
    echo "🌐 Step 8: Setting up AgentCore Gateway (MCP Server)..."

    # Check AWS CLI version for bedrock-agentcore-control support
    if ! aws bedrock-agentcore-control help &>/dev/null 2>&1; then
        echo "   ⚠️ bedrock-agentcore-control not available in this AWS CLI version."
        echo "   Please upgrade AWS CLI: pip install --upgrade awscli"
        echo "   Skipping Steps 8-10."
        SKIP_GATEWAY=true
    else
        SKIP_GATEWAY=false
    fi

    if [ "$SKIP_GATEWAY" = "false" ] && [ -n "${CONNECT_ALIAS:-}" ]; then

        # Step 8.1: Create or reuse Gateway IAM Role
        echo "   Setting up IAM role: $ROLE_NAME"

        TRUST_POLICY=$(cat <<'TRUSTEOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
TRUSTEOF
)

        GATEWAY_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" \
            --query 'Role.Arn' --output text 2>/dev/null || echo "")

        if [ -z "$GATEWAY_ROLE_ARN" ]; then
            GATEWAY_ROLE_ARN=$(aws iam create-role \
                --role-name "$ROLE_NAME" \
                --assume-role-policy-document "$TRUST_POLICY" \
                --description "IAM role for ${PROJECT_NAME} AgentCore Gateway" \
                --query 'Role.Arn' --output text)
            echo "   Created IAM role: $GATEWAY_ROLE_ARN"

            # Attach basic permissions
            PERMISSION_POLICY=$(cat <<PERMEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockAgentCoreFullAccess",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:*",
      "Resource": "arn:aws:bedrock-agentcore:*:${ACCOUNT_ID}:*"
    },
    {
      "Sid": "BedrockInvokeAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3ReadAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${KB_BUCKET:-*}",
        "arn:aws:s3:::${KB_BUCKET:-*}/*",
        "arn:aws:s3:::bedrock-agentcore-gateway-*",
        "arn:aws:s3:::bedrock-agentcore-runtime-*"
      ]
    },
    {
      "Sid": "LambdaAccess",
      "Effect": "Allow",
      "Action": [
        "lambda:InvokeFunction",
        "lambda:GetFunction"
      ],
      "Resource": "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PROJECT_NAME}-*"
    },
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:bedrock-agentcore*"
    }
  ]
}
PERMEOF
)
            aws iam put-role-policy \
                --role-name "$ROLE_NAME" \
                --policy-name "${PROJECT_NAME}-gateway-policy" \
                --policy-document "$PERMISSION_POLICY"
            echo "   Attached gateway permissions"

            # Wait for IAM propagation
            sleep 10
        else
            echo "   Reusing existing IAM role: $GATEWAY_ROLE_ARN"
        fi

        # Step 8.2: Create API Key Credential Provider
        echo "   Creating API Key credential provider..."

        # Check for existing credential provider
        EXISTING_CRED=$(aws bedrock-agentcore-control list-api-key-credential-providers \
            --region "$REGION" --output json 2>/dev/null || echo '{"credentialProviders":[]}')

        CREDENTIAL_PROVIDER_ARN=$(echo "$EXISTING_CRED" | python3 -c "
import sys, json
providers = json.load(sys.stdin).get('credentialProviders', [])
for p in providers:
    if p.get('name') == '${CRED_PROVIDER_NAME}':
        print(p['credentialProviderArn'])
        break
else:
    print('')
" 2>/dev/null || echo "")

        if [ -z "$CREDENTIAL_PROVIDER_ARN" ]; then
            CRED_RESULT=$(aws bedrock-agentcore-control create-api-key-credential-provider \
                --name "$CRED_PROVIDER_NAME" \
                --api-key "$API_KEY" \
                --region "$REGION" --output json 2>&1)

            CREDENTIAL_PROVIDER_ARN=$(echo "$CRED_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('credentialProviderArn',''))" 2>/dev/null || echo "")

            if [ -n "$CREDENTIAL_PROVIDER_ARN" ]; then
                echo "   Created credential provider: $CREDENTIAL_PROVIDER_ARN"
            else
                echo "   ⚠️ Failed to create credential provider: $CRED_RESULT"
                SKIP_GATEWAY=true
            fi
        else
            echo "   Reusing existing credential provider: $CREDENTIAL_PROVIDER_ARN"
        fi
    fi

    if [ "$SKIP_GATEWAY" = "false" ] && [ -n "${CONNECT_ALIAS:-}" ]; then

        # Step 8.3: Construct Connect Discovery URL
        DISCOVERY_URL="https://${CONNECT_ALIAS}.my.connect.aws/.well-known/openid-configuration"
        echo "   Discovery URL: $DISCOVERY_URL"

        # Step 8.4: Create Gateway (with placeholder audience)
        echo "   Creating Gateway: $GATEWAY_NAME"

        # Check for existing gateway
        EXISTING_GW=$(aws bedrock-agentcore-control list-gateways \
            --region "$REGION" --output json 2>/dev/null || echo '{"gateways":[]}')

        GATEWAY_ID=$(echo "$EXISTING_GW" | python3 -c "
import sys, json
gateways = json.load(sys.stdin).get('gateways', [])
for gw in gateways:
    if gw.get('name') == '${GATEWAY_NAME}':
        print(gw['gatewayId'])
        break
else:
    print('')
" 2>/dev/null || echo "")

        if [ -z "$GATEWAY_ID" ]; then
            GW_RESULT=$(aws bedrock-agentcore-control create-gateway \
                --name "$GATEWAY_NAME" \
                --role-arn "$GATEWAY_ROLE_ARN" \
                --protocol-type MCP \
                --authorizer-type CUSTOM_JWT \
                --authorizer-configuration "{
                    \"customJWTAuthorizer\": {
                        \"discoveryUrl\": \"${DISCOVERY_URL}\",
                        \"allowedAudience\": [\"placeholder\"]
                    }
                }" \
                --region "$REGION" --output json 2>&1)

            GATEWAY_ID=$(echo "$GW_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('gatewayId',''))" 2>/dev/null || echo "")

            if [ -n "$GATEWAY_ID" ]; then
                echo "   Created Gateway: $GATEWAY_ID"
            else
                echo "   ⚠️ Failed to create gateway: $GW_RESULT"
                SKIP_GATEWAY=true
            fi
        else
            echo "   Reusing existing Gateway: $GATEWAY_ID"
        fi
    fi

    if [ "$SKIP_GATEWAY" = "false" ] && [ -n "${GATEWAY_ID:-}" ]; then

        # Step 8.5: Wait for Gateway to become READY
        echo -n "   Waiting for Gateway to become READY..."
        for i in $(seq 1 60); do
            GW_STATUS=$(aws bedrock-agentcore-control get-gateway \
                --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
                --query 'status' --output text 2>/dev/null || echo "UNKNOWN")
            if [ "$GW_STATUS" = "READY" ] || [ "$GW_STATUS" = "ACTIVE" ]; then
                echo " ✅"
                break
            fi
            if [ "$GW_STATUS" = "FAILED" ]; then
                echo " ❌ Gateway creation failed."
                SKIP_GATEWAY=true
                break
            fi
            echo -n "."
            sleep 10
        done

        # Get Gateway ARN
        GATEWAY_ARN=$(aws bedrock-agentcore-control get-gateway \
            --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
            --query 'gatewayArn' --output text 2>/dev/null || echo "")
    fi

    # =============================================================================
    # Step 9: Update Gateway Audience (placeholder -> actual gateway ID)
    #   Always runs — ensures audience is set correctly even for reused gateways
    # =============================================================================
    if [ "$SKIP_GATEWAY" = "false" ] && [ -n "${GATEWAY_ID:-}" ]; then
        echo ""
        echo "🔄 Step 9: Updating Gateway JWT audience to gateway ID..."

        aws bedrock-agentcore-control update-gateway \
            --gateway-identifier "$GATEWAY_ID" \
            --name "$GATEWAY_NAME" \
            --authorizer-type CUSTOM_JWT \
            --authorizer-configuration "{
                \"customJWTAuthorizer\": {
                    \"discoveryUrl\": \"${DISCOVERY_URL}\",
                    \"allowedAudience\": [\"${GATEWAY_ID}\"]
                }
            }" \
            --region "$REGION" --output text --query 'gatewayId' &>/dev/null || true

        # Wait for gateway to return to READY after update
        echo -n "   Waiting for Gateway to become READY after audience update..."
        for i in $(seq 1 30); do
            GW_STATUS=$(aws bedrock-agentcore-control get-gateway \
                --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
                --query 'status' --output text 2>/dev/null || echo "UNKNOWN")
            if [ "$GW_STATUS" = "READY" ] || [ "$GW_STATUS" = "ACTIVE" ]; then
                echo " ✅"
                break
            fi
            echo -n "."
            sleep 5
        done

        echo "   ✅ Audience updated to: $GATEWAY_ID"

        # Step 9.1: Create or reconcile Gateway Target (OpenAPI from S3)
        echo "   Creating Gateway Target..."

        EXPECTED_S3_URI="s3://${KB_BUCKET}/openapi/openapi.yaml"

        TARGET_CONFIG=$(cat <<TCEOF
{
    "mcp": {
        "openApiSchema": {
            "s3": {
                "uri": "${EXPECTED_S3_URI}"
            }
        }
    }
}
TCEOF
)

        CRED_CONFIG=$(cat <<CCEOF
[{
    "credentialProviderType": "API_KEY",
    "credentialProvider": {
        "apiKeyCredentialProvider": {
            "providerArn": "${CREDENTIAL_PROVIDER_ARN}",
            "credentialParameterName": "x-api-key",
            "credentialLocation": "HEADER"
        }
    }
}]
CCEOF
)

        # List existing targets and find one with our name.
        EXISTING_TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
            --gateway-identifier "$GATEWAY_ID" \
            --region "$REGION" --output json 2>/dev/null || echo '{"targets":[]}')

        # Resolve existing target's id + current S3 URI in one pass so we can
        # reconcile rather than silently skip when the bucket name drifts
        # (e.g., stack recreated in a different region/env → KnowledgeBaseBucketName
        # changed from `${PROJECT}-dev-kb-...` to `${PROJECT}-ap-northeast-2-kb-...`
        # but the old Gateway Target still points at the dead bucket → MCP reads
        # a 404 for openapi.yaml until someone fixes it in the console).
        TARGET_INFO=$(echo "$EXISTING_TARGETS" | TARGET_NAME="$TARGET_NAME" python3 -c "
import sys, json, os
name = os.environ['TARGET_NAME']
data = json.load(sys.stdin)
for t in data.get('targets', []):
    if t.get('name') == name:
        tid = t.get('targetId') or t.get('id') or ''
        cfg = t.get('targetConfiguration') or {}
        uri = ((cfg.get('mcp') or {}).get('openApiSchema') or {}).get('s3', {}).get('uri') or ''
        print(tid + '\t' + uri)
        break
else:
    print('\t')
" 2>/dev/null || echo $'\t')

        EXISTING_TARGET_ID=$(echo "$TARGET_INFO" | cut -f1)
        EXISTING_S3_URI=$(echo "$TARGET_INFO" | cut -f2)

        if [ -z "$EXISTING_TARGET_ID" ]; then
            TARGET_RESULT=$(aws bedrock-agentcore-control create-gateway-target \
                --gateway-identifier "$GATEWAY_ID" \
                --name "$TARGET_NAME" \
                --target-configuration "$TARGET_CONFIG" \
                --credential-provider-configurations "$CRED_CONFIG" \
                --region "$REGION" --output json 2>&1)

            if echo "$TARGET_RESULT" | python3 -c "import sys,json; json.load(sys.stdin)" &>/dev/null; then
                echo "   ✅ Gateway Target created -> $EXPECTED_S3_URI"
            else
                echo "   ⚠️ Failed to create target: $TARGET_RESULT"
            fi
        elif [ "$EXISTING_S3_URI" != "$EXPECTED_S3_URI" ]; then
            # Bucket name drifted — update in place so MCP reads the live OpenAPI.
            echo "   ⚠️ Existing Gateway Target points to a stale S3 URI:"
            echo "        was:  $EXISTING_S3_URI"
            echo "        want: $EXPECTED_S3_URI"
            echo "   Updating in place..."
            UPDATE_RESULT=$(aws bedrock-agentcore-control update-gateway-target \
                --gateway-identifier "$GATEWAY_ID" \
                --target-id "$EXISTING_TARGET_ID" \
                --name "$TARGET_NAME" \
                --target-configuration "$TARGET_CONFIG" \
                --credential-provider-configurations "$CRED_CONFIG" \
                --region "$REGION" --output json 2>&1)

            if echo "$UPDATE_RESULT" | python3 -c "import sys,json; json.load(sys.stdin)" &>/dev/null; then
                echo "   ✅ Gateway Target updated -> $EXPECTED_S3_URI"
            else
                echo "   ⚠️ Failed to update target: $UPDATE_RESULT"
                echo "      Fix by hand in the Bedrock AgentCore console, or delete"
                echo "      the target (\"$TARGET_NAME\") and re-run deploy.sh."
            fi
        else
            echo "   Gateway Target already exists and points to the current bucket ✅"
        fi
    fi

    # =============================================================================
    # Summary
    # =============================================================================
    echo ""
    echo "============================================="
    echo "  ✅ Deployment Complete!"
    echo "============================================="
    echo ""
    echo "  Project:           $PROJECT_NAME"
    echo "  Region:            $REGION"
    echo "  API Endpoint:      $API_ENDPOINT"
    echo "  API Key:           ${API_KEY:0:10}..."
    echo ""
    echo "  Connect Instance:  ${CONNECT_INSTANCE_ID:-N/A}"
    echo "  Connect Alias:     ${CONNECT_ALIAS:-N/A}"
    echo "  AI Assistant ID:   ${AI_ASSISTANT_ID:-N/A}"
    if [ -n "${GATEWAY_ID:-}" ]; then
    echo "  Gateway ID:        $GATEWAY_ID"
    echo "  Gateway ARN:       ${GATEWAY_ARN:-N/A}"
    fi
    echo ""

    # Contact Flow placeholders
    if [ -d "$SCRIPT_DIR/contact-flow" ]; then
        echo "  ─── Contact Flow 설정 ───"
        echo ""
        echo "  Contact Flow JSON을 Import한 후 아래 placeholder를 교체하세요:"
        echo ""
        [ -n "${CUSTOMER_LOOKUP_ARN:-}" ] && echo "  {{CUSTOMER_LOOKUP_LAMBDA_ARN}}"
        [ -n "${CUSTOMER_LOOKUP_ARN:-}" ] && echo "    -> $CUSTOMER_LOOKUP_ARN"
        [ -n "${CUSTOMER_LOOKUP_ARN:-}" ] && echo ""
        [ -n "${UPDATE_Q_SESSION_ARN:-}" ] && echo "  {{UPDATE_Q_SESSION_LAMBDA_ARN}}"
        [ -n "${UPDATE_Q_SESSION_ARN:-}" ] && echo "    -> $UPDATE_Q_SESSION_ARN"
        [ -n "${UPDATE_Q_SESSION_ARN:-}" ] && echo ""
        echo "  {{LEX_BOT_ALIAS_ARN}}  -> Connect > Bot 연결 후 ARN"
        echo "  {{QUEUE_ARN}}          -> Connect > Routing > Queues > ARN"
        if [ -n "${AI_ASSISTANT_ID:-}" ] && [ -n "${CONNECT_INSTANCE_ARN:-}" ]; then
            echo ""
            echo "  {{WISDOM_ASSISTANT_ARN}}"
            echo "    -> arn:aws:wisdom:${REGION}:${ACCOUNT_ID}:assistant/${AI_ASSISTANT_ID}"
        fi
        echo ""
    fi

    echo "  ─── 남은 수동 작업 ───"
    echo ""
    echo "  1. Connect > Third-party applications에서 MCP Server 연결"
    echo "  2. AI Agent 프롬프트 설정 (prompts/ai_agent_prompt.yaml 내용 붙여넣기)"
    echo "  3. Contact Flow Import + Lex Bot 연결 + 전화번호 할당"
    if [ -d "$SCRIPT_DIR/faq" ]; then
        echo "  4. Knowledge Base S3 Data Source 연결 (콘솔에서 진행)"
    fi
    echo ""
    echo "  ※ 리소스 정리: ./deploy.sh cleanup"
    echo ""
    echo "============================================="
}

# #############################################################################
#  Main: Route to command
# #############################################################################
case "$COMMAND" in
    deploy)
        do_deploy
        ;;
    cleanup|clean|destroy|delete)
        do_cleanup
        ;;
    status)
        do_status
        ;;
    *)
        echo "Usage: $0 {deploy|cleanup|status}"
        echo ""
        echo "  deploy   Deploy all workshop assets (default)"
        echo "  cleanup  Tear down ALL deployed resources"
        echo "  status   Show current deployment status"
        exit 1
        ;;
esac
