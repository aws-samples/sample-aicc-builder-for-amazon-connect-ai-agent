# Asset Flow Architecture

How subagents create, read, collaborate on, stream, and package assets.

---

## 1. Storage Layer

All assets live in two places, kept in sync by a dual-write strategy:

```
NFS (fast, local)                          S3 (durable, backup)
/mnt/s3/sessions/{sid}/                    assets/{sid}/
  assets/                                    {type}/{op_id?}/{file}
    v1/  (v2, v3 for regeneration)
      {type}/{op_id?}/{file}
  state/                                   assets/{sid}/state/
    project.json                             project.json
    progress.json                            progress.json
    specs/{op_id}.json                       specs/{op_id}.json
    schemas/infrastructure.json              schemas/infrastructure.json
```

### Write path (`save_asset_to_s3`)

```
subagent generates content
  -> stream_asset(is_complete=True)
       -> save_asset_to_s3(session_id, type, file, content, op_id)
            1. _save_to_nfs()   -> /mnt/s3/sessions/{sid}/assets/v1/{type}/{op_id}/{file}
            2. s3.put_object()  -> s3://{bucket}/assets/{sid}/{type}/{op_id}/{file}
```

Both writes happen on every save. NFS is the fast path (local disk on ECS);
S3 is the durable backup. If NFS fails, S3 still succeeds.

### Read path (`get_asset_from_s3`) -- NFS-first

```
any consumer calls get_asset_from_s3(s3_key)
  1. _parse_s3_key_to_nfs_components(s3_key)
       -> (session_id, asset_type, file_name, operation_id)
       -> returns (None,...) for state keys or unparseable keys -> skip to S3
  2. _get_from_nfs(session_id, type, file, op_id)
       -> reads /mnt/s3/sessions/{sid}/assets/{latest_version}/{type}/{op_id}/{file}
       -> returns content if found
  3. If NFS miss or unavailable: fall through to s3.get_object()
```

This guarantees that **all readers see the latest patched content** after the
orchestrator modifies files on NFS via workspace tools, even before those
patches propagate to S3.

### List path (`list_session_assets`) -- NFS-first

```
any consumer calls list_session_assets(session_id)
  1. _list_nfs_assets(session_id)
       -> walks /mnt/s3/sessions/{sid}/assets/{latest_version}/
       -> reconstructs S3-key-format strings for backward compat
       -> returns list (possibly empty) if NFS available, None if not
  2. If None: fall through to s3.list_objects_v2()
```

### Versioning

Regeneration creates new version directories (`v1/`, `v2/`, ...).
`_get_current_version()` always returns the latest.
`get_next_version()` is called when the orchestrator triggers regeneration.

---

## 2. Subagents and What They Produce

| Agent | Assets Created | Asset Type Key | Tools Used to Read Others |
|---|---|---|---|
| **Interviewer** | (none -- returns state in-band) | -- | -- |
| **Research** | `research.json` | `research` | Web search (Brave API) |
| **Infrastructure Generator** | `infrastructure.yaml`, or `infrastructure-base.yaml` + `{op}-fragment.yaml` | `cloudformation` | Auto-loads operation specs |
| **Lambda Generator** | `index.py` (per operation) | `lambda` | Auto-loads operation spec, infra schema |
| **OpenAPI Generator** | `openapi.yaml`, or `openapi-base.yaml` + `openapi-chunk-*.yaml` | `openapi` | Auto-loads operation specs |
| **Prompt Generator** | `ai_agent_prompt.yaml` | `prompt` | Auto-loads operation specs, flow config, infra schema |
| **Contact Flow Generator** | `contact_flow.json`, `contact_flow_diagram.md` | `contact_flow` | Auto-loads operation specs, flow config, infra schema |
| **FAQ Generator** | `faq_{category}.txt` + `{company}_knowledge_base.zip` | `faq` | Loads `research.json` from S3 |
| **Reviewer** | `review_report.md` | `review` | `lookup_assets`, `get_asset_content`, workspace file tools, spec manager |

---

## 3. How Agents Collaborate

### 3.1 Orchestrator Wiring

The orchestrator (`backend/agentcore/agent.py`) is a Strands Agent that has
all subagents registered as `@tool` functions. On each WebSocket request:

```python
# 1. Wire session ID into global state
set_streaming_session_id(effective_session_id)
set_workspace_session_id(effective_session_id)

# 2. Register callback handler for every subagent
set_research_callback(callback_handler)
set_faq_callback(callback_handler)
set_lambda_callback(callback_handler)
set_openapi_callback(callback_handler)
set_prompt_callback(callback_handler)
set_contact_flow_callback(callback_handler)
set_infrastructure_callback(callback_handler)
set_reviewer_callback(callback_handler)

# 3. LLM decides which tools to call
agent = Agent(model=model, tools=[
    research_agent, infrastructure_generator_agent,
    lambda_generator_agent, openapi_generator_agent,
    prompt_generator_agent, contact_flow_generator_agent,
    faq_generator_agent, reviewer_agent,
    # + utility tools: save_operation_spec, merge_*_fragments, etc.
])
```

The orchestrator LLM sees each subagent as a tool and decides the execution
order. Subagents communicate through **shared storage** (NFS/S3), not direct
message passing.

### 3.2 Data Dependencies Between Agents

```
Interviewer
  |  (collected requirements -> orchestrator context)
  v
Research Agent -----> FAQ Generator
  |  (research.json)       (reads research.json to generate knowledge base)
  v
Operation Specs (orchestrator saves via save_operation_spec)
  |
  +---> Infrastructure Generator (reads specs -> generates CloudFormation)
  |        |
  |        +---> infra schema (extracted, passed to downstream agents)
  |
  +---> Lambda Generator (reads spec + infra schema -> generates handler.py per op)
  |
  +---> OpenAPI Generator (reads specs -> generates openapi.yaml)
  |
  +---> Prompt Generator (reads specs + infra schema + flow config)
  |
  +---> Contact Flow Generator (reads specs + flow config + infra schema)
  |
  v
Reviewer Agent
  (reads ALL assets via lookup_assets + get_asset_content)
  (cross-validates field consistency, completeness)
```

### 3.3 Parallel Generation with Fragment Merge

For projects with many operations (>6), Infrastructure and OpenAPI generators
switch to parallel mode:

```
Orchestrator
  |
  +-- call infra_generator(mode="base")     -> infrastructure-base.yaml
  |     (shared resources: DynamoDB, IAM, API Gateway RestApi)
  |
  +-- call infra_generator(mode="operation", op="createReservation")  -> fragment.yaml
  +-- call infra_generator(mode="operation", op="cancelReservation")  -> fragment.yaml
  +-- ...
  |
  +-- call merge_infrastructure_fragments(project_name)
        1. Reads base + all fragments from fragment registry (NFS-backed)
        2. Inserts fragments at anchor comment in base
        3. Fixes ApiDeployment DependsOn block
        4. Streams merged infrastructure.yaml
```

OpenAPI uses the same pattern with `base` + `chunk` modes and
`merge_openapi_fragments`, which deduplicates paths by `operationId` and
schemas by name.

Fragment registries persist to NFS for crash recovery -- if the process
restarts mid-generation, previously completed fragments are restored.

### 3.4 Orchestrator Patches (Workspace Tools)

After the Reviewer reports issues, the orchestrator can fix assets directly:

```
Reviewer: "Lambda uses phoneNumber but OpenAPI has phone_number"
  |
  v
Orchestrator calls replace_asset_field() or workspace patch_workspace_file()
  -> modifies file on NFS directly
  -> NFS content is now newer than S3 content
  |
  v
Next reader (Reviewer re-check, Packager, Validator)
  -> calls get_asset_from_s3(s3_key)
  -> NFS-first read picks up the patched version
```

This is the core reason for the NFS-first read strategy: patches happen on NFS
only (fast, no S3 round-trip), and all subsequent reads see them immediately.

---

## 4. Streaming to Frontend

### 4.1 WebSocket Flow

```
Frontend <--WebSocket--> BedrockAgentCoreApp
                             |
                             v
                    StrandsCallbackHandler
                      .stream_asset_preview(
                          asset_type, content, operation_id,
                          file_name, is_complete, s3_key, ...)
                             |
                             v
                       WebSocket.send({
                         "type": "asset_preview",
                         "asset_type": "lambda",
                         "file_name": "index.py",
                         "operation_id": "createReservation",
                         "content": "def handler(event, context):...",
                         "is_complete": false
                       })
```

### 4.2 Progressive Streaming

Each subagent streams content progressively as it generates:

```python
# Inside lambda_generator_agent (simplified)
async for event in agent.stream_async(prompt):
    if "data" in event:
        full_response += event["data"]

        # Stream every N chars of new content
        if len(full_response) - last_stream_len >= STREAM_INTERVAL:
            stream_asset("lambda", "index.py", full_response,
                         operation_id=op_id, is_complete=False)
            last_stream_len = len(full_response)

# Final save
stream_asset("lambda", "index.py", full_response,
             operation_id=op_id, is_complete=True)
# is_complete=True triggers save_asset_to_s3() (dual-write NFS + S3)
```

The frontend receives chunks in real-time and renders a live code preview.

### 4.3 Heartbeat Keep-Alive

Long-running subagents use a heartbeat manager to prevent WebSocket timeouts:

```python
heartbeat = create_heartbeat_manager(
    callback_handler=get_callback_handler(),
    agent_name="reviewer_agent",
    project_name="review"
)
async with heartbeat:
    async for event in agent.stream_async(prompt):
        heartbeat.update_progress(len(full_response))
        ...
```

Sends progress events every ~10 seconds to keep the 60-second idle timeout
from firing.

### 4.4 Auto-Progress Tracking

The orchestrator maps subagent tool completions to frontend progress steps:

```python
SUBAGENT_TO_PROGRESS_ID = {
    "infrastructure_generator_agent": "cdk",
    "lambda_generator_agent": "lambda",
    "openapi_generator_agent": "openapi",
    "prompt_generator_agent": "prompt",
    "contact_flow_generator_agent": "contact_flow",
    "faq_generator_agent": "knowledge_base",
    "reviewer_agent": "review",
}
```

When a subagent returns `{"_completion_marker": "SUBAGENT_COMPLETE"}`, the
orchestrator auto-sends a progress update to the frontend.

---

## 5. Asset Packaging (Download)

### 5.1 ZIP Assembly

`package_and_upload_assets(session_id, project_name)` in `asset_packager.py`:

```
1. list_session_assets(session_id)   <- NFS-first listing
2. For each key:
     get_asset_from_s3(key)          <- NFS-first read
     Determine ZIP path from asset type
3. Bundle into ZIP
4. Upload ZIP to S3
5. Generate presigned URL (24h expiry)
```

### 5.2 ZIP Directory Structure

```
{project_name}/
  README.md                              # Auto-generated deployment guide
  deploy.sh                              # One-click CloudShell deploy script
  cloudformation/
    infrastructure.yaml                  # CloudFormation template
  lambda/
    {operation_id}/
      index.py                           # Lambda handler
    update_q_session/
      index.js                           # Static Node.js Lambda (bundled)
  openapi/
    openapi.yaml                         # OpenAPI 3.0 spec
  prompts/
    ai_agent_prompt.yaml                 # Connect AI agent prompt
  contact-flow/
    contact_flow.json                    # Amazon Connect contact flow
    contact_flow_diagram.md              # Mermaid diagram
  knowledge-base/
    {category}/
      faq_{topic}.txt                    # FAQ documents
```

### 5.3 Asset Type to Folder Mapping

```python
ASSET_TYPE_FOLDER_MAP = {
    "cloudformation": "cloudformation",
    "infrastructure": "cloudformation",
    "cdk":            "cloudformation",
    "lambda":         "lambda",
    "openapi":        "openapi",
    "prompt":         "prompts",
    "contact_flow":   "contact-flow",
    "contactflow":    "contact-flow",
    "contact-flow":   "contact-flow",
    "mermaid":        "contact-flow",
    "faq":            "knowledge-base",
    "knowledge_base": "knowledge-base",
    "knowledge-base": "knowledge-base",
}
```

### 5.4 Static Bundled Assets

The packager includes `UPDATE_Q_SESSION_LAMBDA_CODE` -- a Node.js 18.x Lambda
that injects customer lookup data into Amazon Q in Connect sessions. This code
is static (not LLM-generated) and always included when the contact flow uses
Q in Connect.

---

## 6. Reviewer Agent -- The Cross-Asset Validator

The reviewer is the only agent that **reads all other agents' outputs**. It has
access to:

| Tool | Purpose |
|---|---|
| `lookup_assets(session_id)` | List all assets (NFS-first via `list_session_assets`) |
| `get_asset_content(s3_key)` | Read asset content (NFS-first via `get_asset_from_s3`) |
| `validate_openapi_schema(yaml)` | Structural validation |
| `check_field_consistency(lambda, openapi, prompt)` | Cross-asset field name matching |
| `validate_parameter_consistency(session_id)` | Automated mismatch detection |
| `list_operations()` / `get_operation_spec(op_id)` | Source-of-truth specs |
| `read_workspace_file(session_id, path)` | Direct NFS file read |
| `find_workspace_files(session_id, pattern)` | Glob search across workspace |
| `grep_workspace(session_id, pattern)` | Text search across workspace |
| `list_workspace_dir(session_id, path)` | Directory listing |

The workspace tools give the reviewer direct filesystem access for searching
across files (e.g., `grep_workspace(sid, "phoneNumber", file_pattern="*.py")`)
without going through the S3 key abstraction.

---

## 7. Edge Cases and Fallback Behavior

| Scenario | Behavior |
|---|---|
| NFS available, file exists on NFS | Returns NFS content (freshest, post-patch) |
| NFS available, file NOT on NFS | Falls through to S3 (pre-migration or old data) |
| NFS unavailable (local dev, non-ECS) | Falls through to S3 (existing behavior, no regression) |
| State keys (`assets/{sid}/state/...`) | Parser returns `None` -> always reads from S3 |
| Multiple versions (v1, v2) | `_get_current_version()` returns latest |
| NFS mount goes stale mid-request | IOError caught -> S3 fallback |
| Empty NFS file (0 bytes) | Returns `""` (correct for cleared content) |
| Binary files (.zip, .png) | Detected by extension, returns placeholder or `None` |

---

## 8. End-to-End Trace

A single asset's lifecycle from generation to download:

```
1. GENERATE
   orchestrator calls lambda_generator_agent(session_id, op_id="createReservation")
     -> LLM generates Python code
     -> stream_asset("lambda", "index.py", partial_code, is_complete=False)
        -> callback -> WebSocket -> frontend preview (live typing)
     -> stream_asset("lambda", "index.py", final_code, is_complete=True)
        -> save_asset_to_s3()
           -> NFS write: /mnt/s3/sessions/{sid}/assets/v1/lambda/createReservation/index.py
           -> S3 write:  s3://bucket/assets/{sid}/lambda/createReservation/index.py
        -> callback -> WebSocket -> frontend (final preview)

2. REVIEW
   orchestrator calls reviewer_agent(session_id)
     -> lookup_assets(session_id)
        -> list_session_assets() -> _list_nfs_assets() -> reads NFS directory
     -> get_asset_content("assets/{sid}/lambda/createReservation/index.py")
        -> get_asset_from_s3() -> _parse_s3_key -> _get_from_nfs() -> NFS read
     -> check_field_consistency(lambda_code, openapi_yaml)
     -> reports: "field mismatch: Lambda uses phone_number, OpenAPI uses phoneNumber"

3. PATCH
   orchestrator calls replace_asset_field(session_id, ...)
     -> patch_workspace_file() modifies NFS file directly
     -> NFS now has corrected content; S3 still has old content

4. RE-REVIEW
   orchestrator calls reviewer_agent(session_id, focus_items=[...])
     -> get_asset_content() -> NFS-first -> reads patched content
     -> confirms fix: "RESOLVED"

5. PACKAGE
   orchestrator calls package_and_upload_assets(session_id, "my-project")
     -> list_session_assets() -> NFS listing (sees all current files)
     -> get_asset_from_s3() per file -> NFS read (gets patched versions)
     -> assembles ZIP with corrected content
     -> uploads to S3, returns presigned URL (24h)
     -> frontend shows download button
```
