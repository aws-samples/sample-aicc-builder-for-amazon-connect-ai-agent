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
# D8: OpenAPI success response status code vs spec.success_status_code
# ─────────────────────────────────────────────────────────────────────────────

OPENAPI_MISSING_201 = """\
openapi: "3.0.1"
info:
  title: Demo API
  version: "1.0.0"
paths:
  /tools/create_cleaning_reservation:
    post:
      operationId: create_cleaning_reservation
      responses:
        '200':
          description: business failure
          content:
            application/json:
              schema:
                type: object
        '500':
          description: internal error
          content:
            application/json:
              schema:
                type: object
"""

OPENAPI_HAS_201 = OPENAPI_MISSING_201.replace(
    "      responses:\n        '200':",
    "      responses:\n        '201':\n          description: created\n          content:\n            application/json:\n              schema:\n                type: object\n        '200':",
)


def test_success_status_code_missing_flagged(monkeypatch, tmp_path):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import OperationSpec

    spec = OperationSpec(
        operation_id="create_cleaning_reservation",
        input_fields=[], output_fields=[],
        success_status_code=201,
    )
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"create_cleaning_reservation": spec})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [])
    assets = {"sessions/s/openapi/openapi.yaml": OPENAPI_MISSING_201}
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: list(assets.keys()))
    monkeypatch.setattr(vc, "get_asset_from_s3", lambda key: assets.get(key))

    result = vc.validate_parameter_consistency("s")
    issues = [m for m in result["mismatches"] if m["asset_type"] == "openapi_status_code"]
    assert len(issues) == 1
    assert issues[0]["operation_id"] == "create_cleaning_reservation"
    assert "201" in issues[0]["issue"]


def test_success_status_code_present_not_flagged(monkeypatch, tmp_path):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import OperationSpec

    spec = OperationSpec(
        operation_id="create_cleaning_reservation",
        input_fields=[], output_fields=[],
        success_status_code=201,
    )
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"create_cleaning_reservation": spec})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [])
    assets = {"sessions/s/openapi/openapi.yaml": OPENAPI_HAS_201}
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: list(assets.keys()))
    monkeypatch.setattr(vc, "get_asset_from_s3", lambda key: assets.get(key))

    result = vc.validate_parameter_consistency("s")
    issues = [m for m in result["mismatches"] if m["asset_type"] == "openapi_status_code"]
    assert issues == []


def test_success_status_code_default_200_not_flagged(monkeypatch, tmp_path):
    """The default success_status_code=200 must not false-positive when
    OpenAPI correctly declares '200' (the overwhelmingly common case)."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import OperationSpec

    spec = OperationSpec(operation_id="op1", input_fields=[], output_fields=[])
    assert spec.success_status_code == 200
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"op1": spec})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [])
    openapi = OPENAPI_MISSING_201.replace("create_cleaning_reservation", "op1")
    assets = {"sessions/s/openapi/openapi.yaml": openapi}
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: list(assets.keys()))
    monkeypatch.setattr(vc, "get_asset_from_s3", lambda key: assets.get(key))

    result = vc.validate_parameter_consistency("s")
    issues = [m for m in result["mismatches"] if m["asset_type"] == "openapi_status_code"]
    assert issues == []


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


def _sample_rows_gate(monkeypatch, tmp_path, template: str):
    """Run the consistency gate with one infrastructure template and a spec that
    carries customer-supplied sample rows."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import InfrastructureSpec, DynamoDbConfig

    ispec = InfrastructureSpec.model_construct(
        project_name="daon", db_type="dynamodb", region="ap-northeast-2",
        dynamodb_config=DynamoDbConfig.model_construct(
            tables=[{"name": "subscribers", "partition_key": "phoneNumber"}],
            sample_rows={"subscribers": [
                {"phoneNumber": "010-2222-3333", "customerName": "홍길동",
                 "birthDate": "19900512", "planCode": "STD15", "usedGb": 12.4},
            ]}),
    )
    import tools.spec_manager as sm
    from tools.spec_manager import OperationSpec
    op = OperationSpec.model_construct(operation_id="get_plan_info", input_fields=[], output_fields=[], tools=[])
    monkeypatch.setattr(sm, "get_infrastructure_spec", lambda: ispec)
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"get_plan_info": op})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [])
    monkeypatch.setattr(vc, "list_session_assets",
                        lambda sid: ["assets/session-x/infrastructure/daon/infrastructure.yaml"])
    monkeypatch.setattr(vc, "get_asset_from_s3", lambda key: template)
    return vc.validate_parameter_consistency("session-x")


def test_sample_rows_seeded_verbatim_pass(monkeypatch, tmp_path):
    template = ("Resources:\n  Seeder:\n    Properties:\n      Code: |\n"
                "        rows = [{'phoneNumber': '010-2222-3333', 'customerName': '홍길동',"
                " 'birthDate': '19900512', 'planCode': 'STD15', 'usedGb': 12.4}]\n")
    result = _sample_rows_gate(monkeypatch, tmp_path, template)
    assert [m for m in result["mismatches"] if m["asset_type"] == "infrastructure"] == []


def test_sample_rows_altered_by_the_seeder_is_a_blocking_mismatch(monkeypatch, tmp_path):
    """Live (Daon, 2026-09-13): the requirements said 홍길동 / 19900512; the seeder
    shipped 19880312 and the identity check in every test dialog failed."""
    template = ("Resources:\n  Seeder:\n    Properties:\n      Code: |\n"
                "        rows = [{'phoneNumber': '010-2222-3333', 'customerName': '홍길동',"
                " 'birthDate': '19880312', 'planCode': 'STD15', 'usedGb': 12.4}]\n")
    result = _sample_rows_gate(monkeypatch, tmp_path, template)
    hits = [m for m in result["mismatches"] if m["asset_type"] == "infrastructure"]
    assert len(hits) == 1
    assert "birthDate='19900512'" in hits[0]["issue"]
    assert hits[0]["operation_id"] == "subscribers"


def test_retrieve_guide_is_added_once_after_the_tool_list():
    """Live (Daon, 2026-09-13): the generated prompt had guides for every
    operation tool, Escalate and Complete but none for RETRIEVE, and the agent
    answered a FAQ the knowledge base held with "I cannot tell you precisely"."""
    from tools.asset_linters import ensure_retrieve_tool_guide

    prompt = (
        "system_prompt: |\n"
        "  <tool_instructions>\n"
        "  사용 가능한 도구:\n"
        "  {{$.toolConfigurationList}}\n"
        "\n"
        "  [get_plan_info 도구 사용 가이드 - 요금제 조회]\n"
        "  고객이 자신의 요금제를 문의할 때 사용합니다.\n"
        "  </tool_instructions>\n"
    )
    fixed, fixes = ensure_retrieve_tool_guide(prompt, "ko")
    assert fixes and "RETRIEVE" in fixes[0]
    lines = fixed.split("\n")
    i = next(k for k, l in enumerate(lines) if "toolConfigurationList" in l)
    assert lines[i + 2].startswith("  [RETRIEVE 도구 사용 가이드")
    assert "  [get_plan_info 도구 사용 가이드" in fixed
    again, fixes2 = ensure_retrieve_tool_guide(fixed, "ko")
    assert again == fixed and fixes2 == []
    # a prompt that already guides RETRIEVE is left alone
    ok_prompt = prompt.replace("[get_plan_info", "[RETRIEVE 도구 사용 가이드 - 지식 검색]\n  검색하세요.\n  [get_plan_info")
    assert ensure_retrieve_tool_guide(ok_prompt, "ko") == (ok_prompt, [])
    # no tool block → untouched
    assert ensure_retrieve_tool_guide("system_prompt: |\n  hello\n", "en") == ("system_prompt: |\n  hello\n", [])


def test_cfn_gsi_names_reads_every_table_in_the_template():
    """Live (Hanbit, 2026-09-13): D1-1 knew only the schema registry's GSIs and
    blocked a Lambda querying PatientsTable's 'phone-birth-index', which the
    CloudFormation template (the artifact that deploys) defined."""
    from tools.validate_consistency import _cfn_gsi_names

    template = """
Resources:
  PatientsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: !Sub "${AWS::StackName}-patients"
      GlobalSecondaryIndexes:
        - IndexName: phone-birth-index
          KeySchema: [{AttributeName: phone, KeyType: HASH}]
  AppointmentsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: hanbit-appointments
      GlobalSecondaryIndexes:
        - IndexName: phone-index
        - IndexName: dept-date-index
  Fn:
    Type: AWS::Lambda::Function
"""
    names = _cfn_gsi_names(template)
    assert names["hanbit-appointments"] == {"phone-index", "dept-date-index"}
    assert {"phone-birth-index"} in names.values()
    assert _cfn_gsi_names(None) == {} and _cfn_gsi_names("not: [valid") == {}


def test_ai_prompt_unknown_variable_is_rewritten_to_a_custom_attribute():
    """Live (Hanul, 2026-09-13): `{{$.channel}}` made CreateAIPrompt reject the
    prompt ('Prompt contains unknown variable') and the deploy finished with no
    AI agent. Known variables and $.Custom.* pass; a bare unknown name becomes a
    custom attribute; a non-identifier loses its braces."""
    from tools.asset_linters import lint_ai_prompt

    text = ("채널은 {{$.channel}}입니다. 도구: {{$.toolConfigurationList}} "
            "이름: {{$.Custom.firstName}} 이상: {{$.foo.bar}}")
    result = lint_ai_prompt(text)
    assert "{{$.Custom.channel}}" in result["fixed_text"]
    assert "{{$.channel}}" not in result["fixed_text"]
    assert "{{$.toolConfigurationList}}" in result["fixed_text"]
    assert "{{$.Custom.firstName}}" in result["fixed_text"]
    assert "$.foo.bar" in result["fixed_text"] and "{{$.foo.bar}}" not in result["fixed_text"]
    assert result["unknown_variables"] == ["channel", "foo.bar"]
    assert any("Custom.channel" in w for w in result["warnings"])
    clean = lint_ai_prompt("{{$.toolConfigurationList}} and {{$.Custom.x}}")
    assert clean["fixes_applied"] == [] and clean["unknown_variables"] == []


def test_cors_headers_mapped_by_integration_are_declared_in_method_responses():
    """Live (GreenCart, 2026-09-13): the model declared
    'Access-Control-All-Methods' (typo) while mapping 'Access-Control-Allow-Methods';
    API Gateway rejected the OPTIONS method and the whole stack rolled back."""
    from tools.merge_infrastructure import _fix_cors_response_headers

    tpl = """Resources:
  RequestReturnOptions:
    Type: AWS::ApiGateway::Method
    Properties:
      HttpMethod: OPTIONS
      Integration:
        Type: MOCK
        IntegrationResponses:
          - StatusCode: 200
            ResponseParameters:
              method.response.header.Access-Control-Allow-Headers: "'Content-Type'"
              method.response.header.Access-Control-Allow-Methods: "'*'"
              method.response.header.Access-Control-Allow-Origin: "'*'"
      MethodResponses:
        - StatusCode: 200
          ResponseParameters:
            method.response.header.Access-Control-Allow-Headers: true
            method.response.header.Access-Control-All-Methods: true
  GetOrderStatus:
    Type: AWS::ApiGateway::Method
    Properties:
      HttpMethod: GET
      Integration:
        Type: AWS_PROXY
      MethodResponses:
        - StatusCode: 200
  Bucket:
    Type: AWS::S3::Bucket
"""
    out = _fix_cors_response_headers(tpl)
    assert "method.response.header.Access-Control-All-Methods" not in out
    assert out.count("method.response.header.Access-Control-Allow-Methods: true") == 1
    assert out.count("method.response.header.Access-Control-Allow-Origin: true") == 1
    assert _fix_cors_response_headers(out) == out          # idempotent
    assert "Bucket:\n    Type: AWS::S3::Bucket" in out       # untouched neighbours


def test_template_under_cloudformation_folder_feeds_the_template_checks(monkeypatch, tmp_path):
    """Live (Hanbit, 2026-09-14): the generator stores the template under
    cloudformation/<project>/infrastructure.yaml; the gate only read
    infrastructure/…, so the template-based checks (IAM, GSI union) silently
    never ran and a correct 'phone-birth-index' query stayed blocked."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    import tools.validate_consistency as vc
    from tools.spec_manager import OperationSpec

    spec = OperationSpec(operation_id="book_appointment", input_fields=[], output_fields=[])
    monkeypatch.setattr(vc, "get_all_specs", lambda: {"book_appointment": spec})
    monkeypatch.setattr(vc, "get_all_tools", lambda: [])
    template = """
Resources:
  PatientsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: patients
      GlobalSecondaryIndexes:
        - IndexName: phone-birth-index
  AppointmentsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: appointments
      GlobalSecondaryIndexes:
        - IndexName: phone-index
"""
    handler = "import boto3\ndef lambda_handler(e, c):\n    t.query(IndexName='phone-birth-index')\n"
    assets = {
        "sessions/s1/cloudformation/hanbit/infrastructure.yaml": template,
        "sessions/s1/lambda/book_appointment/handler.py": handler,
    }
    monkeypatch.setattr(vc, "list_session_assets", lambda sid: list(assets.keys()))
    monkeypatch.setattr(vc, "get_asset_from_s3", lambda key: assets.get(key))
    # a registry that only knows the appointments table's GSI (the live drift)
    schema = {"tables": {"appointments": {"gsi": [{"index_name": "phone-index"}]}}}
    import agents.infrastructure_generator.agent as infra_agent
    monkeypatch.setattr(infra_agent, "get_infrastructure_schema", lambda: __import__("json").dumps(schema))
    result = vc.validate_parameter_consistency("s1")
    gsi_issues = [m for m in result["mismatches"] if m["asset_type"] == "lambda_gsi"]
    assert gsi_issues == [], gsi_issues
