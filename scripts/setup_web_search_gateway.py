#!/usr/bin/env python3
"""
Provision (idempotently) an Amazon Bedrock AgentCore Gateway with the managed
**Web Search** connector target, so the AICC Builder agents get web search with
no API key.

Why this exists: ``./deploy.sh`` calls this so web search is created as part of a
normal deploy — the operator never has to hand-create a gateway or paste a URL.

What it creates (all in us-east-1, the only region Web Search is GA):
  1. Gateway execution role  — assumed by the AgentCore service; allows
     ``bedrock-agentcore:InvokeGateway`` and ``bedrock-agentcore:InvokeWebSearch``.
  2. Gateway                 — protocolType=MCP, authorizerType=AWS_IAM (so our
     ECS task role invokes it with SigV4 — no JWT).
  3. Web Search target       — connectorId="web-search", tool "WebSearch",
     outbound auth GATEWAY_IAM_ROLE.

Idempotent: re-running finds the existing role/gateway/target by name and reuses
them. On success it prints the gateway MCP URL as the LAST stdout line (and, if
GITHUB-style ``--output-env`` is given, writes ``AGENTCORE_GATEWAY_URL=<url>``)
so the caller can capture it.

Requires boto3 >= 1.43 (older SDKs lack the connector schema). The bash wrapper
(setup-web-search-gateway.sh) guarantees that.

Ground truth:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html
"""
import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

# Web Search on Amazon Bedrock AgentCore is GA in us-east-1 only.
REGION = "us-east-1"
CONNECTOR_ID = "web-search"
WEB_SEARCH_TOOL_ARN = "arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1"


def log(msg: str) -> None:
    # Diagnostics go to stderr so stdout's last line stays the gateway URL.
    print(msg, file=sys.stderr, flush=True)


def ensure_execution_role(iam, account_id: str, role_name: str) -> str:
    """Create or reuse the gateway execution role. Returns its ARN."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreToAssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/*"
                    },
                },
            }
        ],
    }
    # Web Search Tool needs InvokeGateway + InvokeWebSearch on the service tool ARN.
    permissions = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeGateway",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeGateway",
                "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/*",
            },
            {
                "Sid": "InvokeWebSearch",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeWebSearch",
                "Resource": WEB_SEARCH_TOOL_ARN,
            },
        ],
    }

    try:
        existing = iam.get_role(RoleName=role_name)
        role_arn = existing["Role"]["Arn"]
        log(f"  [role] reusing {role_name}")
        # Keep the trust policy current (e.g. account changed).
        iam.update_assume_role_policy(
            RoleName=role_name, PolicyDocument=json.dumps(trust_policy)
        )
    except iam.exceptions.NoSuchEntityException:
        log(f"  [role] creating {role_name}")
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="AICC Builder - AgentCore Gateway execution role for Web Search",
        )["Role"]["Arn"]

    # Always (re)put the inline permission policy so re-runs converge.
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="WebSearchGatewayPermissions",
        PolicyDocument=json.dumps(permissions),
    )
    return role_arn


def find_gateway(client, name: str):
    """Return the gateway dict matching name, or None."""
    paginator_token = None
    while True:
        kwargs = {"maxResults": 100}
        if paginator_token:
            kwargs["nextToken"] = paginator_token
        resp = client.list_gateways(**kwargs)
        for gw in resp.get("items", []):
            if gw.get("name") == name:
                return gw
        paginator_token = resp.get("nextToken")
        if not paginator_token:
            return None


def get_gateway_url(client, gateway_id: str) -> str:
    return client.get_gateway(gatewayIdentifier=gateway_id).get("gatewayUrl", "")


def wait_gateway_ready(client, gateway_id: str, timeout_s: int = 180) -> None:
    """Poll the gateway until it leaves CREATING/UPDATING (targets need READY)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = client.get_gateway(gatewayIdentifier=gateway_id).get("status", "UNKNOWN")
        if status == "READY":
            return
        if status in ("FAILED", "DELETING", "DELETED"):
            raise RuntimeError(f"Gateway entered terminal status {status}")
        log(f"  [gateway] status={status}, waiting...")
        time.sleep(5)
    log("  [gateway] not READY within timeout — attempting target anyway")


def ensure_gateway(client, name: str, role_arn: str) -> dict:
    """Create or reuse the MCP/AWS_IAM gateway. Returns {id, url}."""
    existing = find_gateway(client, name)
    if existing:
        gid = existing["gatewayId"]
        log(f"  [gateway] reusing {name} ({gid})")
        return {"id": gid, "url": get_gateway_url(client, gid)}

    log(f"  [gateway] creating {name} (MCP / AWS_IAM)")
    # IAM auth can need a beat after the role is created (eventual consistency).
    last_err = None
    for attempt in range(5):
        try:
            created = client.create_gateway(
                name=name,
                roleArn=role_arn,
                protocolType="MCP",
                authorizerType="AWS_IAM",
                description="AICC Builder web search (managed Web Search connector)",
            )
            gid = created["gatewayId"]
            return {"id": gid, "url": created.get("gatewayUrl") or get_gateway_url(client, gid)}
        except ClientError as e:
            last_err = e
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ValidationException", "AccessDeniedException") and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise last_err


def find_target(client, gateway_id: str, name: str):
    resp = client.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=100)
    for t in resp.get("items", []):
        if t.get("name") == name:
            return t
    return None


def ensure_web_search_target(client, gateway_id: str, name: str) -> None:
    """Create or reuse the web-search connector target."""
    existing = find_target(client, gateway_id, name)
    if existing:
        log(f"  [target] reusing {name} ({existing.get('targetId')}, status={existing.get('status')})")
        return

    log(f"  [target] creating {name} (connectorId={CONNECTOR_ID})")
    client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=name,
        targetConfiguration={
            "mcp": {
                "connector": {
                    "source": {"connectorId": CONNECTOR_ID},
                    "configurations": [{"name": "WebSearch", "parameterValues": {}}],
                }
            }
        },
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )


def wait_until_ready(client, gateway_id: str, target_name: str, timeout_s: int = 120) -> None:
    """Poll the target until READY (or FAILED / timeout). Best-effort."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = find_target(client, gateway_id, target_name)
        status = (t or {}).get("status", "UNKNOWN")
        if status == "READY":
            log("  [target] READY")
            return
        if status == "FAILED":
            reason = (t or {}).get("statusReasons") or (t or {}).get("statusReason") or ""
            log(f"  [target] FAILED: {reason}")
            return
        time.sleep(5)
    log("  [target] still provisioning (not READY yet) — it should converge shortly")


def main() -> int:
    ap = argparse.ArgumentParser(description="Provision AgentCore Web Search gateway")
    ap.add_argument("--name", default="aicc-builder-web-search",
                    help="Gateway name (also basis for role/target names)")
    ap.add_argument("--stage", default="", help="Optional stage suffix, e.g. prod")
    ap.add_argument("--output-env", default="",
                    help="If set, append AGENTCORE_GATEWAY_URL=<url> to this file")
    ap.add_argument("--no-wait", action="store_true", help="Don't poll target for READY")
    args = ap.parse_args()

    suffix = f"-{args.stage}" if args.stage else ""
    gateway_name = f"{args.name}{suffix}"
    # IAM role names allow up to 64 chars; this stays well under.
    role_name = f"AiccBuilderWebSearchGateway{('-' + args.stage) if args.stage else ''}"
    target_name = "web-search-tool"

    session = boto3.session.Session(region_name=REGION)
    account_id = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")
    agentcore = session.client("bedrock-agentcore-control", region_name=REGION)

    log(f"Provisioning AgentCore Web Search gateway in {REGION} (account {account_id})")
    log(f"  gateway={gateway_name}  role={role_name}  target={target_name}")

    role_arn = ensure_execution_role(iam, account_id, role_name)
    gateway = ensure_gateway(agentcore, gateway_name, role_arn)
    # A target can't be created while the gateway is still CREATING.
    wait_gateway_ready(agentcore, gateway["id"])
    # Refresh URL once READY (create_gateway may return it before it's final).
    gateway["url"] = gateway["url"] or get_gateway_url(agentcore, gateway["id"])
    ensure_web_search_target(agentcore, gateway["id"], target_name)
    if not args.no_wait:
        wait_until_ready(agentcore, gateway["id"], target_name)

    url = gateway["url"]
    if not url:
        log("ERROR: gateway has no URL")
        return 1

    if args.output_env:
        with open(args.output_env, "a", encoding="utf-8") as fh:
            fh.write(f"AGENTCORE_GATEWAY_URL={url}\n")

    log(f"Gateway URL: {url}")
    # LAST stdout line = the URL, for shell capture.
    print(url)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ClientError as e:
        log(f"ERROR: {e}")
        sys.exit(1)
