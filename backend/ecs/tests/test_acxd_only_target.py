"""ACXD only (v3.1): the Agentic CX Designer application alone.

The target reuses every ACXD code path and withholds the Classic backend,
Contact Flow and AI Prompt: no generator, no gate and no bundle file for them,
and Data Requests that call the customer's own API at {WEBHOOK_URL}.
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for _path in (_SRC, _ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


class _Model:
    def __init__(self, payload: dict):
        self.payload = payload

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self.payload))


# ---------------------------------------------------------------------------
# Runtime target
# ---------------------------------------------------------------------------

def test_acxd_only_is_a_persisted_runtime_target_of_the_acxd_family():
    from tools import acxd_flow_spec as fs

    assert "acxd_only" in fs.RUNTIME_TARGETS
    sid = "session-acxd-only-target"
    try:
        assert fs.set_runtime_target(sid, "acxd_only") is True
        assert fs.get_runtime_target(sid) == "acxd_only"
        assert fs.is_acxd_target(sid) is True          # every ACXD code path applies
        assert fs.is_acxd_only_target(sid) is True
        fs.set_runtime_target(sid, "acxd")
        assert fs.is_acxd_target(sid) is True
        assert fs.is_acxd_only_target(sid) is False
    finally:
        fs.clear_runtime_target(sid)


def test_backend_setting_normalizers():
    from tools.acxd_flow_spec import normalize_backend_auth_header, normalize_backend_base_url

    assert normalize_backend_auth_header("x-api-key") == ("x-api-key", None)
    assert normalize_backend_auth_header("Authorization: Bearer abc") == ("Authorization", None)
    assert normalize_backend_auth_header("none") == ("", None)
    assert normalize_backend_auth_header("없음") == ("", None)
    assert normalize_backend_auth_header(None) == (None, None)
    header, error = normalize_backend_auth_header("x api key!")
    assert header is None and "header NAME" in error

    assert normalize_backend_base_url("https://api.example.com/v1/") == ("https://api.example.com/v1", None)
    assert normalize_backend_base_url("") == ("", None)
    url, error = normalize_backend_base_url("http://api.example.com")
    assert url is None and "https://" in error
    url, error = normalize_backend_base_url("{WEBHOOK_URL}")
    assert url is None and error


def test_acxd_only_readiness_requires_a_decided_auth_header_and_an_ascii_project_name():
    from tools.acxd_flow_spec import (
        ACXDFlowSpec, acxd_only_backend_problems, derived_project_name, normalize_project_name)

    spec = ACXDFlowSpec()
    spec.application.name = "Order Assistant"
    assert spec.application.backend_auth_header is None
    assert any("backend_auth_header" in p for p in acxd_only_backend_problems(spec))
    spec.application.backend_auth_header = ""          # explicitly none
    assert acxd_only_backend_problems(spec) == []
    spec.application.backend_auth_header = "x-api-key"
    assert acxd_only_backend_problems(spec) == []
    assert derived_project_name(spec.application) == "order"

    # A Korean application name carries no ASCII project name: it must be recorded.
    spec.application.name = "삼성전자로지텍 AI 상담원"
    assert derived_project_name(spec.application) is None
    assert any("project_name" in p for p in acxd_only_backend_problems(spec))
    spec.application.project_name = "selc-voice"
    assert acxd_only_backend_problems(spec) == []

    assert normalize_project_name("SELC Voice") == ("selc-voice", None)
    assert normalize_project_name("셀크")[0] is None


# ---------------------------------------------------------------------------
# Tools and prompts per phase
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("SESSION_STORE_BACKEND", "s3files")
    import app

    return app


def test_acxd_only_withholds_every_classic_backend_and_contact_flow_tool(app_mod, monkeypatch):
    def generate_acxd_application():
        pass

    def patch_acxd_asset():
        pass

    monkeypatch.setattr(app_mod, "_load_acxd_application_tool", lambda: generate_acxd_application)
    monkeypatch.setattr(app_mod, "_load_acxd_asset_patcher", lambda: patch_acxd_asset)
    from tools.deterministic_repairs import enforce_openapi_contract_tool, rebuild_acxd_slot_types_tool

    interview = app_mod.get_tools_for_phase("interview", runtime_target="acxd_only")
    for withheld in (app_mod.save_infrastructure_spec, app_mod.save_contact_flow_spec,
                     app_mod.introspect_database):
        assert withheld not in interview
    assert set(app_mod.ACXD_INTERVIEW_TOOLS).issubset(interview)
    assert app_mod.save_operation_spec in interview
    assert app_mod.complete_interview in interview

    for phase in ("generation", "review", "post_generation"):
        tools = app_mod.get_tools_for_phase(phase, runtime_target="acxd_only")
        for withheld in app_mod.ACXD_ONLY_EXCLUDED_TOOLS:
            assert withheld not in tools, (phase, getattr(withheld, "tool_name", withheld))
        assert enforce_openapi_contract_tool not in tools
        for kept in (app_mod.faq_generator_agent, app_mod.reviewer_agent,
                     app_mod.validate_parameter_consistency, generate_acxd_application,
                     patch_acxd_asset, rebuild_acxd_slot_types_tool, app_mod.update_operation_spec):
            assert kept in tools, (phase, kept)

    # The full ACXD target is unchanged: it still builds the Classic backend.
    acxd = app_mod.get_tools_for_phase("generation", runtime_target="acxd")
    assert app_mod.lambda_generator_agent in acxd
    assert app_mod.contact_flow_generator_agent in acxd
    assert enforce_openapi_contract_tool in acxd


def test_acxd_only_asset_set_and_normalization(app_mod):
    assert app_mod._normalize_runtime_target("acxd_only") == "acxd_only"
    assert app_mod._full_asset_set_for_runtime("acxd_only") == {"acxd_application", "knowledge_base"}


def test_acxd_only_prompts_replace_the_classic_generation_body():
    from prompts.interview_agent_prompt import get_interview_agent_prompt
    from prompts import system_prompt as sp

    interview = get_interview_agent_prompt("acxd_only")[0]["text"]
    assert "ACXD RUNTIME TARGET: FLOW DESIGN INSERTION" in interview      # the ACXD design still applies
    assert "ACXD ONLY: THE APPLICATION ALONE" in interview
    assert "backend_auth_header" in interview
    assert "ACXD ONLY" not in get_interview_agent_prompt("acxd")[0]["text"]

    for phase in ("generation", "review", "post_generation"):
        text = sp.get_phase_system_prompt(phase, runtime_target="acxd_only")[0]["text"]
        assert "ACXD ONLY — THIS OVERRIDES" in text, phase
        assert "### ACXD native-capability rules" in text, phase
        assert "### Generator Sub-Agents (Single-turn)" not in text, phase   # Classic TOOLS_REFERENCE
        assert "CRITICAL: SCHEMA PROPAGATION WORKFLOW" not in text, phase   # Classic SCHEMA_REFERENCE
        assert "### ACXD generation phases" not in text, phase               # the full-ACXD phase list
    generation = sp.get_phase_system_prompt("generation", runtime_target="acxd_only")[0]["text"]
    assert "FIVE-PHASE GENERATION" not in generation
    assert "| Turn 2 | ACXD application | `generate_acxd_application` |" in generation

    # The full ACXD overlay reads exactly as before the split.
    assert sp.ACXD_RUNTIME_TARGET_PROMPT == sp._ACXD_TARGET_OVERLAY + sp._ACXD_SHARED_RULES
    assert "### ACXD generation phases" in sp.ACXD_RUNTIME_TARGET_PROMPT
    acxd = sp.get_phase_system_prompt("generation", runtime_target="acxd")[0]["text"]
    assert "ACXD ONLY — THIS OVERRIDES" not in acxd


def test_generation_progress_uses_the_acxd_only_asset_sets(monkeypatch):
    from context import generation_progress as gp

    monkeypatch.setattr(gp, "_is_acxd_only_target", lambda _sid: True)
    monkeypatch.setattr(gp, "_is_acxd_target", lambda _sid: True)
    assert gp.get_full_asset_set("s") == {"acxd_application", "knowledge_base"}
    assert gp._generation_assets_for_session("s") == {"acxd_application"}


# ---------------------------------------------------------------------------
# Generation context and Data Requests
# ---------------------------------------------------------------------------

def _acxd_only_context(monkeypatch, *, auth_header, path="/v1/orders/lookup", base_url=None):
    from tools import acxd_generation_context as context
    import tools.acxd_flow_spec as fs

    operation = _Model({
        "operation_id": "lookup_order",
        "http_method": "POST",
        "path": path,
        "summary": "Look up an order",
        "input_fields": [{"name": "orderNumber", "field_type": "string", "required": True}],
        "output_fields": [{"name": "status", "field_type": "string"}],
    })
    flow_spec = _Model({
        "flows": [],
        "guardrails": [],
        "knowledge_base": {},
        "application": {"name": "SELC Assistant", "backend_auth_header": auth_header,
                        "backend_base_url": base_url},
    })

    def _openapi_must_not_load(_sid):
        raise AssertionError("ACXD only has no OpenAPI asset to read")

    monkeypatch.setattr(context, "get_all_specs", lambda: {"lookup_order": operation})
    monkeypatch.setattr(context, "get_infrastructure_spec", lambda: None)
    monkeypatch.setattr(context, "get_acxd_flow_spec", lambda _sid=None: flow_spec)
    monkeypatch.setattr(context, "ensure_workspace", lambda: None)
    monkeypatch.setattr(context, "_load_openapi_document", _openapi_must_not_load)
    monkeypatch.setattr(context, "_load_faq_articles", lambda _sid: [])
    monkeypatch.setattr(fs, "is_acxd_only_target", lambda _sid=None: True)
    return context.build_generation_context("session-acxd-only").model_dump()


def test_acxd_only_data_requests_call_the_customers_path_with_its_own_auth_header(monkeypatch):
    from tools.acxd_data_request_builder import build_data_request, build_secret_assets

    payload = _acxd_only_context(monkeypatch, auth_header="x-api-key")
    assert payload["infrastructure"]["project_name"] == "selc"          # from the application name
    integration = payload["data_integrations"][0]
    assert integration["path"] == "/v1/orders/lookup"                    # not coerced to /tools/
    assert integration["external_backend"] is True

    document = build_data_request(integration)
    webhook = document["webhook"]
    assert webhook["url"] == "{WEBHOOK_URL}/v1/orders/lookup"
    header = {"key": "x-api-key", "value": "{selcBackendApiKey:NLX.Secret}", "sensitive": True}
    assert webhook["headers"] == [header]
    assert webhook["environments"]["production"] == {"url": webhook["url"], "headers": [header]}

    secrets = build_secret_assets(payload)
    assert secrets == [{
        "name": "selcBackendApiKey",
        "description": secrets[0]["description"],
        "valueEnv": "ACXD_SECRET_SELCBACKENDAPIKEY",
    }]
    assert "x-api-key header your backend API expects" in secrets[0]["description"]
    assert "CloudFormation" not in secrets[0]["description"]


def test_acxd_only_without_auth_sends_no_implicit_api_key(monkeypatch):
    from tools.acxd_data_request_builder import build_data_request, build_secret_assets

    payload = _acxd_only_context(monkeypatch, auth_header="")
    document = build_data_request(payload["data_integrations"][0])
    assert document["webhook"]["headers"] == []
    assert "headers" not in document["webhook"]["environments"]["development"]
    assert build_secret_assets(payload) == []


@pytest.mark.parametrize("acxd_only", [True, False])
def test_every_helper_tool_of_an_operation_gets_its_own_data_request(monkeypatch, acxd_only):
    """Live (SELC e2e, 2026-09-22): the booking journey needed the price quote
    first; the helper tool had a Lambda and an API path but no Data Request, and
    the orchestrator had to add one by hand during review."""
    from tools import acxd_generation_context as context
    import tools.acxd_flow_spec as fs

    operation = _Model({
        "operation_id": "create_cleaning_reservation",
        "http_method": "POST",
        "path": "/v1/reservations",
        "summary": "Book a cleaning",
        "input_fields": [{"name": "productType", "field_type": "string"}],
        "output_fields": [{"name": "reservationId", "field_type": "string"}],
        "tools": [
            {"tool_id": "create_cleaning_reservation", "role": "primary"},
            {"tool_id": "get_cleaning_price", "role": "helper", "path": "/v1/prices/quote",
             "input_fields": [{"name": "productType", "field_type": "string"}],
             "output_fields": [{"name": "unitPrice", "field_type": "integer"}]},
            {"tool_id": "legacy_quote", "role": "helper", "generate_lambda": False, "generate_openapi": False},
        ],
    })
    flow_spec = _Model({"flows": [], "guardrails": [], "knowledge_base": {},
                        "application": {"name": "Cleaning Assistant", "backend_auth_header": ""}})
    monkeypatch.setattr(context, "get_all_specs", lambda: {"create_cleaning_reservation": operation})
    monkeypatch.setattr(context, "get_infrastructure_spec",
                        lambda: None if acxd_only else _Model({"project_name": "cleaning"}))
    monkeypatch.setattr(context, "get_acxd_flow_spec", lambda _sid=None: flow_spec)
    monkeypatch.setattr(context, "ensure_workspace", lambda: None)
    monkeypatch.setattr(context, "_load_openapi_document", lambda _sid: {})
    monkeypatch.setattr(context, "_load_faq_articles", lambda _sid: [])
    monkeypatch.setattr(fs, "is_acxd_only_target", lambda _sid=None: acxd_only)

    integrations = context.build_generation_context("s").model_dump()["data_integrations"]
    by_id = {i["data_request_id"]: i for i in integrations}
    assert sorted(by_id) == ["createCleaningReservation", "getCleaningPrice"]   # the excluded helper is left out
    helper = by_id["getCleaningPrice"]
    assert helper["operation_ref"] == "get_cleaning_price"
    assert [f["name"] for f in helper["response_fields"]] == ["success", "errorCode", "message", "unitPrice"]
    if acxd_only:
        assert helper["path"] == "/v1/prices/quote"          # the customer's own endpoint
    else:
        assert helper["path"].startswith("/tools/")          # the generated API's contract
    assert bool(helper.get("external_backend")) is acxd_only


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _bundle(**overrides):
    bundle = {
        "flows": [{"flowId": "MainFlow", "nodes": {}}],
        "slot_types": [],
        "context_variables": [{"key": "failReason", "type": "text"}],
        "data_requests": [{
            "dataRequestId": "lookupOrder",
            "description": "Look up an order",
            "webhook": {"implementation": "external", "method": "POST",
                        "url": "{WEBHOOK_URL}/v1/orders/lookup",
                        "headers": [{"key": "x-api-key", "value": "{selcBackendApiKey:NLX.Secret}",
                                     "sensitive": True}]},
            "requestSchema": {"type": "object", "required": ["orderNumber"],
                              "properties": {"orderNumber": {"type": "string"}}},
            "responseSchema": {"type": "object", "required": ["success"],
                               "properties": {"success": {"type": "boolean"},
                                              "status": {"type": "string"}}},
        }],
        "knowledge_bases": [{"name": "FAQ", "articles": [{"question": "q", "answer": "a"}]}],
        "guardrails": [],
        "secrets": [{"name": "selcBackendApiKey", "description": "key",
                     "valueEnv": "ACXD_SECRET_SELCBACKENDAPIKEY"}],
        "application": {"name": "SELC Assistant"},
        "contact_flows": [],
    }
    bundle.update(overrides)
    return bundle


def test_acxd_only_manifest_has_no_cloudformation_and_reads_webhook_url_from_env():
    from tools.acxd_manifest_builder import build_manifest, check_manifest_coverage, validate_deploy_manifest

    bundle = _bundle()
    manifest = build_manifest(bundle, project_name="selc", external_backend=True,
                              backend_base_url="https://api.example.com/v1/")
    types = [step["type"] for step in manifest["steps"]]
    assert "deploy-cfn-backend" not in types and "import-contact-flows" not in types
    assert manifest["steps"][0] == {
        "type": "wire-webhook-urls",
        "description": "Point the Data Requests at your backend API (WEBHOOK_URL)",
        "params": {"source": "env", "defaultUrl": "https://api.example.com/v1"},
    }
    assert validate_deploy_manifest(manifest) == []
    assert check_manifest_coverage(manifest, bundle, external_backend=True) == []
    # The Classic rule still applies to the full ACXD target...
    assert any("deploy-cfn-backend" in p for p in check_manifest_coverage(manifest, bundle))
    # ...and an ACXD-only manifest may not carry a backend or Contact Flow step.
    classic = build_manifest(bundle, project_name="selc")
    assert any("must not carry 'deploy-cfn-backend'" in p
               for p in check_manifest_coverage(classic, bundle, external_backend=True))


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

class _FakeS3:
    def __init__(self):
        self.payload = None

    def upload_fileobj(self, fileobj, _bucket, _key, ExtraArgs=None):
        self.payload = fileobj.read()

    def generate_presigned_url(self, *_args, **_kwargs):
        return "https://example.test/aicc.zip"


def test_acxd_only_bundle_is_the_application_and_its_deploy_kit(monkeypatch):
    import tools.asset_packager as packager
    import tools.acxd_manifest_builder as manifest_builder

    session_id = "session-acxd-only"
    # Stray Classic assets in the session must not reach the archive.
    contents = {
        f"assets/{session_id}/lambda/lookup_order/index.py": "def handler(e, c): return {}\n",
        f"assets/{session_id}/openapi/openapi.yaml": "openapi: 3.0.1\n",
        f"assets/{session_id}/contact_flow/contact_flow.json": '{"Actions": []}',
        f"assets/{session_id}/faq/general/faq_1.txt": "## Question\nq\n## Answer\na\n",
    }
    client = _FakeS3()
    monkeypatch.setattr(packager, "_is_acxd_target", lambda _s: True)
    monkeypatch.setattr(packager, "_is_acxd_only_target", lambda _s: True)
    monkeypatch.setattr(packager, "_load_acxd_bundle",
                        lambda _s: _bundle(contact_flows=[{"Actions": [], "Metadata": {"acxdBinding": {}}}]))
    monkeypatch.setattr(packager, "_run_d9_checks", lambda _s, _b: [])
    monkeypatch.setattr(packager, "_acxd_only_backend_settings",
                        lambda _s: {"base_url": "https://api.example.com/v1", "auth_header": "x-api-key"})
    monkeypatch.setattr(manifest_builder, "acxd_only_backend",
                        lambda _s: (True, "https://api.example.com/v1"))
    monkeypatch.setattr(packager, "get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(packager, "get_s3_client", lambda: client)
    monkeypatch.setattr(packager, "list_session_assets", lambda *_a, **_k: list(contents))
    monkeypatch.setattr(packager, "get_asset_from_s3", lambda key, **_k: contents.get(key))

    result = packager.package_assets_impl(session_id, "selc")

    assert result["success"] is True, result
    assert result["runtime_target"] == "acxd_only"
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("selc/deploy-manifest.json"))
        deploy = archive.read("selc/deploy.sh").decode()
        readme = archive.read("selc/README.md").decode()
        wiring = archive.read("selc/WIRING-GUIDE.md").decode()
        contract = archive.read("selc/BACKEND-CONTRACT.md").decode()
        deploy_mode = archive.getinfo("selc/deploy.sh").external_attr >> 16
    assert {
        "selc/assets/acxd/flows/MainFlow.json",
        "selc/assets/acxd/data-requests/lookupOrder.json",
        "selc/assets/acxd/knowledge-bases/FAQ.json",
        "selc/assets/acxd/secrets/selcBackendApiKey.json",
        "selc/assets/acxd/application.json",
        "selc/deploy-manifest.json",
        "selc/runner.js",
        "selc/lib/steps.js",
    }.issubset(names)
    for prefix in ("selc/lambda/", "selc/openapi/", "selc/cloudformation/", "selc/contact-flow/",
                   "selc/prompts/", "selc/knowledge-base/"):
        assert not any(name.startswith(prefix) for name in names), prefix
    types = [step["type"] for step in manifest["steps"]]
    assert "deploy-cfn-backend" not in types and "import-contact-flows" not in types
    assert manifest["steps"][0]["params"] == {"source": "env", "defaultUrl": "https://api.example.com/v1"}
    assert "ACXD-only deploy" in deploy and "node runner.js" in deploy
    assert "aws cloudformation" not in deploy and "aws lambda" not in deploy
    assert deploy_mode & 0o111
    assert "ACXD only" in readme and "WEBHOOK_URL=https://api.example.com/v1" in readme
    assert "ships no Contact Flow" in wiring
    assert "`POST {WEBHOOK_URL}/v1/orders/lookup`" in contract
    assert "`x-api-key`" in contract and "selcBackendApiKey" in contract
    # Live (sandbox 2026-09-27): journey tool calls send `010-1111-2222`, fixed
    # captures send `01011112222` — the contract must tell the API to take both.
    assert "`010-1111-2222`" in contract and "`01011112222`" in contract
    assert "Accept both forms of a value" in contract


def test_acxd_only_download_without_a_name_uses_the_recorded_project_name(monkeypatch):
    """Live (SELC, 2026-09-26): the download endpoint passes no project name, so
    the bundle folder, README title and manifest all said 'aicc-poc' while the
    application was 'selc-voice'. ACXD only has no stack named after the project,
    so it takes the name the interview recorded; the other targets keep theirs."""
    import tools.asset_packager as packager
    import tools.acxd_flow_spec as flow_spec

    class _App:
        project_name = "selc-voice"
        name = "삼성전자로지텍 AI 상담원"

    class _Spec:
        application = _App()

    monkeypatch.setattr(flow_spec, "get_acxd_flow_spec", lambda _sid=None: _Spec())
    assert packager._session_project_name("session-x") == "selc-voice"

    session_id = "session-x"
    contents = {f"assets/{session_id}/faq/general/faq_1.txt": "## Question\nq\n## Answer\na\n"}
    client = _FakeS3()
    monkeypatch.setattr(packager, "_is_acxd_target", lambda _s: True)
    monkeypatch.setattr(packager, "_is_acxd_only_target", lambda _s: True)
    monkeypatch.setattr(packager, "_load_acxd_bundle", lambda _s: _bundle())
    monkeypatch.setattr(packager, "_run_d9_checks", lambda _s, _b: [])
    monkeypatch.setattr(packager, "_acxd_only_backend_settings",
                        lambda _s: {"base_url": "https://api.example.com/v1", "auth_header": "x-api-key"})
    import tools.acxd_manifest_builder as manifest_builder
    monkeypatch.setattr(manifest_builder, "acxd_only_backend", lambda _s: (True, "https://api.example.com/v1"))
    monkeypatch.setattr(packager, "get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(packager, "get_s3_client", lambda: client)
    monkeypatch.setattr(packager, "list_session_assets", lambda *_a, **_k: list(contents))
    monkeypatch.setattr(packager, "get_asset_from_s3", lambda key, **_k: contents.get(key))

    result = packager.package_assets_impl(session_id)          # the download endpoint's call shape
    assert result["success"] is True, result
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        manifest = json.loads(archive.read("selc-voice/deploy-manifest.json"))
    assert manifest["project"] == "selc-voice"

    monkeypatch.setattr(flow_spec, "get_acxd_flow_spec", lambda _sid=None: None)

def test_packaging_ships_whole_faq_answers_and_no_mask_over_a_collected_value(monkeypatch):
    """Live (5-use-case run, 2026-09-28): the SELC and GreenCart knowledge
    bases held one lead sentence of each FAQ answer, and input masks over the
    phone and birth date the flows collect handed the journeys "[REDACTED]". A
    bundle generated before those fixes is repaired when it is packaged."""
    import tools.asset_packager as packager
    import tools.acxd_manifest_builder as manifest_builder

    session_id = "session-f23"
    faq = ("# 반품 정책\n\n## 질문 (Question)\n반품 정책이 어떻게 되나요?\n\n## 답변 (Answer)\n"
           "반품 정책을 안내해 드립니다.\n\n## 반품 가능 기간\n- 배송완료일로부터 14일 이내\n\n"
           "## 관련 정보 (Related Information)\n- 반품 조회\n\n## 메타데이터 (Metadata)\n- 키워드: 반품\n")
    contents = {f"assets/{session_id}/faq/knowledge_base/01_return_policy.md": faq}
    knowledge_base = {"name": "FAQ", "type": "articles", "articles": [{
        "question": {"text": "반품 정책이 어떻게 되나요?"},
        "responses": [{"type": "text", "body": "반품 정책을 안내해 드립니다."}]}]}
    flow = {"flowId": "MainFlow", "nodes": {}, "slotTypes": [
        {"name": "phoneNumber", "type": "NLX.PhoneNumber", "sensitive": True,
         "regex": "^010-[0-9]{4}-[0-9]{4}$"}]}
    guardrail = {"name": "selcvoice-guardrail1", "trigger": "input", "active": True, "rules": [{
        "name": "selcvoice-guardrail1", "detection": {"method": "regex", "pattern": r"\d{3}-\d{4}-\d{4}"},
        "enforcement": {"action": "mask", "behavior": {"maskText": "[REDACTED]"}}, "active": True}],
        "fallbackBehavior": {"type": "continue"}}
    client = _FakeS3()
    monkeypatch.setattr(packager, "_is_acxd_target", lambda _s: True)
    monkeypatch.setattr(packager, "_is_acxd_only_target", lambda _s: True)
    monkeypatch.setattr(packager, "_load_acxd_bundle", lambda _s: _bundle(
        flows=[flow], knowledge_bases=[knowledge_base], guardrails=[guardrail]))
    monkeypatch.setattr(packager, "_run_d9_checks", lambda _s, _b: [])
    monkeypatch.setattr(packager, "_acxd_only_backend_settings",
                        lambda _s: {"base_url": "https://api.example.com/v1", "auth_header": "x-api-key"})
    monkeypatch.setattr(manifest_builder, "acxd_only_backend",
                        lambda _s: (True, "https://api.example.com/v1"))
    monkeypatch.setattr(packager, "get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(packager, "get_s3_client", lambda: client)
    monkeypatch.setattr(packager, "list_session_assets", lambda *_a, **_k: list(contents))
    monkeypatch.setattr(packager, "get_asset_from_s3", lambda key, **_k: contents.get(key))

    result = packager.package_assets_impl(session_id, "selc")

    assert result["success"] is True, result
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        shipped_kb = json.loads(archive.read("selc/assets/acxd/knowledge-bases/FAQ.json"))
        guardrail_files = [n for n in archive.namelist() if n.startswith("selc/assets/acxd/guardrails/")]
        shipped_guardrail = json.loads(archive.read(guardrail_files[0]))
    body = shipped_kb["articles"][0]["responses"][0]["body"]
    assert body.startswith("반품 정책을 안내해 드립니다.")
    assert "**반품 가능 기간**" in body and "14일 이내" in body and "키워드" not in body
    rule = shipped_guardrail["rules"][0]
    assert rule["enforcement"] == {"action": "flag"}
    assert "phoneNumber" in rule["description"]

    assert packager._session_project_name("session-y") is None


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_d9_external_api_check_refuses_path_templates_and_literal_hosts():
    from tools.validate_consistency import _d9_external_api_checks

    ok = _bundle()
    assert _d9_external_api_checks(ok) == []
    templated = _bundle()
    templated["data_requests"][0]["webhook"]["url"] = "{WEBHOOK_URL}/orders/{orderNumber}"
    assert [i["id"] for i in _d9_external_api_checks(templated)] == ["D9-3"]
    literal = _bundle()
    literal["data_requests"][0]["webhook"]["url"] = "https://api.example.com/orders"
    assert "must start with {WEBHOOK_URL}" in _d9_external_api_checks(literal)[0]["message"]


def test_d9_for_acxd_only_skips_contact_flow_openapi_and_generated_backend_checks(monkeypatch):
    import tools.validate_consistency as vc

    monkeypatch.setattr(vc, "is_acxd_target", lambda _s: True)
    monkeypatch.setattr(vc, "is_acxd_only_target", lambda _s: True)
    monkeypatch.setattr(vc, "load_acxd_bundle", lambda _s: _bundle())
    monkeypatch.setattr(vc, "get_acxd_flow_spec", lambda _s: _Model({"flows": [], "knowledge_base": {},
                                                                         "application": {}}))
    called = []
    for name in ("_d9_data_request_checks", "_d9_contact_flow_checks",
                 "_d9_external_backend_checks", "_d9_backend_auth_checks"):
        monkeypatch.setattr(vc, name, lambda *a, _n=name, **k: called.append(_n) or [])
    monkeypatch.setattr(vc, "_d9_structural_checks", lambda *_a: [])
    monkeypatch.setattr(vc, "_d9_slot_type_checks", lambda *_a: [])
    monkeypatch.setattr(vc, "_d9_knowledge_base_checks", lambda *_a: [])
    monkeypatch.setattr(vc, "_d9_identity_metadata_checks", lambda *_a: [])

    assert vc.run_d9_checks("session-acxd-only", classic_mismatches=[]) == []
    assert called == []


def test_missing_asset_gate_asks_acxd_only_for_data_requests_not_lambdas(monkeypatch):
    import tools.review_gates as gates
    import tools.acxd_flow_spec as fs
    import tools.spec_manager as sm
    import tools.s3_asset_storage as storage

    monkeypatch.setattr(sm, "get_backend_specs", lambda: {"lookup_order": object()})
    monkeypatch.setattr(fs, "is_acxd_target", lambda _s=None: True)
    monkeypatch.setattr(fs, "is_acxd_only_target", lambda _s=None: True)
    monkeypatch.setattr(storage, "list_session_assets", lambda *_a, **_k: [])
    monkeypatch.setattr(gates, "_missing_knowledge_findings", lambda *_a: [])

    findings = gates._missing_asset_findings("session-acxd-only")
    assert [f["id"] for f in findings] == ["MISSING:acxd_data_request:lookup_order"]


def test_operation_summary_shows_the_customers_api_instead_of_a_table():
    from types import SimpleNamespace

    from tools.spec_manager import _format_spec_as_markdown

    spec = SimpleNamespace(
        http_method="POST", path="/v1/delivery/status", summary="배송 조회",
        input_fields=[SimpleNamespace(name="orderNumber", field_type="string", required=True)],
        output_fields=[SimpleNamespace(name="deliveryStatus", field_type="enum")],
        data_source=SimpleNamespace(db_type="external_api", table_name=None, partition_key=None),
        business_rules=[], tools=[], conversation_steps=[], conversation_script=None,
        call_direction=None, flow_type=None, scenario_step_count=None,
    )
    md = _format_spec_as_markdown("get_delivery_status", spec)
    assert "Existing API: `POST /v1/delivery/status`" in md
    assert "Table:" not in md and "PK:" not in md
