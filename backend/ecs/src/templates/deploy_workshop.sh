#!/bin/bash
# =============================================================================
# AICC Builder - Fully Automated Interactive Deployment (CloudShell)
#
# Deploys the ENTIRE workshop end-to-end from the CLI — including everything
# that previously required console work (Gateway audience, MCP integration,
# Lex bot with Nova Sonic, Contact Flow import, AI Agent, phone number).
# Every decision point is an interactive multiple-choice prompt.
#
# Commands:
#   ./deploy.sh           Deploy all workshop assets (default)
#   ./deploy.sh cleanup   Tear down ALL deployed resources (reverse order)
#   ./deploy.sh status    Show current deployment status
#
# Phases:
#    1. CloudFormation stack (S3 upload for large templates)
#    2. Lambda function code updates (with retry)
#    3. OpenAPI spec update & S3 upload
#    4. FAQ document upload
#    5. Amazon Connect instance create/select (interactive)
#    6. Q in Connect Assistant + Knowledge Base
#    7. Lambda environment variable injection
#    8. AgentCore Gateway (MCP Server) + JWT audience + Target
#    9. Connect integrations: MCP server registration + Lambda associations
#   10. Lex bot (language / Nova Sonic speech-to-speech / TTS voice choices)
#   11. Contact Flow import (placeholder auto-resolution + voice + language)
#   12. AI Prompt + AI Agent + Security Profile (tool permissions)
#   13. Phone number claim + flow association (optional, interactive)
#
# Environment variable overrides (all optional):
#   AWS_DEFAULT_REGION   - Deployment region (interactive if unset)
#   PROJECT_NAME         - Deployment name / resource prefix (interactive if unset;
#                          use different names for side-by-side deployments)
#   CONNECT_INSTANCE_ID  - Skip Connect instance selection
#   AI_ASSISTANT_ID      - Skip Q in Connect assistant creation
#   AUTO_CONFIRM=1       - Non-interactive: accept all defaults, skip phone
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMAND="${1:-deploy}"
STATE_FILE="$SCRIPT_DIR/.aicc_deploy_state"

# Auto-detect project name from CloudFormation template directory
DETECTED_PROJECT_NAME=""
if [ -d "$SCRIPT_DIR/cloudformation" ]; then
    first_dir=$(ls "$SCRIPT_DIR/cloudformation" 2>/dev/null | head -1)
    if [ -n "$first_dir" ] && [ -d "$SCRIPT_DIR/cloudformation/$first_dir" ]; then
        DETECTED_PROJECT_NAME="$first_dir"
    fi
fi
DETECTED_PROJECT_NAME="${DETECTED_PROJECT_NAME:-aicc-poc}"
# Deployment name resolution: env var > previously used (state file) > detected.
# The deploy command additionally asks interactively (see preflight); every
# resource name (stack, buckets, IAM role, gateway, bot, flow, agent, ...)
# derives from this name, so distinct names = fully isolated deployments.
PROJECT_NAME="${PROJECT_NAME:-}"
ENVIRONMENT="dev"

# =============================================================================
# Interactive helpers
# =============================================================================
IS_TTY=false
[ -t 0 ] && IS_TTY=true
AUTO="${AUTO_CONFIRM:-}"

say()  { echo "$@"; }
info() { echo "   $*"; }
warn() { echo "   ⚠️  $*"; }
ok()   { echo "   ✅ $*"; }
hr()   { echo "============================================="; }

# choose "Prompt" "default_index(1-based)" "opt1" "opt2" ...
#   -> sets CHOICE (1-based index) and CHOICE_VALUE (option text)
choose() {
    local prompt="$1"; shift
    local default_idx="$1"; shift
    local opts=("$@")
    local n=${#opts[@]}
    echo ""
    echo "   ❓ $prompt"
    local i=1
    for opt in "${opts[@]}"; do
        if [ "$i" -eq "$default_idx" ]; then
            echo "      [$i] $opt   (default)"
        else
            echo "      [$i] $opt"
        fi
        i=$((i+1))
    done
    if [ -n "$AUTO" ] || [ "$IS_TTY" = "false" ]; then
        CHOICE=$default_idx
        CHOICE_VALUE="${opts[$((default_idx-1))]}"
        echo "      -> auto-selected: [$CHOICE] $CHOICE_VALUE"
        return 0
    fi
    while true; do
        read -r -p "      Select [1-$n] (Enter=default $default_idx): " sel
        sel="${sel:-$default_idx}"
        if [[ "$sel" =~ ^[0-9]+$ ]] && [ "$sel" -ge 1 ] && [ "$sel" -le "$n" ]; then
            CHOICE=$sel
            CHOICE_VALUE="${opts[$((sel-1))]}"
            return 0
        fi
        echo "      Invalid input. Enter a number between 1 and $n."
    done
}

# ask_yn "Prompt" "y|n(default)" -> returns 0 for yes, 1 for no
ask_yn() {
    local prompt="$1"
    local def="${2:-y}"
    local hint="[Y/n]"
    [ "$def" = "n" ] && hint="[y/N]"
    if [ -n "$AUTO" ] || [ "$IS_TTY" = "false" ]; then
        echo "   ❓ $prompt $hint -> auto: $def"
        [ "$def" = "y" ] && return 0 || return 1
    fi
    read -r -p "   ❓ $prompt $hint: " ans
    ans=$(echo "${ans:-$def}" | tr '[:upper:]' '[:lower:]')
    case "$ans" in y|yes) return 0 ;; *) return 1 ;; esac
}

# ask_text "Prompt" "default" -> sets ANSWER
ask_text() {
    local prompt="$1"
    local def="${2:-}"
    if [ -n "$AUTO" ] || [ "$IS_TTY" = "false" ]; then
        ANSWER="$def"
        echo "   ❓ $prompt -> auto: $def"
        return 0
    fi
    read -r -p "   ❓ $prompt${def:+ (default: $def)}: " ANSWER
    ANSWER="${ANSWER:-$def}"
}

# Delete every object version + delete marker from a versioned bucket
# (plain `aws s3 rm --recursive` leaves versions behind -> bucket delete fails)
purge_bucket_versions() {
    local bucket="$1"
    python3 - "$bucket" "$REGION" <<'PYEOF' 2>/dev/null || true
import sys, subprocess, json
bucket, region = sys.argv[1], sys.argv[2]
def run(*args):
    r = subprocess.run(["aws"] + list(args) + ["--region", region, "--output", "json"],
                       capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout.strip() else {}
while True:
    d = run("s3api", "list-object-versions", "--bucket", bucket, "--max-items", "500")
    objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
            for k in ("Versions", "DeleteMarkers") for o in d.get(k, [])]
    if not objs:
        break
    for i in range(0, len(objs), 500):
        run("s3api", "delete-objects", "--bucket", bucket,
            "--delete", json.dumps({"Objects": objs[i:i+500], "Quiet": True}))
PYEOF
}

# =============================================================================
# State file (records created resource IDs for idempotent re-runs & cleanup)
# =============================================================================
state_get() { [ -f "$STATE_FILE" ] && grep "^$1=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
state_set() {
    touch "$STATE_FILE"
    grep -v "^$1=" "$STATE_FILE" > "$STATE_FILE.tmp" 2>/dev/null || true
    echo "$1=$2" >> "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# =============================================================================
# JSON helpers (python3 is preinstalled on CloudShell)
# =============================================================================
jget() {
    # jget '<json>' '<dotted.path or [idx]>'  — prints value or empty
    # JSON is passed via env var to avoid any shell quoting issues
    J="$1" P="$2" python3 -c "
import os, json
try:
    cur = json.loads(os.environ['J'])
    for part in os.environ['P'].replace(']', '').replace('[', '.').split('.'):
        if part == '': continue
        cur = cur[int(part)] if isinstance(cur, list) else cur.get(part)
        if cur is None: break
    if cur is not None:
        print(cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False))
except Exception:
    pass
"
}

get_output() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || echo ""
}

# Lambda name resolution against actual stack resources
STACK_LAMBDA_NAMES=""
_load_stack_lambda_names() {
    [ -n "$STACK_LAMBDA_NAMES" ] && return 0
    STACK_LAMBDA_NAMES=$(aws cloudformation list-stack-resources \
        --stack-name "$STACK_NAME" --region "$REGION" \
        --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId" \
        --output text 2>/dev/null || echo "")
}
resolve_stack_function() {
    local dir_name="$1"
    _load_stack_lambda_names
    [ -z "$STACK_LAMBDA_NAMES" ] && { echo ""; return; }
    local needle fn norm
    needle=$(echo "$dir_name" | tr '[:upper:]' '[:lower:]' | tr -d '_-')
    for fn in $STACK_LAMBDA_NAMES; do
        norm=$(echo "$fn" | tr '[:upper:]' '[:lower:]' | tr -d '_-')
        case "$norm" in *"$needle"*) echo "$fn"; return ;; esac
    done
    echo ""
}

# =============================================================================
# Region + naming (region is chosen interactively in preflight for deploy)
# =============================================================================
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# finish deployment-name resolution (state file helpers are defined above)
[ -z "$PROJECT_NAME" ] && PROJECT_NAME="$(state_get PROJECT_NAME)"
[ -z "$PROJECT_NAME" ] && PROJECT_NAME="$DETECTED_PROJECT_NAME"

set_names() {
    STACK_NAME="${PROJECT_NAME}-stack"
    TEMPLATE_BUCKET="${PROJECT_NAME}-cfn-${REGION}-${ACCOUNT_ID}"
    ROLE_NAME="${PROJECT_NAME}-gateway-role"
    CRED_PROVIDER_NAME="${PROJECT_NAME}-api-key"
    GATEWAY_NAME="${PROJECT_NAME}-mcp-server"
    TARGET_NAME="${PROJECT_NAME}-api"
    KB_NAME="${PROJECT_NAME}-faq-kb"
    MCP_APP_NAME="${PROJECT_NAME}-mcp"
    BOT_NAME=$(echo "${PROJECT_NAME}" | sed 's/[^0-9a-zA-Z]/_/g')Bot
    FLOW_NAME="${PROJECT_NAME}-flow"
    PROMPT_NAME="${PROJECT_NAME}-orchestration-prompt"
    AGENT_NAME="${PROJECT_NAME}-agent"
    SP_NAME=$(echo "${PROJECT_NAME}" | sed 's/[^0-9a-zA-Z_-]/-/g')-ai-agent
    UPDATE_Q_FUNC="${PROJECT_NAME}-update-qsession-${ENVIRONMENT}"
}
set_names

# Find CloudFormation template (nested or flat structure)
CFN_TEMPLATE=""
if [ -f "$SCRIPT_DIR/cloudformation/$DETECTED_PROJECT_NAME/infrastructure.yaml" ]; then
    CFN_TEMPLATE="$SCRIPT_DIR/cloudformation/$DETECTED_PROJECT_NAME/infrastructure.yaml"
elif [ -f "$SCRIPT_DIR/cloudformation/infrastructure.yaml" ]; then
    CFN_TEMPLATE="$SCRIPT_DIR/cloudformation/infrastructure.yaml"
fi

# Contact flow asset
FLOW_JSON=""
if [ -d "$SCRIPT_DIR/contact-flow" ]; then
    FLOW_JSON=$(find "$SCRIPT_DIR/contact-flow" -name "*.json" | head -1)
fi

# AI prompt asset
PROMPT_FILE=""
if [ -d "$SCRIPT_DIR/prompts" ]; then
    PROMPT_FILE=$(find "$SCRIPT_DIR/prompts" -name "*.yaml" -o -name "*.yml" -o -name "*.md" 2>/dev/null | head -1)
fi

# OpenAPI asset
OPENAPI_FILE=""
if [ -d "$SCRIPT_DIR/openapi" ]; then
    OPENAPI_FILE=$(find "$SCRIPT_DIR/openapi" -name "*.yaml" -o -name "*.yml" 2>/dev/null | head -1)
fi

# =============================================================================
# Phase 0: Preflight — asset scan, region & account confirmation
# =============================================================================
do_preflight() {
    hr
    echo "  AICC Builder - Full Automation Deployment"
    hr
    echo ""
    echo "  📂 Asset scan results:"
    [ -n "$CFN_TEMPLATE" ]  && echo "     ✅ CloudFormation:  ${CFN_TEMPLATE#$SCRIPT_DIR/}" || echo "     ❌ CloudFormation template NOT FOUND"
    if [ -d "$SCRIPT_DIR/lambda" ]; then
        echo "     ✅ Lambda:          $(ls "$SCRIPT_DIR/lambda" | tr '\n' ' ')"
    fi
    [ -n "$OPENAPI_FILE" ]  && echo "     ✅ OpenAPI:         ${OPENAPI_FILE#$SCRIPT_DIR/}"
    [ -n "$FLOW_JSON" ]     && echo "     ✅ Contact Flow:    ${FLOW_JSON#$SCRIPT_DIR/}"
    [ -n "$PROMPT_FILE" ]   && echo "     ✅ AI Prompt:       ${PROMPT_FILE#$SCRIPT_DIR/}"
    [ -d "$SCRIPT_DIR/faq" ] && echo "     ✅ FAQ:             faq/"

    # Detect language from contact flow set-voice metadata or flow_config
    DETECTED_LANG=""
    if [ -n "$FLOW_JSON" ]; then
        DETECTED_LANG=$(python3 - "$FLOW_JSON" <<'PYEOF' 2>/dev/null || true
import sys, json, re
d = json.load(open(sys.argv[1]))
# 1) Metadata set-voice languageCode
meta = d.get('Metadata', {}).get('ActionMetadata', {})
for k, v in meta.items():
    p = v.get('parameters', {}).get('TextToSpeechVoice', {})
    if isinstance(p, dict) and p.get('languageCode'):
        print(p['languageCode']); sys.exit()
# 2) any ko/ja/etc. hint in TTS voice names is unreliable; give up
PYEOF
)
    fi
    if [ -z "$DETECTED_LANG" ] && [ -f "$SCRIPT_DIR/state/flow_config.json" ]; then
        # Heuristic: greeting language detection (Hangul/Kana/Han)
        DETECTED_LANG=$(python3 - "$SCRIPT_DIR/state/flow_config.json" <<'PYEOF' 2>/dev/null || true
import sys, json, re
d = json.load(open(sys.argv[1]))
text = (d.get('common_greeting') or '') + (d.get('agent_persona') or '')
if re.search(r'[가-힣]', text): print('ko-KR')
elif re.search(r'[ぁ-んァ-ン]', text): print('ja-JP')
elif re.search(r'[一-鿿]', text): print('zh-CN')
else: print('en-US')
PYEOF
)
    fi
    DETECTED_LANG="${DETECTED_LANG:-en-US}"
    echo "     🌐 Detected language: $DETECTED_LANG"
    echo ""

    # Region selection
    local cfg_region
    cfg_region=$(aws configure get region 2>/dev/null || echo "")
    if [ -n "${AWS_DEFAULT_REGION:-}" ]; then
        REGION="$AWS_DEFAULT_REGION"
        info "Region (env): $REGION"
    else
        local opts=()
        local default_idx=1
        opts+=("us-west-2  (workshop default — Nova Sonic supported)")
        [ -n "$cfg_region" ] && [ "$cfg_region" != "us-west-2" ] && opts+=("$cfg_region  (current AWS CLI default region)")
        opts+=("Enter manually")
        choose "Which region do you want to deploy to?" "$default_idx" "${opts[@]}"
        case "$CHOICE_VALUE" in
            "Enter manually") ask_text "Region code (e.g. ap-northeast-2)" "us-west-2"; REGION="$ANSWER" ;;
            *) REGION=$(echo "$CHOICE_VALUE" | awk '{print $1}') ;;
        esac
    fi
    set_names

    # ── Deployment name (prefix for the stack and EVERY resource) ───────────
    #    Distinct names = fully isolated side-by-side deployments in one account
    #    (stack, S3 buckets, IAM role, gateway, bot, flow, AI agent, ...)
    while true; do
        ask_text "Deployment name — used as the stack/resource prefix (a-z, 0-9, '-', 3-24 chars)" "$PROJECT_NAME"
        CANDIDATE=$(echo "$ANSWER" | tr '[:upper:]' '[:lower:]')
        if echo "$CANDIDATE" | grep -qE '^[a-z][a-z0-9-]{2,23}$'; then
            PROJECT_NAME="$CANDIDATE"
            break
        fi
        echo "      Invalid name: must start with a letter, only a-z 0-9 '-', 3-24 chars."
        [ -n "$AUTO" ] && { PROJECT_NAME="$DETECTED_PROJECT_NAME"; break; }
    done
    set_names
    state_set PROJECT_NAME "$PROJECT_NAME"
    info "Deployment name: $PROJECT_NAME  (stack: $STACK_NAME)"

    echo ""
    echo "  Project: $PROJECT_NAME"
    echo "  Region:  $REGION"
    echo "  Account: $ACCOUNT_ID"
    echo "  Caller:  $(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo '?')"
    echo ""
    if ! ask_yn "Proceed with this account/region?" "y"; then
        echo "  Aborted."
        exit 0
    fi
}

# =============================================================================
# Phase 1: CloudFormation
# =============================================================================
phase_cloudformation() {
    echo ""
    echo "📦 Phase 1: Deploying CloudFormation stack..."

    if ! aws s3 ls "s3://$TEMPLATE_BUCKET" --region "$REGION" &>/dev/null; then
        info "Creating S3 bucket for templates..."
        if [ "$REGION" = "us-east-1" ]; then
            aws s3api create-bucket --bucket "$TEMPLATE_BUCKET" --region "$REGION" >/dev/null
        else
            aws s3api create-bucket --bucket "$TEMPLATE_BUCKET" --region "$REGION" \
                --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
        fi
        aws s3api put-bucket-versioning --bucket "$TEMPLATE_BUCKET" \
            --versioning-configuration Status=Enabled --region "$REGION"
    fi

    TEMPLATE_KEY="infrastructure-$(date +%Y%m%d-%H%M%S).yaml"
    info "Uploading template: s3://$TEMPLATE_BUCKET/$TEMPLATE_KEY"
    aws s3 cp "$CFN_TEMPLATE" "s3://$TEMPLATE_BUCKET/$TEMPLATE_KEY" --region "$REGION" >/dev/null
    TEMPLATE_URL="https://s3.${REGION}.amazonaws.com/${TEMPLATE_BUCKET}/${TEMPLATE_KEY}"

    # Un-updatable terminal states (failed first creation etc.) must be
    # deleted before anything else can proceed.
    CUR_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "")
    if echo "$CUR_STATUS" | grep -qE '^(ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED)$'; then
        warn "Existing stack is in $CUR_STATUS (failed creation) — deleting it first"
        aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
        aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null || true
    fi

    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
        # A same-named stack exists. It may be from an OLDER asset bundle
        # (different resources / Environment) — blindly reusing it poisons
        # every later phase with stale outputs. Check compatibility first.
        local EXISTING_ENV
        EXISTING_ENV=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
            --query "Stacks[0].Parameters[?ParameterKey=='Environment'].ParameterValue" --output text 2>/dev/null || echo "")
        [ -n "$EXISTING_ENV" ] && [ "$EXISTING_ENV" != "$ENVIRONMENT" ] && \
            warn "Existing stack was deployed with Environment=$EXISTING_ENV (this bundle: $ENVIRONMENT) — likely from an older asset bundle"

        info "Stack exists, updating..."
        UPDATE_ERR=$(aws cloudformation update-stack \
            --stack-name "$STACK_NAME" --template-url "$TEMPLATE_URL" \
            --parameters ParameterKey=ProjectName,ParameterValue="$PROJECT_NAME" \
                         ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
            --capabilities CAPABILITY_NAMED_IAM --region "$REGION" 2>&1) || true
        if echo "$UPDATE_ERR" | grep -q '"StackId"'; then
            info "Waiting for stack update..."
            if ! aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME" --region "$REGION"; then
                warn "Stack update FAILED (rolled back). The existing stack is incompatible with this bundle."
                UPDATE_ERR="__ROLLBACK__"
            fi
        fi
        if echo "$UPDATE_ERR" | grep -q "No updates are to be performed"; then
            info "No updates needed."
        elif echo "$UPDATE_ERR" | grep -qE "__ROLLBACK__|Update of resource type is not permitted|cannot be updated"; then
            # Incompatible existing stack (e.g. created by a different asset bundle)
            warn "Existing stack '$STACK_NAME' is incompatible with this bundle's template:"
            echo "$UPDATE_ERR" | grep -v '^__' | head -2 | sed 's/^/        /'
            choose "How do you want to handle the existing stack?" 1 \
                "Delete and recreate it with this bundle's template  (recommended for workshops)" \
                "Keep using the existing stack as-is  (outputs may not match this bundle!)" \
                "Abort deployment"
            case "$CHOICE" in
                1)
                    info "Deleting old stack..."
                    OLD_KB_BUCKET=$(get_output "KnowledgeBaseBucketName")
                    if [ -n "$OLD_KB_BUCKET" ]; then
                        aws s3 rm "s3://$OLD_KB_BUCKET" --recursive --region "$REGION" >/dev/null 2>&1 || true
                        purge_bucket_versions "$OLD_KB_BUCKET"
                    fi
                    aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
                    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
                    # S3 bucket names free up asynchronously after deletion; a
                    # same-named bucket in the new stack can 409 for a while.
                    # Retry creation up to 3 times.
                    for create_try in 1 2 3; do
                        info "Creating new stack... (attempt $create_try/3)"
                        aws cloudformation create-stack \
                            --stack-name "$STACK_NAME" --template-url "$TEMPLATE_URL" \
                            --parameters ParameterKey=ProjectName,ParameterValue="$PROJECT_NAME" \
                                         ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
                            --capabilities CAPABILITY_NAMED_IAM --region "$REGION" >/dev/null
                        info "Waiting for stack creation... (5-10 min)"
                        if aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null; then
                            break
                        fi
                        FAIL_REASON=$(aws cloudformation describe-stack-events --stack-name "$STACK_NAME" --region "$REGION" \
                            --query "StackEvents[?ResourceStatus=='CREATE_FAILED'] | [0].ResourceStatusReason" --output text 2>/dev/null || echo "")
                        warn "Stack creation failed: $(echo "$FAIL_REASON" | head -c 160)"
                        aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
                        aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null || true
                        if [ "$create_try" = "3" ]; then
                            echo "❌ Stack creation failed after 3 attempts. Aborting."
                            exit 1
                        fi
                        if echo "$FAIL_REASON" | grep -q "conflicting conditional operation"; then
                            info "S3 bucket name still releasing — waiting 60s before retry..."
                            sleep 60
                        else
                            sleep 15
                        fi
                    done
                    ;;
                2)  warn "Continuing with the OLD stack — later phases may use stale outputs" ;;
                3)  echo "  Aborted."; exit 1 ;;
            esac
        elif ! echo "$UPDATE_ERR" | grep -q '"StackId"'; then
            warn "Stack update error: $(echo "$UPDATE_ERR" | head -2)"
        fi
    else
        info "Creating new stack..."
        aws cloudformation create-stack \
            --stack-name "$STACK_NAME" --template-url "$TEMPLATE_URL" \
            --parameters ParameterKey=ProjectName,ParameterValue="$PROJECT_NAME" \
                         ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
            --capabilities CAPABILITY_NAMED_IAM --region "$REGION" >/dev/null
        info "Waiting for stack creation... (5-10 min)"
        aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME" --region "$REGION"
    fi
    ok "CloudFormation stack ready"

    API_ENDPOINT=$(get_output "ApiEndpoint")
    API_KEY=$(get_output "ApiKeyValue")
    KB_BUCKET=$(get_output "KnowledgeBaseBucketName")
    CUSTOMER_LOOKUP_ARN=$(get_output "CustomerLookupFunctionArn")
    UPDATE_Q_SESSION_ARN=$(get_output "UpdateQSessionFunctionArn")
    if [ -n "${UPDATE_Q_SESSION_ARN:-}" ]; then
        UPDATE_Q_FUNC="$(echo "$UPDATE_Q_SESSION_ARN" | awk -F: '{print $NF}')"
    fi
    info "API Endpoint: $API_ENDPOINT"
}

# =============================================================================
# Phase 2: Lambda function code
# =============================================================================
phase_lambda_code() {
    echo ""
    echo "🔧 Phase 2: Updating Lambda function code..."
    [ -d "$SCRIPT_DIR/lambda" ] || { info "No lambda/ directory, skipping"; return 0; }

    arn_to_func_name() { echo "$1" | awk -F: '{print $NF}'; }

    for func_dir in "$SCRIPT_DIR/lambda"/*/; do
        [ -d "$func_dir" ] || continue
        func_name=$(basename "$func_dir")
        if [ -f "$func_dir/index.py" ]; then entry_file="index.py"
        elif [ -f "$func_dir/index.js" ]; then entry_file="index.js"
        else entry_file=$(ls "$func_dir" | head -1); fi
        [ -z "$entry_file" ] && continue

        case "$func_name" in
            customer_lookup)
                if [ -n "${CUSTOMER_LOOKUP_ARN:-}" ]; then aws_func="$(arn_to_func_name "$CUSTOMER_LOOKUP_ARN")"
                else aws_func=$(resolve_stack_function "$func_name"); fi ;;
            update_q_session)
                if [ -n "${UPDATE_Q_SESSION_ARN:-}" ]; then aws_func="$(arn_to_func_name "$UPDATE_Q_SESSION_ARN")"
                else aws_func=$(resolve_stack_function "$func_name"); fi ;;
            *)  aws_func=$(resolve_stack_function "$func_name") ;;
        esac

        if [ -z "$aws_func" ]; then
            warn "$func_name: no matching function in stack, skipping"
            continue
        fi

        echo -n "   $aws_func <- $func_name/$entry_file ... "
        (cd "$func_dir" && zip -qj /tmp/_deploy.zip "$entry_file")
        retry=0
        while [ $retry -lt 3 ]; do
            if aws lambda update-function-code --function-name "$aws_func" \
                --zip-file fileb:///tmp/_deploy.zip --region "$REGION" \
                --output text --query 'FunctionName' &>/dev/null; then
                aws lambda wait function-updated --function-name "$aws_func" --region "$REGION" 2>/dev/null || true
                echo "✅"; break
            else
                retry=$((retry+1))
                [ $retry -lt 3 ] && { echo -n "⏳ "; sleep 5; } || echo "⚠️ failed"
            fi
        done
        rm -f /tmp/_deploy.zip
    done
}

# =============================================================================
# Phase 3: OpenAPI spec update & upload
# =============================================================================
phase_openapi() {
    echo ""
    echo "📝 Phase 3: Updating OpenAPI spec..."
    [ -d "$SCRIPT_DIR/openapi" ] || { info "No openapi/ directory, skipping"; return 0; }

    API_HOST=$(echo "$API_ENDPOINT" | sed -E 's|^https?://||; s|/+$||; s|/tools/?$||')
    find "$SCRIPT_DIR/openapi" -name "*.yaml" | while read -r f; do
        sed -i.bak "s|{API_ENDPOINT}|$API_HOST|g" "$f" && rm -f "${f}.bak"
        info "Updated: ${f#$SCRIPT_DIR/}"
    done
    if [ -n "${KB_BUCKET:-}" ]; then
        aws s3 sync "$SCRIPT_DIR/openapi" "s3://$KB_BUCKET/openapi/" \
            --exclude "*.DS_Store" --exclude "*.bak" --region "$REGION" >/dev/null
        ok "Uploaded to s3://$KB_BUCKET/openapi/"
    fi
}

# =============================================================================
# Phase 4: FAQ upload
# =============================================================================
phase_faq() {
    echo ""
    echo "📚 Phase 4: Uploading FAQ documents..."
    FAQ_DIR="$SCRIPT_DIR/faq/knowledge_base"
    [ -d "$FAQ_DIR" ] || FAQ_DIR="$SCRIPT_DIR/faq"
    if [ -d "$FAQ_DIR" ] && [ -n "${KB_BUCKET:-}" ]; then
        count=$(find "$FAQ_DIR" -name "*.txt" | wc -l | tr -d ' ')
        aws s3 sync "$FAQ_DIR" "s3://$KB_BUCKET/faq/" --exclude "*.DS_Store" --region "$REGION" >/dev/null
        ok "$count documents -> s3://$KB_BUCKET/faq/"
    else
        info "No FAQ directory or KB bucket, skipping"
    fi
}

# =============================================================================
# Phase 5: Amazon Connect Instance — interactive create/select
# =============================================================================
phase_connect_instance() {
    echo ""
    echo "📞 Phase 5: Setting up Amazon Connect instance..."

    if [ -n "${CONNECT_INSTANCE_ID:-}" ]; then
        info "Using CONNECT_INSTANCE_ID from env: $CONNECT_INSTANCE_ID"
    else
        local saved
        saved=$(state_get CONNECT_INSTANCE_ID)
        INSTANCES_JSON=$(aws connect list-instances --region "$REGION" --output json 2>/dev/null || echo '{"InstanceSummaryList":[]}')
        INSTANCE_COUNT=$(jget "$INSTANCES_JSON" "InstanceSummaryList" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

        if [ "$INSTANCE_COUNT" -eq 0 ]; then
            info "No Connect instance found. Creating a new one..."
            ALIAS="aicc-workshop-${ACCOUNT_ID: -4}"
            CREATE_RESULT=$(aws connect create-instance \
                --identity-management-type "CONNECT_MANAGED" \
                --instance-alias "$ALIAS" \
                --inbound-calls-enabled --outbound-calls-enabled \
                --region "$REGION" --output json)
            CONNECT_INSTANCE_ID=$(jget "$CREATE_RESULT" "Id")
            info "Created: $CONNECT_INSTANCE_ID (alias: $ALIAS)"
            echo -n "   Waiting for ACTIVE..."
            for i in $(seq 1 60); do
                STATUS=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
                    --query 'Instance.InstanceStatus' --output text 2>/dev/null || echo "UNKNOWN")
                [ "$STATUS" = "ACTIVE" ] && { echo " ✅"; break; }
                echo -n "."; sleep 10
            done
        else
            # Build menu from all instances (+ create-new option)
            local menu=() ids=() default_idx=1 i=1
            while IFS=$'\t' read -r id alias; do
                menu+=("$alias ($id)")
                ids+=("$id")
                [ -n "$saved" ] && [ "$id" = "$saved" ] && default_idx=$i
                i=$((i+1))
            done < <(echo "$INSTANCES_JSON" | python3 -c "
import sys, json
for inst in json.load(sys.stdin)['InstanceSummaryList']:
    print(inst['Id'] + '\t' + inst.get('InstanceAlias','N/A'))
")
            menu+=("Create a new instance")
            choose "Which Connect instance do you want to deploy to?" "$default_idx" "${menu[@]}"
            if [ "$CHOICE_VALUE" = "Create a new instance" ]; then
                ask_text "New instance alias" "aicc-workshop-${ACCOUNT_ID: -4}"
                ALIAS="$ANSWER"
                CREATE_RESULT=$(aws connect create-instance \
                    --identity-management-type "CONNECT_MANAGED" \
                    --instance-alias "$ALIAS" \
                    --inbound-calls-enabled --outbound-calls-enabled \
                    --region "$REGION" --output json)
                CONNECT_INSTANCE_ID=$(jget "$CREATE_RESULT" "Id")
                echo -n "   Waiting for ACTIVE..."
                for i in $(seq 1 60); do
                    STATUS=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
                        --query 'Instance.InstanceStatus' --output text 2>/dev/null || echo "UNKNOWN")
                    [ "$STATUS" = "ACTIVE" ] && { echo " ✅"; break; }
                    echo -n "."; sleep 10
                done
            else
                CONNECT_INSTANCE_ID="${ids[$((CHOICE-1))]}"
            fi
        fi
    fi
    state_set CONNECT_INSTANCE_ID "$CONNECT_INSTANCE_ID"

    # Enable instance attributes.
    #   BOT_MANAGEMENT + ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS unlock the Connect
    #   console bot-building experience (Flows > Bots tab) so Lex bots can be
    #   viewed/managed through Connect — without them the tab is hidden and the
    #   bot appears "uncontrollable" from the Connect console.
    #   MESSAGE_STREAMING reduces AI agent response latency.
    info "Enabling instance attributes..."
    for attr in ENHANCED_CONTACT_MONITORING USE_CUSTOM_TTS_VOICES AUTO_RESOLVE_BEST_VOICES \
                 CONTACTFLOW_LOGS CONTACT_LENS MULTI_PARTY_CONFERENCE \
                 BOT_MANAGEMENT ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS \
                 HIGH_VOLUME_OUTBOUND EARLY_MEDIA MESSAGE_STREAMING; do
        aws connect update-instance-attribute \
            --instance-id "$CONNECT_INSTANCE_ID" --attribute-type "$attr" \
            --value "true" --region "$REGION" 2>/dev/null || true
    done

    # Storage config for recordings/transcripts
    CONNECT_STORAGE_BUCKET="${PROJECT_NAME}-connect-${ACCOUNT_ID}-${REGION}"
    if ! aws s3 ls "s3://$CONNECT_STORAGE_BUCKET" --region "$REGION" &>/dev/null; then
        if [ "$REGION" = "us-east-1" ]; then
            aws s3api create-bucket --bucket "$CONNECT_STORAGE_BUCKET" --region "$REGION" >/dev/null 2>&1 || true
        else
            aws s3api create-bucket --bucket "$CONNECT_STORAGE_BUCKET" --region "$REGION" \
                --create-bucket-configuration LocationConstraint="$REGION" >/dev/null 2>&1 || true
        fi
    fi
    for STORAGE_TYPE in CALL_RECORDINGS CHAT_TRANSCRIPTS; do
        aws connect associate-instance-storage-config \
            --instance-id "$CONNECT_INSTANCE_ID" --resource-type "$STORAGE_TYPE" \
            --storage-config "{\"StorageType\":\"S3\",\"S3Config\":{\"BucketName\":\"${CONNECT_STORAGE_BUCKET}\",\"BucketPrefix\":\"${STORAGE_TYPE}\"}}" \
            --region "$REGION" 2>/dev/null || true
    done

    CONNECT_ALIAS=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
        --query 'Instance.InstanceAlias' --output text 2>/dev/null || echo "")
    CONNECT_INSTANCE_ARN=$(aws connect describe-instance --instance-id "$CONNECT_INSTANCE_ID" --region "$REGION" \
        --query 'Instance.Arn' --output text 2>/dev/null || echo "")
    ok "Connect instance: $CONNECT_INSTANCE_ID (alias: ${CONNECT_ALIAS:-N/A})"
}

# =============================================================================
# Phase 6: Q in Connect Assistant + Knowledge Base
# =============================================================================
phase_assistant() {
    echo ""
    echo "🤖 Phase 6: Setting up Q in Connect Assistant..."

    if [ -n "${AI_ASSISTANT_ID:-}" ]; then
        info "Using AI_ASSISTANT_ID from env: $AI_ASSISTANT_ID"
        ASSISTANT_ARN="arn:aws:wisdom:${REGION}:${ACCOUNT_ID}:assistant/${AI_ASSISTANT_ID}"
    else
        EXISTING_ASSOC=$(aws connect list-integration-associations \
            --instance-id "$CONNECT_INSTANCE_ID" --integration-type WISDOM_ASSISTANT \
            --region "$REGION" --output json 2>/dev/null || echo '{"IntegrationAssociationSummaryList":[]}')
        ASSOC_COUNT=$(echo "$EXISTING_ASSOC" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('IntegrationAssociationSummaryList',[])))" 2>/dev/null || echo 0)

        if [ "$ASSOC_COUNT" -gt 0 ]; then
            ASSISTANT_ARN=$(echo "$EXISTING_ASSOC" | python3 -c "import sys,json; print(json.load(sys.stdin)['IntegrationAssociationSummaryList'][0]['IntegrationArn'])")
            AI_ASSISTANT_ID=$(echo "$ASSISTANT_ARN" | awk -F'/' '{print $NF}')
            info "Using assistant already associated with instance: $AI_ASSISTANT_ID"
        else
            info "Creating new Q in Connect assistant..."
            CREATE_ASSISTANT=$(aws qconnect create-assistant \
                --name "${PROJECT_NAME}-assistant" --type AGENT \
                --description "AI assistant for ${PROJECT_NAME}" \
                --region "$REGION" --output json)
            AI_ASSISTANT_ID=$(jget "$CREATE_ASSISTANT" "assistant.assistantId")
            ASSISTANT_ARN=$(jget "$CREATE_ASSISTANT" "assistant.assistantArn")
            echo -n "   Waiting for ACTIVE..."
            for i in $(seq 1 30); do
                ASTATUS=$(aws qconnect get-assistant --assistant-id "$AI_ASSISTANT_ID" --region "$REGION" \
                    --query 'assistant.status' --output text 2>/dev/null || echo "UNKNOWN")
                [ "$ASTATUS" = "ACTIVE" ] && { echo " ✅"; break; }
                echo -n "."; sleep 5
            done
            info "Associating with Connect instance..."
            aws connect create-integration-association \
                --instance-id "$CONNECT_INSTANCE_ID" \
                --integration-type WISDOM_ASSISTANT \
                --integration-arn "$ASSISTANT_ARN" \
                --region "$REGION" >/dev/null 2>&1 || info "(already associated)"
        fi
    fi
    state_set AI_ASSISTANT_ID "$AI_ASSISTANT_ID"
    ok "Assistant: $AI_ASSISTANT_ID"

    # Knowledge Base (only when FAQ assets exist)
    if [ -d "$SCRIPT_DIR/faq" ] && [ -n "${AI_ASSISTANT_ID:-}" ]; then
        info "Setting up FAQ Knowledge Base..."
        EXISTING_KB=$(aws qconnect list-knowledge-bases --region "$REGION" --output json 2>/dev/null || echo '{"knowledgeBaseSummaries":[]}')
        KB_ID=$(echo "$EXISTING_KB" | python3 -c "
import sys, json
for kb in json.load(sys.stdin).get('knowledgeBaseSummaries', []):
    if kb.get('name') == '${KB_NAME}': print(kb['knowledgeBaseId']); break
" 2>/dev/null || echo "")
        if [ -z "$KB_ID" ]; then
            CREATE_KB=$(aws qconnect create-knowledge-base \
                --name "$KB_NAME" --knowledge-base-type CUSTOM \
                --description "FAQ knowledge base for ${PROJECT_NAME}" \
                --region "$REGION" --output json 2>&1) || true
            KB_ID=$(jget "$CREATE_KB" "knowledgeBase.knowledgeBaseId")
            [ -n "$KB_ID" ] && info "KB created: $KB_ID" || warn "KB creation failed"
        else
            info "Using existing KB: $KB_ID"
        fi
        if [ -n "$KB_ID" ]; then
            aws qconnect create-assistant-association \
                --assistant-id "$AI_ASSISTANT_ID" \
                --association-type KNOWLEDGE_BASE \
                --association "knowledgeBaseId=$KB_ID" \
                --region "$REGION" >/dev/null 2>&1 || info "(KB already associated)"
            ok "Knowledge Base associated"
        fi
    fi
}

# =============================================================================
# Phase 7: Lambda environment variables
# =============================================================================
phase_env_vars() {
    echo ""
    echo "🔑 Phase 7: Injecting Lambda environment variables..."
    # Prefer the exported ARN; fall back to fuzzy-matching the stack's Lambdas
    # (many generated templates don't export UpdateQSessionFunctionArn)
    if [ -z "${UPDATE_Q_SESSION_ARN:-}" ]; then
        resolved=$(resolve_stack_function "update_q_session")
        [ -z "$resolved" ] && resolved=$(resolve_stack_function "update_qsession")
        if [ -n "$resolved" ]; then
            UPDATE_Q_FUNC="$resolved"
            info "Resolved from stack resources: $UPDATE_Q_FUNC"
        else
            info "update_q_session Lambda not in stack, skipping"
            return 0
        fi
    fi
    EXISTING_ENV=$(aws lambda get-function-configuration \
        --function-name "$UPDATE_Q_FUNC" --region "$REGION" \
        --query 'Environment.Variables' --output json 2>/dev/null || echo '{}')
    MERGED_ENV=$(echo "$EXISTING_ENV" | python3 -c "
import sys, json
raw = sys.stdin.read().strip()
try: env = json.loads(raw) if raw not in ('', 'null', 'None') else {}
except Exception: env = {}
if not isinstance(env, dict): env = {}
env['CONNECT_INSTANCE_ID'] = '${CONNECT_INSTANCE_ID}'
env['AI_ASSISTANT_ID'] = '${AI_ASSISTANT_ID}'
print(json.dumps({'Variables': env}))
")
    retry=0
    while [ $retry -lt 3 ]; do
        if aws lambda update-function-configuration \
            --function-name "$UPDATE_Q_FUNC" --environment "$MERGED_ENV" \
            --region "$REGION" --output text --query 'FunctionName' &>/dev/null; then
            aws lambda wait function-updated --function-name "$UPDATE_Q_FUNC" --region "$REGION" 2>/dev/null || true
            ok "Environment variables injected ($UPDATE_Q_FUNC)"
            break
        fi
        retry=$((retry+1)); sleep 5
    done
    [ $retry -ge 3 ] && warn "Failed to inject environment variables"

    # ── Ensure the execution role can reach Connect + Q in Connect ──────────
    #    Generated CFN templates often grant only AWSLambdaBasicExecutionRole,
    #    but the handler calls connect:DescribeContact (to resolve the Wisdom
    #    session ARN) and wisdom:UpdateSessionData/GetSession at runtime.
    #    Without this the call fails with AccessDeniedException mid-call.
    QSESSION_ROLE_ARN=$(aws lambda get-function-configuration \
        --function-name "$UPDATE_Q_FUNC" --region "$REGION" \
        --query 'Role' --output text 2>/dev/null || echo "")
    if [ -n "$QSESSION_ROLE_ARN" ]; then
        QSESSION_ROLE_NAME="${QSESSION_ROLE_ARN##*/}"
        QSESSION_POLICY=$(cat <<QPEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ConnectDescribeContact",
      "Effect": "Allow",
      "Action": "connect:DescribeContact",
      "Resource": "arn:aws:connect:${REGION}:${ACCOUNT_ID}:instance/${CONNECT_INSTANCE_ID}/contact/*"
    },
    {
      "Sid": "QConnectSessionData",
      "Effect": "Allow",
      "Action": ["wisdom:UpdateSessionData", "wisdom:GetSession", "wisdom:GetAssistant"],
      "Resource": [
        "arn:aws:wisdom:${REGION}:${ACCOUNT_ID}:assistant/${AI_ASSISTANT_ID}",
        "arn:aws:wisdom:${REGION}:${ACCOUNT_ID}:session/${AI_ASSISTANT_ID}/*"
      ]
    }
  ]
}
QPEOF
)
        aws iam put-role-policy \
            --role-name "$QSESSION_ROLE_NAME" \
            --policy-name "aicc-qsession-runtime-access" \
            --policy-document "$QSESSION_POLICY" 2>/dev/null \
            && ok "Runtime permissions granted to $QSESSION_ROLE_NAME (connect:DescribeContact + wisdom session)" \
            || warn "Could not attach runtime policy to $QSESSION_ROLE_NAME (check iam:PutRolePolicy permission)"
    fi
}

# =============================================================================
# Phase 8: AgentCore Gateway (MCP Server) + JWT audience + Target
#   FIX: update-gateway now passes --role-arn/--protocol-type (required params
#   whose omission made the old script's audience update always fail — the
#   workshop's "Chapter 3 manual fix" is now automated correctly)
# =============================================================================
phase_gateway() {
    echo ""
    echo "🌐 Phase 8: Setting up AgentCore Gateway (MCP Server)..."

    SKIP_GATEWAY=false
    if ! aws bedrock-agentcore-control help &>/dev/null; then
        warn "bedrock-agentcore-control not available in this CLI. Skipping gateway phases"
        SKIP_GATEWAY=true; return 0
    fi
    [ -z "${CONNECT_ALIAS:-}" ] && { warn "No Connect alias, skipping"; SKIP_GATEWAY=true; return 0; }

    # 8.1 IAM role
    GATEWAY_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" \
        --query 'Role.Arn' --output text 2>/dev/null || echo "")
    if [ -z "$GATEWAY_ROLE_ARN" ]; then
        info "Creating Gateway IAM role: $ROLE_NAME"
        TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
        GATEWAY_ROLE_ARN=$(aws iam create-role \
            --role-name "$ROLE_NAME" \
            --assume-role-policy-document "$TRUST_POLICY" \
            --description "IAM role for ${PROJECT_NAME} AgentCore Gateway" \
            --query 'Role.Arn' --output text)
        PERMISSION_POLICY=$(cat <<PERMEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "BedrockAgentCore", "Effect": "Allow", "Action": "bedrock-agentcore:*", "Resource": "arn:aws:bedrock-agentcore:*:${ACCOUNT_ID}:*"},
    {"Sid": "BedrockInvoke", "Effect": "Allow", "Action": ["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
    {"Sid": "S3Read", "Effect": "Allow", "Action": ["s3:GetObject","s3:GetObjectVersion","s3:ListBucket"],
     "Resource": ["arn:aws:s3:::${KB_BUCKET:-*}","arn:aws:s3:::${KB_BUCKET:-*}/*","arn:aws:s3:::bedrock-agentcore-gateway-*","arn:aws:s3:::bedrock-agentcore-runtime-*"]},
    {"Sid": "Lambda", "Effect": "Allow", "Action": ["lambda:InvokeFunction","lambda:GetFunction"], "Resource": "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PROJECT_NAME}-*"},
    {"Sid": "Secrets", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:bedrock-agentcore*"}
  ]
}
PERMEOF
)
        aws iam put-role-policy --role-name "$ROLE_NAME" \
            --policy-name "${PROJECT_NAME}-gateway-policy" \
            --policy-document "$PERMISSION_POLICY"
        info "Waiting for IAM propagation (10s)..."
        sleep 10
    else
        info "Reusing existing IAM role: $GATEWAY_ROLE_ARN"
    fi

    # 8.2 API Key credential provider
    EXISTING_CRED=$(aws bedrock-agentcore-control list-api-key-credential-providers \
        --region "$REGION" --output json 2>/dev/null || echo '{"credentialProviders":[]}')
    CREDENTIAL_PROVIDER_ARN=$(echo "$EXISTING_CRED" | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('credentialProviders', []):
    if p.get('name') == '${CRED_PROVIDER_NAME}': print(p['credentialProviderArn']); break
" 2>/dev/null || echo "")
    if [ -z "$CREDENTIAL_PROVIDER_ARN" ]; then
        CRED_RESULT=$(aws bedrock-agentcore-control create-api-key-credential-provider \
            --name "$CRED_PROVIDER_NAME" --api-key "$API_KEY" \
            --region "$REGION" --output json 2>&1) || true
        CREDENTIAL_PROVIDER_ARN=$(jget "$CRED_RESULT" "credentialProviderArn")
        [ -n "$CREDENTIAL_PROVIDER_ARN" ] && info "Credential provider created" || { warn "Credential provider creation failed: $CRED_RESULT"; SKIP_GATEWAY=true; return 0; }
    else
        # Refresh the stored key — the stack (and its API key) may have been
        # recreated since the provider was made, which would break target auth
        aws bedrock-agentcore-control update-api-key-credential-provider \
            --name "$CRED_PROVIDER_NAME" --api-key "$API_KEY" \
            --region "$REGION" >/dev/null 2>&1 \
            && info "Reusing credential provider (API key refreshed to current stack)" \
            || info "Reusing existing credential provider"
    fi

    # 8.3 Gateway
    DISCOVERY_URL="https://${CONNECT_ALIAS}.my.connect.aws/.well-known/openid-configuration"
    EXISTING_GW=$(aws bedrock-agentcore-control list-gateways \
        --region "$REGION" --output json 2>/dev/null || echo '{}')
    GATEWAY_ID=$(echo "$EXISTING_GW" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for gw in d.get('items', d.get('gateways', [])):
    if gw.get('name') == '${GATEWAY_NAME}': print(gw['gatewayId']); break
" 2>/dev/null || echo "")

    if [ -z "$GATEWAY_ID" ]; then
        info "Creating Gateway: $GATEWAY_NAME"
        # Fresh IAM roles can take 10-60s to propagate; retry on assume-role /
        # authorization errors (most common on brand-new accounts)
        for gw_try in 1 2 3 4 5; do
            GW_RESULT=$(aws bedrock-agentcore-control create-gateway \
                --name "$GATEWAY_NAME" \
                --role-arn "$GATEWAY_ROLE_ARN" \
                --protocol-type MCP \
                --authorizer-type CUSTOM_JWT \
                --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"${DISCOVERY_URL}\",\"allowedAudience\":[\"placeholder\"]}}" \
                --region "$REGION" --output json 2>&1) || true
            GATEWAY_ID=$(jget "$GW_RESULT" "gatewayId")
            [ -n "$GATEWAY_ID" ] && break
            if echo "$GW_RESULT" | grep -qiE 'assume|authoriz|denied|role'; then
                info "IAM role still propagating (attempt $gw_try/5), waiting 20s..."
                sleep 20
            else
                break
            fi
        done
        [ -n "$GATEWAY_ID" ] && info "Gateway created: $GATEWAY_ID" || { warn "Gateway creation failed: $GW_RESULT"; SKIP_GATEWAY=true; return 0; }
    else
        info "Reusing existing Gateway: $GATEWAY_ID"
    fi
    state_set GATEWAY_ID "$GATEWAY_ID"

    # 8.4 Wait READY
    echo -n "   Waiting for Gateway READY..."
    for i in $(seq 1 60); do
        GW_STATUS=$(aws bedrock-agentcore-control get-gateway \
            --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
            --query 'status' --output text 2>/dev/null || echo "UNKNOWN")
        [ "$GW_STATUS" = "READY" ] && { echo " ✅"; break; }
        [ "$GW_STATUS" = "FAILED" ] && { echo " ❌"; SKIP_GATEWAY=true; return 0; }
        echo -n "."; sleep 10
    done
    GATEWAY_ARN=$(aws bedrock-agentcore-control get-gateway \
        --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
        --query 'gatewayArn' --output text 2>/dev/null || echo "")
    GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

    # 8.5 JWT audience: placeholder -> gateway ID
    #     now includes the required --role-arn/--protocol-type params whose omission broke it)
    CURRENT_AUD=$(aws bedrock-agentcore-control get-gateway \
        --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
        --query 'authorizerConfiguration.customJWTAuthorizer.allowedAudience[0]' --output text 2>/dev/null || echo "")
    if [ "$CURRENT_AUD" != "$GATEWAY_ID" ]; then
        info "Replacing JWT audience: '$CURRENT_AUD' -> '$GATEWAY_ID'"
        AUD_OK=false
        for aud_try in 1 2 3; do
            UPDATE_RESULT=$(aws bedrock-agentcore-control update-gateway \
                --gateway-identifier "$GATEWAY_ID" \
                --name "$GATEWAY_NAME" \
                --role-arn "$GATEWAY_ROLE_ARN" \
                --protocol-type MCP \
                --authorizer-type CUSTOM_JWT \
                --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"${DISCOVERY_URL}\",\"allowedAudience\":[\"${GATEWAY_ID}\"]}}" \
                --region "$REGION" --output json 2>&1) || true
            if echo "$UPDATE_RESULT" | grep -q '"gatewayId"'; then
                AUD_OK=true; break
            fi
            warn "Audience update attempt $aud_try/3 failed: $(echo "$UPDATE_RESULT" | head -2)"
            sleep 15
        done
        if [ "$AUD_OK" = "true" ]; then
            echo -n "   Waiting for READY after audience update..."
            for i in $(seq 1 30); do
                GW_STATUS=$(aws bedrock-agentcore-control get-gateway \
                    --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
                    --query 'status' --output text 2>/dev/null || echo "UNKNOWN")
                [ "$GW_STATUS" = "READY" ] && { echo " ✅"; break; }
                echo -n "."; sleep 5
            done
            # verify — never trust the call alone
            CURRENT_AUD=$(aws bedrock-agentcore-control get-gateway \
                --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
                --query 'authorizerConfiguration.customJWTAuthorizer.allowedAudience[0]' --output text 2>/dev/null || echo "")
            if [ "$CURRENT_AUD" = "$GATEWAY_ID" ]; then
                ok "Audience = $GATEWAY_ID (verified)"
            else
                warn "Audience is still '$CURRENT_AUD' after update!"
                warn "Manual fix required in console (Bedrock AgentCore > Gateways > Inbound Auth)"
            fi
        else
            warn "Audience update failed after 3 attempts."
            warn "Manual fix required in console (Bedrock AgentCore > Gateways > Inbound Auth):"
            warn "  Allowed audiences: replace 'placeholder' with '$GATEWAY_ID'"
        fi
    else
        ok "Audience already correct: $GATEWAY_ID"
    fi

    # 8.6 Gateway Target (OpenAPI from S3) — create or reconcile stale S3 URI
    EXPECTED_S3_URI="s3://${KB_BUCKET}/openapi/openapi.yaml"
    TARGET_CONFIG="{\"mcp\":{\"openApiSchema\":{\"s3\":{\"uri\":\"${EXPECTED_S3_URI}\"}}}}"
    CRED_CONFIG="[{\"credentialProviderType\":\"API_KEY\",\"credentialProvider\":{\"apiKeyCredentialProvider\":{\"providerArn\":\"${CREDENTIAL_PROVIDER_ARN}\",\"credentialParameterName\":\"x-api-key\",\"credentialLocation\":\"HEADER\"}}}]"
    EXISTING_TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "$GATEWAY_ID" --region "$REGION" --output json 2>/dev/null || echo '{}')
    TARGET_INFO=$(echo "$EXISTING_TARGETS" | TARGET_NAME="$TARGET_NAME" python3 -c "
import sys, json, os
name = os.environ['TARGET_NAME']
d = json.load(sys.stdin)
for t in d.get('items', d.get('targets', [])):
    if t.get('name') == name:
        tid = t.get('targetId') or t.get('id') or ''
        cfg = t.get('targetConfiguration') or {}
        uri = ((cfg.get('mcp') or {}).get('openApiSchema') or {}).get('s3', {}).get('uri') or ''
        print(tid + '\t' + uri); break
else: print('\t')
" 2>/dev/null || echo $'\t')
    EXISTING_TARGET_ID=$(echo "$TARGET_INFO" | cut -f1)
    EXISTING_S3_URI=$(echo "$TARGET_INFO" | cut -f2)

    if [ -z "$EXISTING_TARGET_ID" ]; then
        # Fresh-account IAM propagation can make the first attempt fail or land
        # in FAILED (gateway role can't read the S3 spec yet) — retry up to 3x
        TGT_STATUS=""
        for tgt_try in 1 2 3; do
            info "Creating Gateway Target... (attempt $tgt_try/3)"
            TARGET_RESULT=$(aws bedrock-agentcore-control create-gateway-target \
                --gateway-identifier "$GATEWAY_ID" --name "$TARGET_NAME" \
                --target-configuration "$TARGET_CONFIG" \
                --credential-provider-configurations "$CRED_CONFIG" \
                --region "$REGION" --output json 2>&1) || true
            NEW_TARGET_ID=$(jget "$TARGET_RESULT" "targetId")
            if [ -z "$NEW_TARGET_ID" ]; then
                warn "Target create call failed: $(echo "$TARGET_RESULT" | head -3)"
                sleep 15
                continue
            fi
            # Wait for the target to finish validating the OpenAPI schema
            echo -n "   Waiting for Target READY..."
            TGT_STATUS=""
            for i in $(seq 1 30); do
                TGT=$(aws bedrock-agentcore-control get-gateway-target \
                    --gateway-identifier "$GATEWAY_ID" --target-id "$NEW_TARGET_ID" \
                    --region "$REGION" --output json 2>/dev/null || echo '{}')
                TGT_STATUS=$(jget "$TGT" "status")
                [ "$TGT_STATUS" = "READY" ] && { echo " ✅"; break; }
                [ "$TGT_STATUS" = "FAILED" ] && { echo " ❌"; break; }
                echo -n "."; sleep 5
            done
            if [ "$TGT_STATUS" = "READY" ]; then
                ok "Target created -> $EXPECTED_S3_URI"
                break
            fi
            warn "Target status: ${TGT_STATUS:-TIMEOUT} — reason: $(jget "$TGT" "statusReasons")"
            # delete the failed target before retrying
            aws bedrock-agentcore-control delete-gateway-target \
                --gateway-identifier "$GATEWAY_ID" --target-id "$NEW_TARGET_ID" \
                --region "$REGION" >/dev/null 2>&1 || true
            sleep 20
        done
        if [ "$TGT_STATUS" != "READY" ]; then
            warn "Gateway Target could not reach READY after 3 attempts."
            warn "Check that s3://${KB_BUCKET}/openapi/openapi.yaml exists, is valid OpenAPI 3.0,"
            warn "and that role $ROLE_NAME has s3:GetObject on that bucket."
        fi
    elif [ "$EXISTING_S3_URI" != "$EXPECTED_S3_URI" ]; then
        info "Updating Target S3 URI (stale bucket -> current bucket)..."
        aws bedrock-agentcore-control update-gateway-target \
            --gateway-identifier "$GATEWAY_ID" --target-id "$EXISTING_TARGET_ID" \
            --name "$TARGET_NAME" --target-configuration "$TARGET_CONFIG" \
            --credential-provider-configurations "$CRED_CONFIG" \
            --region "$REGION" --output json >/dev/null 2>&1 \
            && ok "Target updated" || warn "Target update failed"
    else
        ok "Gateway Target already up to date"
    fi
}

# =============================================================================
# Phase 9: Connect integrations — MCP server registration + Lambda associations
#   (automates the console-only steps of workshop chapter 4)
# =============================================================================
phase_connect_integrations() {
    echo ""
    echo "🔗 Phase 9: Connect integrations (MCP server + Lambda associations)..."

    # 9.1 MCP server registration: appintegrations MCP_SERVER app + Connect association
    if [ "$SKIP_GATEWAY" = "false" ] && [ -n "${GATEWAY_ID:-}" ]; then
        EXISTING_APPS=$(aws appintegrations list-applications --region "$REGION" --output json 2>/dev/null || echo '{"Applications":[]}')
        MCP_APP_ARN=$(echo "$EXISTING_APPS" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('Applications', []):
    if a.get('Namespace') == '${GATEWAY_ID}': print(a['Arn']); break
" 2>/dev/null || echo "")
        if [ -z "$MCP_APP_ARN" ]; then
            # unique per gateway generation (avoids name conflicts with stale apps)
            local APP_NAME="${MCP_APP_NAME}-${GATEWAY_ID##*-}"
            info "Registering MCP server application: $APP_NAME"
            APP_RESULT=$(aws appintegrations create-application \
                --name "$APP_NAME" \
                --namespace "$GATEWAY_ID" \
                --description "MCP server for ${PROJECT_NAME}" \
                --application-type MCP_SERVER \
                --application-source-config "{\"ExternalUrlConfig\":{\"AccessUrl\":\"${GATEWAY_URL}\"}}" \
                --region "$REGION" --output json 2>&1) || true
            MCP_APP_ARN=$(jget "$APP_RESULT" "Arn")
            [ -n "$MCP_APP_ARN" ] && info "Registered: $MCP_APP_ARN" || warn "MCP app registration failed: $APP_RESULT"
        else
            info "Reusing existing MCP application"
        fi
        state_set MCP_APP_ARN "${MCP_APP_ARN:-}"

        if [ -n "$MCP_APP_ARN" ]; then
            APP_ASSOCS=$(aws connect list-integration-associations \
                --instance-id "$CONNECT_INSTANCE_ID" --integration-type APPLICATION \
                --region "$REGION" --output json 2>/dev/null || echo '{"IntegrationAssociationSummaryList":[]}')
            if echo "$APP_ASSOCS" | grep -q "$MCP_APP_ARN"; then
                ok "MCP server already integrated with Connect"
            else
                aws connect create-integration-association \
                    --instance-id "$CONNECT_INSTANCE_ID" \
                    --integration-type APPLICATION \
                    --integration-arn "$MCP_APP_ARN" \
                    --region "$REGION" >/dev/null 2>&1 \
                    && ok "MCP server registered as Connect integration" \
                    || warn "MCP integration association failed (check app-integrations:CreateApplication permission)"
            fi
        fi
    else
        info "No Gateway — skipping MCP registration"
    fi

    # 9.2 Lambda associations (register flow-invoked functions with Connect)
    _load_stack_lambda_names
    local flow_lambda_arns=""
    if [ -n "$FLOW_JSON" ]; then
        # functions referenced by the flow's {{X_LAMBDA_ARN}} placeholders
        flow_lambda_arns=$(python3 - "$FLOW_JSON" <<'PYEOF' 2>/dev/null || true
import sys, json, re
tokens = set(re.findall(r'\{\{([A-Z_]+)_LAMBDA_ARN\}\}', open(sys.argv[1]).read()))
for t in tokens: print(t.lower())
PYEOF
)
    fi
    ASSOCIATED=0
    for stem in $flow_lambda_arns; do
        fn=$(resolve_stack_function "$stem")
        [ -z "$fn" ] && continue
        fn_arn="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${fn}"
        aws connect associate-lambda-function \
            --instance-id "$CONNECT_INSTANCE_ID" \
            --function-arn "$fn_arn" \
            --region "$REGION" 2>/dev/null && info "Lambda associated: $fn" || info "Lambda already associated: $fn"
        ASSOCIATED=$((ASSOCIATED+1))
    done
    [ "$ASSOCIATED" -gt 0 ] && ok "Associated $ASSOCIATED flow-referenced Lambda(s) with Connect" || info "No flow-referenced Lambdas"
}

# =============================================================================
# Phase 10: Lex bot (automates workshop chapter 5 bot creation)
#   - Language selection (auto-detected from assets + menu)
#   - Speech model choice: Nova Sonic Speech-to-Speech (agentic voice) vs standard TTS
#   - AMAZON.QInConnectIntent (AI agent intent) + build + Connect association
# =============================================================================
LOCALE_ID=""
LEX_VOICE_ID=""
LEX_VOICE_ENGINE=""
SPEECH_MODE=""

phase_lex_bot() {
    echo ""
    echo "🗣️  Phase 10: Creating Lex bot (voice entry point)..."

    # ── Language selection (detected language first, deduplicated) ──────────
    local langs=("$DETECTED_LANG  (detected from assets)")
    for l in en-US ko-KR ja-JP es-US; do
        [ "$l" != "$DETECTED_LANG" ] && langs+=("$l")
    done
    langs+=("Enter manually")
    choose "Select the bot language" 1 "${langs[@]}"
    case "$CHOICE_VALUE" in
        "Enter manually") ask_text "Language code (e.g. fr-FR)" "en-US"; FLOW_LANG="$ANSWER" ;;
        *) FLOW_LANG=$(echo "$CHOICE_VALUE" | awk '{print $1}') ;;
    esac
    LOCALE_ID=$(echo "$FLOW_LANG" | tr '-' '_')
    info "Language: $FLOW_LANG (Lex locale: $LOCALE_ID)"

    # ── Speech model choice (menu built after Nova Sonic availability check) ─
    #    Three tiers of Connect bot speech models:
    #    1. Nova Sonic Speech-to-Speech  (unifiedSpeechSettings; model-availability gated)
    #    2. Amazon agentic voice / Advanced ASR (speechRecognitionSettings Neural; broadly available)
    #    3. Standard ASR + Polly TTS
    NOVA_SONIC_ARN=""
    NOVA_MODEL=$(aws bedrock list-foundation-models --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
try:
    for m in json.load(sys.stdin)['modelSummaries']:
        if 'nova' in m['modelId'] and 'sonic' in m['modelId']: print(m['modelArn']); break
except Exception: pass
" 2>/dev/null || echo "")
    local speech_opts=()
    if [ -n "$NOVA_MODEL" ]; then
        speech_opts+=("Amazon Nova Sonic Speech-to-Speech  (agentic voice S2S, most natural — recommended)")
    fi
    speech_opts+=("Amazon agentic voice — Advanced ASR  (low-latency turn taking + expressive TTS)")
    speech_opts+=("Standard ASR + Polly TTS")
    choose "Select the bot speech model" 1 "${speech_opts[@]}"
    case "$CHOICE_VALUE" in
        *"Nova Sonic"*)   SPEECH_MODE="nova-sonic"; NOVA_SONIC_ARN="$NOVA_MODEL"; info "Nova Sonic: $NOVA_SONIC_ARN" ;;
        *"Advanced ASR"*) SPEECH_MODE="advanced-asr" ;;
        *)                SPEECH_MODE="standard" ;;
    esac

    # ── Voice provider choice ───────────────────────────────────────────────
    #    Amazon Connect agentic voice has NO public list/set API today: its voice
    #    catalog is per-language (polyglot voices Katie/Blake/Brooke/Ronald/Gemma
    #    cover En/De/Es/Fr/Hi/It/Ja/No/Pt/Ru; other locales have locale-specific
    #    voices selectable only in the console) and its flow JSON representation
    #    is undocumented. Either way the flow keeps a proper Set Voice block with
    #    a working Polly voice; picking "agentic" adds a documented ~1-min console
    #    step (switch the block's Voice Provider) printed in the deploy summary.
    VOICE_PROVIDER="polly"
    choose "Select the voice provider for the Contact Flow" 1 \
        "Amazon Connect agentic voice  (expressive, 50+ languages — deployed with a Polly fallback; switch the Set voice block's provider in console after deploy, ~1 min)" \
        "Amazon Polly                  (fully scripted here — voice/engine selected below)"
    [ "$CHOICE" = "1" ] && VOICE_PROVIDER="agentic"
    state_set VOICE_PROVIDER "$VOICE_PROVIDER"

    # ── Polly voice selection (bot TTS fallback; flow set-voice when provider=polly)
    local voices_json
    voices_json=$(aws polly describe-voices --language-code "$FLOW_LANG" --region "$REGION" --output json 2>/dev/null || echo '{"Voices":[]}')
    local voice_menu=() voice_ids=() voice_engines=()
    while IFS=$'\t' read -r vid gender engines; do
        voice_menu+=("$vid ($gender, engines: $engines)")
        voice_ids+=("$vid")
        # pick best engine: generative > neural > standard
        local best="standard"
        case "$engines" in *generative*) best="generative" ;; *neural*) best="neural" ;; esac
        voice_engines+=("$best")
    done < <(echo "$voices_json" | python3 -c "
import sys, json
for v in json.load(sys.stdin).get('Voices', []):
    print(v['Id'] + '\t' + v['Gender'] + '\t' + '/'.join(v['SupportedEngines']))
")
    if [ ${#voice_ids[@]} -eq 0 ]; then
        warn "No Polly voices for '$FLOW_LANG'. Falling back to Joanna/neural"
        LEX_VOICE_ID="Joanna"; LEX_VOICE_ENGINE="neural"
    elif [ "$VOICE_PROVIDER" = "agentic" ]; then
        # keep a working Polly voice in the Set Voice block until the provider is
        # switched in console — auto-pick the best one for the language
        LEX_VOICE_ID="${voice_ids[0]}"
        LEX_VOICE_ENGINE="${voice_engines[0]}"
        info "Agentic voice selected — Set voice block keeps Polly fallback ($LEX_VOICE_ID/$LEX_VOICE_ENGINE) until you switch the provider in console"
        case "$FLOW_LANG" in
            en-*|de-*|es-*|fr-*|hi-*|it-*|ja-*|no-*|pt-*|ru-*)
                info "Polyglot agentic voices available for this language: Katie, Blake, Brooke, Ronald, Gemma" ;;
            *)
                info "This language uses locale-specific agentic voices — preview & pick in console (Set voice block > Voice Provider: Amazon Connect agentic voice)" ;;
        esac
    else
        choose "Select a TTS voice (also applied to the Contact Flow Set voice block)" 1 "${voice_menu[@]}"
        LEX_VOICE_ID="${voice_ids[$((CHOICE-1))]}"
        LEX_VOICE_ENGINE="${voice_engines[$((CHOICE-1))]}"
        # offer engine choice when the voice supports multiple engines
        local supported
        supported=$(echo "$voices_json" | python3 -c "
import sys, json
for v in json.load(sys.stdin).get('Voices', []):
    if v['Id'] == '$LEX_VOICE_ID': print('\n'.join(v['SupportedEngines'])); break
")
        local engine_count
        engine_count=$(echo "$supported" | grep -c . || echo 1)
        if [ "$engine_count" -gt 1 ]; then
            local engine_opts=() default_engine_idx=1 i=1
            while read -r eng; do
                engine_opts+=("$eng")
                [ "$eng" = "$LEX_VOICE_ENGINE" ] && default_engine_idx=$i
                i=$((i+1))
            done <<< "$supported"
            choose "Select the TTS engine for voice $LEX_VOICE_ID" "$default_engine_idx" "${engine_opts[@]}"
            LEX_VOICE_ENGINE="$CHOICE_VALUE"
        fi
    fi
    ok "Voice: $LEX_VOICE_ID / $LEX_VOICE_ENGINE"
    state_set FLOW_LANG "$FLOW_LANG"
    state_set LEX_VOICE_ID "$LEX_VOICE_ID"
    state_set LEX_VOICE_ENGINE "$LEX_VOICE_ENGINE"

    # ── Speech detection sensitivity (VAD, agentic voice best practice) ─────
    choose "Select speech detection sensitivity (VAD — match the caller's environment)" 1 \
        "Default                (quiet environments — recommended)" \
        "HighNoiseTolerance     (moderate background noise)" \
        "MaximumNoiseTolerance  (very noisy environments, e.g. call from street/car)"
    SPEECH_SENSITIVITY=$(echo "$CHOICE_VALUE" | awk '{print $1}')
    info "Speech detection sensitivity: $SPEECH_SENSITIVITY"

    # ── Lex service-linked role ─────────────────────────────────────────────
    LEX_ROLE_ARN=$(aws iam get-role --role-name "AWSServiceRoleForLexV2Bots" \
        --query 'Role.Arn' --output text 2>/dev/null || echo "")
    if [ -z "$LEX_ROLE_ARN" ]; then
        # find any lexv2 SLR (suffixed) or create one
        LEX_ROLE_ARN=$(aws iam list-roles --path-prefix "/aws-service-role/lexv2.amazonaws.com/" \
            --query 'Roles[0].Arn' --output text 2>/dev/null || echo "")
        [ "$LEX_ROLE_ARN" = "None" ] && LEX_ROLE_ARN=""
    fi
    if [ -z "$LEX_ROLE_ARN" ]; then
        info "Creating LexV2 service-linked role..."
        LEX_ROLE_ARN=$(aws iam create-service-linked-role \
            --aws-service-name lexv2.amazonaws.com \
            --custom-suffix "AICC" \
            --query 'Role.Arn' --output text 2>/dev/null || echo "")
        sleep 5
    fi
    [ -z "$LEX_ROLE_ARN" ] && { warn "Could not obtain Lex role — skipping bot creation"; return 0; }

    # ── Create or reuse the bot ─────────────────────────────────────────────
    BOT_ID=$(aws lexv2-models list-bots --region "$REGION" \
        --filters "name=BotName,values=$BOT_NAME,operator=EQ" \
        --query 'botSummaries[0].botId' --output text 2>/dev/null || echo "")
    [ "$BOT_ID" = "None" ] && BOT_ID=""
    if [ -z "$BOT_ID" ]; then
        info "Creating bot: $BOT_NAME"
        BOT_RESULT=$(aws lexv2-models create-bot \
            --bot-name "$BOT_NAME" \
            --description "AI agent entry bot for ${PROJECT_NAME}" \
            --role-arn "$LEX_ROLE_ARN" \
            --data-privacy '{"childDirected":false}' \
            --idle-session-ttl-in-seconds 600 \
            --region "$REGION" --output json)
        BOT_ID=$(jget "$BOT_RESULT" "botId")
        echo -n "   Waiting for bot..."
        for i in $(seq 1 30); do
            BSTATUS=$(aws lexv2-models describe-bot --bot-id "$BOT_ID" --region "$REGION" \
                --query 'botStatus' --output text 2>/dev/null || echo "UNKNOWN")
            [ "$BSTATUS" = "Available" ] && { echo " ✅"; break; }
            echo -n "."; sleep 3
        done
    else
        info "Reusing existing bot: $BOT_NAME ($BOT_ID)"
    fi
    state_set BOT_ID "$BOT_ID"

    # ── Create locale (Nova Sonic S2S or standard voice-settings) ───────────
    LOCALE_EXISTS=$(aws lexv2-models describe-bot-locale --bot-id "$BOT_ID" \
        --bot-version DRAFT --locale-id "$LOCALE_ID" --region "$REGION" \
        --query 'botLocaleStatus' --output text 2>/dev/null || echo "")
    if [ -z "$LOCALE_EXISTS" ]; then
        info "Creating locale: $LOCALE_ID"
        if [ "$SPEECH_MODE" = "nova-sonic" ]; then
            aws lexv2-models create-bot-locale \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --nlu-intent-confidence-threshold 0.40 \
                --unified-speech-settings "{\"speechFoundationModel\":{\"modelArn\":\"${NOVA_SONIC_ARN}\"}}" \
                --speech-detection-sensitivity "$SPEECH_SENSITIVITY" \
                --region "$REGION" >/dev/null
        elif [ "$SPEECH_MODE" = "advanced-asr" ]; then
            aws lexv2-models create-bot-locale \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --nlu-intent-confidence-threshold 0.40 \
                --voice-settings "{\"voiceId\":\"${LEX_VOICE_ID}\",\"engine\":\"${LEX_VOICE_ENGINE}\"}" \
                --speech-recognition-settings '{"speechModelPreference":"Neural"}' \
                --speech-detection-sensitivity "$SPEECH_SENSITIVITY" \
                --region "$REGION" >/dev/null
        else
            aws lexv2-models create-bot-locale \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --nlu-intent-confidence-threshold 0.40 \
                --voice-settings "{\"voiceId\":\"${LEX_VOICE_ID}\",\"engine\":\"${LEX_VOICE_ENGINE}\"}" \
                --speech-detection-sensitivity "$SPEECH_SENSITIVITY" \
                --region "$REGION" >/dev/null
        fi
        echo -n "   Waiting for locale..."
        for i in $(seq 1 30); do
            LSTATUS=$(aws lexv2-models describe-bot-locale --bot-id "$BOT_ID" \
                --bot-version DRAFT --locale-id "$LOCALE_ID" --region "$REGION" \
                --query 'botLocaleStatus' --output text 2>/dev/null || echo "UNKNOWN")
            case "$LSTATUS" in NotBuilt|Built|ReadyExpressTesting) echo " ✅"; break ;; esac
            echo -n "."; sleep 3
        done
    else
        info "Reusing existing locale: $LOCALE_ID ($LOCALE_EXISTS)"
        # Apply the (possibly changed) speech model choice to the existing locale
        if [ "$SPEECH_MODE" = "nova-sonic" ]; then
            aws lexv2-models update-bot-locale \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --nlu-intent-confidence-threshold 0.40 \
                --unified-speech-settings "{\"speechFoundationModel\":{\"modelArn\":\"${NOVA_SONIC_ARN}\"}}" \
                --speech-detection-sensitivity "$SPEECH_SENSITIVITY" \
                --region "$REGION" >/dev/null 2>&1 && info "Locale speech settings updated (Nova Sonic S2S)" || true
        elif [ "$SPEECH_MODE" = "advanced-asr" ]; then
            aws lexv2-models update-bot-locale \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --nlu-intent-confidence-threshold 0.40 \
                --voice-settings "{\"voiceId\":\"${LEX_VOICE_ID}\",\"engine\":\"${LEX_VOICE_ENGINE}\"}" \
                --speech-recognition-settings '{"speechModelPreference":"Neural"}' \
                --speech-detection-sensitivity "$SPEECH_SENSITIVITY" \
                --region "$REGION" >/dev/null 2>&1 && info "Locale speech settings updated (Advanced ASR)" || true
        else
            aws lexv2-models update-bot-locale \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --nlu-intent-confidence-threshold 0.40 \
                --voice-settings "{\"voiceId\":\"${LEX_VOICE_ID}\",\"engine\":\"${LEX_VOICE_ENGINE}\"}" \
                --speech-detection-sensitivity "$SPEECH_SENSITIVITY" \
                --region "$REGION" >/dev/null 2>&1 && info "Locale speech settings updated (Standard)" || true
        fi
    fi

    # ── AI agent intent (AMAZON.QInConnectIntent) ───────────────────────────
    QIC_INTENT=$(aws lexv2-models list-intents --bot-id "$BOT_ID" --bot-version DRAFT \
        --locale-id "$LOCALE_ID" --region "$REGION" \
        --query "intentSummaries[?parentIntentSignature=='AMAZON.QInConnectIntent'].intentId" \
        --output text 2>/dev/null || echo "")
    if [ -z "$QIC_INTENT" ] || [ "$QIC_INTENT" = "None" ]; then
        info "Adding AI agent intent (AMAZON.QInConnectIntent)..."
        aws lexv2-models create-intent \
            --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
            --intent-name "AIAgentIntent" \
            --parent-intent-signature "AMAZON.QInConnectIntent" \
            --q-in-connect-intent-configuration "{\"qInConnectAssistantConfiguration\":{\"assistantArn\":\"${ASSISTANT_ARN}\"}}" \
            --region "$REGION" >/dev/null \
            && ok "AI agent intent enabled (assistant linked)" \
            || warn "QInConnect intent creation failed"
    else
        # Backfill: intents created by earlier versions (or by hand) may lack the
        # assistant linkage — without qInConnectIntentConfiguration the bot is not
        # actually connected to the Connect assistant and self-service silently
        # fails. Verify and repair.
        local QIC_ASST_ARN
        QIC_ASST_ARN=$(aws lexv2-models describe-intent \
            --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
            --intent-id "$QIC_INTENT" --region "$REGION" \
            --query 'qInConnectIntentConfiguration.qInConnectAssistantConfiguration.assistantArn' \
            --output text 2>/dev/null || echo "")
        if [ "$QIC_ASST_ARN" = "$ASSISTANT_ARN" ]; then
            info "AI agent intent already linked to assistant"
        else
            info "AI agent intent exists but assistant link is missing/stale — repairing..."
            local QIC_NAME
            QIC_NAME=$(aws lexv2-models describe-intent \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --intent-id "$QIC_INTENT" --region "$REGION" \
                --query 'intentName' --output text 2>/dev/null || echo "AIAgentIntent")
            aws lexv2-models update-intent \
                --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" \
                --intent-id "$QIC_INTENT" --intent-name "$QIC_NAME" \
                --parent-intent-signature "AMAZON.QInConnectIntent" \
                --q-in-connect-intent-configuration "{\"qInConnectAssistantConfiguration\":{\"assistantArn\":\"${ASSISTANT_ARN}\"}}" \
                --region "$REGION" >/dev/null 2>&1 \
                && ok "AI agent intent relinked to assistant" \
                || warn "Failed to relink AI agent intent to assistant (check bot in console)"
        fi
    fi

    # ── Build ───────────────────────────────────────────────────────────────
    info "Building bot locale... (1-2 min)"
    aws lexv2-models build-bot-locale --bot-id "$BOT_ID" --bot-version DRAFT \
        --locale-id "$LOCALE_ID" --region "$REGION" >/dev/null 2>&1 || true
    echo -n "   Waiting for build..."
    for i in $(seq 1 60); do
        LSTATUS=$(aws lexv2-models describe-bot-locale --bot-id "$BOT_ID" \
            --bot-version DRAFT --locale-id "$LOCALE_ID" --region "$REGION" \
            --query 'botLocaleStatus' --output text 2>/dev/null || echo "UNKNOWN")
        case "$LSTATUS" in
            Built) echo " ✅"; break ;;
            Failed) echo " ❌ build failed"; aws lexv2-models describe-bot-locale --bot-id "$BOT_ID" --bot-version DRAFT --locale-id "$LOCALE_ID" --region "$REGION" --query 'failureReasons' --output json 2>/dev/null; break ;;
        esac
        echo -n "."; sleep 5
    done

    # ── TestBotAlias ARN + Connect association (CLI equivalent of Lex toggle) ─
    LEX_BOT_ALIAS_ARN="arn:aws:lex:${REGION}:${ACCOUNT_ID}:bot-alias/${BOT_ID}/TSTALIASID"
    EXISTING_BOTS=$(aws connect list-bots --instance-id "$CONNECT_INSTANCE_ID" \
        --lex-version V2 --region "$REGION" --output json 2>/dev/null || echo '{"LexBots":[]}')
    if echo "$EXISTING_BOTS" | grep -q "$BOT_ID"; then
        info "Bot already associated with Connect"
    else
        aws connect associate-bot \
            --instance-id "$CONNECT_INSTANCE_ID" \
            --lex-v2-bot "AliasArn=$LEX_BOT_ALIAS_ARN" \
            --region "$REGION" 2>/dev/null \
            && ok "Bot associated with Connect: $LEX_BOT_ALIAS_ARN" \
            || warn "Bot association failed (associate-bot)"
    fi
    state_set LEX_BOT_ALIAS_ARN "$LEX_BOT_ALIAS_ARN"
}

# =============================================================================
# Phase 11: Contact Flow import (automates chapter 5 import + placeholders)
#   - Auto-resolves {{...}} placeholders from deployed resources (menus for the rest)
#   - Applies the selected voice/engine to set-voice blocks
#   - Automates "Set language attribute": injects UpdateContactData(LanguageCode)
#   - Auto-fixes known import errors (RealTime analytics etc.) and retries
# =============================================================================
CONTACT_FLOW_ID=""
CONTACT_FLOW_ARN=""

phase_contact_flow() {
    echo ""
    echo "📋 Phase 11: Preparing & importing Contact Flow..."
    [ -z "$FLOW_JSON" ] && { info "No contact-flow/ directory, skipping"; return 0; }

    local WORK_FLOW="/tmp/${PROJECT_NAME}_flow_resolved.json"
    cp "$FLOW_JSON" "$WORK_FLOW"

    # ── Collect placeholders ────────────────────────────────────────────────
    local placeholders
    placeholders=$(grep -o '{{[A-Z_]*}}' "$WORK_FLOW" | sort -u | tr -d '{}')
    info "Placeholders found: $(echo $placeholders | tr '\n' ' ')"

    # helper: substitution
    replace_ph() { # token value
        python3 - "$WORK_FLOW" "$1" "$2" <<'PYEOF'
import sys
path, token, value = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()
open(path, 'w').write(s.replace('{{%s}}' % token, value))
PYEOF
    }

    # queue / hours menus (lazy-loaded once)
    local QUEUES_JSON="" HOURS_JSON=""

    for token in $placeholders; do
        local value=""
        case "$token" in
            *_LAMBDA_ARN)
                local stem
                stem=$(echo "$token" | sed 's/_LAMBDA_ARN$//' | tr '[:upper:]' '[:lower:]')
                local fn
                fn=$(resolve_stack_function "$stem")
                if [ -n "$fn" ]; then
                    value="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${fn}"
                    info "$token -> $fn (auto-resolved from stack)"
                else
                    # multiple choice: pick from stack Lambdas
                    _load_stack_lambda_names
                    local menu=() arns=()
                    for f in $STACK_LAMBDA_NAMES; do
                        menu+=("$f"); arns+=("arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:$f")
                    done
                    menu+=("Skip (configure manually in console)")
                    choose "Select the Lambda to map to placeholder {{$token}}" 1 "${menu[@]}"
                    if [ "$CHOICE_VALUE" != "Skip (configure manually in console)" ]; then
                        value="${arns[$((CHOICE-1))]}"
                    fi
                fi
                ;;
            WISDOM_ASSISTANT_ARN)
                value="$ASSISTANT_ARN"
                info "$token -> Assistant ARN (auto)"
                ;;
            LEX_BOT_ALIAS_ARN)
                value="${LEX_BOT_ALIAS_ARN:-}"
                [ -n "$value" ] && info "$token -> $BOT_NAME TestBotAlias (auto)"
                ;;
            QUEUE_ARN|*_QUEUE_ARN)
                [ -z "$QUEUES_JSON" ] && QUEUES_JSON=$(aws connect list-queues \
                    --instance-id "$CONNECT_INSTANCE_ID" --queue-types STANDARD \
                    --region "$REGION" --output json 2>/dev/null || echo '{"QueueSummaryList":[]}')
                local qmenu=() qarns=() qdefault=1 qi=1
                while IFS=$'\t' read -r qname qarn; do
                    qmenu+=("$qname"); qarns+=("$qarn")
                    [ "$qname" = "BasicQueue" ] && qdefault=$qi
                    qi=$((qi+1))
                done < <(echo "$QUEUES_JSON" | python3 -c "
import sys, json
for q in json.load(sys.stdin).get('QueueSummaryList', []):
    print((q.get('Name') or 'N/A') + '\t' + q['Arn'])
")
                if [ ${#qarns[@]} -gt 0 ]; then
                    choose "Select the queue for placeholder {{$token}}" "$qdefault" "${qmenu[@]}"
                    value="${qarns[$((CHOICE-1))]}"
                fi
                ;;
            HOURS_ARN|*_HOURS_ARN|*HOURS_OF_OPERATION*ARN)
                [ -z "$HOURS_JSON" ] && HOURS_JSON=$(aws connect list-hours-of-operations \
                    --instance-id "$CONNECT_INSTANCE_ID" \
                    --region "$REGION" --output json 2>/dev/null || echo '{"HoursOfOperationSummaryList":[]}')
                local hmenu=() harns=()
                while IFS=$'\t' read -r hname harn; do
                    hmenu+=("$hname"); harns+=("$harn")
                done < <(echo "$HOURS_JSON" | python3 -c "
import sys, json
for h in json.load(sys.stdin).get('HoursOfOperationSummaryList', []):
    print((h.get('Name') or 'N/A') + '\t' + h['Arn'])
")
                if [ ${#harns[@]} -gt 0 ]; then
                    choose "Select the hours of operation for placeholder {{$token}}" 1 "${hmenu[@]}"
                    value="${harns[$((CHOICE-1))]}"
                fi
                ;;
            *)
                ask_text "Enter a value for placeholder {{$token}} (empty = skip)" ""
                value="$ANSWER"
                ;;
        esac
        [ -n "$value" ] && replace_ph "$token" "$value"
    done

    # ── Agentic voice (ASR) tuning presets ──────────────────────────────────
    #    Based on the agentic-voice best practices guide: barge-in + end-of-turn
    #    controls are Lex session attributes on the Get customer input block.
    #    https://docs.aws.amazon.com/connect/latest/adminguide/agentic-voice-best-practices.html
    local ASR_CONFIDENCE="" ASR_TIMEOUT="" ALLOW_INTERRUPT=""
    choose "Select a conversation tuning preset (ASR turn-taking & barge-in, applied to the Get customer input block)" 1 \
        "Natural conversation      (defaults: confidence 0.7 / 640ms, barge-in ON — recommended)" \
        "Pause-tolerant            (confidence 0.9 / 3000ms — for dictated digits, noisy or slow speakers)" \
        "Snappy short exchanges    (confidence 0.5 / 500ms — fast yes/no menus)" \
        "Compliance-first          (barge-in OFF — disclaimers must be heard in full)" \
        "Keep asset's original session attributes"
    case "$CHOICE" in
        1) ASR_CONFIDENCE="0.7"; ASR_TIMEOUT="640";  ALLOW_INTERRUPT="true"  ;;
        2) ASR_CONFIDENCE="0.9"; ASR_TIMEOUT="3000"; ALLOW_INTERRUPT="true"  ;;
        3) ASR_CONFIDENCE="0.5"; ASR_TIMEOUT="500";  ALLOW_INTERRUPT="true"  ;;
        4) ASR_CONFIDENCE="0.7"; ASR_TIMEOUT="640";  ALLOW_INTERRUPT="false" ;;
        5) ;; # keep as-is
    esac
    [ -n "$ASR_CONFIDENCE" ] && info "ASR preset: confidence=$ASR_CONFIDENCE timeout=${ASR_TIMEOUT}ms barge-in=$ALLOW_INTERRUPT"

    # ── Apply voice + Set language attribute + fix known import bugs ────────
    python3 - "$WORK_FLOW" "$LEX_VOICE_ID" "$LEX_VOICE_ENGINE" "$FLOW_LANG" "$ASR_CONFIDENCE" "$ASR_TIMEOUT" "$ALLOW_INTERRUPT" "${VOICE_PROVIDER:-polly}" <<'PYEOF'
import sys, json
path, voice, engine, lang = sys.argv[1:5]
asr_conf, asr_timeout, allow_interrupt = sys.argv[5:8]
provider = sys.argv[8] if len(sys.argv) > 8 else 'polly'
d = json.load(open(path))
actions = d.get('Actions', [])
meta = d.setdefault('Metadata', {}).setdefault('ActionMetadata', {})

# 1) Apply the selected voice/engine to set-voice blocks.
#    NOTE: even when the user picked "Amazon Connect agentic voice", the Set
#    Voice block is kept INTACT with a working Polly voice — the agentic
#    provider has no public API/flow-JSON representation, so switching the
#    block's Voice Provider is a documented ~1-min console step after deploy
#    (printed in the summary). Converting/removing the block here would leave
#    the flow without a proper voice configuration block, which is worse UX.
for a in actions:
    if a.get('Type') == 'UpdateContactTextToSpeechVoice':
        a['Parameters']['TextToSpeechVoice'] = voice
        a['Parameters']['TextToSpeechEngine'] = engine.capitalize()
        # also refresh the display language code in metadata
        m = meta.get(a['Identifier'], {})
        if isinstance(m.get('parameters', {}).get('TextToSpeechVoice'), dict):
            m['parameters']['TextToSpeechVoice']['languageCode'] = lang

# 2) Automate "Set language attribute":
#    inject an UpdateContactData(LanguageCode) action right after set-voice
#    (without it, Lex fails with BotId/BotLocale 404 — the #1 workshop pitfall)
has_lang = any(a.get('Type') == 'UpdateContactData' and 'LanguageCode' in a.get('Parameters', {}) for a in actions)
if not has_lang:
    for a in list(actions):
        if a.get('Type') == 'UpdateContactTextToSpeechVoice':
            nxt = a['Transitions'].get('NextAction')
            lang_id = 'aicc-set-language'
            actions.append({
                "Identifier": lang_id,
                "Type": "UpdateContactData",
                "Parameters": {"LanguageCode": lang},
                "Transitions": {"NextAction": nxt,
                                "Errors": [{"ErrorType": "NoMatchingError", "NextAction": nxt}]}
            })
            a['Transitions']['NextAction'] = lang_id
            # keep the error transition on set-voice pointing at its original target
            pos = meta.get(a['Identifier'], {}).get('position', {'x': 0, 'y': 0})
            meta[lang_id] = {"position": {"x": pos.get('x', 0) + 140, "y": pos.get('y', 0) + 120},
                             "isFriendlyName": True, "dynamicParams": []}
            break

# 3) Upgrade legacy recording blocks to the current console block type.
#    UpdateContactRecordingBehavior is the OUTDATED block — the console now
#    uses UpdateContactRecordingAndAnalyticsBehavior (VoiceBehavior shape),
#    which supports RealTime + AutomatedInteraction analytics. API-verified:
#    the new type requires NoMatchingError + ChannelMismatch error branches.
for a in actions:
    if a.get('Type') != 'UpdateContactRecordingBehavior':
        continue
    p = a.get('Parameters', {})
    rb = p.get('RecordingBehavior', {}) or {}
    ab = p.get('AnalyticsBehavior', {}) or {}
    vab = {
        "Enabled": ab.get('Enabled', 'True'),
        "AnalyticsLanguage": ab.get('AnalyticsLanguage', lang),
        "AnalyticsModes": ["RealTime", "AutomatedInteraction"],
        "ConversationalAnalyticsRedactionConfiguration": {"Enabled": "False"},
        "SentimentConfiguration": ab.get('SentimentConfiguration', {"Enabled": "True"}),
        "SummaryConfiguration": {"SummaryModes": ["PostContact", "AutomatedInteraction"]},
    }
    a['Type'] = 'UpdateContactRecordingAndAnalyticsBehavior'
    a['Parameters'] = {"VoiceBehavior": {
        "VoiceRecordingBehavior": {
            "RecordedParticipants": rb.get('RecordedParticipants', ["Agent", "Customer"]),
            "IVRRecordingBehavior": rb.get('IVRRecordingBehavior', 'Enabled'),
        },
        "VoiceAnalyticsBehavior": vab,
    }}
    # required error branches for the new block type
    tr = a.setdefault('Transitions', {})
    nxt = tr.get('NextAction')
    have = {e.get('ErrorType') for e in tr.get('Errors', [])}
    tr.setdefault('Errors', [])
    for et in ('NoMatchingError', 'ChannelMismatch'):
        if et not in have and nxt:
            tr['Errors'].append({"ErrorType": et, "NextAction": nxt})

# 4) Agentic voice ASR tuning: set Lex session attributes on the
#    Get customer input (ConnectParticipantWithLexBot) block per best practices
if asr_conf:
    for a in actions:
        if a.get('Type') == 'ConnectParticipantWithLexBot':
            attrs = a['Parameters'].setdefault('LexSessionAttributes', {})
            attrs['x-amz-lex:audio:end-confidence-threshold:*:*'] = asr_conf
            attrs['x-amz-lex:audio:end-timeout-ms:*:*'] = asr_timeout
            attrs['x-amz-lex:allow-interrupt:*:*'] = allow_interrupt

json.dump(d, open(path, 'w'), ensure_ascii=False, indent=1)
print("   Flow JSON post-processing done (voice=%s/%s, lang=%s)" % (voice, engine, lang))
PYEOF

    # check for unresolved placeholders
    local remaining
    remaining=$(grep -o '{{[A-Z_]*}}' "$WORK_FLOW" | sort -u || true)
    if [ -n "$remaining" ]; then
        warn "Unresolved placeholders remain (configure in console after import):"
        echo "$remaining" | sed 's/^/        /'
        # leftover placeholders may fail ARN validation on import,
        # so confirm with the user before attempting
        if ! ask_yn "Attempt the import anyway?" "y"; then
            info "Contact Flow import skipped. File: $WORK_FLOW"
            return 0
        fi
    fi

    # ── Import (create or update content); auto-fix problems and retry ──────
    local EXISTING_FLOW_ID
    EXISTING_FLOW_ID=$(aws connect list-contact-flows --instance-id "$CONNECT_INSTANCE_ID" \
        --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for f in json.load(sys.stdin).get('ContactFlowSummaryList', []):
    if f.get('Name') == '${FLOW_NAME}': print(f['Id']); break
" 2>/dev/null || echo "")

    local attempt=1 RESULT=""
    while [ $attempt -le 3 ]; do
        if [ -n "$EXISTING_FLOW_ID" ]; then
            RESULT=$(aws connect update-contact-flow-content \
                --instance-id "$CONNECT_INSTANCE_ID" --contact-flow-id "$EXISTING_FLOW_ID" \
                --content "file://$WORK_FLOW" --region "$REGION" \
                --cli-error-format json --output json 2>&1) && {
                CONTACT_FLOW_ID="$EXISTING_FLOW_ID"
                ok "Updated existing Contact Flow content: $FLOW_NAME"
                break
            }
        else
            RESULT=$(aws connect create-contact-flow \
                --instance-id "$CONNECT_INSTANCE_ID" --name "$FLOW_NAME" \
                --type CONTACT_FLOW --status PUBLISHED \
                --content "file://$WORK_FLOW" --region "$REGION" \
                --cli-error-format json --output json 2>&1) && {
                CONTACT_FLOW_ID=$(jget "$RESULT" "ContactFlowId")
                CONTACT_FLOW_ARN=$(jget "$RESULT" "ContactFlowArn")
                ok "Contact Flow created & published: $FLOW_NAME ($CONTACT_FLOW_ID)"
                break
            }
        fi
        # failed -> parse problems and attempt auto-fix
        warn "Import failed (attempt $attempt/3). Analyzing problems..."
        echo "$RESULT" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    for p in d.get('problems', []): print('        -', p.get('message'))
except Exception: pass
" 2>/dev/null || true
        # generic auto-fix: remove invalid parameters named in problem messages
        python3 - "$WORK_FLOW" <<PYEOF 2>/dev/null || true
import sys, json, re
problems = '''$RESULT'''
path = sys.argv[1]
d = json.load(open(path))
# Invalid Action property name. Path: Actions[i].Parameters.X -> drop that parameter
for m in re.finditer(r'Actions\[(\d+)\]\.Parameters\.([A-Za-z]+)', problems):
    idx, prop = int(m.group(1)), m.group(2)
    if idx < len(d['Actions']):
        d['Actions'][idx]['Parameters'].pop(prop, None)
json.dump(d, open(path, 'w'), ensure_ascii=False, indent=1)
PYEOF
        attempt=$((attempt+1))
    done

    if [ -z "$CONTACT_FLOW_ID" ]; then
        warn "Contact Flow import failed. Import manually in the console: $WORK_FLOW"
        return 0
    fi
    if [ -z "$CONTACT_FLOW_ARN" ]; then
        CONTACT_FLOW_ARN=$(aws connect describe-contact-flow \
            --instance-id "$CONNECT_INSTANCE_ID" --contact-flow-id "$CONTACT_FLOW_ID" \
            --region "$REGION" --query 'ContactFlow.Arn' --output text 2>/dev/null || echo "")
    fi
    state_set CONTACT_FLOW_ID "$CONTACT_FLOW_ID"
}

# =============================================================================
# Phase 12: AI Prompt + AI Agent + Security Profile (automates chapter 6)
#   - AI Prompt: creates ORCHESTRATION prompt from asset yaml (model is a choice)
#   - AI Agent: copies the system SelfServiceOrchestratorVoice/Chat config and
#               auto-configures MCP tools from openapi operationIds (confirmation policy is a choice)
#   - Security Profile: grants MCP tool access + attaches to the AI Agent
#   - Sets the default self-service agent (orchestratorUseCase=Connect.SelfService)
# =============================================================================
phase_ai_agent() {
    echo ""
    echo "🧠 Phase 12: AI Prompt + AI Agent + Security Profile..."
    [ -z "${AI_ASSISTANT_ID:-}" ] && { warn "No assistant, skipping"; return 0; }
    [ -z "$PROMPT_FILE" ] && { warn "No prompts/ asset, skipping"; return 0; }

    # ── Model selection ─────────────────────────────────────────────────────
    choose "Select the model for the AI Prompt" 1 \
        "Claude Haiku 4.5   (fast/low-cost — recommended)" \
        "Claude Sonnet 4.5  (accurate/complex scenarios)"
    case "$CHOICE" in
        1) MODEL_ID="global.anthropic.claude-haiku-4-5-20251001-v1:0" ;;
        2) MODEL_ID="global.anthropic.claude-sonnet-4-5-20250929-v1:0" ;;
    esac
    info "Model: $MODEL_ID"

    # ── Create AI Prompt (asset yaml verbatim) ──────────────────────────────
    AI_PROMPT_ID=$(aws qconnect list-ai-prompts --assistant-id "$AI_ASSISTANT_ID" \
        --origin CUSTOMER --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('aiPromptSummaries', []):
    if p.get('name') == '${PROMPT_NAME}': print(p['aiPromptId'].split(':')[0]); break
" 2>/dev/null || echo "")
    if [ -z "$AI_PROMPT_ID" ]; then
        info "Creating AI Prompt: $PROMPT_NAME"
        python3 - "$PROMPT_FILE" <<PYEOF
import json, sys
text = open(sys.argv[1]).read()
req = {
  "assistantId": "${AI_ASSISTANT_ID}",
  "name": "${PROMPT_NAME}",
  "type": "ORCHESTRATION",
  "templateType": "TEXT",
  "modelId": "${MODEL_ID}",
  "apiFormat": "MESSAGES",
  "visibilityStatus": "PUBLISHED",
  "templateConfiguration": {"textFullAIPromptEditTemplateConfiguration": {"text": text}}
}
json.dump(req, open('/tmp/_ai_prompt_req.json', 'w'), ensure_ascii=False)
PYEOF
        PROMPT_RESULT=$(aws qconnect create-ai-prompt \
            --cli-input-json file:///tmp/_ai_prompt_req.json \
            --region "$REGION" --output json 2>&1) || true
        rm -f /tmp/_ai_prompt_req.json
        AI_PROMPT_ID=$(jget "$PROMPT_RESULT" "aiPrompt.aiPromptId" | cut -d: -f1)
        if [ -z "$AI_PROMPT_ID" ]; then
            warn "AI Prompt creation failed: $(echo "$PROMPT_RESULT" | head -3)"
            return 0
        fi
    else
        info "Reusing existing AI Prompt: $PROMPT_NAME"
    fi
    # publish a version
    PROMPT_VERSION=$(aws qconnect create-ai-prompt-version \
        --assistant-id "$AI_ASSISTANT_ID" --ai-prompt-id "$AI_PROMPT_ID" \
        --region "$REGION" --query 'versionNumber' --output text 2>/dev/null || echo "1")
    AI_PROMPT_VERSIONED="${AI_PROMPT_ID}:${PROMPT_VERSION}"
    ok "AI Prompt: $AI_PROMPT_VERSIONED"
    state_set AI_PROMPT_ID "$AI_PROMPT_ID"

    # ── Channel selection (system agent to copy from) ───────────────────────
    choose "Select the AI Agent base channel (source of default tool config)" 1 \
        "Voice (phone-first — copy SelfServiceOrchestratorVoice)" \
        "Chat  (chat-first — copy SelfServiceOrchestratorChat)"
    local SYS_AGENT_NAME="SelfServiceOrchestratorVoice"
    [ "$CHOICE" = "2" ] && SYS_AGENT_NAME="SelfServiceOrchestratorChat"

    # ── MCP tool preview + user-confirmation policy ─────────────────────────
    #    toolId = gateway_{GATEWAY_ID}__{targetName}___{operationId}
    #    read-only ops (get/check/track/list/search/find/query/lookup) -> no confirmation
    local TOOL_PREVIEW=""
    if [ -n "$OPENAPI_FILE" ] && [ -n "${GATEWAY_ID:-}" ]; then
        TOOL_PREVIEW=$(python3 - "$OPENAPI_FILE" "$TARGET_NAME" <<'PYEOF'
import sys, re
ops = re.findall(r'operationId:\s*([A-Za-z0-9_\-]+)', open(sys.argv[1]).read())
target = sys.argv[2]
readonly_prefix = ('get','check','track','list','search','find','query','lookup','describe','read','retrieve')
for op in ops:
    confirm = 'false' if op.lower().startswith(readonly_prefix) else 'true'
    print(f"{target}___{op}\t{confirm}")
PYEOF
)
        echo ""
        info "Auto-configured MCP tools (whether customer confirmation is required):"
        echo "$TOOL_PREVIEW" | while IFS=$'\t' read -r t c; do
            [ "$c" = "true" ] && echo "        🔒 $t  (confirmation required — mutating operation)" \
                              || echo "        👁  $t  (no confirmation — read-only operation)"
        done
        choose "How should per-tool user confirmation be set?" 1 \
            "Use the classification above (read-only=run, mutating=confirm — recommended)" \
            "Require confirmation for ALL tools" \
            "No confirmation for any tool"
        TOOL_CONFIRM_POLICY="$CHOICE"
    fi

    # ── Create AI Agent: copy system agent config + add MCP tools ───────────
    AI_AGENT_ID=$(aws qconnect list-ai-agents --assistant-id "$AI_ASSISTANT_ID" \
        --origin CUSTOMER --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('aiAgentSummaries', []):
    if a.get('name') == '${AGENT_NAME}': print(a['aiAgentId'].split(':')[0]); break
" 2>/dev/null || echo "")

    if [ -z "$AI_AGENT_ID" ]; then
        info "Creating AI Agent: $AGENT_NAME (base: $SYS_AGENT_NAME)"
        aws qconnect list-ai-agents --assistant-id "$AI_ASSISTANT_ID" \
            --origin SYSTEM --region "$REGION" --output json > /tmp/_sys_agents.json
        python3 - <<PYEOF
import json, re
sys_agents = json.load(open('/tmp/_sys_agents.json'))['aiAgentSummaries']
base = next(a for a in sys_agents if a['name'] == '${SYS_AGENT_NAME}')
cfg = base['configuration']['orchestrationAIAgentConfiguration']

# swap the system prompt for ours
cfg['orchestrationAIPromptId'] = '${AI_PROMPT_VERSIONED}'
cfg['connectInstanceArn'] = '${CONNECT_INSTANCE_ARN}'
cfg['locale'] = '${LOCALE_ID:-en_US}'

# AWS-managed MCP tools (e.g. Retrieve) reject overriding description/schema
# on create; keep only the identifying fields copied from the system config
cfg['toolConfigurations'] = [
    ({k: v for k, v in t.items() if k in ('toolName', 'toolType', 'toolId')}
     if (t.get('toolId') or '').startswith('aws_service__') else t)
    for t in cfg.get('toolConfigurations', [])
]

# add MCP tools (Complete/Escalate/Retrieve are copied from system config)
policy = '${TOOL_CONFIRM_POLICY:-1}'
preview = '''${TOOL_PREVIEW:-}'''
for line in preview.strip().splitlines():
    if not line.strip(): continue
    perm, auto_confirm = line.split('\t')
    op = perm.split('___', 1)[1]
    tool_name = perm.replace('-', '_')
    confirm = {'1': auto_confirm == 'true', '2': True, '3': False}[policy]
    cfg['toolConfigurations'].append({
        "toolName": tool_name,
        "toolType": "MODEL_CONTEXT_PROTOCOL",
        "toolId": "gateway_${GATEWAY_ID}__" + perm,
        "userInteractionConfiguration": {"isUserConfirmationRequired": confirm}
    })

req = {
  "assistantId": "${AI_ASSISTANT_ID}",
  "name": "${AGENT_NAME}",
  "type": "ORCHESTRATION",
  "visibilityStatus": "PUBLISHED",
  "configuration": {"orchestrationAIAgentConfiguration": cfg}
}
json.dump(req, open('/tmp/_ai_agent_req.json', 'w'), ensure_ascii=False)
PYEOF
        AGENT_RESULT=""
        # MCP tools may take a moment to propagate after integration registration
        for attempt in 1 2 3 4; do
            AGENT_RESULT=$(aws qconnect create-ai-agent \
                --cli-input-json file:///tmp/_ai_agent_req.json \
                --region "$REGION" --output json 2>&1) || true
            if echo "$AGENT_RESULT" | grep -q '"aiAgentId"'; then
                break
            elif echo "$AGENT_RESULT" | grep -q 'not found in MCP tools'; then
                info "MCP tools not visible yet (attempt $attempt/4), waiting 15s..."
                sleep 15
            else
                break
            fi
        done
        rm -f /tmp/_ai_agent_req.json /tmp/_sys_agents.json
        AI_AGENT_ID=$(jget "$AGENT_RESULT" "aiAgent.aiAgentId" | cut -d: -f1)
        AI_AGENT_ARN=$(jget "$AGENT_RESULT" "aiAgent.aiAgentArn")
        if [ -z "$AI_AGENT_ID" ]; then
            warn "AI Agent creation failed: $(echo "$AGENT_RESULT" | head -5)"
            return 0
        fi
    else
        info "Reusing existing AI Agent: $AGENT_NAME"
        AI_AGENT_ARN=$(aws qconnect get-ai-agent --assistant-id "$AI_ASSISTANT_ID" \
            --ai-agent-id "$AI_AGENT_ID" --region "$REGION" \
            --query 'aiAgent.aiAgentArn' --output text 2>/dev/null || echo "")
        if [ -z "$AI_AGENT_ARN" ] || [ "$AI_AGENT_ARN" = "None" ]; then
            AI_AGENT_ARN=$(aws qconnect list-ai-agents --assistant-id "$AI_ASSISTANT_ID" \
                --origin CUSTOMER --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('aiAgentSummaries', []):
    if a.get('name') == '${AGENT_NAME}':
        print(a.get('aiAgentArn','').split(':\$LATEST')[0]); break
" 2>/dev/null || echo "")
        fi
    fi
    state_set AI_AGENT_ID "$AI_AGENT_ID"

    # publish a version
    AGENT_VERSION=$(aws qconnect create-ai-agent-version \
        --assistant-id "$AI_ASSISTANT_ID" --ai-agent-id "$AI_AGENT_ID" \
        --region "$REGION" --query 'versionNumber' --output text 2>/dev/null || echo "1")
    ok "AI Agent: ${AI_AGENT_ID}:${AGENT_VERSION}"

    # ── Set as default self-service agent ───────────────────────────────────
    info "Setting as default self-service AI agent..."
    aws qconnect update-assistant-ai-agent \
        --assistant-id "$AI_ASSISTANT_ID" \
        --ai-agent-type ORCHESTRATION \
        --configuration "aiAgentId=${AI_AGENT_ID}:${AGENT_VERSION}" \
        --orchestrator-use-case "Connect.SelfService" \
        --region "$REGION" >/dev/null 2>&1 \
        && ok "Default self-service agent = $AGENT_NAME" \
        || warn "Failed to set default agent (check console)"

    # ── Security Profile: MCP tool access + attach to AI Agent ──────────────
    #    Without this profile attached to the AI Agent, every MCP tool call is
    #    rejected with "Tool is not allowed" (MCP -32001) and the AI escalates.
    if [ -n "${GATEWAY_ID:-}" ] && [ -z "${TOOL_PREVIEW:-}" ]; then
        warn "MCP tool list unavailable (missing OpenAPI spec) — security profile will grant base permissions only. Grant per-tool Access in console: Users > Security profiles > ${SP_NAME} > Tools"
    fi
    if [ -n "${GATEWAY_ID:-}" ]; then
        info "Configuring security profile (MCP tool access)..."
        local PERMS_JSON
        PERMS_JSON=$(echo "${TOOL_PREVIEW:-}" | python3 -c "
import sys, json
perms = [l.split('\t')[0] for l in sys.stdin.read().strip().splitlines() if l.strip()]
apps = [{'Namespace': '${GATEWAY_ID}', 'ApplicationPermissions': perms, 'Type': 'MCP'}] if perms else []
print(json.dumps(apps))
")
        SP_ID=$(aws connect list-security-profiles --instance-id "$CONNECT_INSTANCE_ID" \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for s in json.load(sys.stdin).get('SecurityProfileSummaryList', []):
    if s.get('Name') == '${SP_NAME}': print(s['Id']); break
" 2>/dev/null || echo "")
        local APPS_ARGS=()
        [ "$PERMS_JSON" != "[]" ] && APPS_ARGS=(--applications "$PERMS_JSON")
        if [ -z "$SP_ID" ]; then
            SP_RESULT=$(aws connect create-security-profile \
                --security-profile-name "$SP_NAME" \
                --description "AI agent tool permissions for ${PROJECT_NAME}" \
                --permissions '["BasicAgentAccess","Wisdom.View"]' \
                ${APPS_ARGS[@]+"${APPS_ARGS[@]}"} \
                --instance-id "$CONNECT_INSTANCE_ID" \
                --region "$REGION" --output json 2>&1) || true
            SP_ID=$(jget "$SP_RESULT" "SecurityProfileId")
            [ -n "$SP_ID" ] && info "Security profile created: $SP_NAME" || warn "Security profile creation failed: $(echo "$SP_RESULT" | head -3)"
        elif [ "$PERMS_JSON" != "[]" ]; then
            aws connect update-security-profile \
                --security-profile-id "$SP_ID" --instance-id "$CONNECT_INSTANCE_ID" \
                --applications "$PERMS_JSON" --region "$REGION" 2>/dev/null \
                && info "Updated tool permissions on existing security profile" || warn "Security profile update failed"
        fi
        state_set SP_ID "${SP_ID:-}"

        if [ -z "${AI_AGENT_ARN:-}" ]; then
            warn "AI Agent ARN could not be resolved — security profile NOT attached."
            warn "Attach manually: Connect console > AI agents > ${AGENT_NAME} > Security Profiles > ${SP_NAME}"
        elif [ -n "$SP_ID" ]; then
            # The entity ARN must carry a version qualifier, and the profile
            # must be attached to BOTH :$LATEST (what the console editor shows
            # as the draft) AND the PUBLISHED version (what the default
            # self-service assignment actually runs). Attaching only $LATEST
            # passes a naive check while runtime tool calls still fail with
            # "Tool is not allowed" — found in live workshop QA.
            local BASE_ARN="${AI_AGENT_ARN%:\$LATEST}"
            local PUBLISHED_OK="" LATEST_OK=""
            for QUAL in "\$LATEST" "${AGENT_VERSION:-1}"; do
                local ENTITY_ARN="${BASE_ARN}:${QUAL}"
                aws connect associate-security-profiles \
                    --instance-id "$CONNECT_INSTANCE_ID" \
                    --security-profiles "[{\"Id\":\"$SP_ID\"}]" \
                    --entity-type AI_AGENT \
                    --entity-arn "$ENTITY_ARN" \
                    --region "$REGION" >/dev/null 2>&1 || true
                # verify — never trust the call alone
                local VERIFIED
                VERIFIED=$(aws connect list-entity-security-profiles \
                    --instance-id "$CONNECT_INSTANCE_ID" \
                    --entity-type AI_AGENT --entity-arn "$ENTITY_ARN" \
                    --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
try:
    for p in json.load(sys.stdin).get('SecurityProfiles', []):
        if p.get('Id') == '${SP_ID}': print('yes'); break
except Exception: pass
" 2>/dev/null || echo "")
                if [ "$QUAL" = "\$LATEST" ]; then LATEST_OK="$VERIFIED"; else PUBLISHED_OK="$VERIFIED"; fi
            done
            if [ "$PUBLISHED_OK" = "yes" ]; then
                ok "Security profile attached to AI Agent v${AGENT_VERSION:-1} + \$LATEST — verified (tool access granted)"
            else
                warn "Security profile attachment to the PUBLISHED agent version could NOT be verified (\$LATEST: ${LATEST_OK:-no})."
                warn "Without it, tool calls fail with 'Tool is not allowed' (MCP -32001)."
                warn "Attach manually: Connect console > AI agents > ${AGENT_NAME} > Security Profiles > select '${SP_NAME}' > Publish"
            fi
        fi
    fi
}

# =============================================================================
# Phase 13: Phone number claim + flow association (optional, chapter 6 finale)
# =============================================================================
CLAIMED_PHONE=""

phase_phone_number() {
    echo ""
    echo "☎️  Phase 13: Phone number claim (optional)..."
    [ -z "${CONTACT_FLOW_ID:-}" ] && { info "No Contact Flow, skipping"; return 0; }

    # skip when a number was already claimed for this project
    local EXISTING_NUM
    EXISTING_NUM=$(aws connect list-phone-numbers-v2 --instance-id "$CONNECT_INSTANCE_ID" \
        --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('ListPhoneNumbersSummaryList', []):
    if p.get('PhoneNumberDescription') == '${PROJECT_NAME}-workshop':
        print(p['PhoneNumber']); break
" 2>/dev/null || echo "")
    if [ -n "$EXISTING_NUM" ]; then
        CLAIMED_PHONE="$EXISTING_NUM"
        ok "Reusing previously claimed number: $CLAIMED_PHONE"
        return 0
    fi

    if [ -n "$AUTO" ]; then
        info "AUTO_CONFIRM mode — skipping phone claim (avoids charges)"
        return 0
    fi
    if ! ask_yn "Claim a US phone number now and attach it to the flow? (incurs charges)" "n"; then
        info "Phone claim skipped. You can do it later in console (Channels > Phone numbers)"
        return 0
    fi

    choose "Select the number type" 1 \
        "DID (direct-dial local number)" \
        "TOLL_FREE (toll-free)"
    local NUM_TYPE="DID"
    [ "$CHOICE" = "2" ] && NUM_TYPE="TOLL_FREE"

    local AVAIL
    AVAIL=$(aws connect search-available-phone-numbers \
        --target-arn "$CONNECT_INSTANCE_ARN" \
        --phone-number-country-code US \
        --phone-number-type "$NUM_TYPE" \
        --max-results 8 --region "$REGION" --output json 2>&1) || true
    local nums=()
    while read -r n; do [ -n "$n" ] && nums+=("$n"); done < <(echo "$AVAIL" | python3 -c "
import sys, json
try:
    for p in json.load(sys.stdin).get('AvailableNumbersList', []):
        print(p['PhoneNumber'])
except Exception: pass
" 2>/dev/null)
    if [ ${#nums[@]} -eq 0 ]; then
        warn "Failed to search available numbers: $(echo "$AVAIL" | head -2)"
        return 0
    fi

    choose "Select the number to claim" 1 "${nums[@]}" "Cancel"
    [ "$CHOICE_VALUE" = "Cancel" ] && { info "Claim cancelled"; return 0; }
    local PICKED="$CHOICE_VALUE"

    local CLAIM_RESULT
    CLAIM_RESULT=$(aws connect claim-phone-number \
        --instance-id "$CONNECT_INSTANCE_ID" \
        --phone-number "$PICKED" \
        --phone-number-description "${PROJECT_NAME}-workshop" \
        --region "$REGION" --output json 2>&1) || true
    local PHONE_ID
    PHONE_ID=$(jget "$CLAIM_RESULT" "PhoneNumberId")
    if [ -z "$PHONE_ID" ]; then
        warn "Phone claim failed: $(echo "$CLAIM_RESULT" | head -2)"
        return 0
    fi
    CLAIMED_PHONE="$PICKED"
    state_set PHONE_NUMBER_ID "$PHONE_ID"
    ok "Number claimed: $PICKED"

    # attach number -> contact flow
    sleep 3  # brief propagation wait after claim
    aws connect associate-phone-number-contact-flow \
        --phone-number-id "$PHONE_ID" \
        --instance-id "$CONNECT_INSTANCE_ID" \
        --contact-flow-id "$CONTACT_FLOW_ID" \
        --region "$REGION" 2>/dev/null \
        && ok "Number attached to Contact Flow ($FLOW_NAME) — call it now!" \
        || warn "Number-flow association failed (attach manually in console)"
}

# =============================================================================
# Summary
# =============================================================================
do_summary() {
    echo ""
    hr
    echo "  ✅ Deployment Complete!"
    hr
    echo ""
    echo "  Project:           $PROJECT_NAME"
    echo "  Region:            $REGION"
    echo "  API Endpoint:      ${API_ENDPOINT:-N/A}"
    echo "  API Key:           ${API_KEY:0:10}..."
    echo ""
    echo "  Connect Instance:  ${CONNECT_INSTANCE_ID:-N/A} (${CONNECT_ALIAS:-})"
    echo "  Assistant:         ${AI_ASSISTANT_ID:-N/A}"
    echo "  Gateway:           ${GATEWAY_ID:-N/A}"
    echo "  Lex Bot:           ${BOT_ID:-N/A} ($BOT_NAME, ${LOCALE_ID:-})"
    echo "  Contact Flow:      ${CONTACT_FLOW_ID:-N/A} ($FLOW_NAME)"
    echo "  AI Agent:          ${AI_AGENT_ID:-N/A} ($AGENT_NAME)"
    if [ "${VOICE_PROVIDER:-polly}" = "agentic" ]; then
        echo "  Voice:             Polly ${LEX_VOICE_ID:-N/A} (temporary) -> switch to Amazon Connect agentic voice in console (see below)"
    else
        echo "  Voice:             ${LEX_VOICE_ID:-N/A}/${LEX_VOICE_ENGINE:-} (${FLOW_LANG:-})"
    fi
    echo "  Speech Mode:       ${SPEECH_MODE:-N/A}"
    [ -n "${CLAIMED_PHONE:-}" ] && echo "  📞 Phone number:    $CLAIMED_PHONE  <- call it now!"
    echo ""
    echo "  ─── How to test ───"
    echo ""
    if [ -n "${CLAIMED_PHONE:-}" ]; then
        echo "  1. Call $CLAIMED_PHONE and talk to the AI agent"
    fi
    echo "  · Test chat: https://${CONNECT_ALIAS}.my.connect.aws/test-chat"
    echo "    (pick flow '$FLOW_NAME' in Test Settings, then chat)"
    echo "  · Trace AI agent reasoning & tool calls:"
    echo "    aws qconnect list-spans --assistant-id ${AI_ASSISTANT_ID:-<ID>} \\"
    echo "      --session-id <last UUID of WisdomSessionArn> --region $REGION"
    echo ""
    if [ "${VOICE_PROVIDER:-polly}" = "agentic" ]; then
        echo "  ─── ⚠️  REQUIRED console step — switch to agentic voice (~1 min) ───"
        echo "  The flow currently speaks with Polly ${LEX_VOICE_ID:-} (a working fallback)."
        echo "  To use Amazon Connect agentic voice as selected:"
        echo "    Flows > $FLOW_NAME > open the 'Set voice' block >"
        echo "    Voice Provider: Amazon Connect agentic voice > Language: ${FLOW_LANG:-} >"
        echo "    pick a voice (Listen to voice sample) > keep 'Set language attribute' ON >"
        echo "    Save > Publish"
        echo "  (AWS exposes no API to list/set agentic voices yet — console only)"
        echo ""
    fi
    echo "  ─── Cleanup ───"
    echo "  ./deploy.sh cleanup"
    echo ""
    hr
}

# =============================================================================
# DEPLOY: phase runner
# =============================================================================
do_deploy() {
    if [ -z "$CFN_TEMPLATE" ]; then
        echo "❌ No CloudFormation template found under cloudformation/."
        exit 1
    fi
    do_preflight

    # ── Deployment scope ─────────────────────────────────────────────────────
    #    full: everything through AI agent + phone number (chapters 2-6)
    #    core: infrastructure/gateway only (chapters 5-6 done by hand in the
    #          console — the workshop learning path)
    #    Env override: DEPLOY_SCOPE=full|core
    local SCOPE="${DEPLOY_SCOPE:-}"
    if [ -z "$SCOPE" ]; then
        choose "Select the deployment scope" 1 \
            "Full automation      — everything incl. Lex bot, Contact Flow, AI Agent, security profile (workshop chapters 2-6)" \
            "Core infrastructure  — stack/Lambda/Connect/Assistant/Gateway/MCP only; do Lex/Flow/AI Agent yourself in the console (learning path)"
        [ "$CHOICE" = "2" ] && SCOPE="core" || SCOPE="full"
    fi
    state_set DEPLOY_SCOPE "$SCOPE"

    phase_cloudformation
    phase_lambda_code
    phase_openapi
    phase_faq
    phase_connect_instance
    phase_assistant
    phase_env_vars
    phase_gateway
    phase_connect_integrations
    if [ "$SCOPE" = "core" ]; then
        echo ""
        info "Core scope selected — stopping after MCP registration."
        info "Continue in the console with workshop chapters 5-6 (Lex bot, Contact Flow, AI Agent),"
        info "or re-run './deploy.sh' and pick Full automation at any time (idempotent)."
        do_summary
        return 0
    fi
    phase_lex_bot
    phase_contact_flow
    phase_ai_agent
    phase_phone_number
    do_summary
}

# =============================================================================
# CLEANUP (reverse order — phone/flow/AI agent/bot/MCP/Gateway/Assistant/CFN)
# =============================================================================
do_cleanup() {
    hr
    echo "  AICC Builder - Resource Cleanup"
    echo "  Project: $PROJECT_NAME | Region: $REGION | Account: $ACCOUNT_ID"
    hr
    echo ""
    echo "  ⚠️  This will delete ALL of the following resources:"
    echo "     Phone numbers, Contact Flow, AI Agent/Prompt, security profile, Lex bot,"
    echo "     MCP integration, all AgentCore Gateway resources, Assistant + KB, CloudFormation stack"
    echo "     * The Connect instance itself is NOT deleted."
    echo ""
    if [ -z "$AUTO" ]; then
        read -r -p "  Are you sure you want to delete everything? (yes/no): " CONFIRM
        [ "$CONFIRM" != "yes" ] && { echo "  Cancelled."; exit 0; }
    fi
    echo ""

    local INSTANCE_ID="${CONNECT_INSTANCE_ID:-$(state_get CONNECT_INSTANCE_ID)}"
    if [ -z "$INSTANCE_ID" ]; then
        INSTANCES_JSON=$(aws connect list-instances --region "$REGION" --output json 2>/dev/null || echo '{"InstanceSummaryList":[]}')
        N=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('InstanceSummaryList',[])))" 2>/dev/null || echo 0)
        [ "$N" = "1" ] && INSTANCE_ID=$(echo "$INSTANCES_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['InstanceSummaryList'][0]['Id'])")
    fi

    # ── Release phone numbers ───────────────────────────────────────────────
    echo "☎️  Releasing phone numbers..."
    if [ -n "$INSTANCE_ID" ]; then
        aws connect list-phone-numbers-v2 --instance-id "$INSTANCE_ID" \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('ListPhoneNumbersSummaryList', []):
    if p.get('PhoneNumberDescription') == '${PROJECT_NAME}-workshop':
        print(p['PhoneNumberId'])
" 2>/dev/null | while read -r pid; do
            [ -z "$pid" ] && continue
            info "Releasing number: $pid"
            aws connect release-phone-number --phone-number-id "$pid" --region "$REGION" 2>/dev/null || true
        done
    fi

    # ── Contact Flow ─────────────────────────────────────────────────────────
    echo "📋 Deleting Contact Flow..."
    if [ -n "$INSTANCE_ID" ]; then
        FLOW_ID=$(aws connect list-contact-flows --instance-id "$INSTANCE_ID" \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for f in json.load(sys.stdin).get('ContactFlowSummaryList', []):
    if f.get('Name') == '${FLOW_NAME}': print(f['Id']); break
" 2>/dev/null || echo "")
        [ -n "$FLOW_ID" ] && aws connect delete-contact-flow --instance-id "$INSTANCE_ID" \
            --contact-flow-id "$FLOW_ID" --region "$REGION" 2>/dev/null && info "Deleted: $FLOW_NAME" || true
    fi

    # ── AI Agent / Prompt / Security Profile ────────────────────────────────
    echo "🧠 Deleting AI Agent / Prompt / security profile..."
    ASST_ID="${AI_ASSISTANT_ID:-$(state_get AI_ASSISTANT_ID)}"
    if [ -z "$ASST_ID" ]; then
        ASST_ID=$(aws qconnect list-assistants --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assistantSummaries', []):
    if a.get('name') == '${PROJECT_NAME}-assistant': print(a['assistantId']); break
" 2>/dev/null || echo "")
    fi
    if [ -n "$ASST_ID" ]; then
        aws qconnect remove-assistant-ai-agent --assistant-id "$ASST_ID" \
            --ai-agent-type ORCHESTRATION --region "$REGION" 2>/dev/null || true
        AGENT_ID=$(aws qconnect list-ai-agents --assistant-id "$ASST_ID" --origin CUSTOMER \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('aiAgentSummaries', []):
    if a.get('name') == '${AGENT_NAME}': print(a['aiAgentId'].split(':')[0]); break
" 2>/dev/null || echo "")
        if [ -n "$AGENT_ID" ]; then
            for v in $(aws qconnect list-ai-agent-versions --assistant-id "$ASST_ID" \
                --ai-agent-id "$AGENT_ID" --region "$REGION" \
                --query 'aiAgentVersionSummaries[].versionNumber' --output text 2>/dev/null); do
                aws qconnect delete-ai-agent-version --assistant-id "$ASST_ID" \
                    --ai-agent-id "$AGENT_ID" --version-number "$v" --region "$REGION" 2>/dev/null || true
            done
            aws qconnect delete-ai-agent --assistant-id "$ASST_ID" \
                --ai-agent-id "$AGENT_ID" --region "$REGION" 2>/dev/null && info "AI Agent deleted: $AGENT_NAME" || true
        fi
        PROMPT_ID=$(aws qconnect list-ai-prompts --assistant-id "$ASST_ID" --origin CUSTOMER \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('aiPromptSummaries', []):
    if p.get('name') == '${PROMPT_NAME}': print(p['aiPromptId'].split(':')[0]); break
" 2>/dev/null || echo "")
        if [ -n "$PROMPT_ID" ]; then
            for v in $(aws qconnect list-ai-prompt-versions --assistant-id "$ASST_ID" \
                --ai-prompt-id "$PROMPT_ID" --region "$REGION" \
                --query 'aiPromptVersionSummaries[].versionNumber' --output text 2>/dev/null); do
                aws qconnect delete-ai-prompt-version --assistant-id "$ASST_ID" \
                    --ai-prompt-id "$PROMPT_ID" --version-number "$v" --region "$REGION" 2>/dev/null || true
            done
            aws qconnect delete-ai-prompt --assistant-id "$ASST_ID" \
                --ai-prompt-id "$PROMPT_ID" --region "$REGION" 2>/dev/null && info "AI Prompt deleted: $PROMPT_NAME" || true
        fi
    fi
    if [ -n "$INSTANCE_ID" ]; then
        SP_ID=$(aws connect list-security-profiles --instance-id "$INSTANCE_ID" \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for s in json.load(sys.stdin).get('SecurityProfileSummaryList', []):
    if s.get('Name') == '${SP_NAME}': print(s['Id']); break
" 2>/dev/null || echo "")
        if [ -n "$SP_ID" ]; then
            # An SP attached to an AI agent entity can't be deleted; parse the
            # associated entity ARNs from the error and disassociate them first.
            SP_DEL_ERR=$(aws connect delete-security-profile --instance-id "$INSTANCE_ID" \
                --security-profile-id "$SP_ID" --region "$REGION" 2>&1) || true
            if echo "$SP_DEL_ERR" | grep -q ResourceInUseException; then
                echo "$SP_DEL_ERR" | grep -oE 'arn:aws:wisdom:[^],[:space:]]+' | while read -r earn; do
                    aws connect disassociate-security-profiles --instance-id "$INSTANCE_ID" \
                        --security-profiles "[{\"Id\":\"$SP_ID\"}]" \
                        --entity-type AI_AGENT --entity-arn "$earn" \
                        --region "$REGION" 2>/dev/null || true
                done
                aws connect delete-security-profile --instance-id "$INSTANCE_ID" \
                    --security-profile-id "$SP_ID" --region "$REGION" 2>/dev/null || true
            fi
            aws connect describe-security-profile --security-profile-id "$SP_ID" \
                --instance-id "$INSTANCE_ID" --region "$REGION" &>/dev/null \
                && warn "Security profile could not be deleted: $SP_NAME" \
                || info "Security profile deleted: $SP_NAME"
        fi
    fi

    # ── Lex bot ─────────────────────────────────────────────────────────────
    echo "🗣️  Deleting Lex bot..."
    LEX_BOT_ID=$(aws lexv2-models list-bots --region "$REGION" \
        --filters "name=BotName,values=$BOT_NAME,operator=EQ" \
        --query 'botSummaries[0].botId' --output text 2>/dev/null || echo "")
    [ "$LEX_BOT_ID" = "None" ] && LEX_BOT_ID=""
    if [ -n "$LEX_BOT_ID" ]; then
        if [ -n "$INSTANCE_ID" ]; then
            aws connect disassociate-bot --instance-id "$INSTANCE_ID" \
                --lex-v2-bot "AliasArn=arn:aws:lex:${REGION}:${ACCOUNT_ID}:bot-alias/${LEX_BOT_ID}/TSTALIASID" \
                --region "$REGION" 2>/dev/null || true
        fi
        aws lexv2-models delete-bot --bot-id "$LEX_BOT_ID" \
            --skip-resource-in-use-check --region "$REGION" >/dev/null 2>&1 && info "Bot deleted: $BOT_NAME" || true
    fi

    # ── MCP integration ─────────────────────────────────────────────────────
    echo "🔗 Removing MCP integration..."
    # find ALL project MCP apps (by name prefix OR namespace prefix) — a stale
    # app from an earlier gateway generation must be removed too
    aws appintegrations list-applications --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('Applications', []):
    if a.get('Name','').startswith('${MCP_APP_NAME}') or a.get('Namespace','').startswith('${GATEWAY_NAME}'):
        print(a['Arn'])
" 2>/dev/null | while read -r app_arn; do
        [ -z "$app_arn" ] && continue
        if [ -n "$INSTANCE_ID" ]; then
            aws connect list-integration-associations --instance-id "$INSTANCE_ID" \
                --integration-type APPLICATION --region "$REGION" --output json 2>/dev/null | \
                APP_ARN="$app_arn" python3 -c "
import sys, json, os
for a in json.load(sys.stdin).get('IntegrationAssociationSummaryList', []):
    if a.get('IntegrationArn') == os.environ['APP_ARN']: print(a['IntegrationAssociationId'])
" 2>/dev/null | while read -r aid; do
                [ -z "$aid" ] && continue
                aws connect delete-integration-association --instance-id "$INSTANCE_ID" \
                    --integration-association-id "$aid" --region "$REGION" 2>/dev/null || true
            done
        fi
        sleep 2
        aws appintegrations delete-application --arn "$app_arn" \
            --region "$REGION" 2>/dev/null && info "MCP app deleted: $app_arn" || warn "MCP app not deleted (check associations): $app_arn"
    done

    # ── WISDOM integration + Assistant + KB ─────────────────────────────────
    echo "🤖 Deleting Assistant / KB..."
    # Ownership guard: only touch the assistant (and its Connect association)
    # when it was created by this project (name match).
    ASST_OWNED=false
    if [ -n "$ASST_ID" ]; then
        ASST_NAME=$(aws qconnect get-assistant --assistant-id "$ASST_ID" --region "$REGION" \
            --query 'assistant.name' --output text 2>/dev/null || echo "")
        [ "$ASST_NAME" = "${PROJECT_NAME}-assistant" ] && ASST_OWNED=true
    fi
    if [ -n "$INSTANCE_ID" ] && [ "$ASST_OWNED" = "true" ]; then
        aws connect list-integration-associations --instance-id "$INSTANCE_ID" \
            --integration-type WISDOM_ASSISTANT --region "$REGION" --output json 2>/dev/null | \
            python3 -c "
import sys, json
for a in json.load(sys.stdin).get('IntegrationAssociationSummaryList', []):
    if a.get('IntegrationArn','').endswith('$ASST_ID'): print(a['IntegrationAssociationId'])
" 2>/dev/null | while read -r aid; do
            [ -z "$aid" ] && continue
            aws connect delete-integration-association --instance-id "$INSTANCE_ID" \
                --integration-association-id "$aid" --region "$REGION" 2>/dev/null || true
        done
    fi
    KB_ID=$(aws qconnect list-knowledge-bases --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for kb in json.load(sys.stdin).get('knowledgeBaseSummaries', []):
    if kb.get('name') == '${KB_NAME}': print(kb['knowledgeBaseId']); break
" 2>/dev/null || echo "")
    if [ "$ASST_OWNED" = "true" ]; then
        aws qconnect list-assistant-associations --assistant-id "$ASST_ID" \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assistantAssociationSummaries', []):
    print(a['assistantAssociationId'])
" 2>/dev/null | while read -r aaid; do
            [ -z "$aaid" ] && continue
            aws qconnect delete-assistant-association --assistant-id "$ASST_ID" \
                --assistant-association-id "$aaid" --region "$REGION" 2>/dev/null || true
        done
        aws qconnect delete-assistant --assistant-id "$ASST_ID" --region "$REGION" 2>/dev/null \
            && info "Assistant deleted" || true
    elif [ -n "$ASST_ID" ]; then
        info "Assistant ($ASST_NAME) is not owned by this project — keeping it and its associations"
    fi
    [ -n "$KB_ID" ] && aws qconnect delete-knowledge-base --knowledge-base-id "$KB_ID" \
        --region "$REGION" 2>/dev/null && info "KB deleted" || true

    # ── Gateway resources ───────────────────────────────────────────────────
    echo "🌐 Deleting AgentCore Gateway..."
    if aws bedrock-agentcore-control help &>/dev/null; then
        GW_ID=$(aws bedrock-agentcore-control list-gateways --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
for gw in d.get('items', d.get('gateways', [])):
    if gw.get('name') == '${GATEWAY_NAME}': print(gw['gatewayId']); break
" 2>/dev/null || echo "")
        if [ -n "$GW_ID" ]; then
            aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GW_ID" \
                --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
for t in d.get('items', d.get('targets', [])):
    print(t.get('targetId',''))
" 2>/dev/null | while read -r tid; do
                [ -z "$tid" ] && continue
                aws bedrock-agentcore-control delete-gateway-target \
                    --gateway-identifier "$GW_ID" --target-id "$tid" \
                    --region "$REGION" >/dev/null 2>&1 || true
            done
            # wait until all targets are actually gone
            for i in $(seq 1 24); do
                TCOUNT=$(aws bedrock-agentcore-control list-gateway-targets \
                    --gateway-identifier "$GW_ID" --region "$REGION" --output json 2>/dev/null | \
                    python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('items', d.get('targets', []))))" 2>/dev/null || echo 0)
                [ "$TCOUNT" = "0" ] && break
                sleep 5
            done
            aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GW_ID" \
                --region "$REGION" >/dev/null 2>&1 || true
            echo -n "   Waiting for gateway deletion..."
            for i in $(seq 1 30); do
                aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" \
                    --region "$REGION" &>/dev/null || { echo " ✅"; break; }
                echo -n "."; sleep 5
            done
        fi
        CRED_ARN=$(aws bedrock-agentcore-control list-api-key-credential-providers \
            --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('credentialProviders', []):
    if p.get('name') == '${CRED_PROVIDER_NAME}': print(p['credentialProviderArn']); break
" 2>/dev/null || echo "")
        [ -n "$CRED_ARN" ] && aws bedrock-agentcore-control delete-api-key-credential-provider \
            --name "$CRED_PROVIDER_NAME" --region "$REGION" 2>/dev/null || true
    fi

    # ── Gateway IAM Role ────────────────────────────────────────────────────
    echo "🔑 Deleting Gateway IAM role..."
    if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
        for pname in $(aws iam list-role-policies --role-name "$ROLE_NAME" \
            --query 'PolicyNames' --output text 2>/dev/null); do
            aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$pname" 2>/dev/null || true
        done
        for parn in $(aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
            --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
            aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$parn" 2>/dev/null || true
        done
        aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null && info "IAM role deleted" || true
    fi

    # ── S3 assets + CFN stack + template bucket ─────────────────────────────
    echo "📦 Deleting CloudFormation stack..."
    KB_BUCKET=$(get_output "KnowledgeBaseBucketName")
    if [ -n "$KB_BUCKET" ]; then
        aws s3 rm "s3://$KB_BUCKET" --recursive --region "$REGION" >/dev/null 2>&1 || true
        purge_bucket_versions "$KB_BUCKET"
    fi
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
        aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
        info "Waiting for stack deletion..."
        aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" || true
    fi
    if aws s3 ls "s3://$TEMPLATE_BUCKET" --region "$REGION" &>/dev/null; then
        aws s3 rm "s3://$TEMPLATE_BUCKET" --recursive --region "$REGION" >/dev/null 2>&1 || true
        purge_bucket_versions "$TEMPLATE_BUCKET"
        aws s3api delete-bucket --bucket "$TEMPLATE_BUCKET" --region "$REGION" 2>/dev/null || true
    fi

    rm -f "$STATE_FILE"
    echo ""
    hr
    echo "  ✅ Cleanup complete! (Connect instance preserved)"
    echo "     Delete instance: aws connect delete-instance --instance-id <ID>"
    hr
}

# =============================================================================
# STATUS
# =============================================================================
do_status() {
    hr
    echo "  AICC Builder - Deployment Status"
    echo "  Project: $PROJECT_NAME | Region: $REGION"
    hr
    echo ""
    STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NOT_FOUND")
    echo "  📦 CloudFormation:   $STACK_STATUS"
    [ "$STACK_STATUS" != "NOT_FOUND" ] && echo "     API Endpoint:     $(get_output ApiEndpoint)"

    N=$(aws connect list-instances --region "$REGION" --output json 2>/dev/null | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('InstanceSummaryList',[])))" 2>/dev/null || echo 0)
    echo "  📞 Connect instances:  ${N}"

    for kv in CONNECT_INSTANCE_ID AI_ASSISTANT_ID GATEWAY_ID BOT_ID CONTACT_FLOW_ID AI_AGENT_ID SP_ID PHONE_NUMBER_ID; do
        v=$(state_get $kv)
        [ -n "$v" ] && echo "  🔹 $kv: $v"
    done

    if aws bedrock-agentcore-control help &>/dev/null; then
        GW_STATUS=$(aws bedrock-agentcore-control list-gateways --region "$REGION" --output json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
for gw in d.get('items', d.get('gateways', [])):
    if gw.get('name') == '${GATEWAY_NAME}': print(gw.get('status','?')); break
else: print('NOT_FOUND')
" 2>/dev/null || echo "?")
        echo "  🌐 Gateway ($GATEWAY_NAME): $GW_STATUS"
    fi
    echo ""
    hr
}

# =============================================================================
# Main
# =============================================================================
# Record a full transcript for diagnostics (share this file when reporting issues)
DEPLOY_LOG="$SCRIPT_DIR/aicc_deploy_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$DEPLOY_LOG") 2>&1
echo "(transcript: $DEPLOY_LOG)"

case "$COMMAND" in
    deploy)                     do_deploy ;;
    cleanup|clean|destroy|delete) do_cleanup ;;
    status)                     do_status ;;
    *)
        echo "Usage: $0 {deploy|cleanup|status}"
        exit 1
        ;;
esac
