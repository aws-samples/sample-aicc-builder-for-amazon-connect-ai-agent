#!/bin/bash
# setup-web-search-gateway.sh
# Idempotently provisions an Amazon Bedrock AgentCore Gateway with the managed
# Web Search connector target (us-east-1) and prints its MCP URL.
#
# The managed Web Search connector schema requires boto3 >= 1.43, which is newer
# than the bundled AWS CLI / system boto3. This wrapper guarantees a suitable
# boto3 in a cached venv, then runs setup_web_search_gateway.py.
#
# Usage:
#   ./setup-web-search-gateway.sh [--stage prod] [--output-env /path/to/.env.local]
# On success the gateway URL is the LAST stdout line.
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PY_SCRIPT="$SCRIPT_DIR/setup_web_search_gateway.py"
VENV_DIR="$SCRIPT_DIR/.websearch-venv"   # cached across runs
MIN_BOTO3="1.43.0"

# Diagnostics → stderr so stdout's last line stays the URL.
log() { echo -e "$@" >&2; }

PYBIN="$(command -v python3 || command -v python)"
if [ -z "$PYBIN" ]; then
    log "${RED}ERROR: python3 not found — required to create the AgentCore gateway.${NC}"
    exit 1
fi

# Reuse the cached venv if it already has a connector-capable boto3.
need_setup=true
if [ -x "$VENV_DIR/bin/python" ]; then
    if "$VENV_DIR/bin/python" - <<'PY' >/dev/null 2>&1
import boto3
from packaging.version import Version
import sys
sys.exit(0 if Version(boto3.__version__) >= Version("1.43.0") else 1)
PY
    then
        need_setup=false
    fi
fi

if [ "$need_setup" = true ]; then
    log "${CYAN}Preparing boto3>=${MIN_BOTO3} for AgentCore gateway provisioning...${NC}"
    "$PYBIN" -m venv "$VENV_DIR" 2>/dev/null || {
        log "${RED}ERROR: could not create venv at $VENV_DIR${NC}"; exit 1; }
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$VENV_DIR/bin/pip" install --quiet "boto3>=${MIN_BOTO3}" "botocore>=${MIN_BOTO3}" packaging >&2 || {
        log "${RED}ERROR: failed to install boto3>=${MIN_BOTO3}${NC}"; exit 1; }
fi

# Forward all args (e.g. --stage, --output-env) to the Python script.
"$VENV_DIR/bin/python" "$PY_SCRIPT" "$@"
