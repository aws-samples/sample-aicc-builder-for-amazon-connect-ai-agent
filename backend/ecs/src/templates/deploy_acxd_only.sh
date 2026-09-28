#!/usr/bin/env bash
# =============================================================================
# AICC Builder - ACXD-only deploy (Agentic CX Designer application)
#
# This bundle carries ONLY the ACXD application: flows, slot types, data
# requests, guardrails, knowledge base, context variables, secret declarations
# and the application itself. It has no CloudFormation, Lambda, OpenAPI or
# Contact Flow: the data requests call YOUR existing API at WEBHOOK_URL, and you
# add the Agentic CX block to your own Contact Flow (WIRING-GUIDE.md).
#
# Commands:
#   ./deploy.sh              Deploy (idempotent, safe to re-run)
#   ./deploy.sh --dry-run    Print the plan and change nothing
#   ./deploy.sh status       Show what this bundle deployed (.deploy-state.json)
#   ./deploy.sh cleanup      Delete what this bundle deployed
#
# Environment (prompted when missing and a terminal is attached):
#   ACXD_WORKSPACE_ID          Agentic CX Designer workspace id
#   ACXD_API_KEY               Programmatic API key (acxd_live_...), never stored
#   WEBHOOK_URL                https base URL of your backend API; each data
#                              request calls WEBHOOK_URL + its path
#                              (BACKEND-CONTRACT.md lists the paths)
#   ACXD_SECRET_<NAME>         Value of a secret the data requests send as a
#                              header (see assets/acxd/secrets/*.json); the
#                              generic ACXD_SECRET_BACKENDAPIKEY works for the
#                              project's backend-key secret
#   ACXD_REGION                ACXD API region (auto-detected when unset)
#   AUTO_CONFIRM=1             Never prompt: fail on a missing required value
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="deploy-manifest.json"
COMMAND="deploy"
DRY_RUN=false

usage() {
    echo "Usage: $0 [deploy|status|cleanup] [--dry-run]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        deploy|status|cleanup) COMMAND="$1"; shift ;;
        clean|destroy|delete) COMMAND="cleanup"; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

IS_TTY=false
if [ -t 0 ] && [ "${AUTO_CONFIRM:-0}" != "1" ]; then
    IS_TTY=true
fi

info() { echo "  - $*"; }
warn() { echo "  ! $*" >&2; }
fail() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$SCRIPT_DIR/$MANIFEST" ] || fail "$MANIFEST not found next to deploy.sh"
[ -f "$SCRIPT_DIR/runner.js" ] || fail "runner.js not found next to deploy.sh"

ensure_node() {
    command -v node >/dev/null 2>&1 || fail "Node.js 20+ is required (https://nodejs.org)"
    local major
    major=$(node -p "process.versions.node.split('.')[0]")
    [ "$major" -ge 20 ] || fail "Node.js 20+ is required (found $(node --version))"
}

ensure_sdk() {
    if [ ! -d "$SCRIPT_DIR/node_modules/amazon-connect-acxd-sdk" ]; then
        info "Installing the pinned ACXD SDK (npm install)..."
        (cd "$SCRIPT_DIR" && npm install --omit=dev --no-fund --no-audit)
    fi
}

# Read one value from the manifest / bundle without jq (Node is required anyway).
manifest_default_url() {
    node -e '
const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf-8"));
const step = (m.steps || []).find((s) => s.type === "wire-webhook-urls");
if (step) process.stdout.write(((step.params || {}).defaultUrl || "") + "\n" + "wire");
' "$SCRIPT_DIR/$MANIFEST"
}

secret_declarations() {
    # One line per secret: <name> <valueEnv>
    node -e '
const fs = require("fs"); const path = require("path");
const dir = path.join(process.argv[1], "assets", "acxd", "secrets");
if (!fs.existsSync(dir)) process.exit(0);
for (const f of fs.readdirSync(dir).filter((n) => n.endsWith(".json")).sort()) {
  const d = JSON.parse(fs.readFileSync(path.join(dir, f), "utf-8"));
  const env = d.valueEnv || ("ACXD_SECRET_" + String(d.name || "").toUpperCase());
  console.log(`${d.name} ${env}`);
}
' "$SCRIPT_DIR"
}

prompt_value() {
    # prompt_value <VAR> <label> <hidden:true|false>
    local var="$1" label="$2" hidden="$3" value=""
    if [ "$IS_TTY" != "true" ]; then
        return 1
    fi
    if [ "$hidden" = "true" ]; then
        read -r -s -p "   $label: " value
        echo ""
    else
        read -r -p "   $label: " value
    fi
    [ -n "$value" ] || return 1
    export "$var=$value"
}

ensure_credentials() {
    if [ -z "${ACXD_WORKSPACE_ID:-}" ]; then
        prompt_value ACXD_WORKSPACE_ID "ACXD workspace ID" false \
            || fail "ACXD_WORKSPACE_ID must be set (Agentic CX designer > Workspace settings > Advanced)"
    fi
    if [ -z "${ACXD_API_KEY:-}" ]; then
        prompt_value ACXD_API_KEY "ACXD API key (input hidden)" true \
            || fail "ACXD_API_KEY must be set (Admin Hub > Users > API access > Generate API key)"
    fi
}

ensure_backend() {
    local out default_url wired
    out="$(manifest_default_url)"
    wired="$(printf '%s' "$out" | tail -n 1)"
    [ "$wired" = "wire" ] || return 0      # every data request is static: no backend URL needed
    default_url="$(printf '%s' "$out" | head -n 1)"
    if [ -z "${WEBHOOK_URL:-}" ] && [ -n "$default_url" ]; then
        export WEBHOOK_URL="$default_url"
        info "WEBHOOK_URL not set; using the base URL recorded in the interview: $WEBHOOK_URL"
    fi
    if [ -z "${WEBHOOK_URL:-}" ]; then
        prompt_value WEBHOOK_URL "Backend API base URL (https://...)" false \
            || fail "WEBHOOK_URL must be set: the https base URL of the API the data requests call (BACKEND-CONTRACT.md)"
    fi
    case "$WEBHOOK_URL" in
        https://*) ;;
        *) fail "WEBHOOK_URL must start with https:// (got '$WEBHOOK_URL')" ;;
    esac
    export WEBHOOK_URL="${WEBHOOK_URL%/}"

    local name env_var
    while read -r name env_var; do
        [ -n "$name" ] || continue
        if [ -n "${!env_var:-}" ]; then
            continue
        fi
        case "$name" in
            *BackendApiKey)
                if [ -n "${ACXD_SECRET_BACKENDAPIKEY:-}" ]; then
                    continue
                fi ;;
        esac
        if ! prompt_value "$env_var" "Value for secret $name (input hidden)" true; then
            warn "secret '$name' has no value ($env_var not set): the data requests will send an empty credential header"
        fi
    done < <(secret_declarations)
}

run_runner() {
    (cd "$SCRIPT_DIR" && node runner.js "$@")
}

do_deploy() {
    echo ""
    echo "=========================================="
    echo "  AICC Builder - ACXD-only deploy"
    echo "=========================================="
    ensure_node
    if [ "$DRY_RUN" = "true" ]; then
        run_runner deploy --manifest "$MANIFEST" --dry-run
        return
    fi
    ensure_credentials
    ensure_backend
    ensure_sdk
    run_runner deploy --manifest "$MANIFEST"
    echo ""
    echo "Next steps (WIRING-GUIDE.md):"
    echo "  1. In your Connect Customer instance, open the contact flow that should reach"
    echo "     the assistant and add the Agentic CX block: pick this workspace, the"
    echo "     application printed above and its alias, then publish the flow."
    echo "  2. Make sure the ACXD workspace has a default generative model; without one the"
    echo "     knowledge-base and generative nodes answer nothing."
}

case "$COMMAND" in
    deploy) do_deploy ;;
    status) ensure_node; run_runner status ;;
    cleanup)
        ensure_node
        ensure_credentials
        ensure_sdk
        if [ "${AUTO_CONFIRM:-0}" = "1" ]; then
            run_runner cleanup --yes
        else
            run_runner cleanup
        fi ;;
esac
