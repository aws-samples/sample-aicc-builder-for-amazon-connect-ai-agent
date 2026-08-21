#!/usr/bin/env python3
"""Smoke tests for the hand-ported validator scripts.

`resources/scripts/validate_consistency.py` and `resources/scripts/shape_parity.py`
are hand ports of `backend/ecs/src/tools/{validate_consistency,shape_parity}.py` —
they are NOT covered by `extract_prompts.sh --check` (which only guards the
auto-extracted prompts/schemas/linter). When a check changes in the backend,
port it by hand and re-run THIS file. It is the "smoke tests" the top-level
README's re-sync section refers to.

What it does, with a synthetic <output_dir> built in a tempdir:

  1. Seeds violations for every contract-level consistency check
     (connect_invoke_permission, session_attribute_contract, lambda_env,
     openapi_status_code) and asserts each one is reported.
  2. Asserts the canonical 'Tool' session attribute is exempted.
  3. Fixes the fixtures and asserts the same checks report nothing
     (no false positives).
  4. Asserts shape_parity exempts the standard errorCode/message response
     envelope at the response ROOT, still flags a genuinely undeclared extra
     property, and passes cleanly when the success response is keyed under
     the spec's success_status_code (201).

Usage:
    python3 scripts/smoke_test_validators.py

Exit 0 = all pass. Requires PyYAML (same as the validators themselves).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "resources" / "scripts"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BAD_FLOW = json.dumps({
    "Actions": [
        {"Type": "InvokeLambdaFunction",
         "Parameters": {"LambdaFunctionARN": "{{LOG_CALL_RESULT_LAMBDA_ARN}}"}},
        {"Type": "UpdateContactAttributes",
         "Parameters": {"Attributes": {
             # 'conversationSummary' is the live-observed synonym drift the
             # session_attribute_contract check exists for.
             "summaryText": "$.Lex.SessionAttributes.conversationSummary",
             "reason": "$.Lex.SessionAttributes.escalationReason"}}},
        {"Type": "Compare",
         "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.Tool"}},
    ]
})

GOOD_FLOW = BAD_FLOW.replace("conversationSummary", "escalationSummary")

PROMPT = "Set escalationReason, escalationSummary and customerIntent before escalating.\n"

BAD_INFRA = """\
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  LogCallResultFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: log-call-result
  UpdateQSessionFunction:
    Type: AWS::Lambda::Function
    Properties:
      Environment:
        Variables:
          OTHER: ""
  LogCallResultApiPermission:
    Type: AWS::Lambda::Permission
    Properties:
      FunctionName: !GetAtt LogCallResultFunction.Arn
      Principal: apigateway.amazonaws.com
"""

GOOD_INFRA = """\
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  LogCallResultFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: log-call-result
  UpdateQSessionFunction:
    Type: AWS::Lambda::Function
    Properties:
      Environment:
        Variables:
          CONNECT_INSTANCE_ID: ""
          AI_ASSISTANT_ID: ""
  LogCallResultConnectPermission:
    Type: AWS::Lambda::Permission
    Properties:
      FunctionName: !GetAtt LogCallResultFunction.Arn
      Principal: connect.amazonaws.com
      SourceAccount: !Ref AWS::AccountId
"""

SPEC = {
    "operation_id": "create_reservation",
    "http_method": "POST",
    "path": "/tools/create_reservation",
    "success_status_code": 201,
    "input_fields": [{"name": "customerName", "field_type": "string", "required": True}],
    "output_fields": [
        {"name": "reservationId", "field_type": "string"},
        {"name": "success", "field_type": "boolean"},
    ],
}

BAD_OPENAPI = """\
openapi: 3.0.0
info: {title: t, version: '1.0'}
paths:
  /tools/create_reservation:
    post:
      operationId: create_reservation
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                customerName: {type: string}
      responses:
        '200':
          content:
            application/json:
              schema:
                type: object
                properties:
                  reservationId: {type: string}
                  success: {type: boolean}
                  errorCode: {type: string}
                  message: {type: string}
                  bogusField: {type: string}
        '500':
          description: err
"""

GOOD_OPENAPI = BAD_OPENAPI.replace("'200':", "'201':").replace(
    "                  bogusField: {type: string}\n", "")

LAMBDA = """\
def handler(event, context):
    body = event.get("body") or {}
    name = body.get("customerName")
    return {"statusCode": 201, "body": {"reservationId": "r1", "success": True,
                                        "errorCode": None, "message": "ok"}}
"""

NEW_CHECKS = {"connect_invoke_permission", "session_attribute_contract",
              "lambda_env", "openapi_status_code"}


def build(root: Path, good: bool) -> None:
    (root / "state/specs").mkdir(parents=True)
    a = root / "assets/v1"
    for d in ("contact_flow", "prompt", "infrastructure", "openapi",
              "lambda/create_reservation"):
        (a / d).mkdir(parents=True)
    (root / "state/specs/create_reservation.json").write_text(json.dumps(SPEC))
    (a / "contact_flow/contact_flow.json").write_text(GOOD_FLOW if good else BAD_FLOW)
    (a / "prompt/ai_agent_prompt.yaml").write_text(PROMPT)
    (a / "infrastructure/template.yaml").write_text(GOOD_INFRA if good else BAD_INFRA)
    (a / "openapi/openapi.yaml").write_text(GOOD_OPENAPI if good else BAD_OPENAPI)
    (a / "lambda/create_reservation/index.py").write_text(LAMBDA)


def run_vc(root: Path) -> dict:
    p = subprocess.run([sys.executable, str(SCRIPTS / "validate_consistency.py"),
                        str(root), "--json"], capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        print(f"FATAL: validate_consistency.py produced no JSON.\n"
              f"stderr: {p.stderr}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad"
        build(bad, good=False)
        res = run_vc(bad)
        checks = {m["check"] for m in res["mismatches"]}
        for want in sorted(NEW_CHECKS):
            if want in checks:
                print(f"PASS  bad fixture triggers {want}")
            else:
                failures.append(f"bad fixture did NOT trigger {want}; got {checks}")

        attrs = {m["field"] for m in res["mismatches"]
                 if m["check"] == "session_attribute_contract"}
        if "Tool" in attrs:
            failures.append("'Tool' must be exempt from session_attribute_contract")
        else:
            print("PASS  'Tool' session attribute exempted")

        good = Path(td) / "good"
        build(good, good=True)
        res2 = run_vc(good)
        fp = [m for m in res2["mismatches"] if m["check"] in NEW_CHECKS]
        if fp:
            failures.append(f"good fixture false positives: {fp}")
        else:
            print("PASS  good fixture: no false positives from the contract checks")

        # ---- shape_parity: envelope exemption + real extras still flagged ----
        sys.path.insert(0, str(SCRIPTS))
        import yaml  # required by the validators themselves
        from shape_parity import validate_shape_parity

        bad_doc = yaml.safe_load(BAD_OPENAPI)
        mm = validate_shape_parity(SPEC, bad_doc)
        reasons = {(m.reason, m.path.rsplit(".", 1)[-1]) for m in mm}
        if ("property_extra_in_openapi", "errorCode") in reasons or \
           ("property_extra_in_openapi", "message") in reasons:
            failures.append(f"shape_parity flagged the standard envelope: {reasons}")
        else:
            print("PASS  shape_parity exempts errorCode/message at response root")
        if ("property_extra_in_openapi", "bogusField") in reasons:
            print("PASS  shape_parity still flags a genuinely undeclared extra property")
        else:
            failures.append(f"shape_parity missed bogusField: {reasons}")

        good_doc = yaml.safe_load(GOOD_OPENAPI)
        mm2 = validate_shape_parity(SPEC, good_doc)
        if mm2:
            failures.append(
                f"shape_parity good fixture not clean: {[(m.reason, m.path) for m in mm2]}")
        else:
            print("PASS  shape_parity clean on the fixed fixture (201 response found)")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
