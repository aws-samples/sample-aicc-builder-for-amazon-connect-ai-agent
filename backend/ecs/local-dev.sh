#!/bin/bash
# ========================================
# AICC Builder ECS — Local Development Server
# ========================================
# Runs the ECS FastAPI app locally with a simulated S3 Files NFS mount.
# No Docker required — runs directly with uvicorn.
#
# Usage:
#   ./local-dev.sh                    # Start server on port 8080
#   ./local-dev.sh --test             # Run quick smoke tests instead
#   ./local-dev.sh --test-ws          # Test WebSocket connection
#
# Prerequisites:
#   pip install -r requirements.txt   # (one-time setup)
#   AWS credentials configured        # (for Bedrock model calls)

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
LOCAL_NFS="/tmp/s3files"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ========================================
# Setup local NFS simulation
# ========================================
setup_local_nfs() {
    echo -e "${CYAN}Setting up local NFS simulation at ${LOCAL_NFS}${NC}"
    mkdir -p "$LOCAL_NFS/sessions"
    mkdir -p "$LOCAL_NFS/prompts"
    mkdir -p "$LOCAL_NFS/config"
    echo -e "${GREEN}NFS directory structure ready${NC}"
    echo "  $LOCAL_NFS/"
    echo "  ├── sessions/    (session data, assets, context)"
    echo "  ├── prompts/     (hot-reloadable prompts)"
    echo "  └── config/      (model config)"
}

# ========================================
# Ensure src/ symlink exists
# ========================================
setup_src() {
    if [ ! -d "$SCRIPT_DIR/src" ] && [ ! -L "$SCRIPT_DIR/src" ]; then
        echo -e "${CYAN}Linking src/ → ../src/ ${NC}"
        ln -s "$BACKEND_DIR/src" "$SCRIPT_DIR/src"
    fi
}

# ========================================
# Check dependencies
# ========================================
check_deps() {
    echo -e "${CYAN}Checking dependencies...${NC}"
    python3 -c "import fastapi; import uvicorn; import strands" 2>/dev/null || {
        echo -e "${YELLOW}Missing dependencies. Installing...${NC}"
        pip install -r "$SCRIPT_DIR/requirements.txt"
    }
    echo -e "${GREEN}Dependencies OK${NC}"
}

# ========================================
# Smoke Tests — NFS + tools + HTTP
# ========================================
run_smoke_tests() {
    echo -e "\n${CYAN}========================================${NC}"
    echo -e "${CYAN}  Running Smoke Tests${NC}"
    echo -e "${CYAN}========================================${NC}\n"

    setup_local_nfs
    setup_src

    cd "$SCRIPT_DIR"

    PASS=0
    FAIL=0

    run_test() {
        local name=$1
        shift
        printf "  %-50s" "$name"
        if eval "$@" > /tmp/test_output.txt 2>&1; then
            echo -e "${GREEN}PASS${NC}"
            PASS=$((PASS + 1))
        else
            echo -e "${RED}FAIL${NC}"
            cat /tmp/test_output.txt | head -5 | sed 's/^/    /'
            FAIL=$((FAIL + 1))
        fi
    }

    # Helper: import single module without triggering tools/__init__.py cascade
    # Uses importlib to load individual files directly
    IMPORT_HELPER='
import importlib.util, sys, types, os
def load_mod(name, path):
    """Load a single .py file as a module, bypassing __init__.py."""
    # Stub parent packages to avoid cascade imports
    for pkg in ["tools", "agents", "agents.infrastructure_generator", "agents.openapi_generator", "context"]:
        if pkg not in sys.modules:
            sys.modules[pkg] = types.ModuleType(pkg)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
'
    SRC="$SCRIPT_DIR/src"

    # --- Test 1: NFS workspace file tools ---
    run_test "workspace_file_tools: write + read" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS python3 -c \"
${IMPORT_HELPER}
wft = load_mod('tools.workspace_file_tools', '$SRC/tools/workspace_file_tools.py')
w = wft.write_workspace_file(session_id='test-local', path='test/hello.txt', content='Hello NFS!')
assert w['success'], f'Write failed: {w}'
r = wft.read_workspace_file(session_id='test-local', path='test/hello.txt')
assert r['success'] and r['content'] == 'Hello NFS!', f'Read failed: {r}'
\""

    run_test "workspace_file_tools: list_workspace_dir" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS python3 -c \"
${IMPORT_HELPER}
wft = load_mod('tools.workspace_file_tools', '$SRC/tools/workspace_file_tools.py')
r = wft.list_workspace_dir(session_id='test-local', path='test')
assert r['success'] and len(r['entries']) >= 1, f'List failed: {r}'
\""

    run_test "workspace_file_tools: patch file" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS python3 -c \"
${IMPORT_HELPER}
wft = load_mod('tools.workspace_file_tools', '$SRC/tools/workspace_file_tools.py')
wft.write_workspace_file(session_id='test-local', path='test/patch.txt', content='foo bar baz')
r = wft.patch_workspace_file(session_id='test-local', path='test/patch.txt', search='bar', replace='QUX')
assert r['success'] and r['replacements_made'] == 1, f'Patch failed: {r}'
r2 = wft.read_workspace_file(session_id='test-local', path='test/patch.txt')
assert 'QUX' in r2['content'], f'Patch content wrong: {r2}'
\""

    # --- Test 2: S3 Files context store ---
    run_test "s3files_store: save + load session" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS python3 -c \"
${IMPORT_HELPER}
store_mod = load_mod('context.s3files_store', '$SRC/context/s3files_store.py')
store = store_mod.S3FilesContextStore('$LOCAL_NFS')
store.save_conversation_history('test-local', [{'role':'user','content':'hi'}])
h = store.load_conversation_history('test-local')
assert h and len(h) == 1, f'History restore failed: {h}'
\""

    # --- Test 3: Spec manager NFS ---
    run_test "spec_manager: NFS persist + restore" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS CURRENT_SESSION_ID=test-local python3 -c \"
${IMPORT_HELPER}
sm = load_mod('tools.spec_manager', '$SRC/tools/spec_manager.py')
sm.save_operation_spec(
    session_id='test-local', operation_id='op1',
    operation_type='create', tool_name='lambda',
    summary='Test op', resource_type='Lambda'
)
sm._spec_registry.clear()
r = sm.get_operation_spec(session_id='test-local', operation_id='op1')
assert 'op1' in str(r) or 'Test op' in str(r), f'Spec restore failed: {r}'
\""

    # --- Test 4: Asset storage NFS ---
    run_test "s3_asset_storage: NFS save + load" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS ASSETS_BUCKET_NAME=test-bucket python3 -c \"
${IMPORT_HELPER}
asa = load_mod('tools.s3_asset_storage', '$SRC/tools/s3_asset_storage.py')
if asa._nfs_available():
    asa._save_to_nfs('test-local', 'lambda', 'index.py', 'print(42)', operation_id='op1')
    r = asa._load_from_nfs('test-local', 'lambda', 'index.py', operation_id='op1')
    assert r == 'print(42)', f'NFS load failed: {r}'
else:
    raise Exception('NFS not available')
\""

    # --- Test 5: NFS directory structure — write then verify in same process ---
    run_test "NFS directory structure after writes" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS python3 -c \"
${IMPORT_HELPER}
import os
wft = load_mod('tools.workspace_file_tools', '$SRC/tools/workspace_file_tools.py')
wft.write_workspace_file(session_id='test-local', path='state/project.json', content='{\"test\":true}')
wft.write_workspace_file(session_id='test-local', path='assets/v1/lambda/index.py', content='print(1)')
wft.write_workspace_file(session_id='test-local', path='context/history.json', content='[]')
base = '$LOCAL_NFS/sessions/test-local'
found = []
for root, dirs, files in os.walk(base):
    for f in files:
        found.append(os.path.relpath(os.path.join(root, f), base))
found.sort()
print(f'Files: {found}')
assert len(found) >= 3, f'Expected >= 3, got {found}'
\""

    # --- Test 6: Fragment registry NFS ---
    run_test "infrastructure fragment registry: NFS persist" \
        "S3FILES_MOUNT_PATH=$LOCAL_NFS python3 -c \"
${IMPORT_HELPER}
# Need strands for @tool decorator — mock it if not available
try:
    import strands
except ImportError:
    import types
    strands = types.ModuleType('strands')
    strands.tool = lambda f: f
    sys.modules['strands'] = strands
agent_mod = load_mod('agents.infrastructure_generator.agent', '$SRC/agents/infrastructure_generator/agent.py')
agent_mod._store_fragment('local-test-proj', 'base', 'AWSTemplateFormatVersion: 2010-09-09')
agent_mod._store_fragment('local-test-proj', 'op1', '  TestLambda: !Ref X', section='fragments')
agent_mod._fragment_registry.clear()
r = agent_mod.get_fragments('local-test-proj')
assert r is not None and r.get('base') is not None, f'Fragment restore failed: {r}'
\""

    # --- Summary ---
    echo ""
    echo -e "${CYAN}========================================${NC}"
    TOTAL=$((PASS + FAIL))
    echo -e "  Results: ${GREEN}${PASS}/${TOTAL} passed${NC}"
    if [ $FAIL -gt 0 ]; then
        echo -e "           ${RED}${FAIL} failed${NC}"
    fi
    echo -e "${CYAN}========================================${NC}"

    # Cleanup test data
    rm -rf "$LOCAL_NFS/sessions/test-local"
    rm -rf "$LOCAL_NFS/sessions/_fragments"

    [ $FAIL -eq 0 ] && exit 0 || exit 1
}

# ========================================
# WebSocket Test
# ========================================
run_ws_test() {
    echo -e "${CYAN}Testing WebSocket connection...${NC}"
    echo "Make sure the server is running: ./local-dev.sh"
    echo ""

    if ! command -v wscat &>/dev/null && ! command -v websocat &>/dev/null; then
        echo -e "${YELLOW}Install wscat: npm install -g wscat${NC}"
        echo "Or use: python3 -c \"\
import asyncio, websockets, json
async def test():
    async with websockets.connect('ws://localhost:8080/ws?sessionId=ws-test-1') as ws:
        msg = await ws.recv()
        print('Connected:', msg)
        await ws.send(json.dumps({'action': 'ping'}))
        pong = await ws.recv()
        print('Pong:', pong)
        await ws.send(json.dumps({'action': 'sendMessage', 'prompt': 'Hello, test!'}))
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                d = json.loads(msg)
                print(f'[{d.get(\"type\", \"?\")}] {str(d.get(\"content\", \"\"))[:100]}')
                if d.get('type') in ('final_response', 'error'):
                    break
            except asyncio.TimeoutError:
                print('Timeout')
                break
asyncio.run(test())
\""
        exit 0
    fi

    wscat -c "ws://localhost:8080/ws?sessionId=ws-test-1" \
        -x '{"action":"ping"}' \
        --wait 2000
}

# ========================================
# Main — Parse args and run
# ========================================
case "${1:-}" in
    --test)
        run_smoke_tests
        ;;
    --test-ws)
        run_ws_test
        ;;
    *)
        setup_local_nfs
        setup_src
        check_deps

        echo -e "\n${CYAN}========================================${NC}"
        echo -e "${CYAN}  AICC Builder ECS — Local Dev Server${NC}"
        echo -e "${CYAN}========================================${NC}"
        echo -e "  NFS mount:  ${GREEN}$LOCAL_NFS${NC}"
        echo -e "  Port:       ${GREEN}8080${NC}"
        echo -e "  Health:     ${GREEN}http://localhost:8080/ping${NC}"
        echo -e "  WebSocket:  ${GREEN}ws://localhost:8080/ws?sessionId=test-1${NC}"
        echo -e ""
        echo -e "  ${YELLOW}Tip: Run smoke tests first:${NC}"
        echo -e "    ./local-dev.sh --test"
        echo -e "${CYAN}========================================${NC}\n"

        cd "$SCRIPT_DIR"
        export S3FILES_MOUNT_PATH="$LOCAL_NFS"
        export SESSION_STORE_BACKEND="s3files"
        export PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/src:${PYTHONPATH:-}"
        export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-northeast-2}"
        export AWS_REGION="${AWS_REGION:-ap-northeast-2}"

        exec uvicorn app:app \
            --host 0.0.0.0 \
            --port 8080 \
            --reload \
            --reload-dir "$SCRIPT_DIR" \
            --reload-dir "$BACKEND_DIR/src"
        ;;
esac
