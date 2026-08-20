"""Regression tests for the review-gate hardening from two live workshop sessions.

Sessions session-4427fe52 (inbound) and session-c676bdc7 (outbound) surfaced
the same recurring defects that the LLM reviewer had to catch manually:

1. validate_parameter_consistency CRASHED in both sessions
   (``AttributeError: 'dict' object has no attribute 'name'``) — specs restored
   through the model_construct fallback carry raw dict field entries.
2. Lambdas the Contact Flow invokes directly were missing the
   ``connect.amazonaws.com`` AWS::Lambda::Permission.
3. UpdateQSessionFunction was missing CONNECT_INSTANCE_ID / AI_ASSISTANT_ID env
   var keys (handler throws on cold start).
4. UpdateQSessionRole had a policy but with the WRONG action
   (wisdom:UpdateSession instead of wisdom:UpdateSessionData) — the old merge
   backstop only triggered when connect:DescribeContact was absent.
5. Flow read $.Lex.SessionAttributes.conversationSummary while the prompt told
   the bot to set escalationSummary — agent screen context always empty.

These tests pin the deterministic prevention for each.
"""

import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ─────────────────────────────────────────────────────────────────────────────
# 1. _safe_parse_model nested coercion (validator crash root cause)
# ─────────────────────────────────────────────────────────────────────────────

def test_model_construct_fallback_coerces_nested_fields():
    from tools.spec_manager import _safe_parse_model, OperationSpec, FieldSpec, ToolSpec

    # Force validation failure so the model_construct fallback runs:
    # operation_id must be a str — give it a dict.
    bad = {
        "operation_id": {"oops": True},
        "input_fields": [{"name": "phoneNumber", "field_type": "string"}],
        "output_fields": [{"name": "found", "field_type": "boolean"}],
        "tools": [{
            "tool_id": "record_monitoring_result",
            "input_fields": [{"name": "callResult", "field_type": "enum"}],
            "output_fields": [{"name": "resultId"}],
        }],
    }
    spec = _safe_parse_model(OperationSpec, bad)

    # The exact access pattern that crashed the gate live:
    names = {f.name for f in spec.input_fields if f.name}
    assert names == {"phoneNumber"}
    assert {f.name for f in spec.output_fields} == {"found"}

    assert spec.tools and isinstance(spec.tools[0], ToolSpec)
    assert isinstance(spec.tools[0].input_fields[0], FieldSpec)
    assert spec.tools[0].input_fields[0].name == "callResult"


def test_validate_gate_survives_dict_fields(monkeypatch, tmp_path):
    """Even if a dict sneaks past coercion, the gate must not raise."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import OperationSpec

    raw = OperationSpec.model_construct(
        operation_id="op1",
        input_fields=[{"name": "orderNumber"}],   # raw dicts on purpose
        output_fields=[{"name": "found"}],
        tools=[],
    )
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"op1": raw})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [
        {"tool_id": "op1", "input_fields": [{"name": "orderNumber"}], "output_fields": []},
    ])
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: [])

    result = vc.validate_parameter_consistency("session-test")
    assert "mismatches" in result  # structured result, no exception


def test_validate_gate_returns_structured_error_on_crash(monkeypatch):
    """A hard crash inside the impl must yield a structured failure, not a raw
    tool exception (which is what erased the whole gate in both sessions)."""
    import tools.validate_consistency as vc

    def boom():
        raise AttributeError("'dict' object has no attribute 'name'")

    monkeypatch.setattr(vc, "get_all_specs", boom)
    result = vc.validate_parameter_consistency("session-test")
    assert result["success"] is False
    assert result.get("internal_error") is True
    assert "AttributeError" in result["summary"]


# ─────────────────────────────────────────────────────────────────────────────
# 2–3. Validator checks D5/D7 (connect invoke permission, qsession env vars)
# ─────────────────────────────────────────────────────────────────────────────

FLOW_JSON = """
{
  "Actions": [
    {"Identifier": "customer-lookup", "Type": "InvokeLambdaFunction",
     "Parameters": {"LambdaFunctionARN": "{{GET_MONITORING_TARGET_LAMBDA_ARN}}"}},
    {"Identifier": "record-result", "Type": "InvokeLambdaFunction",
     "Parameters": {"LambdaFunctionARN": "{{RECORD_MONITORING_RESULT_LAMBDA_ARN}}"}},
    {"Identifier": "check", "Type": "Compare",
     "Parameters": {"ComparisonValue": "$.Lex.SessionAttributes.Tool"}},
    {"Identifier": "esc", "Type": "UpdateContactAttributes",
     "Parameters": {"Attributes": {"summary": "$.Lex.SessionAttributes.conversationSummary"}}}
  ]
}
"""

INFRA_YAML = """\
Resources:
  GetMonitoringTargetFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: get-monitoring-target
      Handler: index.handler
  RecordMonitoringResultFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: record-monitoring-result
      Handler: index.handler
  GetMonitoringTargetPermission:
    Type: AWS::Lambda::Permission
    Properties:
      FunctionName: !GetAtt GetMonitoringTargetFunction.Arn
      Action: lambda:InvokeFunction
      Principal: apigateway.amazonaws.com
  GetMonitoringTargetConnectPermission:
    Type: AWS::Lambda::Permission
    Properties:
      FunctionName: !GetAtt GetMonitoringTargetFunction.Arn
      Action: lambda:InvokeFunction
      Principal: connect.amazonaws.com
      SourceAccount: !Ref AWS::AccountId
  UpdateQSessionFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: update-q-session
      Runtime: nodejs18.x
      Handler: index.handler
      Environment:
        Variables:
          PROJECT_NAME: demo
"""

PROMPT_TEXT = """\
You are an agent. When escalating, set the session attribute escalationSummary
with a short summary, then use Escalate.
"""


@pytest.fixture()
def gate(monkeypatch, tmp_path):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import OperationSpec

    spec = OperationSpec(operation_id="op1", input_fields=[], output_fields=[])
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"op1": spec})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [])

    assets = {
        "sessions/s1/contact_flow/main_flow.json": FLOW_JSON,
        "sessions/s1/infrastructure/infrastructure.yaml": INFRA_YAML,
        "sessions/s1/prompt/ai_agent_prompt.yaml": PROMPT_TEXT,
    }
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: list(assets.keys()))
    monkeypatch.setattr(vc, "get_asset_from_s3", lambda key: assets.get(key))
    return vc


def test_connect_invoke_permission_check(gate):
    result = gate.validate_parameter_consistency("s1")
    perm_issues = [m for m in result["mismatches"]
                   if m["asset_type"] == "connect_invoke_permission"]
    # record_monitoring_result has NO connect permission → flagged;
    # get_monitoring_target HAS one → not flagged.
    assert len(perm_issues) == 1
    assert perm_issues[0]["operation_id"] == "record_monitoring_result"


def test_session_attribute_contract_check(gate):
    result = gate.validate_parameter_consistency("s1")
    attr_issues = [m for m in result["mismatches"]
                   if m["asset_type"] == "session_attribute_contract"]
    # Flow reads conversationSummary; prompt only defines escalationSummary.
    assert [m["field"] for m in attr_issues] == ["conversationSummary"]
    # 'Tool' (canonical contract) must NOT be flagged.
    assert all(m["field"] != "Tool" for m in attr_issues)


def test_qsession_env_var_check(gate):
    result = gate.validate_parameter_consistency("s1")
    env_issues = [m for m in result["mismatches"]
                  if m["asset_type"] == "lambda_env" and m["operation_id"] == "update_q_session"]
    assert {m["field"] for m in env_issues} == {"CONNECT_INSTANCE_ID", "AI_ASSISTANT_ID"}


# ─────────────────────────────────────────────────────────────────────────────
# 4–5. Merge-time backstops (role actions, env var injection)
# ─────────────────────────────────────────────────────────────────────────────

ROLE_WITH_WRONG_ACTION = """\
Resources:
  UpdateQSessionRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: lambda.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
      Policies:
        - PolicyName: UpdateQSessionPolicy
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action:
                  - wisdom:GetSession
                  - wisdom:UpdateSession
                  - wisdom:GetAssistant
                  - connect:DescribeContact
                Resource: "*"
  Other:
    Type: AWS::SNS::Topic
"""

ROLE_WITHOUT_POLICY = """\
Resources:
  UpdateQSessionRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: lambda.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  Other:
    Type: AWS::SNS::Topic
"""


def test_qsession_role_missing_action_injected_into_existing_policy():
    """The outbound session had wisdom:UpdateSession (wrong) but not
    wisdom:UpdateSessionData — the old trigger (connect:DescribeContact
    present → skip) missed it entirely."""
    from tools.merge_infrastructure import _ensure_qsession_role_permissions
    import yaml

    fixed = _ensure_qsession_role_permissions(ROLE_WITH_WRONG_ACTION)
    assert "wisdom:UpdateSessionData" in fixed
    # Must NOT create a duplicate Policies: key (invalid YAML)
    role_block = re.search(r'(^  UpdateQSessionRole:\n(?:^(?:    |\n).*\n?)*)', fixed, re.M).group(1)
    assert role_block.count("Policies:") == 1
    # Result must stay parseable YAML
    parsed = yaml.safe_load(fixed)
    stmts = parsed["Resources"]["UpdateQSessionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = [a for s in stmts for a in s.get("Action", [])]
    assert "wisdom:UpdateSessionData" in actions


def test_qsession_role_no_policy_appends_full_block():
    from tools.merge_infrastructure import _ensure_qsession_role_permissions
    import yaml

    fixed = _ensure_qsession_role_permissions(ROLE_WITHOUT_POLICY)
    assert "wisdom:UpdateSessionData" in fixed
    assert "connect:DescribeContact" in fixed
    parsed = yaml.safe_load(fixed)
    assert parsed["Resources"]["UpdateQSessionRole"]["Properties"]["Policies"]


def test_qsession_role_complete_is_untouched():
    from tools.merge_infrastructure import _ensure_qsession_role_permissions

    complete = ROLE_WITH_WRONG_ACTION.replace(
        "- wisdom:UpdateSession\n", "- wisdom:UpdateSessionData\n"
    ).replace(
        "- connect:DescribeContact\n",
        "- connect:DescribeContact\n                  - connect:GetContactAttributes\n"
    ).replace(
        "- wisdom:GetAssistant\n", "- wisdom:GetAssistant\n"
    )
    # add remaining required action so nothing is missing
    complete = complete.replace(
        "- wisdom:GetSession\n",
        "- wisdom:GetSession\n                  - wisdom:UpdateSessionData\n")
    fixed = _ensure_qsession_role_permissions(complete)
    assert fixed == complete


FUNC_NO_ENV = """\
Resources:
  UpdateQSessionFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: update-q-session
      Runtime: nodejs18.x
      Handler: index.handler
      Role: arn:aws:iam::123456789012:role/UpdateQSessionRole
      Code:
        ZipFile: |
          exports.handler = async () => ({});
  Other:
    Type: AWS::SNS::Topic
"""

FUNC_PARTIAL_ENV = """\
Resources:
  UpdateQSessionFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: update-q-session
      Runtime: nodejs18.x
      Handler: index.handler
      Environment:
        Variables:
          PROJECT_NAME: demo
          CONNECT_INSTANCE_ID: ""
  Other:
    Type: AWS::SNS::Topic
"""


def test_qsession_env_injected_when_absent():
    from tools.merge_infrastructure import _ensure_qsession_env_vars
    import yaml

    fixed = _ensure_qsession_env_vars(FUNC_NO_ENV)
    parsed = yaml.safe_load(fixed)
    env = parsed["Resources"]["UpdateQSessionFunction"]["Properties"]["Environment"]["Variables"]
    assert "CONNECT_INSTANCE_ID" in env and "AI_ASSISTANT_ID" in env


def test_qsession_env_appended_to_existing_variables():
    from tools.merge_infrastructure import _ensure_qsession_env_vars
    import yaml

    fixed = _ensure_qsession_env_vars(FUNC_PARTIAL_ENV)
    parsed = yaml.safe_load(fixed)
    env = parsed["Resources"]["UpdateQSessionFunction"]["Properties"]["Environment"]["Variables"]
    assert env["PROJECT_NAME"] == "demo"          # untouched
    assert "AI_ASSISTANT_ID" in env               # added
    # Only one Environment block
    assert fixed.count("Environment:") == 1


def test_qsession_env_complete_is_untouched():
    from tools.merge_infrastructure import _ensure_qsession_env_vars

    complete = FUNC_PARTIAL_ENV.replace(
        'CONNECT_INSTANCE_ID: ""\n',
        'CONNECT_INSTANCE_ID: ""\n          AI_ASSISTANT_ID: ""\n')
    assert _ensure_qsession_env_vars(complete) == complete
